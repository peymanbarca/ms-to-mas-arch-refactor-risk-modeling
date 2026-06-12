"""
MEDIA AGENT - Graph Topology

    START
      |
      v
  [check_cache]           Deterministic — Redis lookup per media_id.
      |                   Splits items into cached (done) vs uncached (need processing).
      v
  [reason_validate_media] LLM — Given the uncached (media_id, media_type) pairs,
      |                   validate each media_type is a known/recognizable media
      |                   category and return a confirmed or corrected type per item.
      |                   This is the "static logic" converted to LLM reasoning:
      |                   the original C++ just accepted whatever type string was
      |                   passed in; the agent reasons about whether the type is
      |                   semantically valid and can normalize it (e.g. "jpeg" → "photo",
      |                   "mp4" → "video").
      v
  [validate_output]       Deterministic guard — ensure LLM returned exactly one
      |                   validated_type per uncached item; fall back to original
      |                   type string if LLM response is malformed or count mismatches.
      v
  [persist]               Deterministic — MongoDB upsert + Redis SET per item.
      |                   Identical to original handler._store_to_mongo + _set_in_cache.
      v
     END  →  list[Media] returned to Thrift handler

Key Design Decisions
--------------------
- Thrift interface (MediaService.Iface) UNCHANGED.
- MongoDB schema, Redis key layout UNCHANGED.
- The LLM reasons about media type validation/normalization — the semantic
  decision about whether a given type string is a valid media category.
- validate_output is a hard guard: if LLM count mismatches or any type is
  empty, fall back to the original type strings (pass-through behaviour,
  identical to the original C++ service).
- Cached items bypass all three nodes (check_cache → END shortcut).
- Token metrics accumulated per ComposeMedia call.
"""

import json
import logging
import re
import asyncio

from typing import TypedDict, Optional, List, Dict, Any, Tuple
from langgraph.graph import StateGraph, END
from langchain_ollama import ChatOllama

import redis as redis_lib
from pymongo import MongoClient

logger = logging.getLogger("media-agent")

# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------

llm = ChatOllama(model="llama3.2:3b", temperature=0.0, reasoning=False)


# ---------------------------------------------------------------------------
# Agent State
# ---------------------------------------------------------------------------

class MediaAgentState(TypedDict):
    # Inputs (set before graph invocation)
    req_id:       int
    media_ids:    List[int]     # full input list
    media_types:  List[str]     # full input list

    # After check_cache: items split into cached vs uncached
    cached_results:   List[Dict]   # [{media_id, media_type}] from Redis
    uncached_ids:     List[int]    # media_ids not found in Redis
    uncached_types:   List[str]    # corresponding media_types

    # After reason_validate_media: LLM output for uncached items
    llm_validated_types: Optional[List[str]]   # one per uncached item

    # After validate_output: final confirmed types for uncached items
    validated_types: List[str]   # one per uncached item (fallback-corrected)

    # LLM metrics
    total_input_tokens:  int
    total_output_tokens: int
    total_llm_calls:     int
    fallback_used:       bool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_json(text: str) -> Optional[dict]:
    try:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            return json.loads(m.group(0))
    except Exception as exc:
        logger.warning("JSON parse error: %s — raw: %r", exc, text[:200])
    return None


def _is_valid_type(t: Any) -> bool:
    """A valid media type is a non-empty string."""
    return isinstance(t, str) and len(t.strip()) > 0


# ===========================================================================
# Node factories
# ===========================================================================

def make_check_cache_node(redis_client: redis_lib.Redis):
    """
    Node: check_cache  (deterministic)

    For each media_id, check Redis (key = str(media_id)).
    Split input into cached (already known) vs uncached (need LLM + persist).
    """
    async def check_cache(state: MediaAgentState) -> MediaAgentState:
        cached_results = []
        uncached_ids   = []
        uncached_types = []

        for media_id, media_type in zip(state["media_ids"], state["media_types"]):
            cache_key = str(media_id)
            try:
                val = redis_client.get(cache_key)
                if val is not None:
                    cached_type = val.decode("utf-8")
                    cached_results.append({"media_id": media_id, "media_type": cached_type})
                    logger.debug("check_cache HIT media_id=%d type=%s", media_id, cached_type)
                else:
                    uncached_ids.append(media_id)
                    uncached_types.append(media_type)
                    logger.debug("check_cache MISS media_id=%d", media_id)
            except redis_lib.RedisError as exc:
                logger.warning("Redis GET media_id=%d failed: %s", media_id, exc)
                uncached_ids.append(media_id)
                uncached_types.append(media_type)

        state["cached_results"] = cached_results
        state["uncached_ids"]   = uncached_ids
        state["uncached_types"] = uncached_types

        logger.info(
            "check_cache req_id=%d total=%d cached=%d uncached=%d",
            state["req_id"],
            len(state["media_ids"]),
            len(cached_results),
            len(uncached_ids),
        )
        print(
            f"[check_cache] req_id={state['req_id']} "
            f"cached={len(cached_results)} uncached={len(uncached_ids)}"
        )
        return state
    return check_cache


def make_reason_validate_media_node():
    """
    Node: reason_validate_media  (LLM)

    Given a list of (media_id, media_type) pairs for uncached items,
    ask the LLM to validate each type and return a confirmed/normalized
    type per item.

    The LLM reasoning:
      - Is this a recognizable media category?
      - Should it be normalized (e.g. "jpeg" → "photo", "mp4" → "video")?
      - If completely unrecognizable, return the original type as-is.
    """
    async def reason_validate_media(state: MediaAgentState) -> MediaAgentState:
        # Skip if nothing to process
        if not state["uncached_ids"]:
            logger.info("reason_validate_media SKIPPED (all cached) req_id=%d",
                        state["req_id"])
            state["llm_validated_types"] = []
            return state

        # Build input list for the LLM
        items = [
            {"media_id": mid, "media_type": mtype}
            for mid, mtype in zip(state["uncached_ids"], state["uncached_types"])
        ]
        items_json = json.dumps(items, indent=2)
        n = len(items)

        prompt = f"""
You are a media validation agent for a social network.

Your task is to validate and normalize media type strings for a list of media items.

For each item, examine the media_type string and:
1. Confirm it is a known/recognizable media category.
2. Normalize it to a standard lowercase category name if needed:
   - Image types (jpeg, jpg, png, gif, webp, bmp, tiff, heic, etc.) → "photo"
   - Video types (mp4, mov, avi, mkv, webm, flv, wmv, etc.)         → "video"
   - Audio types (mp3, wav, aac, ogg, flac, m4a, etc.)              → "audio"
   - Document types (pdf, doc, docx, ppt, pptx, xls, xlsx, etc.)    → "document"
   - Already correct category names (photo, video, audio, document)  → return as-is
   - Completely unrecognizable types                                  → return the original string unchanged

Rules:
- Return EXACTLY {n} validated_type entries in the same order as the input items
- Each validated_type must be a non-empty string
- Return ONLY valid JSON — no explanation, no code, no markdown

Schema:
{{
  "validated_items": [
    {{"media_id": <int>, "validated_type": "<string>"}},
    ...
  ]
}}

Input items:
{items_json}
"""

        logger.info("LLM prompt req_id=%d items=%d", state["req_id"], n)

        response = await asyncio.to_thread(llm.invoke, prompt)
        raw      = response.text()
        in_tok   = response.usage_metadata.get("input_tokens",  0)
        out_tok  = response.usage_metadata.get("output_tokens", 0)

        logger.info("LLM raw=%r  in=%d out=%d", raw, in_tok, out_tok)
        print(f"[reason_validate_media] raw={raw!r}  in={in_tok} out={out_tok}")

        # Parse LLM response
        parsed = _parse_json(raw)
        llm_types = None

        if parsed and isinstance(parsed.get("validated_items"), list):
            validated_items = parsed["validated_items"]
            if len(validated_items) == n:
                # Extract validated_type in order
                extracted = []
                valid = True
                for item in validated_items:
                    vtype = item.get("validated_type")
                    if not _is_valid_type(vtype):
                        valid = False
                        break
                    extracted.append(vtype)
                if valid:
                    llm_types = extracted

        if llm_types is None:
            logger.warning(
                "LLM returned invalid/mismatched validated_items req_id=%d raw=%r",
                state["req_id"], raw[:200],
            )

        state["llm_validated_types"]  = llm_types
        state["total_input_tokens"]  += in_tok
        state["total_output_tokens"] += out_tok
        state["total_llm_calls"]     += 1
        return state
    return reason_validate_media


def make_validate_output_node():
    """
    Node: validate_output  (deterministic guard)

    Verify the LLM returned a valid list of types with correct count.
    If not, fall back to the original (pass-through) type strings —
    identical to the original C++ behaviour which accepted any type.
    """
    async def validate_output(state: MediaAgentState) -> MediaAgentState:
        if not state["uncached_ids"]:
            state["validated_types"] = []
            state["fallback_used"]   = False
            return state

        n         = len(state["uncached_ids"])
        llm_types = state.get("llm_validated_types")

        if (
            llm_types is not None
            and len(llm_types) == n
            and all(_is_valid_type(t) for t in llm_types)
        ):
            state["validated_types"] = llm_types
            state["fallback_used"]   = False
            logger.info(
                "validate_output PASS req_id=%d types=%s",
                state["req_id"], llm_types,
            )
            print(f"[validate_output] PASS  types={llm_types}")
        else:
            # Fall back to original type strings (pass-through)
            state["validated_types"] = list(state["uncached_types"])
            state["fallback_used"]   = True
            logger.warning(
                "validate_output FALLBACK req_id=%d llm_types=%r -> fallback=%s",
                state["req_id"], llm_types, state["uncached_types"],
            )
            print(
                f"[validate_output] FALLBACK  "
                f"llm={llm_types!r} -> original={state['uncached_types']}"
            )

        return state
    return validate_output


def make_persist_node(redis_client: redis_lib.Redis, mongo_col):
    """
    Node: persist  (deterministic)

    For each uncached item: MongoDB upsert + Redis SET.
    Identical to the original handler._store_to_mongo + _set_in_cache.
    """
    async def persist(state: MediaAgentState) -> MediaAgentState:
        if not state["uncached_ids"]:
            return state

        for media_id, media_type in zip(
            state["uncached_ids"], state["validated_types"]
        ):
            # ---- MongoDB upsert ----
            try:
                mongo_col.update_one(
                    {"media_id": media_id},
                    {"$set": {"media_id": media_id, "media_type": media_type}},
                    upsert=True,
                )
                logger.debug(
                    "persist MongoDB media_id=%d type=%s", media_id, media_type
                )
            except Exception as exc:
                logger.error("persist MongoDB failed media_id=%d: %s", media_id, exc)
                raise

            # ---- Redis SET ----
            try:
                redis_client.set(str(media_id), media_type)
                logger.debug("persist Redis SET media_id=%d type=%s", media_id, media_type)
            except redis_lib.RedisError as exc:
                logger.warning("persist Redis SET failed media_id=%d: %s", media_id, exc)
                # Non-fatal — data is in MongoDB

        print(
            f"[persist] OK  {len(state['uncached_ids'])} items persisted"
        )
        return state
    return persist


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

def _route_after_cache(state: MediaAgentState) -> str:
    """Skip LLM nodes if all items were cache hits."""
    return "END" if not state["uncached_ids"] else "reason_validate_media"


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------

def build_media_agent(
    redis_client: redis_lib.Redis,
    mongo_col,
) -> any:
    """Build and compile the MediaService LangGraph agent."""
    graph = StateGraph(MediaAgentState)

    graph.add_node("check_cache",            make_check_cache_node(redis_client))
    graph.add_node("reason_validate_media",  make_reason_validate_media_node())
    graph.add_node("validate_output",        make_validate_output_node())
    graph.add_node("persist",                make_persist_node(redis_client, mongo_col))

    graph.set_entry_point("check_cache")

    # All cached → END immediately; any uncached → full pipeline
    graph.add_conditional_edges(
        "check_cache",
        _route_after_cache,
        {"END": END, "reason_validate_media": "reason_validate_media"},
    )
    graph.add_edge("reason_validate_media", "validate_output")
    graph.add_edge("validate_output",       "persist")
    graph.add_edge("persist",               END)

    return graph.compile()
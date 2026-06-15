"""
POST STORAGE AGENT - Graph Topologies

━━━ Graph 1: store_agent  (StorePost) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    START
      |
      v
  [reason_validate_post]  LLM — given the full Post struct fields, validate:
      |                   - post_id > 0
      |                   - creator.user_id > 0, creator.username non-empty
      |                   - text is not None
      |                   - timestamp in reasonable range
      |                   - post_type is a known value (0=POST, 1=REPOST, 2=REPLY, 3=DM)
      |                   Returns: { valid: bool, issues: [str...] }
      v
  [validate_store]        Deterministic guard — enforce hard field checks
      |                   regardless of LLM. If LLM says valid but post_id<=0,
      |                   override. If LLM says invalid for a good post, override.
      v
  [persist_post]          Deterministic — MongoDB upsert (update_one $set) +
      |                   Redis SET (JSON serialised post).
      v
     END

━━━ Graph 2: read_agent  (ReadPost) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    START
      |
      v
  [check_cache]           Deterministic — Redis GET str(post_id).
      |                   Parses cached JSON if present.
      v
  [reason_cache_decision] LLM — given cache result and post_id, decide:
      |                   "Is the cached post valid and complete?
      |                    Or should we fetch from MongoDB?"
      |                   Returns: { use_cache: bool, reason: str }
      v
  [validate_cache]        Deterministic guard — if cache hit AND JSON parses
      |                   cleanly AND post_id matches → always use cache
      |                   (don't let LLM force unnecessary MongoDB calls).
      v
  [fetch_if_needed]       Deterministic — MongoDB find_one if cache miss/invalid.
      |                   Populates Redis on MongoDB hit.
      v
     END → Post

━━━ Graph 3: read_batch_agent  (ReadPosts) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    START
      |
      v
  [check_cache_batch]     Deterministic — Redis GET for every post_id.
      |                   Splits into: cache_hits {id->Post} + missing_ids [id...]
      v
  [reason_batch_complete] LLM — given hit/miss summary, decide:
      |                   "Which post_ids are genuinely missing vs potentially
      |                    corrupt cache entries that need MongoDB refresh?"
      |                   Returns: { ids_to_fetch: [i64...] }
      v
  [validate_batch]        Deterministic guard — always fetch ALL missing_ids
      |                   from MongoDB (LLM cannot reduce the fetch set below
      |                   what's deterministically identified as missing).
      v
  [fetch_missing]         Deterministic — MongoDB find {post_id: {$in: [...]}}
      |                   for all missing IDs. Populates Redis for each hit.
      v
  [assemble_ordered]      Deterministic — merge cache_hits + mongo_hits, reorder
      |                   by original input post_ids order. Raise if any missing.
      v
     END → list[Post]  (same order as input post_ids)

Key Design Decisions
────────────────────
- Thrift interface (PostStorageService.Iface) UNCHANGED.
- MongoDB schema, Redis key layout, serialisation UNCHANGED (uses post_serializer.py).
- LLM reasoning replaces:
    Store:      "Is this Post struct valid enough to persist?"
    ReadPost:   "Is the cached value trustworthy or should I refresh from MongoDB?"
    ReadPosts:  "Which IDs are truly missing and need a MongoDB fetch?"
- validate_store hard-enforces: post_id > 0, creator present, text not None.
- validate_cache hard-enforces: if Redis JSON parses with correct post_id → use it.
- validate_batch hard-enforces: fetch set = superset of deterministic missing_ids.
- Token metrics tracked per request.
"""

import json
import logging
import re
import asyncio
import concurrent.futures
import time
from typing import TypedDict, Optional, List, Dict, Any

from langgraph.graph import StateGraph, END
from langchain_ollama import ChatOllama

import redis as redis_lib
from pymongo import MongoClient

from .post_serializer import post_to_dict, dict_to_post, post_to_json, json_to_post
from ms_baseline.dsb_social.gen_py.social_network.ttypes import Post, PostType

logger = logging.getLogger("post-storage-agent")

# ── LLM ─────────────────────────────────────────────────────────────────────
llm = ChatOllama(model="llama3.2:3b", temperature=0.0, reasoning=False)

# Timestamp bounds (ms): year 2000 → year 2100
_TS_MIN = 946_684_800_000
_TS_MAX = 4_102_444_800_000
_VALID_POST_TYPES = {0, 1, 2, 3}   # POST, REPOST, REPLY, DM


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_json(text: str) -> Optional[dict]:
    try:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            return json.loads(m.group(0))
    except Exception as exc:
        logger.warning("JSON parse error: %s — raw: %r", exc, text[:200])
    return None


def _post_summary(post: Post) -> dict:
    """Compact Post representation for LLM prompts."""
    creator = post.creator
    return {
        "post_id":   post.post_id,
        "creator":   {
            "user_id":  creator.user_id  if creator else None,
            "username": creator.username if creator else None,
        },
        "req_id":    post.req_id,
        "text":      (post.text[:120] + "…") if post.text and len(post.text) > 120 else post.text,
        "timestamp": post.timestamp,
        "post_type": post.post_type,
        "url_count":     len(post.urls          or []),
        "mention_count": len(post.user_mentions or []),
        "media_count":   len(post.media         or []),
    }


def _is_valid_post_deterministic(post: Post) -> tuple[bool, list]:
    """Reference validation — used as fallback and in validate_store."""
    issues = []
    if not post or post.post_id is None or post.post_id <= 0:
        issues.append(f"post_id must be > 0, got {getattr(post, 'post_id', None)}")
    if not post.creator:
        issues.append("creator is None")
    elif not post.creator.user_id or post.creator.user_id <= 0:
        issues.append(f"creator.user_id must be > 0, got {post.creator.user_id}")
    # elif not post.creator.username:
    #     issues.append("creator.username is empty")
    if post.text is None:
        issues.append("text is None")
    # if post.timestamp and not (_TS_MIN <= post.timestamp <= _TS_MAX):
    #     issues.append(f"timestamp {post.timestamp} out of range [{_TS_MIN}, {_TS_MAX}]")
    if post.post_type not in _VALID_POST_TYPES:
        issues.append(f"post_type {post.post_type} not in {_VALID_POST_TYPES}")
    return len(issues) == 0, issues


# ══════════════════════════════════════════════════════════════════════════════
# GRAPH 1 — StorePost
# ══════════════════════════════════════════════════════════════════════════════

class StorePostState(TypedDict):
    req_id:  int
    post:    Any           # Post Thrift struct

    # LLM output
    llm_valid:  Optional[bool]
    llm_issues: Optional[List[str]]

    # Final decision
    valid:   Optional[bool]
    issues:  List[str]

    # Metrics
    total_input_tokens:  int
    total_output_tokens: int
    total_llm_calls:     int
    fallback_used:       bool


def make_reason_validate_post_node():
    """LLM: validate the Post struct fields before persisting."""
    async def reason_validate_post(state: StorePostState) -> StorePostState:
        post    = state["post"]
        summary = _post_summary(post)

        prompt = f"""
You are a post validation agent for a social network.

Your task is to validate a Post struct before it is stored in the database.

Post to validate:
{json.dumps(summary, indent=2)}

Validation rules:
  1. post_id must be a positive integer (> 0)
  2. creator.user_id must be a positive integer (> 0)
  3. text must not be None (empty string is allowed)
  4. post_type must be one of: 0=POST, 1=REPOST, 2=REPLY, 3=DM

If ALL rules pass: valid=true, issues=[]
If ANY rule fails: valid=false, issues=[<description of each failed rule>]

Return ONLY valid JSON — no explanation, no code, no markdown.

Schema:
{{
  "valid":  true | false,
  "issues": ["<issue1>", ...]
}}
"""

        logger.info("LLM reason_validate_post req_id=%d post_id=%d, prompt=%s",
                    state["req_id"], post.post_id, prompt)
        response = await asyncio.to_thread(llm.invoke, prompt)
        raw      = response.text()
        in_tok   = response.usage_metadata.get("input_tokens",  0)
        out_tok  = response.usage_metadata.get("output_tokens", 0)

        logger.info("LLM raw=%r  in=%d out=%d", raw[:150], in_tok, out_tok)
        print(f"[reason_validate_post] raw={raw[:100]!r}  in={in_tok} out={out_tok}")

        parsed = _parse_json(raw)
        llm_valid  = None
        llm_issues = []

        if parsed:
            v = parsed.get("valid")
            i = parsed.get("issues", [])
            if isinstance(v, bool):
                llm_valid = v
            if isinstance(i, list):
                llm_issues = [str(x) for x in i]

        state["llm_valid"]           = llm_valid
        state["llm_issues"]          = llm_issues
        state["total_input_tokens"]  += in_tok
        state["total_output_tokens"] += out_tok
        state["total_llm_calls"]     += 1
        return state
    return reason_validate_post


def make_validate_store_node():
    """
    Deterministic guard: enforce hard field checks.
    LLM result accepted only when it matches deterministic check exactly.
    """
    async def validate_store(state: StorePostState) -> StorePostState:
        post = state["post"]
        det_valid, det_issues = _is_valid_post_deterministic(post)
        llm_valid = state.get("llm_valid")

        if llm_valid is not None and llm_valid == det_valid:
            state["valid"]         = llm_valid
            state["issues"]        = state.get("llm_issues") or det_issues
            state["fallback_used"] = False
            logger.info("validate_store PASS req_id=%d valid=%s",
                        state["req_id"], llm_valid)
            print(f"[validate_store] PASS  valid={llm_valid}")
        else:
            state["valid"]         = det_valid
            state["issues"]        = det_issues
            state["fallback_used"] = True
            logger.warning(
                "validate_store FALLBACK req_id=%d llm=%s -> det=%s issues=%s",
                state["req_id"], llm_valid, det_valid, det_issues,
            )
            print(f"[validate_store] FALLBACK  llm={llm_valid} -> det={det_valid} "
                  f"issues={det_issues}")
        return state
    return validate_store


def make_persist_post_node(redis_client, mongo_col):
    """Deterministic: MongoDB upsert + Redis SET. Raises ServiceException on DB error."""
    async def persist_post(state: StorePostState) -> StorePostState:
        from ms_baseline.dsb_social.gen_py.social_network.ttypes import ServiceException, ErrorCode

        if not state.get("valid"):
            issues = state.get("issues", [])
            logger.error(
                "persist_post REJECTED req_id=%d post_id=%d issues=%s",
                state["req_id"], state["post"].post_id, issues,
            )
            raise ServiceException(
                errorCode=ErrorCode.SE_THRIFT_HANDLER_ERROR,
                message=f"Post validation failed: {'; '.join(issues)}",
            )

        post = state["post"]
        doc  = post_to_dict(post)

        # ── MongoDB upsert ──
        try:
            mongo_col.update_one(
                {"post_id": post.post_id},
                {"$set": doc},
                upsert=True,
            )
            logger.debug("persist_post MongoDB upsert post_id=%d", post.post_id)
        except Exception as exc:
            raise ServiceException(
                errorCode=ErrorCode.SE_MONGODB_ERROR,
                message=f"MongoDB write failed: {exc}",
            )

        # ── Redis SET ──
        try:
            redis_client.set(str(post.post_id), post_to_json(post))
        except redis_lib.RedisError as exc:
            logger.warning("persist_post Redis SET failed post_id=%d: %s",
                           post.post_id, exc)
            # Non-fatal

        print(f"[persist_post] OK  post_id={post.post_id}")
        return state
    return persist_post


def build_store_agent(redis_client, mongo_col) -> any:
    graph = StateGraph(StorePostState)
    graph.add_node("reason_validate_post", make_reason_validate_post_node())
    graph.add_node("validate_store",       make_validate_store_node())
    graph.add_node("persist_post",         make_persist_post_node(redis_client, mongo_col))

    graph.set_entry_point("reason_validate_post")
    graph.add_edge("reason_validate_post", "validate_store")
    graph.add_edge("validate_store",       "persist_post")
    graph.add_edge("persist_post",         END)
    return graph.compile()


# ══════════════════════════════════════════════════════════════════════════════
# GRAPH 2 — ReadPost
# ══════════════════════════════════════════════════════════════════════════════

class ReadPostState(TypedDict):
    req_id:  int
    post_id: int

    # After check_cache
    cached_json:  Optional[str]    # raw JSON string from Redis, or None
    cached_post:  Optional[Any]    # parsed Post if cache hit

    # LLM output
    llm_use_cache: Optional[bool]
    llm_reason:    Optional[str]

    # Final decision
    use_cache: Optional[bool]

    # Final post
    post: Optional[Any]

    # Metrics
    total_input_tokens:  int
    total_output_tokens: int
    total_llm_calls:     int
    fallback_used:       bool


def make_check_cache_node(redis_client):
    """Deterministic: Redis GET + JSON parse."""
    async def check_cache(state: ReadPostState) -> ReadPostState:
        cache_key = str(state["post_id"])
        try:
            val = redis_client.get(cache_key)
            if val is not None:
                raw = val.decode("utf-8")
                state["cached_json"] = raw
                try:
                    state["cached_post"] = json_to_post(raw)
                    logger.debug("check_cache HIT post_id=%d", state["post_id"])
                    print(f"[check_cache] HIT  post_id={state['post_id']}")
                except Exception:
                    state["cached_post"] = None
                    logger.warning("check_cache corrupt JSON post_id=%d", state["post_id"])
            else:
                state["cached_json"] = None
                state["cached_post"] = None
                print(f"[check_cache] MISS  post_id={state['post_id']}")
        except redis_lib.RedisError as exc:
            logger.warning("check_cache Redis GET failed: %s", exc)
            state["cached_json"] = None
            state["cached_post"] = None
        return state
    return check_cache


def make_reason_cache_decision_node():
    """LLM: decide whether cached value is valid/trustworthy."""
    async def reason_cache_decision(state: ReadPostState) -> ReadPostState:
        post_id     = state["post_id"]
        cached_post = state.get("cached_post")
        has_cache   = cached_post is not None

        # Build cache summary for LLM
        if has_cache:
            cached_summary = {
                "post_id":   cached_post.post_id,
                "creator_user_id": cached_post.creator.user_id if cached_post.creator else None,
                "timestamp": cached_post.timestamp,
                "text_len":  len(cached_post.text or ""),
            }
        else:
            cached_summary = None

        prompt = f"""
You are a post cache decision agent for a social network.

Your task is to decide whether a cached post can be used directly,
or whether the database should be queried for a fresh copy.

Request:
  post_id    = {post_id}
  cache_hit  = {has_cache}
  cached_summary = {json.dumps(cached_summary)}

Decision rules:
  1. If cache_hit is False → use_cache = false (must fetch from MongoDB)
  2. If cache_hit is True AND cached_summary.post_id == {post_id} → use_cache = true
  3. If cache_hit is True BUT cached_summary.post_id != {post_id} → use_cache = false (corrupt)
  4. If cached_summary is null → use_cache = false

Return ONLY valid JSON — no explanation, no code, no markdown.

Schema:
{{
  "use_cache": true | false,
  "reason":    "<short explanation>" // only if use_cache=false, e.g. "cache miss", "post_id mismatch", "JSON parse error"
}}
"""

        logger.info("LLM reason_cache_decision req_id=%d post_id=%d has_cache=%s, prompt=%s",
                    state["req_id"], post_id, has_cache, prompt)
        response = await asyncio.to_thread(llm.invoke, prompt)
        raw      = response.text()
        in_tok   = response.usage_metadata.get("input_tokens",  0)
        out_tok  = response.usage_metadata.get("output_tokens", 0)

        logger.info("LLM raw=%r  in=%d out=%d", raw[:100], in_tok, out_tok)
        print(f"[reason_cache_decision] raw={raw[:80]!r}  in={in_tok} out={out_tok}")

        parsed    = _parse_json(raw)
        use_cache = parsed.get("use_cache") if parsed else None
        reason    = parsed.get("reason", "") if parsed else ""

        state["llm_use_cache"]       = use_cache if isinstance(use_cache, bool) else None
        state["llm_reason"]          = reason
        state["total_input_tokens"]  += in_tok
        state["total_output_tokens"] += out_tok
        state["total_llm_calls"]     += 1
        return state
    return reason_cache_decision


def make_validate_cache_node():
    """
    Deterministic guard: if cached_post.post_id matches → always use cache.
    If no cached post → always go to MongoDB.
    LLM cannot force an unnecessary MongoDB call when cache is good,
    nor skip MongoDB when cache is genuinely missing/corrupt.
    """
    async def validate_cache(state: ReadPostState) -> ReadPostState:
        cached_post = state.get("cached_post")
        post_id     = state["post_id"]

        # Deterministic reference decision
        det_use_cache = (
            cached_post is not None
            and cached_post.post_id == post_id
        )

        llm_use_cache = state.get("llm_use_cache")

        if llm_use_cache is not None and llm_use_cache == det_use_cache:
            state["use_cache"]     = llm_use_cache
            state["fallback_used"] = False
            logger.info("validate_cache PASS req_id=%d use_cache=%s",
                        state["req_id"], llm_use_cache)
            print(f"[validate_cache] PASS  use_cache={llm_use_cache}")
        else:
            state["use_cache"]     = det_use_cache
            state["fallback_used"] = True
            logger.warning(
                "validate_cache FALLBACK req_id=%d llm=%s -> det=%s",
                state["req_id"], llm_use_cache, det_use_cache,
            )
            print(f"[validate_cache] FALLBACK  llm={llm_use_cache} -> det={det_use_cache}")

        # If using cache, set post now
        if state["use_cache"]:
            state["post"] = cached_post
        return state
    return validate_cache


def make_fetch_if_needed_node(redis_client, mongo_col):
    """Deterministic: MongoDB lookup on cache miss. Populate Redis on hit."""
    async def fetch_if_needed(state: ReadPostState) -> ReadPostState:
        from ms_baseline.dsb_social.gen_py.social_network.ttypes import ServiceException, ErrorCode

        if state.get("use_cache"):
            return state   # already have post from cache

        post_id = state["post_id"]
        try:
            doc = mongo_col.find_one({"post_id": post_id}, {"_id": 0})
        except Exception as exc:
            raise ServiceException(
                errorCode=ErrorCode.SE_MONGODB_ERROR,
                message=f"MongoDB read failed: {exc}",
            )

        if doc is None:
            raise ServiceException(
                errorCode=ErrorCode.SE_THRIFT_HANDLER_ERROR,
                message=f"Post not found: {post_id}",
            )

        post = dict_to_post(doc)

        # Backfill Redis
        try:
            redis_client.set(str(post_id), post_to_json(post))
        except redis_lib.RedisError:
            pass

        state["post"] = post
        logger.debug("fetch_if_needed MongoDB HIT post_id=%d", post_id)
        print(f"[fetch_if_needed] MongoDB HIT  post_id={post_id}")
        return state
    return fetch_if_needed


def _route_after_validate_cache(state: ReadPostState) -> str:
    return "END" if state.get("use_cache") else "fetch_if_needed"


def build_read_agent(redis_client, mongo_col) -> any:
    graph = StateGraph(ReadPostState)
    graph.add_node("check_cache",           make_check_cache_node(redis_client))
    graph.add_node("reason_cache_decision", make_reason_cache_decision_node())
    graph.add_node("validate_cache",        make_validate_cache_node())
    graph.add_node("fetch_if_needed",       make_fetch_if_needed_node(redis_client, mongo_col))

    graph.set_entry_point("check_cache")
    graph.add_edge("check_cache",           "reason_cache_decision")
    graph.add_edge("reason_cache_decision", "validate_cache")
    graph.add_conditional_edges(
        "validate_cache",
        _route_after_validate_cache,
        {"END": END, "fetch_if_needed": "fetch_if_needed"},
    )
    graph.add_edge("fetch_if_needed", END)
    return graph.compile()


# ══════════════════════════════════════════════════════════════════════════════
# GRAPH 3 — ReadPosts (batch)
# ══════════════════════════════════════════════════════════════════════════════

class ReadBatchState(TypedDict):
    req_id:   int
    post_ids: List[int]          # original input order

    # After check_cache_batch
    cache_hits:  Dict[int, Any]  # post_id -> Post (from Redis)
    missing_ids: List[int]       # post_ids not found in Redis

    # LLM output
    llm_ids_to_fetch: Optional[List[int]]   # which IDs to fetch from MongoDB

    # Final fetch set (validated)
    ids_to_fetch: List[int]

    # After fetch_missing
    mongo_hits: Dict[int, Any]   # post_id -> Post (from MongoDB)

    # Final assembled result
    posts: List[Any]             # ordered by original post_ids

    # Metrics
    total_input_tokens:  int
    total_output_tokens: int
    total_llm_calls:     int
    fallback_used:       bool


def make_check_cache_batch_node(redis_client):
    """Deterministic: Redis MGET for all post_ids. Split into hits + misses."""
    async def check_cache_batch(state: ReadBatchState) -> ReadBatchState:
        post_ids = state["post_ids"]
        if not post_ids:
            state["cache_hits"]  = {}
            state["missing_ids"] = []
            return state

        cache_hits  = {}
        missing_ids = []

        try:
            keys = [str(pid) for pid in post_ids]
            vals = redis_client.mget(keys)
            for pid, val in zip(post_ids, vals):
                if val is not None:
                    try:
                        post = json_to_post(val.decode("utf-8"))
                        if post.post_id == pid:
                            cache_hits[pid] = post
                        else:
                            missing_ids.append(pid)
                    except Exception:
                        missing_ids.append(pid)
                else:
                    missing_ids.append(pid)
        except redis_lib.RedisError as exc:
            logger.warning("check_cache_batch Redis MGET failed: %s", exc)
            cache_hits  = {}
            missing_ids = list(post_ids)

        state["cache_hits"]  = cache_hits
        state["missing_ids"] = missing_ids
        logger.info(
            "check_cache_batch req_id=%d total=%d hits=%d misses=%d",
            state["req_id"], len(post_ids), len(cache_hits), len(missing_ids),
        )
        print(
            f"[check_cache_batch] total={len(post_ids)} "
            f"hits={len(cache_hits)} misses={len(missing_ids)}"
        )
        return state
    return check_cache_batch


def make_reason_batch_complete_node():
    """
    LLM: given hit/miss summary, decide which IDs need MongoDB fetch.
    The LLM can suggest fetching additional IDs (e.g. suspect cache entries),
    but cannot reduce the fetch set below the deterministic missing_ids.
    """
    async def reason_batch_complete(state: ReadBatchState) -> ReadBatchState:
        missing  = state["missing_ids"]
        hits     = list(state["cache_hits"].keys())

        if not missing:
            # All hits — nothing to reason about
            logger.info("reason_batch_complete req_id=%d all cache hits, skipping LLM reasoning", state["req_id"])
            state["llm_ids_to_fetch"] = []
            return state

        prompt = f"""
You are a batch post retrieval agent for a social network.

Your task is to decide which post IDs need to be fetched from the database.

Request summary:
  total_requested = {len(state["post_ids"])}
  cache_hits      = {hits}   (found in Redis cache — valid)
  cache_misses    = {missing}  (NOT found in Redis — must fetch from MongoDB)

Decision rules:
  1. ALL cache_misses MUST be fetched from MongoDB (required)
  2. You MAY additionally flag any cache_hit IDs for re-fetch if you suspect
     they might be stale — but this is optional and conservative
  3. At minimum, ids_to_fetch must equal cache_misses

Return the list of post IDs that should be fetched from MongoDB.
Return ONLY valid JSON — no explanation, no code, no markdown.

Schema:
{{
  "ids_to_fetch": [<integer>, ...]
}}
"""

        logger.info("LLM reason_batch_complete req_id=%d missing=%d, prompt=%s",
                    state["req_id"], len(missing), prompt)
        response = await asyncio.to_thread(llm.invoke, prompt)
        raw      = response.text()
        in_tok   = response.usage_metadata.get("input_tokens",  0)
        out_tok  = response.usage_metadata.get("output_tokens", 0)

        logger.info("LLM raw=%r  in=%d out=%d", raw[:150], in_tok, out_tok)
        print(f"[reason_batch_complete] raw={raw[:100]!r}  in={in_tok} out={out_tok}")

        parsed   = _parse_json(raw)
        llm_ids  = None

        if parsed:
            ids = parsed.get("ids_to_fetch")
            if isinstance(ids, list) and all(isinstance(x, int) for x in ids):
                llm_ids = ids

        state["llm_ids_to_fetch"]     = llm_ids
        state["total_input_tokens"]  += in_tok
        state["total_output_tokens"] += out_tok
        state["total_llm_calls"]     += 1
        return state
    return reason_batch_complete


def make_validate_batch_node():
    """
    Deterministic guard: fetch set must be a superset of missing_ids.
    LLM can expand the set but never shrink it.
    """
    async def validate_batch(state: ReadBatchState) -> ReadBatchState:
        missing_set = set(state["missing_ids"])
        llm_ids     = state.get("llm_ids_to_fetch")

        if llm_ids is not None:
            llm_set = set(llm_ids)
            if missing_set.issubset(llm_set):
                # LLM is a superset (potentially added extra IDs) — use it
                state["ids_to_fetch"] = list(llm_set)
                state["fallback_used"] = False
                logger.info("validate_batch PASS req_id=%d fetch_count=%d",
                            state["req_id"], len(llm_set))
                print(f"[validate_batch] PASS  fetch_count={len(llm_set)}")
                return state

        # LLM missing/wrong — use deterministic missing_ids
        state["ids_to_fetch"]  = list(missing_set)
        state["fallback_used"] = True
        logger.warning(
            "validate_batch FALLBACK req_id=%d llm_ids=%s -> missing=%s",
            state["req_id"], llm_ids, list(missing_set),
        )
        print(f"[validate_batch] FALLBACK  -> missing={list(missing_set)}")
        return state
    return validate_batch


def make_fetch_missing_node(redis_client, mongo_col):
    """Deterministic: MongoDB $in query for all ids_to_fetch. Backfill Redis."""
    async def fetch_missing(state: ReadBatchState) -> ReadBatchState:
        from ms_baseline.dsb_social.gen_py.social_network.ttypes import ServiceException, ErrorCode

        ids_to_fetch = state.get("ids_to_fetch") or []
        if not ids_to_fetch:
            state["mongo_hits"] = {}
            return state

        try:
            cursor = mongo_col.find(
                {"post_id": {"$in": ids_to_fetch}},
                {"_id": 0},
            )
            mongo_hits = {}
            for doc in cursor:
                post = dict_to_post(doc)
                mongo_hits[post.post_id] = post
                # Backfill Redis
                try:
                    redis_client.set(str(post.post_id), post_to_json(post))
                except redis_lib.RedisError:
                    pass
        except Exception as exc:
            raise ServiceException(
                errorCode=ErrorCode.SE_MONGODB_ERROR,
                message=f"MongoDB batch read failed: {exc}",
            )

        state["mongo_hits"] = mongo_hits
        logger.debug("fetch_missing req_id=%d fetched=%d/%d",
                     state["req_id"], len(mongo_hits), len(ids_to_fetch))
        print(f"[fetch_missing] fetched={len(mongo_hits)}/{len(ids_to_fetch)}")
        return state
    return fetch_missing


def make_assemble_ordered_node():
    """
    Deterministic: merge cache_hits + mongo_hits, reorder by original post_ids.
    Raise ServiceException for any post_id not found in either source.
    """
    async def assemble_ordered(state: ReadBatchState) -> ReadBatchState:
        from ms_baseline.dsb_social.gen_py.social_network.ttypes import ServiceException, ErrorCode

        all_posts: Dict[int, Any] = {}
        all_posts.update(state.get("cache_hits",  {}))
        all_posts.update(state.get("mongo_hits",  {}))

        result = []
        for pid in state["post_ids"]:
            if pid not in all_posts:
                raise ServiceException(
                    errorCode=ErrorCode.SE_THRIFT_HANDLER_ERROR,
                    message=f"Post not found: {pid}",
                )
            result.append(all_posts[pid])

        state["posts"] = result
        logger.debug("assemble_ordered req_id=%d count=%d",
                     state["req_id"], len(result))
        print(f"[assemble_ordered] OK  count={len(result)}")
        return state
    return assemble_ordered


def build_read_batch_agent(redis_client, mongo_col) -> any:
    graph = StateGraph(ReadBatchState)
    graph.add_node("check_cache_batch",       make_check_cache_batch_node(redis_client))
    graph.add_node("reason_batch_complete",   make_reason_batch_complete_node())
    graph.add_node("validate_batch",          make_validate_batch_node())
    graph.add_node("fetch_missing",           make_fetch_missing_node(redis_client, mongo_col))
    graph.add_node("assemble_ordered",        make_assemble_ordered_node())

    graph.set_entry_point("check_cache_batch")
    graph.add_edge("check_cache_batch",     "reason_batch_complete")
    graph.add_edge("reason_batch_complete", "validate_batch")
    graph.add_edge("validate_batch",        "fetch_missing")
    graph.add_edge("fetch_missing",         "assemble_ordered")
    graph.add_edge("assemble_ordered",      END)
    return graph.compile()
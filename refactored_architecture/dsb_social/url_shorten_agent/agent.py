"""
URL-SHORTEN AGENT - Graph Topologies

ComposeUrls graph (per URL):
    START
      |
      v
  [check_cache]         Deterministic — Redis lookup (expanded_url → shortened_url)
      |
      v  (cache miss only)
  [reason_short_token]  LLM — given expanded_url, compute:
      |                   1. MD5 hex digest of the URL string
      |                   2. Base62-encode the 128-bit integer
      |                   3. Take the first 10 characters as the token
      v
  [validate_token]      Deterministic guard — verify token is exactly 10 alnum chars;
      |                   if wrong, fall back to url_shortener.make_short_token()
      v
  [persist]             Deterministic — MongoDB upsert + Redis cache (both directions)
      |
      v
     END  →  Url(shortened_url, expanded_url)

GetExtendedUrls graph (per shortened URL):
    START
      |
      v
  [check_reverse_cache] Deterministic — Redis lookup (shortened_url → expanded_url)
      |
      v  (cache miss only)
  [query_mongo]         Deterministic — MongoDB lookup by shortened_url
      |
      v
     END  →  expanded_url string

Key Design Decisions
--------------------
- Thrift interface (UrlShortenService.Iface) UNCHANGED.
- MongoDB schema, Redis key layout UNCHANGED.
- The LLM reasoning replaces make_short_token() — the static algorithm
  that computes MD5 → base62 → 10-char token.
- url_shortener.py is kept as the deterministic fallback reference.
- validate_token is a hard guard: if the LLM's token has wrong length or
  non-alnum chars, we fall back to the reference implementation.
- GetExtendedUrls has NO LLM node — it is pure cache + MongoDB lookup.
  There is no "reasoning" needed for reverse lookup.
- Token metrics (input_tokens, output_tokens, llm_calls) accumulated per
  ComposeUrls call across all URLs in the batch.
"""

import json
import logging
import re
import asyncio

from typing import TypedDict, Optional, List, Dict, Any
from langgraph.graph import StateGraph, END
from langchain_ollama import ChatOllama

import redis as redis_lib
from pymongo import MongoClient

from .url_shortener import make_short_token, make_shortened_url

logger = logging.getLogger("url-shorten-agent")

# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------

llm = ChatOllama(model="llama3.2:3b", temperature=0.0, reasoning=False)

# ---------------------------------------------------------------------------
# Redis cache key prefixes — identical to original handler.py
# ---------------------------------------------------------------------------

_KEY_EXPAND  = "expand:"    # expanded_url  → shortened_url
_KEY_SHORTEN = "shorten:"   # shortened_url → expanded_url

_TOKEN_LEN = 10


# ---------------------------------------------------------------------------
# Agent State — ComposeUrls (single URL)
# ---------------------------------------------------------------------------

class ComposeUrlAgentState(TypedDict):
    # Inputs
    req_id:        int
    expanded_url:  str
    hostname:      str

    # Shared storage (injected at graph build time via closures)
    # (not stored in state — passed via closure to each node)

    # Cache hit result (from check_cache node)
    cache_hit:     bool
    cached_short:  Optional[str]       # shortened_url from Redis on hit

    # LLM output (from reason_short_token node)
    llm_token:     Optional[str]       # raw 10-char token returned by LLM

    # Final result (confirmed by validate_token, corrected if needed)
    short_token:   Optional[str]       # validated 10-char token
    shortened_url: Optional[str]       # hostname + token

    # Metrics
    total_input_tokens:  int
    total_output_tokens: int
    total_llm_calls:     int
    fallback_used:       bool


# ---------------------------------------------------------------------------
# Agent State — GetExtendedUrls (single shortened URL)
# ---------------------------------------------------------------------------

class ExpandUrlAgentState(TypedDict):
    req_id:        int
    shortened_url: str

    cache_hit:     bool
    expanded_url:  Optional[str]


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


def _is_valid_token(token: Any) -> bool:
    """A valid short token is exactly 10 alphanumeric characters."""
    return (
        isinstance(token, str)
        and len(token) == _TOKEN_LEN
        and token.isalnum()
    )


# ===========================================================================
# ComposeUrls graph nodes
# ===========================================================================

def make_check_cache_node(redis_client: redis_lib.Redis):
    """
    Node: check_cache
    Look up expanded_url in Redis. On hit, set cache_hit=True and
    populate shortened_url so the rest of the graph is skipped.
    """
    async def check_cache(state: ComposeUrlAgentState) -> ComposeUrlAgentState:
        key = _KEY_EXPAND + state["expanded_url"]
        try:
            val = redis_client.get(key)
            if val is not None:
                short = val.decode("utf-8")
                state["cache_hit"]    = True
                state["cached_short"] = short
                state["shortened_url"] = short
                logger.info(
                    "check_cache HIT req_id=%d expanded=%s -> %s",
                    state["req_id"], state["expanded_url"][:50], short,
                )
                print(f"[check_cache] HIT  {state['expanded_url'][:50]} -> {short}")
            else:
                state["cache_hit"]    = False
                state["cached_short"] = None
                logger.info("check_cache MISS req_id=%d expanded=%s",
                            state["req_id"], state["expanded_url"][:50])
                print(f"[check_cache] MISS {state['expanded_url'][:50]}")
        except redis_lib.RedisError as exc:
            logger.warning("Redis GET failed key=%s: %s", key, exc)
            state["cache_hit"]    = False
            state["cached_short"] = None
        return state
    return check_cache


def make_reason_short_token_node():
    """
    Node: reason_short_token  (LLM)
    Ask the LLM to compute the short token using the MD5 → base62 → 10-char algorithm.
    """
    async def reason_short_token(state: ComposeUrlAgentState) -> ComposeUrlAgentState:
        # Skip if cache hit
        if state["cache_hit"]:
            logger.info("reason_short_token SKIPPED (cache hit) req_id=%d", state["req_id"])
            return state

        expanded_url = state["expanded_url"]

        prompt = f"""
You are a URL shortening agent.

Your task is to compute a short token for a given URL using the following algorithm:

Algorithm (step by step):
  Step 1 — Compute the MD5 hash of the URL string (UTF-8 encoded).
            The result is a 32-character hexadecimal string (128-bit value).
  Step 2 — Interpret the 128-bit MD5 value as a big-endian unsigned integer.
  Step 3 — Base62-encode that integer.
            Base62 alphabet (in order): 0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz
            Repeatedly divide by 62, collect remainders, reverse to get the base62 string.
  Step 4 — Take the FIRST 10 characters of the base62 string as the token.
            If the base62 string is shorter than 10 characters (very unlikely),
            pad with '0' on the right to reach exactly 10 characters.

Rules:
  - The token MUST be exactly 10 characters long
  - The token MUST contain only alphanumeric characters (0-9, A-Z, a-z)
  - Return ONLY valid JSON — no explanation, no code, no markdown
  - Do not return thinking steps

Schema:
{{
  "token": "<exactly 10 alphanumeric characters>"
}}

Input URL: {expanded_url}
"""

        logger.info("LLM prompt req_id=%d url=%s", state["req_id"], expanded_url[:60])

        response = await asyncio.to_thread(llm.invoke, prompt)
        raw      = response.text()
        in_tok   = response.usage_metadata.get("input_tokens",  0)
        out_tok  = response.usage_metadata.get("output_tokens", 0)

        logger.info("LLM raw=%r  in_tokens=%d out_tokens=%d", raw, in_tok, out_tok)
        print(f"[reason_short_token] raw={raw!r}  in={in_tok} out={out_tok}")

        parsed = _parse_json(raw)
        llm_token = None
        if parsed and isinstance(parsed.get("token"), str):
            llm_token = parsed["token"]

        state["llm_token"]           = llm_token
        state["total_input_tokens"]  += in_tok
        state["total_output_tokens"] += out_tok
        state["total_llm_calls"]     += 1
        return state
    return reason_short_token


def make_validate_token_node(hostname: str):
    """
    Node: validate_token  (deterministic guard)
    Verify the LLM token is valid. Fall back to url_shortener.make_short_token()
    if it's wrong.
    """
    async def validate_token(state: ComposeUrlAgentState) -> ComposeUrlAgentState:
        if state["cache_hit"]:
            return state

        token = state.get("llm_token")

        if _is_valid_token(token):
            state["short_token"]   = token
            state["shortened_url"] = hostname.rstrip("/") + "/" + token
            state["fallback_used"] = False
            logger.info(
                "validate_token PASS req_id=%d token=%s", state["req_id"], token
            )
            print(f"[validate_token] PASS  token={token}")
        else:
            # Fallback to deterministic reference implementation
            fallback_token = make_short_token(state["expanded_url"])
            state["short_token"]   = fallback_token
            state["shortened_url"] = make_shortened_url(hostname, state["expanded_url"])
            state["fallback_used"] = True
            logger.warning(
                "validate_token FALLBACK req_id=%d llm_token=%r -> fallback=%s",
                state["req_id"], token, fallback_token,
            )
            print(f"[validate_token] FALLBACK  llm={token!r} -> correct={fallback_token}")

        return state
    return validate_token


def make_persist_node(redis_client: redis_lib.Redis, mongo_col):
    """
    Node: persist  (deterministic)
    MongoDB upsert + Redis cache set (both directions).
    Identical logic to the original handler._mongo_upsert + _cache_set.
    """
    async def persist(state: ComposeUrlAgentState) -> ComposeUrlAgentState:
        if state["cache_hit"]:
            return state

        expanded  = state["expanded_url"]
        shortened = state["shortened_url"]

        # ---- MongoDB upsert (identical to original handler) ----
        try:
            mongo_col.update_one(
                {"expanded_url": expanded},
                {"$set": {"expanded_url": expanded, "shortened_url": shortened}},
                upsert=True,
            )
            logger.info("persist MongoDB upsert req_id=%d %s -> %s",
                        state["req_id"], expanded[:50], shortened)
        except Exception as exc:
            logger.error("persist MongoDB failed: %s", exc)
            raise

        # ---- Redis cache both directions (identical to original handler) ----
        try:
            redis_client.set(_KEY_EXPAND  + expanded,  shortened)
            redis_client.set(_KEY_SHORTEN + shortened, expanded)
        except redis_lib.RedisError as exc:
            logger.warning("persist Redis SET failed: %s", exc)
            # Non-fatal — data is in MongoDB

        print(f"[persist] OK  {expanded[:50]} -> {shortened}")
        return state
    return persist


# ---------------------------------------------------------------------------
# Routing function for ComposeUrls graph
# ---------------------------------------------------------------------------

def _route_after_cache(state: ComposeUrlAgentState) -> str:
    """Skip LLM + persist nodes if we got a cache hit."""
    return "END" if state["cache_hit"] else "reason_short_token"


# ---------------------------------------------------------------------------
# ComposeUrls graph builder
# ---------------------------------------------------------------------------

def build_compose_url_agent(
    redis_client: redis_lib.Redis,
    mongo_col,
    hostname: str,
):
    """Build and compile the ComposeUrls LangGraph agent."""
    graph = StateGraph(ComposeUrlAgentState)

    graph.add_node("check_cache",        make_check_cache_node(redis_client))
    graph.add_node("reason_short_token", make_reason_short_token_node())
    graph.add_node("validate_token",     make_validate_token_node(hostname))
    graph.add_node("persist",            make_persist_node(redis_client, mongo_col))

    graph.set_entry_point("check_cache")

    # On cache hit → END immediately; on miss → reason → validate → persist → END
    graph.add_conditional_edges(
        "check_cache",
        _route_after_cache,
        {"END": END, "reason_short_token": "reason_short_token"},
    )
    graph.add_edge("reason_short_token", "validate_token")
    graph.add_edge("validate_token",     "persist")
    graph.add_edge("persist",            END)

    return graph.compile()


# ===========================================================================
# GetExtendedUrls graph nodes  (no LLM — pure lookup)
# ===========================================================================

def make_check_reverse_cache_node(redis_client: redis_lib.Redis):
    """Node: check_reverse_cache — Redis lookup shortened_url → expanded_url."""
    async def check_reverse_cache(state: ExpandUrlAgentState) -> ExpandUrlAgentState:
        key = _KEY_SHORTEN + state["shortened_url"]
        try:
            val = redis_client.get(key)
            if val is not None:
                state["cache_hit"]    = True
                state["expanded_url"] = val.decode("utf-8")
                logger.info("reverse_cache HIT %s", state["shortened_url"])
                print(f"[check_reverse_cache] HIT  {state['shortened_url']}")
            else:
                state["cache_hit"]    = False
                state["expanded_url"] = None
        except redis_lib.RedisError as exc:
            logger.warning("Redis GET reverse failed: %s", exc)
            state["cache_hit"]    = False
            state["expanded_url"] = None
        return state
    return check_reverse_cache


def make_query_mongo_node(redis_client: redis_lib.Redis, mongo_col):
    """Node: query_mongo — MongoDB lookup + Redis backfill on miss."""
    async def query_mongo(state: ExpandUrlAgentState) -> ExpandUrlAgentState:
        if state["cache_hit"]:
            return state
        try:
            doc = mongo_col.find_one({"shortened_url": state["shortened_url"]})
        except Exception as exc:
            logger.error("query_mongo MongoDB failed: %s", exc)
            raise

        if doc is None:
            state["expanded_url"] = None
            logger.info("query_mongo NOT FOUND %s", state["shortened_url"])
            print(f"[query_mongo] NOT FOUND  {state['shortened_url']}")
        else:
            expanded = doc["expanded_url"]
            state["expanded_url"] = expanded
            # Backfill Redis
            try:
                redis_client.set(_KEY_SHORTEN + state["shortened_url"], expanded)
                redis_client.set(_KEY_EXPAND  + expanded, state["shortened_url"])
            except redis_lib.RedisError:
                pass
            logger.info("query_mongo FOUND %s -> %s",
                        state["shortened_url"], expanded[:50])
            print(f"[query_mongo] FOUND  {state['shortened_url']} -> {expanded[:50]}")
        return state
    return query_mongo


def _route_after_reverse_cache(state: ExpandUrlAgentState) -> str:
    return "END" if state["cache_hit"] else "query_mongo"


def build_expand_url_agent(redis_client: redis_lib.Redis, mongo_col):
    """Build and compile the GetExtendedUrls LangGraph agent (no LLM)."""
    graph = StateGraph(ExpandUrlAgentState)

    graph.add_node("check_reverse_cache", make_check_reverse_cache_node(redis_client))
    graph.add_node("query_mongo",         make_query_mongo_node(redis_client, mongo_col))

    graph.set_entry_point("check_reverse_cache")
    graph.add_conditional_edges(
        "check_reverse_cache",
        _route_after_reverse_cache,
        {"END": END, "query_mongo": "query_mongo"},
    )
    graph.add_edge("query_mongo", END)

    return graph.compile()
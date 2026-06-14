"""
USER TIMELINE AGENT - Graph Topologies

━━━ Graph 1: write_timeline_agent  (WriteUserTimeline) ━━━━━━━━━━━━━━━━━━━━━

    START
      |
      v
  [fetch_existing]      Deterministic — check Redis sorted set for existing
      |                 entry with this post_id (duplicate detection) and
      |                 retrieve current timeline size for context.
      v
  [reason_write]        LLM — validate the write request:
      |                 - Is post_id a valid positive integer?
      |                 - Is timestamp in a reasonable range (not far future/past)?
      |                 - Is this a duplicate (post_id already in timeline)?
      |                 - Approve or reject the write.
      |                 Returns: { approved: bool, reason: str }
      v
  [validate_write]      Deterministic guard — override LLM if it wrongly
      |                 rejects a valid write, or wrongly approves a duplicate.
      |                 Self-corrects using deterministic duplicate check.
      v
  [apply_write]         Deterministic — Redis ZADD + MongoDB $push.
      |                 Identical to original handler._redis_write + _mongo_write.
      v
     END

━━━ Graph 2: read_timeline_agent  (ReadUserTimeline) ━━━━━━━━━━━━━━━━━━━━━━━

    START
      |
      v
  [fetch_post_ids]      Deterministic — Redis ZREVRANGE (cache-first) or
      |                 MongoDB fallback with timestamp-desc sort + seed Redis.
      |                 Returns all post_ids with scores (timestamps).
      v
  [reason_paginate]     LLM — given the full list of (post_id, timestamp) pairs
      |                 and the requested [start, stop) window, select the
      |                 correct slice in reverse-chronological order.
      |                 Returns: { post_ids: [i64...] } for the requested page.
      v
  [validate_paginate]   Deterministic guard — verify LLM returned exactly
      |                 (stop - start) items from the correct window.
      |                 If wrong, fall back to deterministic slice of DB result.
      v
  [hydrate_posts]       Deterministic — PostStorageService.ReadPosts(post_ids).
      |
      v
     END  →  list[Post]

Key Design Decisions
────────────────────
- Thrift interface (UserTimelineService.Iface) UNCHANGED.
- MongoDB schema, Redis sorted-set layout UNCHANGED.
- LLM reasons about:
    Write: "Is this write valid? Is it a duplicate? Should I approve it?"
    Read:  "Given this list of (post_id, timestamp) pairs, which ones
            belong in the [start, stop) window sorted newest-first?"
- validate_write always enforces: if post_id already in Redis sorted set
  → reject regardless of LLM approval (idempotency guard).
- validate_paginate always uses deterministic DB slice if LLM returns
  wrong count or out-of-window IDs.
- Token metrics tracked per request.
"""

import json
import logging
import re
import asyncio
import time
from typing import TypedDict, Optional, List, Dict, Any, Tuple

from langgraph.graph import StateGraph, END
from langchain_ollama import ChatOllama

import redis as redis_lib
from pymongo import MongoClient

from .thrift_pool import ThriftClientPool

logger = logging.getLogger("user-timeline-agent")

# ── LLM ─────────────────────────────────────────────────────────────────────
llm = ChatOllama(model="llama3.2:3b", temperature=0.0, reasoning=False)

# ── Redis key prefix ─────────────────────────────────────────────────────────
_REDIS_KEY_PREFIX = "user-timeline:"   # user-timeline:<user_id>

# Reasonable timestamp bounds (ms): year 2000 → year 2100
_TS_MIN = 946_684_800_000
_TS_MAX = 4_102_444_800_000


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_json(text: str) -> Optional[dict]:
    try:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            return json.loads(m.group(0))
    except Exception as exc:
        logger.warning("JSON parse error: %s — raw: %r", exc, text[:200])
    return None


def _redis_key(user_id: int) -> str:
    return _REDIS_KEY_PREFIX + str(user_id)


# ══════════════════════════════════════════════════════════════════════════════
# GRAPH 1 — WriteUserTimeline
# ══════════════════════════════════════════════════════════════════════════════

class WriteTimelineState(TypedDict):
    req_id:    int
    post_id:   int
    user_id:   int
    timestamp: int

    # After fetch_existing
    already_exists:   bool    # True if post_id already in Redis sorted set
    timeline_size:    int     # current number of entries in the timeline

    # LLM output
    llm_approved: Optional[bool]
    llm_reason:   Optional[str]

    # Final decision
    approved: Optional[bool]

    # Metrics
    total_input_tokens:  int
    total_output_tokens: int
    total_llm_calls:     int
    fallback_used:       bool


def make_fetch_existing_node(redis_client):
    """Deterministic: check whether post_id is already in the user's timeline."""
    async def fetch_existing(state: WriteTimelineState) -> WriteTimelineState:
        key = _redis_key(state["user_id"])
        try:
            # ZSCORE returns the score if member exists, None otherwise
            score = redis_client.zscore(key, str(state["post_id"]))
            state["already_exists"] = score is not None
            size  = redis_client.zcard(key)
            state["timeline_size"]  = int(size) if size else 0
        except redis_lib.RedisError as exc:
            logger.warning("fetch_existing Redis failed: %s", exc)
            state["already_exists"] = False
            state["timeline_size"]  = 0

        logger.debug(
            "fetch_existing req_id=%d post_id=%d user_id=%d "
            "already_exists=%s timeline_size=%d",
            state["req_id"], state["post_id"], state["user_id"],
            state["already_exists"], state["timeline_size"],
        )
        print(
            f"[fetch_existing] post_id={state['post_id']} "
            f"already_exists={state['already_exists']} "
            f"timeline_size={state['timeline_size']}"
        )
        return state
    return fetch_existing


def make_reason_write_node():
    """LLM: validate the write request and decide approval."""
    async def reason_write(state: WriteTimelineState) -> WriteTimelineState:
        now_ms = int(time.time() * 1000)

        prompt = f"""
You are a user timeline write validation agent for a social network.

Your task is to decide whether a new post should be written to a user's personal timeline.

Write request:
  req_id        = {state["req_id"]}
  user_id       = {state["user_id"]}
  post_id       = {state["post_id"]}
  timestamp     = {state["timestamp"]} ms  (current time ≈ {now_ms} ms)
  already_exists = {state["already_exists"]}  (True = duplicate, post_id already in timeline)
  timeline_size  = {state["timeline_size"]}   (current number of entries)

Validation rules:
  1. post_id must be a positive integer (> 0)
  2. user_id must be a positive integer (> 0)
  3. timestamp must be within a reasonable range:
       minimum: {_TS_MIN} ms  (year 2000)
       maximum: {_TS_MAX} ms  (year 2100)
  4. If already_exists is True → reject (DUPLICATE — idempotency)
  5. If all rules pass → approve

Return ONLY valid JSON — no explanation, no code, no markdown.

Schema:
{{
  "approved": true | false,
  "reason":   "<short explanation>" only in case of rejection (approved=false)
}}
"""

        logger.info("LLM reason_write req_id=%d post_id=%d user_id=%d, prompt=%s",
                    state["req_id"], state["post_id"], state["user_id"], prompt)
        response = await asyncio.to_thread(llm.invoke, prompt)
        raw      = response.text()
        in_tok   = response.usage_metadata.get("input_tokens",  0)
        out_tok  = response.usage_metadata.get("output_tokens", 0)

        logger.info("LLM raw=%r  in=%d out=%d", raw[:150], in_tok, out_tok)
        print(f"[reason_write] raw={raw[:100]!r}  in={in_tok} out={out_tok}")

        parsed   = _parse_json(raw)
        approved = parsed.get("approved") if parsed else None
        reason   = parsed.get("reason",  "") if parsed else ""

        state["llm_approved"]        = approved if isinstance(approved, bool) else None
        state["llm_reason"]          = reason
        state["total_input_tokens"]  += in_tok
        state["total_output_tokens"] += out_tok
        state["total_llm_calls"]     += 1
        return state
    return reason_write


def make_validate_write_node():
    """
    Deterministic guard for write decisions.

    Hard invariants (always enforced):
      - Duplicate (already_exists=True) is ALWAYS rejected.
      - post_id <= 0 is ALWAYS rejected.
      - Timestamp out of range is ALWAYS rejected.
      - If LLM response None → fall back to deterministic logic.
    """
    async def validate_write(state: WriteTimelineState) -> WriteTimelineState:
        post_id   = state["post_id"]
        user_id   = state["user_id"]
        ts        = state["timestamp"]
        duplicate = state["already_exists"]

        # Deterministic reference decision
        det_approved = (
            post_id > 0
            and user_id > 0
            and _TS_MIN <= ts <= _TS_MAX
            and not duplicate
        )

        llm_approved = state.get("llm_approved")

        if llm_approved is not None and llm_approved == det_approved:
            state["approved"]      = llm_approved
            state["fallback_used"] = False
            logger.info("validate_write PASS req_id=%d approved=%s",
                        state["req_id"], llm_approved)
            print(f"[validate_write] PASS  approved={llm_approved}")
        else:
            state["approved"]      = det_approved
            state["fallback_used"] = True
            logger.warning(
                "validate_write FALLBACK req_id=%d llm=%s -> det=%s reason=%r",
                state["req_id"], llm_approved, det_approved, state.get("llm_reason"),
            )
            print(f"[validate_write] FALLBACK  llm={llm_approved} -> det={det_approved}")
        return state
    return validate_write


def make_apply_write_node(redis_client, mongo_col):
    """
    Deterministic: apply approved write to Redis ZADD + MongoDB $push.
    If not approved (duplicate or invalid), skip silently.
    """
    async def apply_write(state: WriteTimelineState) -> WriteTimelineState:
        if not state.get("approved"):
            logger.info(
                "apply_write SKIPPED req_id=%d post_id=%d reason=%r",
                state["req_id"], state["post_id"], state.get("llm_reason"),
            )
            print(f"[apply_write] SKIPPED  post_id={state['post_id']} (not approved)")
            return state

        key = _redis_key(state["user_id"])
        ts  = state["timestamp"]
        pid = state["post_id"]

        # ── Redis ZADD ──
        try:
            redis_client.zadd(key, {str(pid): ts})
            logger.debug("apply_write Redis ZADD key=%s post_id=%d ts=%d", key, pid, ts)
        except redis_lib.RedisError as exc:
            logger.warning("apply_write Redis ZADD failed: %s", exc)
            # Non-fatal

        # ── MongoDB $push ──
        try:
            mongo_col.update_one(
                {"user_id": state["user_id"]},
                {"$push": {"posts": {"post_id": pid, "timestamp": ts}}},
                upsert=True,
            )
            logger.debug("apply_write MongoDB $push user_id=%d post_id=%d",
                         state["user_id"], pid)
        except Exception as exc:
            from ms_baseline.dsb_social.gen_py.social_network.ttypes import ServiceException, ErrorCode
            logger.error("apply_write MongoDB failed: %s", exc)
            raise ServiceException(
                errorCode=ErrorCode.SE_MONGODB_ERROR,
                message=f"MongoDB write failed: {exc}",
            )

        print(f"[apply_write] OK  user_id={state['user_id']} post_id={pid} ts={ts}")
        return state
    return apply_write


def build_write_timeline_agent(redis_client, mongo_col) -> any:
    graph = StateGraph(WriteTimelineState)
    graph.add_node("fetch_existing",  make_fetch_existing_node(redis_client))
    graph.add_node("reason_write",    make_reason_write_node())
    graph.add_node("validate_write",  make_validate_write_node())
    graph.add_node("apply_write",     make_apply_write_node(redis_client, mongo_col))

    graph.set_entry_point("fetch_existing")
    graph.add_edge("fetch_existing", "reason_write")
    graph.add_edge("reason_write",   "validate_write")
    graph.add_edge("validate_write", "apply_write")
    graph.add_edge("apply_write",    END)
    return graph.compile()


# ══════════════════════════════════════════════════════════════════════════════
# GRAPH 2 — ReadUserTimeline
# ══════════════════════════════════════════════════════════════════════════════

class ReadTimelineState(TypedDict):
    req_id:  int
    user_id: int
    start:   int     # inclusive start index
    stop:    int     # exclusive stop index

    # After fetch_post_ids: full reverse-chron list of (post_id, score/timestamp)
    all_post_ids:   List[int]     # all post_ids, newest first
    all_timestamps: List[int]     # corresponding timestamps

    # LLM output
    llm_page_ids: Optional[List[int]]   # LLM's selected page

    # Final page result
    final_page_ids: List[int]    # deterministic-verified page

    # Hydrated posts (after hydrate_posts)
    posts: List[Any]

    # Metrics
    total_input_tokens:  int
    total_output_tokens: int
    total_llm_calls:     int
    fallback_used:       bool


def make_fetch_post_ids_node(redis_client, mongo_col):
    """
    Deterministic: Redis ZREVRANGE (all entries) or MongoDB fallback.
    Returns full reverse-chronological list of (post_id, timestamp) pairs.
    Seeds Redis from MongoDB if cache miss.
    """
    async def fetch_post_ids(state: ReadTimelineState) -> ReadTimelineState:
        uid = state["user_id"]
        key = _redis_key(uid)

        all_ids = []
        all_ts  = []

        try:
            if redis_client.exists(key):
                # ZREVRANGE with WITHSCORES → [(member, score), ...]
                pairs = redis_client.zrevrange(key, 0, -1, withscores=True)
                all_ids = [int(m) for m, _ in pairs]
                all_ts  = [int(s) for _, s in pairs]
                logger.debug("fetch_post_ids Redis HIT uid=%d count=%d", uid, len(all_ids))
                print(f"[fetch_post_ids] Redis HIT uid={uid} count={len(all_ids)}")
            else:
                raise redis_lib.ResponseError("key not found")
        except (redis_lib.RedisError, redis_lib.ResponseError):
            # MongoDB fallback
            try:
                doc = mongo_col.find_one({"user_id": uid}, {"posts": 1, "_id": 0})
                posts = (doc or {}).get("posts", [])
                # Sort by timestamp desc
                posts_sorted = sorted(posts, key=lambda p: p.get("timestamp", 0), reverse=True)
                all_ids = [int(p["post_id"])   for p in posts_sorted]
                all_ts  = [int(p.get("timestamp", 0)) for p in posts_sorted]
                logger.debug("fetch_post_ids MongoDB uid=%d count=%d", uid, len(all_ids))
                print(f"[fetch_post_ids] MongoDB uid={uid} count={len(all_ids)}")

                # Seed Redis
                if all_ids:
                    try:
                        mapping = {str(pid): ts for pid, ts in zip(all_ids, all_ts)}
                        redis_client.zadd(key, mapping)
                    except redis_lib.RedisError:
                        pass
            except Exception as exc:
                from ms_baseline.dsb_social.gen_py.social_network.ttypes import ServiceException, ErrorCode
                raise ServiceException(
                    errorCode=ErrorCode.SE_MONGODB_ERROR,
                    message=f"MongoDB read failed: {exc}",
                )

        state["all_post_ids"]   = all_ids
        state["all_timestamps"] = all_ts
        return state
    return fetch_post_ids


def make_reason_paginate_node():
    """
    LLM: given the full list of (post_id, timestamp) pairs and the requested
    [start, stop) window, select the correct reverse-chronological slice.
    """
    async def reason_paginate(state: ReadTimelineState) -> ReadTimelineState:
        all_ids = state["all_post_ids"]
        all_ts  = state["all_timestamps"]
        start   = state["start"]
        stop    = state["stop"]
        n       = stop - start

        if not all_ids:
            state["llm_page_ids"]        = []
            return state

        # Build concise representation for LLM
        entries = [
            {"index": i, "post_id": pid, "timestamp": ts}
            for i, (pid, ts) in enumerate(zip(all_ids, all_ts))
        ]
        # Limit entries shown to LLM to avoid overly long prompts
        entries_str = json.dumps(entries[:100], indent=2)

        prompt = f"""
You are a timeline pagination agent for a social network.

Your task is to select the correct posts for a requested page of a user's timeline.

The timeline is ordered NEWEST FIRST (reverse chronological order — highest timestamp first).
The list below is already sorted newest-first (index 0 = most recent post).

All timeline entries (index = position in reverse-chronological order):
{entries_str}

Requested page:
  start = {start}  (0-based inclusive start index)
  stop  = {stop}   (exclusive stop index)
  → Select entries at indices {start} through {stop - 1} inclusive (up to {n} items)
  → If the timeline has fewer than {stop} entries, return only what exists from index {start} onward

Return the post_ids for the requested page, in the same newest-first order.
Return ONLY valid JSON — no explanation, no code, no markdown.

Schema:
{{
  "post_ids": [<integer>, ...]
}}
"""

        logger.info("LLM reason_paginate req_id=%d uid=%d start=%d stop=%d total=%d, prompt=%s",
                    state["req_id"], state["user_id"], start, stop, len(all_ids), prompt)
        response = await asyncio.to_thread(llm.invoke, prompt)
        raw      = response.text()
        in_tok   = response.usage_metadata.get("input_tokens",  0)
        out_tok  = response.usage_metadata.get("output_tokens", 0)

        logger.info("LLM raw=%r  in=%d out=%d", raw[:200], in_tok, out_tok)
        print(f"[reason_paginate] raw={raw[:120]!r}  in={in_tok} out={out_tok}")

        parsed       = _parse_json(raw)
        llm_page_ids = None

        if parsed:
            ids = parsed.get("post_ids")
            if isinstance(ids, list) and all(isinstance(x, int) for x in ids):
                llm_page_ids = ids

        state["llm_page_ids"]         = llm_page_ids
        state["total_input_tokens"]  += in_tok
        state["total_output_tokens"] += out_tok
        state["total_llm_calls"]     += 1
        return state
    return reason_paginate


def make_validate_paginate_node():
    """
    Deterministic guard: compute the reference page slice and verify LLM output.

    The reference is always:  all_post_ids[start:stop]
    (already sorted newest-first by fetch_post_ids).

    LLM result is accepted only if it exactly matches the reference.
    Otherwise fall back to the reference slice — data integrity wins.
    """
    async def validate_paginate(state: ReadTimelineState) -> ReadTimelineState:
        all_ids  = state["all_post_ids"]
        start    = state["start"]
        stop     = state["stop"]
        ref_page = all_ids[start:stop]   # deterministic reference

        llm_page = state.get("llm_page_ids")

        if llm_page is not None and llm_page == ref_page:
            state["final_page_ids"] = llm_page
            state["fallback_used"]  = False
            logger.info("validate_paginate PASS req_id=%d count=%d",
                        state["req_id"], len(llm_page))
            print(f"[validate_paginate] PASS  count={len(llm_page)}")
        else:
            state["final_page_ids"] = ref_page
            state["fallback_used"]  = True
            logger.warning(
                "validate_paginate FALLBACK req_id=%d "
                "llm_count=%s ref_count=%d (using DB slice)",
                state["req_id"],
                len(llm_page) if llm_page is not None else "N/A",
                len(ref_page),
            )
            print(
                f"[validate_paginate] FALLBACK  "
                f"llm={llm_page!r} -> ref={ref_page!r}"
            )
        return state
    return validate_paginate


def make_hydrate_posts_node(post_pool: ThriftClientPool):
    """Deterministic: call PostStorageService.ReadPosts to hydrate post_ids."""
    async def hydrate_posts(state: ReadTimelineState) -> ReadTimelineState:
        page_ids = state["final_page_ids"]
        if not page_ids:
            state["posts"] = []
            return state
        try:
            with post_pool.connection() as client:
                posts = client.ReadPosts(state["req_id"], page_ids, {})
            state["posts"] = posts
            logger.debug("hydrate_posts req_id=%d count=%d",
                         state["req_id"], len(posts))
            print(f"[hydrate_posts] OK  count={len(posts)}")
        except Exception as exc:
            from ms_baseline.dsb_social.gen_py.social_network.ttypes import ServiceException, ErrorCode
            logger.error("hydrate_posts failed: %s", exc)
            raise ServiceException(
                errorCode=ErrorCode.SE_THRIFT_HANDLER_ERROR,
                message=f"PostStorageService.ReadPosts failed: {exc}",
            )
        return state
    return hydrate_posts


def build_read_timeline_agent(redis_client, mongo_col, post_pool: ThriftClientPool) -> any:
    graph = StateGraph(ReadTimelineState)
    graph.add_node("fetch_post_ids",     make_fetch_post_ids_node(redis_client, mongo_col))
    graph.add_node("reason_paginate",    make_reason_paginate_node())
    graph.add_node("validate_paginate",  make_validate_paginate_node())
    graph.add_node("hydrate_posts",      make_hydrate_posts_node(post_pool))

    graph.set_entry_point("fetch_post_ids")
    graph.add_edge("fetch_post_ids",    "reason_paginate")
    graph.add_edge("reason_paginate",   "validate_paginate")
    graph.add_edge("validate_paginate", "hydrate_posts")
    graph.add_edge("hydrate_posts",     END)
    return graph.compile()
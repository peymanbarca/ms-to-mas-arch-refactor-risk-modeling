"""
HOME TIMELINE AGENT - Graph Topologies

━━━ Graph 1: write_agent  (WriteHomeTimeline) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    START
      |
      v
  [fetch_followers]         Deterministic — call SocialGraphService.GetFollowers
      |                     to get the author's follower list.
      v
  [reason_fanout_targets]   LLM — given followers list + user_mentions_id list,
      |                     build the deduplicated target set:
      |                     - Union of followers + mentioned users
      |                     - Exclude the author themselves (user_id)
      |                     - No duplicates
      |                     Returns: { targets: [i64...], excluded_author: bool }
      v
  [validate_targets]        Deterministic guard — compute reference target set:
      |                     set(followers) | set(mentions) - {user_id}
      |                     LLM result accepted only if it matches reference exactly.
      |                     If not: fall back to reference set.
      v
  [apply_fanout]            Deterministic — Redis pipeline ZADD post_id
      |                     (score=timestamp) into every target's sorted set.
      |                     Identical to original handler._redis_fanout.
      v
     END

━━━ Graph 2: read_agent  (ReadHomeTimeline) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    START
      |
      v
  [fetch_post_ids]          Deterministic — Redis ZREVRANGE home-timeline:<user_id>
      |                     0 -1 (all entries) to get the complete feed.
      |                     Home timeline has NO MongoDB fallback — Redis only.
      v
  [reason_paginate]         LLM — given full list of (post_id, timestamp) pairs
      |                     and the requested [start, stop) window, select the
      |                     correct reverse-chronological page.
      |                     Returns: { post_ids: [i64...] }
      v
  [validate_paginate]       Deterministic guard — reference slice = all_ids[start:stop].
      |                     LLM result accepted only if it exactly matches reference.
      |                     If not: fall back to reference slice.
      v
  [hydrate_posts]           Deterministic — PostStorageService.ReadPosts(post_ids).
      |
      v
     END  →  list[Post]

Key Design Decisions
────────────────────
- Thrift interface (HomeTimelineService.Iface) UNCHANGED.
- Redis sorted-set layout UNCHANGED (no MongoDB — home timeline is Redis-only).
- LLM reasoning replaces:
    Write: "Given followers + mentions, which users should get this post?"
           (the set union + deduplication + author exclusion logic)
    Read:  "Given the full timeline list, which posts belong in [start, stop)?"
           (the pagination/ordering logic)
- validate_targets: LLM cannot exclude followers who should receive posts,
  and cannot include the author in their own home timeline.
- validate_paginate: data integrity wins — always use the DB-ordered slice.
- Token metrics tracked per request.
"""

import json
import logging
import re
import asyncio
import time
from typing import TypedDict, Optional, List, Dict, Any

from langgraph.graph import StateGraph, END
from langchain_ollama import ChatOllama

import redis as redis_lib

from thrift_pool import ThriftClientPool

logger = logging.getLogger("home-timeline-agent")

# ── LLM ─────────────────────────────────────────────────────────────────────
llm = ChatOllama(model="llama3", temperature=0.0, reasoning=False)

# ── Redis key prefix ─────────────────────────────────────────────────────────
_REDIS_KEY_PREFIX = "home-timeline:"   # home-timeline:<user_id>


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
# GRAPH 1 — WriteHomeTimeline
# ══════════════════════════════════════════════════════════════════════════════

class WriteHomeTimelineState(TypedDict):
    req_id:           int
    post_id:          int
    user_id:          int        # author
    timestamp:        int
    user_mentions_id: List[int]  # mentioned user_ids

    # After fetch_followers
    followers: List[int]

    # LLM output
    llm_targets:         Optional[List[int]]
    llm_excluded_author: Optional[bool]

    # Final validated target set
    final_targets: List[int]

    # Metrics
    total_input_tokens:  int
    total_output_tokens: int
    total_llm_calls:     int
    fallback_used:       bool


def make_fetch_followers_node(social_graph_pool: ThriftClientPool):
    """Deterministic: call SocialGraphService.GetFollowers."""
    async def fetch_followers(state: WriteHomeTimelineState) -> WriteHomeTimelineState:
        req_id  = state["req_id"]
        user_id = state["user_id"]
        try:
            with social_graph_pool.connection() as client:
                followers = client.GetFollowers(req_id, user_id, {})
            state["followers"] = [int(f) for f in followers]
            logger.info(
                "fetch_followers req_id=%d user_id=%d followers=%d",
                req_id, user_id, len(state["followers"]),
            )
            print(
                f"[fetch_followers] user_id={user_id} "
                f"followers={len(state['followers'])}"
            )
        except Exception as exc:
            from ms_baseline.dsb_social.gen_py.social_network.ttypes import ServiceException, ErrorCode
            logger.error("fetch_followers failed: %s", exc)
            raise ServiceException(
                errorCode=ErrorCode.SE_THRIFT_HANDLER_ERROR,
                message=f"GetFollowers failed: {exc}",
            )
        return state
    return fetch_followers


def make_reason_fanout_targets_node():
    """
    LLM: build the deduplicated fan-out target set from followers + mentions,
    excluding the author themselves.
    """
    async def reason_fanout_targets(
        state: WriteHomeTimelineState,
    ) -> WriteHomeTimelineState:
        author_id = state["user_id"]
        followers = state["followers"]
        mentions  = state["user_mentions_id"]

        prompt = f"""
You are a home timeline fan-out agent for a social network.

Your task is to determine which users should receive a new post in their home timeline.

Post details:
  post_id          = {state["post_id"]}
  author_user_id   = {author_id}  ← this user should NOT appear in the target set
  timestamp        = {state["timestamp"]}

Input user sets:
  followers        = {followers}
     (users who follow the author — they should all receive the post)
  user_mentions_id = {mentions}
     (users mentioned in the post — they should also receive it)

Fan-out rules:
  1. Start with the UNION of followers and user_mentions_id
  2. Remove the author (user_id={author_id}) from the set if present
     (authors do not receive their own posts in their home timeline)
  3. Remove any duplicate user_ids (each user gets the post at most once)
  4. All remaining user_ids are the final target set

Return ONLY valid JSON — no explanation, no code, no markdown.

Schema:
{{
  "targets":         [<integer>, ...],
  "excluded_author": true | false
}}
"""

        logger.info(
            "LLM reason_fanout_targets req_id=%d followers=%d mentions=%d",
            state["req_id"], len(followers), len(mentions),
        )
        response = await asyncio.to_thread(llm.invoke, prompt)
        raw      = response.text()
        in_tok   = response.usage_metadata.get("input_tokens",  0)
        out_tok  = response.usage_metadata.get("output_tokens", 0)

        logger.info("LLM raw=%r  in=%d out=%d", raw[:200], in_tok, out_tok)
        print(f"[reason_fanout_targets] raw={raw[:120]!r}  in={in_tok} out={out_tok}")

        parsed = _parse_json(raw)

        llm_targets         = None
        llm_excluded_author = None

        if parsed:
            t = parsed.get("targets")
            e = parsed.get("excluded_author")
            if isinstance(t, list) and all(isinstance(x, int) for x in t):
                llm_targets = t
            if isinstance(e, bool):
                llm_excluded_author = e

        state["llm_targets"]          = llm_targets
        state["llm_excluded_author"]  = llm_excluded_author
        state["total_input_tokens"]  += in_tok
        state["total_output_tokens"] += out_tok
        state["total_llm_calls"]     += 1
        return state
    return reason_fanout_targets


def make_validate_targets_node():
    """
    Deterministic guard: compute reference target set and compare with LLM.

    Reference: set(followers) | set(mentions) - {author_user_id}

    LLM result accepted only if it matches the reference set exactly
    (same elements, order doesn't matter).
    If not: fall back to reference.

    Safety invariant: the author is NEVER in the target set.
    """
    async def validate_targets(
        state: WriteHomeTimelineState,
    ) -> WriteHomeTimelineState:
        author_id = state["user_id"]
        followers = set(state["followers"])
        mentions  = set(state["user_mentions_id"])

        # Deterministic reference
        ref_targets = (followers | mentions) - {author_id}
        ref_list    = sorted(ref_targets)   # canonical sorted form for comparison

        llm_targets = state.get("llm_targets")

        if llm_targets is not None and set(llm_targets) == ref_targets:
            state["final_targets"] = llm_targets
            state["fallback_used"] = False
            logger.info(
                "validate_targets PASS req_id=%d count=%d",
                state["req_id"], len(llm_targets),
            )
            print(f"[validate_targets] PASS  count={len(llm_targets)}")
        else:
            state["final_targets"] = ref_list
            state["fallback_used"] = True
            logger.warning(
                "validate_targets FALLBACK req_id=%d "
                "llm_count=%s ref_count=%d",
                state["req_id"],
                len(llm_targets) if llm_targets is not None else "N/A",
                len(ref_list),
            )
            print(
                f"[validate_targets] FALLBACK  "
                f"llm={llm_targets!r} -> ref_count={len(ref_list)}"
            )

        # Safety: enforce author exclusion
        if author_id in state["final_targets"]:
            state["final_targets"] = [
                t for t in state["final_targets"] if t != author_id
            ]
            logger.warning(
                "validate_targets: removed author %d from targets", author_id
            )

        return state
    return validate_targets


def make_apply_fanout_node(redis_client: redis_lib.Redis):
    """
    Deterministic: pipeline ZADD post_id (score=timestamp) into every
    target's home-timeline sorted set.
    Identical to original handler._redis_fanout.
    """
    async def apply_fanout(
        state: WriteHomeTimelineState,
    ) -> WriteHomeTimelineState:
        targets = state["final_targets"]
        post_id = state["post_id"]
        ts      = state["timestamp"]

        if not targets:
            logger.info(
                "apply_fanout SKIPPED req_id=%d (no targets)", state["req_id"]
            )
            print(f"[apply_fanout] SKIPPED  (no targets)")
            return state

        try:
            pipe = redis_client.pipeline(transaction=False)
            for target_id in targets:
                key = _redis_key(target_id)
                pipe.zadd(key, {str(post_id): ts})
            pipe.execute()
            logger.info(
                "apply_fanout OK req_id=%d post_id=%d ts=%d targets=%d",
                state["req_id"], post_id, ts, len(targets),
            )
            print(
                f"[apply_fanout] OK  post_id={post_id} "
                f"ts={ts} targets={len(targets)}"
            )
        except redis_lib.RedisError as exc:
            from ms_baseline.dsb_social.gen_py.social_network.ttypes import ServiceException, ErrorCode
            logger.error("apply_fanout Redis pipeline failed: %s", exc)
            raise ServiceException(
                errorCode=ErrorCode.SE_REDIS_ERROR,
                message=f"Redis fanout failed: {exc}",
            )

        return state
    return apply_fanout


def build_write_agent(
    redis_client: redis_lib.Redis,
    social_graph_pool: ThriftClientPool,
) -> any:
    graph = StateGraph(WriteHomeTimelineState)

    graph.add_node("fetch_followers",          make_fetch_followers_node(social_graph_pool))
    graph.add_node("reason_fanout_targets",    make_reason_fanout_targets_node())
    graph.add_node("validate_targets",         make_validate_targets_node())
    graph.add_node("apply_fanout",             make_apply_fanout_node(redis_client))

    graph.set_entry_point("fetch_followers")
    graph.add_edge("fetch_followers",       "reason_fanout_targets")
    graph.add_edge("reason_fanout_targets", "validate_targets")
    graph.add_edge("validate_targets",      "apply_fanout")
    graph.add_edge("apply_fanout",          END)
    return graph.compile()


# ══════════════════════════════════════════════════════════════════════════════
# GRAPH 2 — ReadHomeTimeline
# ══════════════════════════════════════════════════════════════════════════════

class ReadHomeTimelineState(TypedDict):
    req_id:  int
    user_id: int
    start:   int     # inclusive
    stop:    int     # exclusive

    # After fetch_post_ids
    all_post_ids:   List[int]    # all post_ids, newest first
    all_timestamps: List[int]    # corresponding timestamps

    # LLM output
    llm_page_ids: Optional[List[int]]

    # Final validated page
    final_page_ids: List[int]

    # Hydrated posts
    posts: List[Any]

    # Metrics
    total_input_tokens:  int
    total_output_tokens: int
    total_llm_calls:     int
    fallback_used:       bool


def make_fetch_post_ids_node(redis_client: redis_lib.Redis):
    """
    Deterministic: Redis ZREVRANGE home-timeline:<user_id> (all entries).
    Home timeline has NO MongoDB fallback — purely Redis-based.
    Returns empty list if key doesn't exist (cold start).
    """
    async def fetch_post_ids(state: ReadHomeTimelineState) -> ReadHomeTimelineState:
        uid = state["user_id"]
        key = _redis_key(uid)

        all_ids = []
        all_ts  = []

        try:
            pairs = redis_client.zrevrange(key, 0, -1, withscores=True)
            all_ids = [int(m) for m, _ in pairs]
            all_ts  = [int(s) for _, s in pairs]
            logger.debug(
                "fetch_post_ids uid=%d count=%d", uid, len(all_ids)
            )
            print(f"[fetch_post_ids] uid={uid} count={len(all_ids)}")
        except redis_lib.RedisError as exc:
            logger.warning("fetch_post_ids Redis ZREVRANGE failed: %s", exc)
            # Return empty — home timeline has no fallback

        state["all_post_ids"]   = all_ids
        state["all_timestamps"] = all_ts
        return state
    return fetch_post_ids


def make_reason_paginate_node():
    """
    LLM: given the full list of (post_id, timestamp) entries and the
    requested [start, stop) window, select the correct reverse-chron page.
    """
    async def reason_paginate(
        state: ReadHomeTimelineState,
    ) -> ReadHomeTimelineState:
        all_ids = state["all_post_ids"]
        all_ts  = state["all_timestamps"]
        start   = state["start"]
        stop    = state["stop"]
        n       = stop - start

        if not all_ids:
            state["llm_page_ids"] = []
            return state

        # Build entries list for LLM (cap at 100 to keep prompt size bounded)
        entries = [
            {"index": i, "post_id": pid, "timestamp": ts}
            for i, (pid, ts) in enumerate(zip(all_ids, all_ts))
        ][:100]
        entries_str = json.dumps(entries, indent=2)

        prompt = f"""
You are a home timeline pagination agent for a social network.

Your task is to select the correct posts for a requested page of a user's home timeline feed.

The timeline is ordered NEWEST FIRST (reverse-chronological — index 0 = most recent).
The list below is already sorted newest-first.

All timeline entries (index = reverse-chronological position):
{entries_str}

Requested page:
  start = {start}  (0-based inclusive start index)
  stop  = {stop}   (exclusive stop index)
  → Select entries at indices {start} to {stop - 1} (up to {n} items)
  → If fewer than {stop} entries exist, return what's available from index {start} onward

Return the post_ids for the requested page in newest-first order.
Return ONLY valid JSON — no explanation, no code, no markdown.

Schema:
{{
  "post_ids": [<integer>, ...]
}}
"""

        logger.info(
            "LLM reason_paginate req_id=%d uid=%d start=%d stop=%d total=%d",
            state["req_id"], state["user_id"], start, stop, len(all_ids),
        )
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
    Deterministic guard: reference page = all_post_ids[start:stop].
    LLM result accepted only if it exactly matches the reference.
    Data integrity always wins.
    """
    async def validate_paginate(
        state: ReadHomeTimelineState,
    ) -> ReadHomeTimelineState:
        all_ids  = state["all_post_ids"]
        start    = state["start"]
        stop     = state["stop"]
        ref_page = all_ids[start:stop]   # deterministic reference

        llm_page = state.get("llm_page_ids")

        if llm_page is not None and llm_page == ref_page:
            state["final_page_ids"] = llm_page
            state["fallback_used"]  = False
            logger.info(
                "validate_paginate PASS req_id=%d count=%d",
                state["req_id"], len(llm_page),
            )
            print(f"[validate_paginate] PASS  count={len(llm_page)}")
        else:
            state["final_page_ids"] = ref_page
            state["fallback_used"]  = True
            logger.warning(
                "validate_paginate FALLBACK req_id=%d "
                "llm_count=%s ref_count=%d (using Redis slice)",
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
    """Deterministic: PostStorageService.ReadPosts for the final page."""
    async def hydrate_posts(
        state: ReadHomeTimelineState,
    ) -> ReadHomeTimelineState:
        page_ids = state["final_page_ids"]
        if not page_ids:
            state["posts"] = []
            return state

        try:
            with post_pool.connection() as client:
                posts = client.ReadPosts(state["req_id"], page_ids, {})
            state["posts"] = posts
            logger.debug(
                "hydrate_posts req_id=%d count=%d", state["req_id"], len(posts)
            )
            print(f"[hydrate_posts] OK  count={len(posts)}")
        except Exception as exc:
            from ms_baseline.dsb_social.gen_py.social_network.ttypes import ServiceException, ErrorCode
            logger.error("hydrate_posts PostStorageService failed: %s", exc)
            raise ServiceException(
                errorCode=ErrorCode.SE_THRIFT_HANDLER_ERROR,
                message=f"PostStorageService.ReadPosts failed: {exc}",
            )
        return state
    return hydrate_posts


def build_read_agent(
    redis_client: redis_lib.Redis,
    post_pool: ThriftClientPool,
) -> any:
    graph = StateGraph(ReadHomeTimelineState)

    graph.add_node("fetch_post_ids",    make_fetch_post_ids_node(redis_client))
    graph.add_node("reason_paginate",   make_reason_paginate_node())
    graph.add_node("validate_paginate", make_validate_paginate_node())
    graph.add_node("hydrate_posts",     make_hydrate_posts_node(post_pool))

    graph.set_entry_point("fetch_post_ids")
    graph.add_edge("fetch_post_ids",    "reason_paginate")
    graph.add_edge("reason_paginate",   "validate_paginate")
    graph.add_edge("validate_paginate", "hydrate_posts")
    graph.add_edge("hydrate_posts",     END)
    return graph.compile()
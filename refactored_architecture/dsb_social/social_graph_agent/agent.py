"""
SOCIAL GRAPH AGENT - Graph Topologies

━━━ Graph 1: follow_graph  (Follow / Unfollow) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    START
      |
      v
  [fetch_current_graph]     Deterministic — fetch current followers + followees
      |                     for both user_id and followee_id from Redis/MongoDB.
      v
  [reason_relationship]     LLM — given current graph state, validate the
      |                     requested relationship mutation:
      |                     - No self-follow (user_id == followee_id)
      |                     - For Follow: not already following
      |                     - For Unfollow: relationship actually exists
      |                     - Both users exist (have entries in the graph)
      |                     Returns: { approved: bool, reason: str }
      v
  [validate_decision]       Deterministic guard — if LLM returns approved=False
      |                     for a valid operation, override with approval.
      |                     If LLM returns approved=True for self-follow, reject.
      |                     Self-follow is ALWAYS rejected deterministically.
      v
  [apply_mutation]          Deterministic — Redis pipeline ZADD/ZREM +
      |                     MongoDB $addToSet/$pull (identical to original).
      v
     END

━━━ Graph 2: get_graph  (GetFollowers / GetFollowees) ━━━━━━━━━━━━━━━━━━━━━━━

    START
      |
      v
  [fetch_ids]               Deterministic — Redis ZRANGE (cache first),
      |                     MongoDB fallback. Seeds Redis if needed.
      v
  [reason_verify_list]      LLM — given the raw list of user_ids and the
      |                     user_id context, confirm the list is a valid
      |                     social graph result (all positive integers, no
      |                     duplicates, no self-reference).
      |                     Returns: { valid: bool, cleaned_ids: [i64...] }
      v
  [validate_list]           Deterministic guard — if LLM cleaned_ids differs
      |                     from original, log discrepancy but use original
      |                     (trust the DB, not the LLM, for data integrity).
      v
     END  →  list[i64]

━━━ Graph 3: resolve_follow_graph  (FollowWithUsername / UnfollowWithUsername) ━

    START
      |
      v
  [resolve_usernames]       Deterministic — parallel UserService.GetUserId × 2.
      |
      v
  [reason_relationship]     LLM — same as follow_graph (reused node factory).
      v
  [validate_decision]       Deterministic guard — same as follow_graph.
      v
  [apply_mutation]          Deterministic — same as follow_graph.
      v
     END

Key Design Decisions
────────────────────
- Thrift interface (SocialGraphService.Iface) UNCHANGED.
- MongoDB schema, Redis sorted-set layout UNCHANGED.
- InsertUser has NO LLM node — pure $setOnInsert upsert, no reasoning needed.
- The LLM reasons about relationship validity (Follow/Unfollow) and list
  coherence (GetFollowers/GetFollowees).
- validate_decision always overrides the LLM on self-follow — safety invariant.
- validate_list always uses the DB result, never the LLM's rewritten list.
- Token metrics tracked per request.
"""

import json
import logging
import re
import asyncio
import concurrent.futures
import time
from typing import TypedDict, Optional, List, Dict, Any, Literal

from langgraph.graph import StateGraph, END
from langchain_ollama import ChatOllama

import redis as redis_lib
from pymongo import MongoClient

logger = logging.getLogger("social-graph-agent")

# ── LLM ─────────────────────────────────────────────────────────────────────
llm = ChatOllama(model="llama3.2:3b", temperature=0.0, reasoning=False)

# ── Redis key prefixes (identical to original handler) ───────────────────────
_KEY_FOLLOWERS = "followers:"
_KEY_FOLLOWEES = "followees:"


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_json(text: str) -> Optional[dict]:
    try:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            return json.loads(m.group(0))
    except Exception as exc:
        logger.warning("JSON parse error: %s — raw: %r", exc, text[:200])
    return None


def _redis_zrange(redis_client, key: str) -> Optional[List[int]]:
    """ZRANGE key 0 -1 → list[int] or None on key-not-exist / error."""
    try:
        if not redis_client.exists(key):
            return None
        members = redis_client.zrange(key, 0, -1)
        return [int(m) for m in members]
    except redis_lib.RedisError as exc:
        logger.warning("Redis ZRANGE key=%s failed: %s", key, exc)
        return None


def _mongo_get_ids(mongo_col, user_id: int, field: str) -> List[int]:
    """Return list of i64 from MongoDB document field ('followers' or 'followees')."""
    try:
        doc = mongo_col.find_one({"user_id": user_id}, {field: 1, "_id": 0})
        return [int(x) for x in (doc or {}).get(field, [])]
    except Exception as exc:
        logger.error("MongoDB find user_id=%d field=%s: %s", user_id, field, exc)
        return []


# ══════════════════════════════════════════════════════════════════════════════
# GRAPH 1 + 3 shared: FollowState
# ══════════════════════════════════════════════════════════════════════════════

class FollowAgentState(TypedDict):
    req_id:       int
    user_id:      int
    followee_id:  int
    operation:    Literal["follow", "unfollow"]   # "follow" | "unfollow"

    # Username resolution (used by resolve_follow_graph only)
    user_username:     Optional[str]
    followee_username: Optional[str]

    # After fetch_current_graph
    user_followees:    List[int]   # who user_id follows
    followee_followers: List[int]  # who follows followee_id

    # LLM output
    llm_approved: Optional[bool]
    llm_reason:   Optional[str]

    # Final decision
    approved:     Optional[bool]

    # Metrics
    total_input_tokens:  int
    total_output_tokens: int
    total_llm_calls:     int
    fallback_used:       bool


def make_fetch_current_graph_node(redis_client, mongo_col):
    """Deterministic: load current followees of user_id and followers of followee_id."""
    async def fetch_current_graph(state: FollowAgentState) -> FollowAgentState:
        uid = state["user_id"]
        fid = state["followee_id"]

        # Get user's current followees (to check if already following)
        followees = _redis_zrange(redis_client, _KEY_FOLLOWEES + str(uid))
        if followees is None:
            followees = _mongo_get_ids(mongo_col, uid, "followees")

        # Get followee's current followers (for context)
        followers = _redis_zrange(redis_client, _KEY_FOLLOWERS + str(fid))
        if followers is None:
            followers = _mongo_get_ids(mongo_col, fid, "followers")

        state["user_followees"]     = followees
        state["followee_followers"] = followers

        logger.debug(
            "fetch_current_graph req_id=%d user=%d followees=%d followee=%d followers=%d",
            state["req_id"], uid, len(followees), fid, len(followers),
        )
        print(
            f"[fetch_current_graph] user={uid} followees={len(followees)} "
            f"followee={fid} followers={len(followers)}"
        )
        return state
    return fetch_current_graph


def make_reason_relationship_node():
    """LLM: validate whether the follow/unfollow operation should be approved."""
    async def reason_relationship(state: FollowAgentState) -> FollowAgentState:
        op          = state["operation"]
        uid         = state["user_id"]
        fid         = state["followee_id"]
        followees   = state["user_followees"]
        already_fol = fid in followees

        prompt = f"""
You are a social graph validation agent for a social network.

Your task is to decide whether a {op.upper()} operation should be approved.

Operation: {op.upper()}
  user_id    = {uid}   (the user performing the action)
  followee_id = {fid}  (the target user)

Current graph state:
  user_id currently follows these IDs: {followees}
  followee_id is already followed by user_id: {already_fol}

Validation rules:
  1. Self-{op} is NEVER allowed (user_id must NOT equal followee_id)
  2. For FOLLOW:   approve only if user_id is NOT already following followee_id
  3. For UNFOLLOW: approve only if user_id IS currently following followee_id
  4. Both user_id and followee_id must be positive integers

Return ONLY valid JSON — no explanation, no code, no markdown.

Schema:
{{
  "approved": true | false,
  "reason":   "<short explanation>"
}}
"""

        logger.info("LLM reason_relationship req_id=%d op=%s", state["req_id"], op)
        response = await asyncio.to_thread(llm.invoke, prompt)
        raw      = response.text()
        in_tok   = response.usage_metadata.get("input_tokens",  0)
        out_tok  = response.usage_metadata.get("output_tokens", 0)

        logger.info("LLM raw=%r  in=%d out=%d", raw[:150], in_tok, out_tok)
        print(f"[reason_relationship] op={op} raw={raw[:100]!r} in={in_tok} out={out_tok}")

        parsed   = _parse_json(raw)
        approved = parsed.get("approved") if parsed else None
        reason   = parsed.get("reason",  "") if parsed else ""

        state["llm_approved"]        = approved if isinstance(approved, bool) else None
        state["llm_reason"]          = reason
        state["total_input_tokens"]  += in_tok
        state["total_output_tokens"] += out_tok
        state["total_llm_calls"]     += 1
        return state
    return reason_relationship


def make_validate_decision_node():
    """
    Deterministic guard for follow/unfollow decisions.

    Safety invariants (always enforced regardless of LLM):
      - Self-follow is ALWAYS rejected.
      - If LLM returns None (parse failure), fall back to deterministic logic.
      - LLM approval is used only when it matches deterministic expectation.
    """
    async def validate_decision(state: FollowAgentState) -> FollowAgentState:
        uid  = state["user_id"]
        fid  = state["followee_id"]
        op   = state["operation"]
        fol  = state["user_followees"]

        # ── Deterministic reference decision ──
        if uid == fid:
            det_approved = False   # never self-follow
        elif op == "follow":
            det_approved = fid not in fol   # approve only if not already following
        else:  # unfollow
            det_approved = fid in fol       # approve only if currently following

        llm_approved = state.get("llm_approved")

        if llm_approved is not None and llm_approved == det_approved:
            state["approved"]      = llm_approved
            state["fallback_used"] = False
            logger.info(
                "validate_decision PASS req_id=%d op=%s approved=%s",
                state["req_id"], op, llm_approved,
            )
            print(f"[validate_decision] PASS  op={op} approved={llm_approved}")
        else:
            state["approved"]      = det_approved
            state["fallback_used"] = True
            logger.warning(
                "validate_decision FALLBACK req_id=%d op=%s llm=%s -> det=%s reason=%r",
                state["req_id"], op, llm_approved, det_approved, state.get("llm_reason"),
            )
            print(
                f"[validate_decision] FALLBACK  op={op} "
                f"llm={llm_approved} -> det={det_approved}"
            )
        return state
    return validate_decision


def make_apply_mutation_node(redis_client, mongo_col):
    """
    Deterministic: apply the approved follow/unfollow to Redis + MongoDB.
    If not approved, skip silently (idempotent).
    Identical to original handler._redis_follow/_mongo_follow etc.
    """
    async def apply_mutation(state: FollowAgentState) -> FollowAgentState:
        if not state.get("approved"):
            logger.info(
                "apply_mutation SKIPPED (not approved) req_id=%d op=%s",
                state["req_id"], state["operation"],
            )
            print(f"[apply_mutation] SKIPPED  op={state['operation']} (not approved)")
            return state

        uid = state["user_id"]
        fid = state["followee_id"]
        op  = state["operation"]
        ts  = int(time.time() * 1000)

        # ── Redis ──
        try:
            pipe = redis_client.pipeline(transaction=False)
            if op == "follow":
                pipe.zadd(_KEY_FOLLOWEES + str(uid), {str(fid): ts})
                pipe.zadd(_KEY_FOLLOWERS + str(fid), {str(uid): ts})
            else:
                pipe.zrem(_KEY_FOLLOWEES + str(uid), str(fid))
                pipe.zrem(_KEY_FOLLOWERS + str(fid), str(uid))
            pipe.execute()
            logger.debug("apply_mutation Redis OK op=%s", op)
        except redis_lib.RedisError as exc:
            logger.warning("apply_mutation Redis failed op=%s: %s", op, exc)
            # Non-fatal — MongoDB still persists

        # ── MongoDB ──
        try:
            if op == "follow":
                mongo_col.update_one(
                    {"user_id": uid},
                    {"$addToSet": {"followees": fid}},
                    upsert=True,
                )
                mongo_col.update_one(
                    {"user_id": fid},
                    {"$addToSet": {"followers": uid}},
                    upsert=True,
                )
            else:
                mongo_col.update_one(
                    {"user_id": uid}, {"$pull": {"followees": fid}}
                )
                mongo_col.update_one(
                    {"user_id": fid}, {"$pull": {"followers": uid}}
                )
            logger.debug("apply_mutation MongoDB OK op=%s", op)
        except Exception as exc:
            from ms_baseline.dsb_social.gen_py.social_network.ttypes import ServiceException, ErrorCode
            logger.error("apply_mutation MongoDB failed op=%s: %s", op, exc)
            raise ServiceException(
                errorCode=ErrorCode.SE_MONGODB_ERROR,
                message=f"MongoDB write failed: {exc}",
            )

        print(f"[apply_mutation] OK  op={op} user={uid} followee={fid}")
        return state
    return apply_mutation


# ── Follow / Unfollow graph builder ─────────────────────────────────────────

def build_follow_agent(redis_client, mongo_col) -> any:
    graph = StateGraph(FollowAgentState)
    graph.add_node("fetch_current_graph", make_fetch_current_graph_node(redis_client, mongo_col))
    graph.add_node("reason_relationship", make_reason_relationship_node())
    graph.add_node("validate_decision",   make_validate_decision_node())
    graph.add_node("apply_mutation",      make_apply_mutation_node(redis_client, mongo_col))

    graph.set_entry_point("fetch_current_graph")
    graph.add_edge("fetch_current_graph", "reason_relationship")
    graph.add_edge("reason_relationship", "validate_decision")
    graph.add_edge("validate_decision",   "apply_mutation")
    graph.add_edge("apply_mutation",      END)
    return graph.compile()


# ══════════════════════════════════════════════════════════════════════════════
# GRAPH 2: GetFollowers / GetFollowees
# ══════════════════════════════════════════════════════════════════════════════

class GetGraphAgentState(TypedDict):
    req_id:    int
    user_id:   int
    direction: Literal["followers", "followees"]  # which list to fetch

    # After fetch_ids
    raw_ids:   List[int]   # from Redis or MongoDB

    # LLM output
    llm_valid:       Optional[bool]
    llm_cleaned_ids: Optional[List[int]]

    # Final result
    final_ids:  List[int]

    # Metrics
    total_input_tokens:  int
    total_output_tokens: int
    total_llm_calls:     int
    fallback_used:       bool


def make_fetch_ids_node(redis_client, mongo_col):
    """Deterministic: Redis ZRANGE → MongoDB fallback → seed Redis."""
    async def fetch_ids(state: GetGraphAgentState) -> GetGraphAgentState:
        uid       = state["user_id"]
        direction = state["direction"]
        redis_key = (_KEY_FOLLOWERS if direction == "followers"
                     else _KEY_FOLLOWEES) + str(uid)

        cached = _redis_zrange(redis_client, redis_key)
        if cached is not None:
            state["raw_ids"] = cached
            logger.debug("fetch_ids Redis HIT uid=%d dir=%s count=%d",
                         uid, direction, len(cached))
            print(f"[fetch_ids] Redis HIT uid={uid} dir={direction} count={len(cached)}")
        else:
            ids = _mongo_get_ids(mongo_col, uid, direction)
            state["raw_ids"] = ids
            # Seed Redis
            if ids:
                try:
                    mapping = {str(i): 0 for i in ids}
                    redis_client.zadd(redis_key, mapping)
                except redis_lib.RedisError:
                    pass
            logger.debug("fetch_ids MongoDB uid=%d dir=%s count=%d",
                         uid, direction, len(ids))
            print(f"[fetch_ids] MongoDB uid={uid} dir={direction} count={len(ids)}")
        return state
    return fetch_ids


def make_reason_verify_list_node():
    """LLM: verify the list of user_ids is coherent."""
    async def reason_verify_list(state: GetGraphAgentState) -> GetGraphAgentState:
        uid       = state["user_id"]
        direction = state["direction"]
        raw_ids   = state["raw_ids"]

        if not raw_ids:
            # Empty list — trivially valid, skip LLM call
            state["llm_valid"]       = True
            state["llm_cleaned_ids"] = []
            return state

        prompt = f"""
You are a social graph verification agent for a social network.

Your task is to verify that a list of user IDs representing social connections is valid.

Context:
  user_id   = {uid}
  direction = {direction}  ({"users who follow this user" if direction == "followers" else "users this user follows"})
  raw_ids   = {raw_ids}

Validation rules:
  1. All IDs must be positive integers (> 0)
  2. No duplicates in the list
  3. The user themselves (user_id={uid}) must NOT appear in the list (no self-reference)
  4. If the list passes all checks: valid=true, return the same list as cleaned_ids
  5. If there are issues: valid=false, return cleaned_ids with invalid entries removed

Return ONLY valid JSON — no explanation, no code, no markdown.

Schema:
{{
  "valid":       true | false,
  "cleaned_ids": [<integer>, ...]
}}
"""

        logger.info("LLM reason_verify_list req_id=%d uid=%d dir=%s count=%d",
                    state["req_id"], uid, direction, len(raw_ids))
        response = await asyncio.to_thread(llm.invoke, prompt)
        raw      = response.text()
        in_tok   = response.usage_metadata.get("input_tokens",  0)
        out_tok  = response.usage_metadata.get("output_tokens", 0)

        logger.info("LLM raw=%r  in=%d out=%d", raw[:150], in_tok, out_tok)
        print(f"[reason_verify_list] raw={raw[:100]!r}  in={in_tok} out={out_tok}")

        parsed      = _parse_json(raw)
        llm_valid   = None
        llm_cleaned = None

        if parsed:
            v = parsed.get("valid")
            c = parsed.get("cleaned_ids")
            if isinstance(v, bool):
                llm_valid = v
            if isinstance(c, list) and all(isinstance(x, int) for x in c):
                llm_cleaned = c

        state["llm_valid"]           = llm_valid
        state["llm_cleaned_ids"]     = llm_cleaned
        state["total_input_tokens"]  += in_tok
        state["total_output_tokens"] += out_tok
        state["total_llm_calls"]     += 1
        return state
    return reason_verify_list


def make_validate_list_node():
    """
    Deterministic guard: always trust the DB result.
    LLM output is logged for observability but never overwrites raw_ids.
    If LLM found issues, log them; the data integrity stays with the DB.
    """
    async def validate_list(state: GetGraphAgentState) -> GetGraphAgentState:
        raw_ids     = state["raw_ids"]
        llm_valid   = state.get("llm_valid")
        llm_cleaned = state.get("llm_cleaned_ids")

        if llm_valid is True and llm_cleaned == raw_ids:
            state["final_ids"]     = raw_ids
            state["fallback_used"] = False
            logger.info("validate_list PASS req_id=%d count=%d",
                        state["req_id"], len(raw_ids))
            print(f"[validate_list] PASS  count={len(raw_ids)}")
        else:
            # Always use DB result regardless of LLM opinion
            state["final_ids"]     = raw_ids
            state["fallback_used"] = True
            logger.info(
                "validate_list FALLBACK req_id=%d llm_valid=%s "
                "llm_count=%s db_count=%d (using DB result)",
                state["req_id"], llm_valid,
                len(llm_cleaned) if llm_cleaned is not None else "N/A",
                len(raw_ids),
            )
            print(
                f"[validate_list] FALLBACK  "
                f"llm_valid={llm_valid} llm_count="
                f"{len(llm_cleaned) if llm_cleaned is not None else 'N/A'} "
                f"-> db_count={len(raw_ids)}"
            )
        return state
    return validate_list


def build_get_graph_agent(redis_client, mongo_col) -> any:
    graph = StateGraph(GetGraphAgentState)
    graph.add_node("fetch_ids",           make_fetch_ids_node(redis_client, mongo_col))
    graph.add_node("reason_verify_list",  make_reason_verify_list_node())
    graph.add_node("validate_list",       make_validate_list_node())

    graph.set_entry_point("fetch_ids")
    graph.add_edge("fetch_ids",          "reason_verify_list")
    graph.add_edge("reason_verify_list", "validate_list")
    graph.add_edge("validate_list",      END)
    return graph.compile()


# ══════════════════════════════════════════════════════════════════════════════
# GRAPH 3: FollowWithUsername / UnfollowWithUsername
# Adds a username resolution step before the shared follow pipeline
# ══════════════════════════════════════════════════════════════════════════════

class ResolveFollowAgentState(TypedDict):
    req_id:            int
    user_username:     str
    followee_username: str
    operation:         Literal["follow", "unfollow"]

    # After resolve_usernames
    user_id:     Optional[int]
    followee_id: Optional[int]

    # Reuse FollowAgentState fields from here on
    user_followees:     List[int]
    followee_followers: List[int]
    llm_approved:       Optional[bool]
    llm_reason:         Optional[str]
    approved:           Optional[bool]

    total_input_tokens:  int
    total_output_tokens: int
    total_llm_calls:     int
    fallback_used:       bool


def make_resolve_usernames_node(user_pool):
    """
    Deterministic: resolve both usernames to user_ids in parallel
    via UserService.GetUserId (using the thrift_pool).
    """
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)

    def _get_id(username: str, req_id: int) -> int:
        with user_pool.connection() as client:
            return client.GetUserId(req_id, username, {})

    async def resolve_usernames(state: ResolveFollowAgentState) -> ResolveFollowAgentState:
        req_id = state["req_id"]
        loop   = asyncio.get_event_loop()

        user_fut = loop.run_in_executor(
            executor, _get_id, state["user_username"], req_id
        )
        fol_fut  = loop.run_in_executor(
            executor, _get_id, state["followee_username"], req_id
        )

        try:
            user_id, followee_id = await asyncio.gather(user_fut, fol_fut)
        except Exception as exc:
            from ms_baseline.dsb_social.gen_py.social_network.ttypes import ServiceException, ErrorCode
            raise ServiceException(
                errorCode=ErrorCode.SE_THRIFT_HANDLER_ERROR,
                message=f"Username resolution failed: {exc}",
            )

        state["user_id"]     = user_id
        state["followee_id"] = followee_id
        logger.info(
            "resolve_usernames req_id=%d %r->%d %r->%d",
            req_id,
            state["user_username"],    user_id,
            state["followee_username"], followee_id,
        )
        print(
            f"[resolve_usernames] "
            f"{state['user_username']}={user_id} "
            f"{state['followee_username']}={followee_id}"
        )
        return state
    return resolve_usernames


def _promote_to_follow_state(state: ResolveFollowAgentState) -> ResolveFollowAgentState:
    """Adaptor: ResolveFollowAgentState is a superset of FollowAgentState fields."""
    # user_id and followee_id are already set by resolve_usernames
    return state


def build_resolve_follow_agent(redis_client, mongo_col, user_pool) -> any:
    """Graph for FollowWithUsername / UnfollowWithUsername."""

    # Reuse the same node factories as the follow graph
    fetch_node    = make_fetch_current_graph_node(redis_client, mongo_col)
    reason_node   = make_reason_relationship_node()
    validate_node = make_validate_decision_node()
    apply_node    = make_apply_mutation_node(redis_client, mongo_col)

    # Wrap fetch/reason/validate/apply to accept ResolveFollowAgentState
    # (they only access keys that exist in both state types)

    graph = StateGraph(ResolveFollowAgentState)
    graph.add_node("resolve_usernames",   make_resolve_usernames_node(user_pool))
    graph.add_node("fetch_current_graph", fetch_node)
    graph.add_node("reason_relationship", reason_node)
    graph.add_node("validate_decision",   validate_node)
    graph.add_node("apply_mutation",      apply_node)

    graph.set_entry_point("resolve_usernames")
    graph.add_edge("resolve_usernames",   "fetch_current_graph")
    graph.add_edge("fetch_current_graph", "reason_relationship")
    graph.add_edge("reason_relationship", "validate_decision")
    graph.add_edge("validate_decision",   "apply_mutation")
    graph.add_edge("apply_mutation",      END)
    return graph.compile()
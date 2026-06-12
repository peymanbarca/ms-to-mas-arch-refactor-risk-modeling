"""
USER MENTION AGENT - ReACT-based Graph Topology with Tool Use

ComposeUserMentions graph (per username) — ReACT Pattern:
    START
      |
      v
  [check_cache]         Deterministic — Redis lookup (username → user_id)
      |
      v  (cache miss only)
  [reason_and_act_loop] ReACT Loop — LLM reasons and calls tools:
      |                   1. LLM reasons: "I need to query MongoDB for username"
      |                   2. Tool call: execute_mongodb_query(username)
      |                   3. LLM receives document, reasons: "Extract user_id=X"
      |                   4. Repeat until final answer (user_id or null)
      v
  [validate_resolved]   Deterministic guard — verify user_id is valid int;
      |                   if missing/invalid, signals not found
      v
  [persist_cache]       Deterministic — Redis cache set (username → user_id)
      |
      v
     END  →  UserMention(user_id, username)

Key Design Decisions
--------------------
- Thrift interface (UserMentionService.Iface) UNCHANGED.
- MongoDB schema, Redis key layout UNCHANGED.
- ReACT pattern: LLM reasons about steps, calls MongoDB tool, iterates until
  final answer. LLM uses tool use (function calling) to query the database.
- Tool: execute_mongodb_query(username) — queries user collection, returns
  document or null.
- Token metrics (input_tokens, output_tokens, llm_calls) accumulated per
  ComposeUserMentions call across all usernames in the batch.
- De-duplication preserved: same order as input, duplicates resolved once.
"""

import json
import logging
import re
import asyncio

from typing import TypedDict, Optional, List, Any
from langgraph.graph import StateGraph, END
from langchain_ollama import ChatOllama

import redis as redis_lib
from pymongo import MongoClient

logger = logging.getLogger("user-mention-agent")

# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------

llm = ChatOllama(model="llama3.2:3b", temperature=0.0, reasoning=False)

# ---------------------------------------------------------------------------
# Redis cache key prefix — identical to original handler.py (plain username)
# ---------------------------------------------------------------------------

_CACHE_TTL = 0  # 0 = no expiry, matching original Memcached behaviour


# ---------------------------------------------------------------------------
# Tools for ReACT loop (MongoDB query)
# ---------------------------------------------------------------------------

def make_mongodb_query_tool(mongo_col):
    """
    Create a MongoDB query tool for the ReACT loop.
    The LLM will call this tool to resolve a username to user_id.
    """
    # @tool
    def query_user_by_username(username: str) -> dict:
        """
        Query the MongoDB user collection for a document matching the given username.
        
        Args:
            username: The username to look up in the user collection.
        
        Returns:
            A dict with the user document if found (containing user_id, username, etc.),
            or None if not found. Format:
            {"user_id": <int>, "username": <str>, ...}
            or null if not found.
        """
        try:
            doc = mongo_col.find_one(
                {"username": username},
                {"_id": 0, "user_id": 1, "username": 1},
            )
            if doc is None:
                return {"found": False, "error": f"User '{username}' not found in database"}
            return {"found": True, "user_id": doc.get("user_id"), "username": doc.get("username")}
        except Exception as exc:
            return {"found": False, "error": f"Database query failed: {str(exc)}"}
    
    return query_user_by_username


# ---------------------------------------------------------------------------
# Agent State — ComposeUserMentions (single username)
# ---------------------------------------------------------------------------

class ResolveUsernameAgentState(TypedDict):
    # Inputs
    req_id:     int
    username:   str

    # Cache hit result (from check_cache node)
    cache_hit:     bool
    cached_user_id: Optional[int]  # user_id from Redis on hit

    # ReACT loop output
    user_id:       Optional[int]   # final resolved user_id

    # Metrics
    total_input_tokens:  int
    total_output_tokens: int
    total_llm_calls:     int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_json(text: str) -> Optional[dict]:
    """Extract JSON object from LLM response."""
    try:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            return json.loads(m.group(0))
    except Exception as exc:
        logger.warning("JSON parse error: %s — raw: %r", exc, text[:200])
    return None


def _extract_user_id_from_response(text: str) -> Optional[int]:
    """
    Extract user_id from LLM's final reasoning about the MongoDB query result.
    Looks for patterns like "user_id: 123" or "user_id is 123" in the response.
    """
    try:
        # Try JSON first
        parsed = _parse_json(text)
        if parsed and "user_id" in parsed:
            uid = parsed["user_id"]
            if isinstance(uid, int):
                return uid
        
        # Try regex patterns: "user_id[: ]+(\d+)" or similar
        match = re.search(r"user_id[:\s]+(\d+)", text, re.IGNORECASE)
        if match:
            return int(match.group(1))
        
        # Try extraction from common patterns
        match = re.search(r"The user_id (?:is|equals) (\d+)", text, re.IGNORECASE)
        if match:
            return int(match.group(1))
            
    except Exception as exc:
        logger.warning("Failed to extract user_id from response: %s", exc)
    
    return None


def _extract_text_from_response(response) -> str:
    """
    Safely extract text from LLM response object.
    Handles different response formats from ChatOllama.
    """
    try:
        # Try .text() method first
        if hasattr(response, 'text') and callable(response.text):
            return response.text()
    except Exception as e:
        logger.warning("response.text() failed: %s", e)
    
    try:
        # Try .content attribute
        if hasattr(response, 'content'):
            return response.content
    except Exception as e:
        logger.warning("response.content failed: %s", e)
    
    try:
        # Try string conversion
        return str(response)
    except Exception as e:
        logger.warning("str(response) failed: %s", e)
    
    return ""


def _extract_usage_metadata(response) -> tuple:
    """
    Safely extract token counts from LLM response.
    Returns (input_tokens, output_tokens) tuple.
    """
    in_tok, out_tok = 0, 0
    
    try:
        if hasattr(response, 'usage_metadata') and isinstance(response.usage_metadata, dict):
            in_tok = response.usage_metadata.get("input_tokens", 0)
            out_tok = response.usage_metadata.get("output_tokens", 0)
    except Exception as e:
        logger.warning("Failed to extract usage_metadata: %s", e)
    
    return in_tok, out_tok


# ===========================================================================
# ComposeUserMentions graph nodes
# ===========================================================================

def make_check_cache_node(redis_client: redis_lib.Redis):
    """
    Node: check_cache
    Look up username in Redis. On hit, set cache_hit=True and
    populate user_id so the rest of the graph is skipped.
    """
    async def check_cache(state: ResolveUsernameAgentState) -> ResolveUsernameAgentState:
        username = state["username"]
        try:
            val = redis_client.get(username)
            if val is not None:
                user_id = int(val)
                state["cache_hit"]       = True
                state["cached_user_id"]  = user_id
                state["user_id"]         = user_id
                logger.info(
                    "check_cache HIT req_id=%d username=%r -> user_id=%d",
                    state["req_id"], username, user_id,
                )
                print(f"[check_cache] HIT  {username} -> {user_id}")
            else:
                state["cache_hit"]       = False
                state["cached_user_id"]  = None
                logger.info("check_cache MISS req_id=%d username=%r",
                            state["req_id"], username)
                print(f"[check_cache] MISS {username}")
        except redis_lib.RedisError as exc:
            logger.warning("Redis GET failed username=%r: %s", username, exc)
            state["cache_hit"]       = False
            state["cached_user_id"]  = None
        return state
    return check_cache


def make_reason_resolve_node(mongo_col):
    """
    Node: reason_and_act_loop  (Manual ReACT pattern with tool use)
    
    The LLM reasons about what to do, we execute the MongoDB query tool,
    the LLM observes the result, and reasons again to extract the user_id.
    """
    query_tool = make_mongodb_query_tool(mongo_col)
    
    async def reason_and_act_loop(state: ResolveUsernameAgentState) -> ResolveUsernameAgentState:
        # Skip if cache hit
        if state["cache_hit"]:
            logger.info("reason_and_act_loop SKIPPED (cache hit) req_id=%d", state["req_id"])
            return state

        username = state["username"]

        # --------- STEP 1: LLM REASON & PLAN ---------
        # Ask LLM to reason about what it needs to do
        reason_prompt = f"""You are resolving a username to a user_id.

Username: {username}

REASON: What do you need to do to resolve this username to a user_id?
Think through the steps. Be concise."""

        logger.info("ReACT step 1 (REASON) req_id=%d username=%r", state["req_id"], username)
        
        try:
            # Use asyncio.to_thread to call blocking LLM
            reason_response = await asyncio.to_thread(llm.invoke, reason_prompt)
            reason_text = _extract_text_from_response(reason_response)
            in_tok_1, out_tok_1 = _extract_usage_metadata(reason_response)
            
            logger.info("ReACT reason response: %r  in=%d out=%d", reason_text[:100], in_tok_1, out_tok_1)
            print(f"[reason_and_act_loop STEP 1] reasoning={reason_text[:100]!r}")

        except Exception as exc:
            logger.error("ReACT STEP 1 FAILED req_id=%d username=%r: %s", state["req_id"], username, exc, exc_info=True)
            print(f"[reason_and_act_loop STEP 1] ERROR: {exc}")
            state["user_id"] = None
            return state

        # --------- STEP 2: ACT (Execute the tool) ---------
        logger.info("ReACT step 2 (ACT) req_id=%d calling tool for username=%r", 
                   state["req_id"], username)
        
        try:
            tool_result = query_tool(username)
            logger.info("ReACT tool result: %r", tool_result)
            print(f"[reason_and_act_loop STEP 2] tool_result={tool_result!r}")
        except Exception as exc:
            logger.error("ReACT STEP 2 FAILED req_id=%d username=%r: %s", state["req_id"], username, exc, exc_info=True)
            print(f"[reason_and_act_loop STEP 2] ERROR: {exc}")
            state["user_id"] = None
            return state

        # --------- STEP 3: OBSERVE & REASON AGAIN ---------
        # Present the tool result to the LLM and ask it to extract user_id
        observe_prompt = f"""You just queried a MongoDB user collection with username: {username}

Tool result: {json.dumps(tool_result)}

OBSERVE the result and REASON:
- If found=True, what is the user_id?
- If found=False, the user was not found.

Respond clearly with: "The user_id is <number>" or "User not found"."""

        logger.info("ReACT step 3 (OBSERVE & REASON) req_id=%d", state["req_id"])
        
        try:
            observe_response = await asyncio.to_thread(llm.invoke, observe_prompt)
            observe_text = _extract_text_from_response(observe_response)
            in_tok_2, out_tok_2 = _extract_usage_metadata(observe_response)
            
            logger.info("ReACT observe response: %r  in=%d out=%d", observe_text[:100], in_tok_2, out_tok_2)
            print(f"[reason_and_act_loop STEP 3] observe={observe_text[:100]!r}")

        except Exception as exc:
            logger.error("ReACT STEP 3 FAILED req_id=%d username=%r: %s", state["req_id"], username, exc, exc_info=True)
            print(f"[reason_and_act_loop STEP 3] ERROR: {exc}")
            state["user_id"] = None
            return state

        # --------- STEP 4: Extract user_id from final reasoning ---------
        try:
            user_id = _extract_user_id_from_response(observe_text)

            state["user_id"]                = user_id
            state["total_input_tokens"]    += in_tok_1 + in_tok_2
            state["total_output_tokens"]   += out_tok_1 + out_tok_2
            state["total_llm_calls"]       += 2  # 2 LLM calls: reason + observe
            
            if user_id is not None:
                logger.info("ReACT resolved req_id=%d username=%r -> user_id=%d",
                           state["req_id"], username, user_id)
                print(f"[reason_and_act_loop] RESOLVED {username} -> {user_id}")
            else:
                logger.warning("ReACT failed to extract user_id req_id=%d username=%r response=%r",
                              state["req_id"], username, observe_text)
                print(f"[reason_and_act_loop] FAILED to extract user_id for {username}")

        except Exception as exc:
            logger.error("ReACT STEP 4 FAILED req_id=%d username=%r: %s", state["req_id"], username, exc, exc_info=True)
            print(f"[reason_and_act_loop STEP 4] ERROR: {exc}")
            state["user_id"] = None

        return state
    
    return reason_and_act_loop


def make_validate_resolved_node():
    """
    Node: validate_resolved  (deterministic guard)
    Verify the ReACT agent extracted a valid user_id (not None).
    If validation fails, the user was not found.
    """
    async def validate_resolved(state: ResolveUsernameAgentState) -> ResolveUsernameAgentState:
        if state["cache_hit"]:
            return state

        user_id = state.get("user_id")

        if user_id is not None and isinstance(user_id, int):
            logger.info(
                "validate_resolved PASS req_id=%d username=%r user_id=%d",
                state["req_id"], state["username"], user_id
            )
            print(f"[validate_resolved] PASS  username={state['username']!r} user_id={user_id}")
        else:
            # ReACT returned null or invalid — signal not found
            logger.warning(
                "validate_resolved FAIL req_id=%d username=%r user_id=%r",
                state["req_id"], state["username"], user_id,
            )
            print(f"[validate_resolved] FAIL  username={state['username']!r} user_id={user_id!r}")

        return state
    return validate_resolved


def make_persist_cache_node(redis_client: redis_lib.Redis):
    """
    Node: persist_cache  (deterministic)
    Redis cache set (username → user_id).
    Only called if validation passed (user_id is not None).
    """
    async def persist_cache(state: ResolveUsernameAgentState) -> ResolveUsernameAgentState:
        if state["cache_hit"] or state["user_id"] is None:
            return state

        username = state["username"]
        user_id  = state["user_id"]

        try:
            if _CACHE_TTL > 0:
                redis_client.setex(username, _CACHE_TTL, str(user_id))
            else:
                redis_client.set(username, str(user_id))
            logger.info("persist_cache OK req_id=%d username=%r user_id=%d",
                        state["req_id"], username, user_id)
            print(f"[persist_cache] OK  {username} -> {user_id}")
        except redis_lib.RedisError as exc:
            logger.warning("persist_cache Redis SET failed username=%r: %s", username, exc)
            # Non-fatal — user_id is still valid, MongoDB has the data

        return state
    return persist_cache


# ---------------------------------------------------------------------------
# Routing function for ComposeUserMentions graph
# ---------------------------------------------------------------------------

def _route_after_cache(state: ResolveUsernameAgentState) -> str:
    """Skip ReACT loop + validate nodes if we got a cache hit."""
    return "END" if state["cache_hit"] else "reason_and_act_loop"


def _route_after_validate(state: ResolveUsernameAgentState) -> str:
    """Skip persist if validation failed (user not found)."""
    return "END" if state["user_id"] is None else "persist_cache"


# ---------------------------------------------------------------------------
# ComposeUserMentions graph builder
# ---------------------------------------------------------------------------

def build_resolve_username_agent(
    redis_client: redis_lib.Redis,
    mongo_col,
):
    """Build and compile the ComposeUserMentions LangGraph agent with ReACT loop."""
    graph = StateGraph(ResolveUsernameAgentState)

    graph.add_node("check_cache",           make_check_cache_node(redis_client))
    graph.add_node("reason_and_act_loop",   make_reason_resolve_node(mongo_col))
    graph.add_node("validate_resolved",     make_validate_resolved_node())
    graph.add_node("persist_cache",         make_persist_cache_node(redis_client))

    graph.set_entry_point("check_cache")

    # On cache hit → END immediately; on miss → ReACT → validate → persist → END
    graph.add_conditional_edges(
        "check_cache",
        _route_after_cache,
        {"END": END, "reason_and_act_loop": "reason_and_act_loop"},
    )
    graph.add_edge("reason_and_act_loop",  "validate_resolved")
    graph.add_conditional_edges(
        "validate_resolved",
        _route_after_validate,
        {"END": END, "persist_cache": "persist_cache"},
    )
    graph.add_edge("persist_cache",        END)

    return graph.compile()

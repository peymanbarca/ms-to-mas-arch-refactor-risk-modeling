"""
WRITE HOME TIMELINE AGENT - Graph Topology

    START
      |
      v
  [decode_message]           Deterministic — parse JSON bytes from RabbitMQ
      |                      delivery body into WriteHomeTimelineMessage fields.
      |                      Sets decode_ok=True/False.
      v
  [reason_validate_message]  LLM — given the decoded message fields, decide:
      |                      "Is this message valid and safe to forward to
      |                       HomeTimelineService.WriteHomeTimeline?"
      |
      |                      Validation reasoning:
      |                      - post_id must be a positive integer (> 0)
      |                      - user_id must be a positive integer (> 0)
      |                      - req_id must be a positive integer (> 0)
      |                      - timestamp must be in a reasonable ms range
      |                        (year 2000 – year 2100)
      |                      - user_mentions_id must be a list of positive ints
      |                        (empty list is valid)
      |                      - carrier must be a dict (empty is valid)
      |
      |                      Returns: { approved: bool, reason: str,
      |                                 cleaned_mentions: [i64...] }
      v
  [validate_decision]        Deterministic guard — enforce hard field checks
      |                      regardless of LLM output:
      |                      - post_id <= 0 → ALWAYS reject
      |                      - user_id <= 0 → ALWAYS reject
      |                      - decode_ok=False → ALWAYS reject
      |                      - LLM cannot approve a message that fails hard checks
      |                      - LLM cannot reject a message that passes all checks
      |                        (prevents spurious NACKs that would clog the queue)
      |                      Also validates cleaned_mentions (falls back to
      |                      filtering negatives from original list).
      v
  [forward_to_home_timeline] Deterministic — call
      |                      HomeTimelineService.WriteHomeTimeline(
      |                        req_id, post_id, user_id, timestamp,
      |                        validated_mentions, carrier
      |                      )
      |                      Skip silently if approved=False.
      v
     END  →  approved (bool) returned to consumer for ACK/NACK decision

Key Design Decisions
────────────────────
- RabbitMQ consumer infrastructure (consumer.py, pika) UNCHANGED.
- HomeTimelineService Thrift client pool UNCHANGED.
- message.py encode/decode UNCHANGED.
- The LLM reasoning replaces the static "accept any decoded message" logic
  from the original worker.py. Instead of blindly forwarding every decoded
  message, the agent reasons about each message's validity.
- validate_decision is a hard safety net: LLM cannot approve truly invalid
  messages (prevents corrupting home timelines) nor wrongly reject valid
  messages (prevents message loss / queue poisoning).
- cleaned_mentions: LLM may clean the mention list (remove negatives, dedup);
  the guard verifies cleaned_mentions ⊆ valid subset of original mentions.
- Token metrics tracked per message.
"""

import json
import logging
import re
import asyncio
import time
from typing import TypedDict, Optional, List, Dict, Any

from langgraph.graph import StateGraph, END
from langchain_ollama import ChatOllama

from .message import decode, WriteHomeTimelineMessage
from .thrift_pool import ThriftClientPool

logger = logging.getLogger("write-home-timeline-agent")

# ── LLM ─────────────────────────────────────────────────────────────────────
llm = ChatOllama(model="llama3.2:3b", temperature=0.0, reasoning=False)

# Timestamp bounds (ms): year 2000 → year 2100
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


def _is_valid_message_deterministic(msg: WriteHomeTimelineMessage) -> tuple[bool, list]:
    """Hard field checks — used as reference and in validate_decision."""
    issues = []
    if not msg.post_id or msg.post_id <= 0:
        issues.append(f"post_id must be > 0, got {msg.post_id}")
    if not msg.user_id or msg.user_id <= 0:
        issues.append(f"user_id must be > 0, got {msg.user_id}")
    if not msg.req_id or msg.req_id <= 0:
        issues.append(f"req_id must be > 0, got {msg.req_id}")
    if msg.timestamp and not (_TS_MIN <= msg.timestamp <= _TS_MAX):
        issues.append(
            f"timestamp {msg.timestamp} out of range [{_TS_MIN}, {_TS_MAX}]"
        )
    if not isinstance(msg.user_mentions_id, list):
        issues.append("user_mentions_id must be a list")
    else:
        bad = [x for x in msg.user_mentions_id if not isinstance(x, int) or x <= 0]
        if bad:
            issues.append(f"user_mentions_id contains invalid entries: {bad}")
    return len(issues) == 0, issues


def _clean_mentions_deterministic(raw_mentions: list) -> list:
    """Remove non-positive integers from mentions list."""
    return [x for x in raw_mentions if isinstance(x, int) and x > 0]


# ══════════════════════════════════════════════════════════════════════════════
# Agent State
# ══════════════════════════════════════════════════════════════════════════════

class WriteHomeTimelineAgentState(TypedDict):
    # Raw input
    body: bytes    # raw RabbitMQ message body

    # After decode_message
    decode_ok:       bool
    decode_error:    Optional[str]
    req_id:          Optional[int]
    post_id:         Optional[int]
    user_id:         Optional[int]
    timestamp:       Optional[int]
    user_mentions_id: Optional[List[int]]
    carrier:         Optional[Dict[str, str]]

    # LLM output
    llm_approved:        Optional[bool]
    llm_reason:          Optional[str]
    llm_cleaned_mentions: Optional[List[int]]

    # Final validated values
    approved:           Optional[bool]
    validated_mentions: Optional[List[int]]

    # Metrics
    total_input_tokens:  int
    total_output_tokens: int
    total_llm_calls:     int
    fallback_used:       bool


# ══════════════════════════════════════════════════════════════════════════════
# Node: decode_message  (deterministic)
# ══════════════════════════════════════════════════════════════════════════════

async def decode_message(state: WriteHomeTimelineAgentState) -> WriteHomeTimelineAgentState:
    """
    Parse raw RabbitMQ bytes into message fields.
    Sets decode_ok=True on success, decode_ok=False with decode_error on failure.
    """
    try:
        msg = decode(state["body"])
        state["decode_ok"]        = True
        state["decode_error"]     = None
        state["req_id"]           = msg.req_id
        state["post_id"]          = msg.post_id
        state["user_id"]          = msg.user_id
        state["timestamp"]        = msg.timestamp
        state["user_mentions_id"] = msg.user_mentions_id
        state["carrier"]          = msg.carrier
        logger.info(
            "decode_message OK req_id=%d post_id=%d user_id=%d mentions=%d",
            msg.req_id, msg.post_id, msg.user_id, len(msg.user_mentions_id),
        )
        print(
            f"[decode_message] OK  req_id={msg.req_id} "
            f"post_id={msg.post_id} user_id={msg.user_id}"
        )
    except Exception as exc:
        state["decode_ok"]    = False
        state["decode_error"] = str(exc)
        logger.error("decode_message FAILED: %s  body=%r", exc, state["body"][:100])
        print(f"[decode_message] FAILED  error={exc}")
    return state


# ══════════════════════════════════════════════════════════════════════════════
# Node: reason_validate_message  (LLM)
# ══════════════════════════════════════════════════════════════════════════════

async def reason_validate_message(
    state: WriteHomeTimelineAgentState,
) -> WriteHomeTimelineAgentState:
    """
    LLM reasoning: validate message fields and decide whether to forward.
    Also cleans the user_mentions_id list.
    """
    # If decode failed, skip LLM — nothing to reason about
    if not state["decode_ok"]:
        state["llm_approved"]         = False
        state["llm_reason"]           = f"Decode failed: {state['decode_error']}"
        state["llm_cleaned_mentions"] = []
        return state

    now_ms   = int(time.time() * 1000)
    mentions = state["user_mentions_id"] or []

    prompt = f"""
You are a message validation agent for a social network's home timeline fan-out service.

Your task is to validate a WriteHomeTimeline message received from RabbitMQ
before it is forwarded to the HomeTimelineService.

Message fields:
  req_id           = {state["req_id"]}
  post_id          = {state["post_id"]}
  user_id          = {state["user_id"]}   (author of the post)
  timestamp        = {state["timestamp"]} ms  (current time ≈ {now_ms} ms)
  user_mentions_id = {mentions}
  carrier          = {json.dumps(state["carrier"] or {})}

Validation rules:
  1. post_id must be a positive integer (> 0)
  2. user_id must be a positive integer (> 0)
  3. req_id must be a positive integer (> 0)
  4. user_mentions_id: all entries must be positive integers (> 0)
     Empty list [] is valid. Remove any invalid entries from cleaned_mentions.
  5. carrier: must be a dict (empty dict is valid)

If ALL rules pass (after cleaning mentions): approved = true
If ANY hard field (post_id, user_id, req_id) fails: approved = false

Return ONLY valid JSON — no explanation, no code, no markdown.

Schema:
{{
  "approved":         true | false,
  "reason":           "<short explanation>",
  "cleaned_mentions": [<positive integers only>]
}}
"""

    logger.info(
        "LLM reason_validate_message req_id=%d post_id=%d, prompt=%r",
        state["req_id"] or 0, state["post_id"] or 0, prompt
    )
    response = await asyncio.to_thread(llm.invoke, prompt)
    raw      = response.text()
    in_tok   = response.usage_metadata.get("input_tokens",  0)
    out_tok  = response.usage_metadata.get("output_tokens", 0)

    logger.info("LLM raw=%r  in=%d out=%d", raw[:200], in_tok, out_tok)
    print(f"[reason_validate_message] raw={raw[:120]!r}  in={in_tok} out={out_tok}")

    parsed   = _parse_json(raw)

    llm_approved  = None
    llm_reason    = ""
    llm_cleaned   = None

    if parsed:
        a = parsed.get("approved")
        r = parsed.get("reason", "")
        c = parsed.get("cleaned_mentions")
        if isinstance(a, bool):
            llm_approved = a
        if isinstance(r, str):
            llm_reason = r
        if isinstance(c, list) and all(isinstance(x, int) for x in c):
            llm_cleaned = c

    state["llm_approved"]         = llm_approved
    state["llm_reason"]           = llm_reason
    state["llm_cleaned_mentions"] = llm_cleaned
    state["total_input_tokens"]  += in_tok
    state["total_output_tokens"] += out_tok
    state["total_llm_calls"]     += 1
    return state


# ══════════════════════════════════════════════════════════════════════════════
# Node: validate_decision  (deterministic guard)
# ══════════════════════════════════════════════════════════════════════════════

async def validate_decision(
    state: WriteHomeTimelineAgentState,
) -> WriteHomeTimelineAgentState:
    """
    Deterministic guard: enforce hard field checks.

    Hard invariants:
      - decode_ok=False  → ALWAYS reject (nothing to forward)
      - post_id <= 0     → ALWAYS reject
      - user_id <= 0     → ALWAYS reject
      - req_id  <= 0     → ALWAYS reject
      - LLM approval overridden by deterministic result when they disagree

    cleaned_mentions:
      - If LLM returned a valid cleaned list → use it (may be a subset)
      - If LLM cleaned list contains invalid entries → fall back to deterministic filter
      - Deterministic filter: keep only positive ints from original list
    """
    if not state["decode_ok"]:
        state["approved"]           = False
        state["validated_mentions"] = []
        state["fallback_used"]      = False
        logger.info("validate_decision REJECTED (decode failed)")
        print("[validate_decision] REJECTED  (decode failed)")
        return state

    # Build a temp WriteHomeTimelineMessage for the deterministic check
    msg = WriteHomeTimelineMessage(
        req_id=state["req_id"] or 0,
        post_id=state["post_id"] or 0,
        user_id=state["user_id"] or 0,
        timestamp=state["timestamp"] or 0,
        user_mentions_id=state["user_mentions_id"] or [],
        carrier=state["carrier"] or {},
    )
    det_valid, det_issues = _is_valid_message_deterministic(msg)

    llm_approved = state.get("llm_approved")

    if llm_approved is not None and llm_approved == det_valid:
        state["approved"]      = llm_approved
        state["fallback_used"] = False
        logger.info(
            "validate_decision PASS req_id=%d approved=%s",
            state["req_id"], llm_approved,
        )
        print(f"[validate_decision] PASS  approved={llm_approved}")
    else:
        state["approved"]      = det_valid
        state["fallback_used"] = True
        logger.warning(
            "validate_decision FALLBACK req_id=%d llm=%s -> det=%s reason=%r issues=%s",
            state["req_id"], llm_approved, det_valid,
            state.get("llm_reason"), det_issues,
        )
        print(
            f"[validate_decision] FALLBACK  llm={llm_approved} -> det={det_valid} "
            f"issues={det_issues}"
        )

    # ── Validate cleaned_mentions ──
    original_mentions = state["user_mentions_id"] or []
    llm_cleaned       = state.get("llm_cleaned_mentions")
    det_cleaned       = _clean_mentions_deterministic(original_mentions)

    if llm_cleaned is not None:
        # Verify LLM cleaned list is a valid subset (no entries not in original)
        original_set  = set(original_mentions)
        llm_set       = set(llm_cleaned)
        # LLM may only keep positive ints from the original
        # Any entry in llm_cleaned that is not a positive int or not in
        # original_set is rejected
        valid_llm = [
            x for x in llm_cleaned
            if isinstance(x, int) and x > 0 and x in original_set
        ]
        if sorted(valid_llm) == sorted(llm_cleaned):
            state["validated_mentions"] = llm_cleaned
        else:
            state["validated_mentions"] = det_cleaned
            state["fallback_used"]      = True
    else:
        state["validated_mentions"] = det_cleaned
        state["fallback_used"]      = True

    return state


# ══════════════════════════════════════════════════════════════════════════════
# Node: forward_to_home_timeline  (deterministic)
# ══════════════════════════════════════════════════════════════════════════════

def make_forward_node(home_timeline_pool: ThriftClientPool):
    """
    Deterministic: call HomeTimelineService.WriteHomeTimeline with validated fields.
    Skip silently if approved=False.
    """
    async def forward_to_home_timeline(
        state: WriteHomeTimelineAgentState,
    ) -> WriteHomeTimelineAgentState:
        if not state.get("approved"):
            logger.info(
                "forward_to_home_timeline SKIPPED req_id=%s reason=%r",
                state.get("req_id"), state.get("llm_reason"),
            )
            print(
                f"[forward_to_home_timeline] SKIPPED  "
                f"req_id={state.get('req_id')} reason={state.get('llm_reason')!r}"
            )
            return state

        req_id   = state["req_id"]
        post_id  = state["post_id"]
        user_id  = state["user_id"]
        ts       = state["timestamp"]
        mentions = state["validated_mentions"] or []
        carrier  = state["carrier"] or {}

        try:
            with home_timeline_pool.connection() as client:
                client.WriteHomeTimeline(
                    req_id, post_id, user_id, ts, mentions, carrier
                )
            logger.info(
                "forward_to_home_timeline OK req_id=%d post_id=%d user_id=%d",
                req_id, post_id, user_id,
            )
            print(
                f"[forward_to_home_timeline] OK  "
                f"req_id={req_id} post_id={post_id} user_id={user_id}"
            )
        except Exception as exc:
            from ms_baseline.dsb_social.gen_py.social_network.ttypes import ServiceException
            logger.error(
                "forward_to_home_timeline FAILED req_id=%d: %s", req_id, exc
            )
            raise   # re-raise so consumer can NACK

        return state
    return forward_to_home_timeline


# ══════════════════════════════════════════════════════════════════════════════
# Graph builder
# ══════════════════════════════════════════════════════════════════════════════

def build_write_home_timeline_agent(
    home_timeline_pool: ThriftClientPool,
) -> any:
    """Build and compile the WriteHomeTimeline LangGraph agent."""
    graph = StateGraph(WriteHomeTimelineAgentState)

    graph.add_node("decode_message",           decode_message)
    graph.add_node("reason_validate_message",  reason_validate_message)
    graph.add_node("validate_decision",        validate_decision)
    graph.add_node("forward_to_home_timeline", make_forward_node(home_timeline_pool))

    graph.set_entry_point("decode_message")
    graph.add_edge("decode_message",          "reason_validate_message")
    graph.add_edge("reason_validate_message", "validate_decision")
    graph.add_edge("validate_decision",       "forward_to_home_timeline")
    graph.add_edge("forward_to_home_timeline", END)

    return graph.compile()
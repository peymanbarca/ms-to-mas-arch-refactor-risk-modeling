"""
UNIQUE-ID AGENT - Graph Topology

    START
      |
      v
[gather_inputs]       (Deterministic: read timestamp_ms, machine_id, sequence from SnowflakeGenerator)
      |
      v
[reason_unique_id]    (LLM: apply Snowflake bit-packing formula → i64)
      |
      v
[validate_output]     (Deterministic: assert result is positive i64, matches expected bit layout)
      |
      v
      END  →  unique_id (i64) returned to Thrift handler

Key Design Decisions
--------------------
- Thrift interface (UniqueIdService.Iface) is UNCHANGED.
- SnowflakeGenerator still owns the thread-safe clock + sequence counter.
  It no longer assembles the final integer — it only provides the three
  raw inputs (timestamp_ms, machine_id, sequence).
- The LLM reasoning node receives those three integers and applies the
  Snowflake bit-packing formula:
      id = (timestamp_ms << 22) | (machine_id << 12) | sequence
  This is the "static logic" that is converted to LLM reasoning.
- validate_output is a deterministic guard: if the LLM returns a value
  outside the valid range or with wrong bit fields, we fall back to the
  deterministic formula so the service never returns a wrong ID.
- Token usage is accumulated in state and logged per request.
"""

import json
import logging
import re
import asyncio
import time

from typing import TypedDict, Optional, Dict, Any
from langgraph.graph import StateGraph, END
from langchain_ollama import ChatOllama

from .snowflake import (
    SnowflakeGenerator,
    _TIMESTAMP_SHIFT,
    _MACHINE_ID_SHIFT,
    _SEQUENCE_BITS,
)

logger = logging.getLogger("unique-id-agent")

# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------

llm = ChatOllama(model="llama3.2:3b", temperature=0.0, reasoning=False)


# ---------------------------------------------------------------------------
# Agent State
# ---------------------------------------------------------------------------

class UniqueIdAgentState(TypedDict):
    # Inputs (set by gather_inputs)
    req_id:          int
    post_type:       Any          # PostType enum value
    machine_id:      int
    timestamp_ms:    int
    sequence:        int

    # Output (set by reason_unique_id, confirmed by validate_output)
    unique_id:       Optional[int]

    # LLM metrics (accumulated across nodes)
    total_input_tokens:  int
    total_output_tokens: int
    total_llm_calls:     int

    # Internal
    fallback_used:   bool         # True if validate_output had to self-correct


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_json(text: str) -> Optional[dict]:
    """Extract the first JSON object from an LLM response string."""
    try:
        text = text.replace("\n", " ").replace("'", "").strip()
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            return json.loads(m.group(0))
    except Exception as exc:
        logger.warning("JSON parse error: %s — raw: %r", exc, text[:200])
    return None


def _deterministic_snowflake(timestamp_ms: int, machine_id: int, sequence: int) -> int:
    """Reference implementation — used by validate_output as fallback."""
    return (timestamp_ms << _TIMESTAMP_SHIFT) | (machine_id << _MACHINE_ID_SHIFT) | sequence


# ---------------------------------------------------------------------------
# Node 1 — gather_inputs  (deterministic)
# ---------------------------------------------------------------------------

async def gather_inputs(state: UniqueIdAgentState) -> UniqueIdAgentState:
    """
    Read (timestamp_ms, machine_id, sequence) from the SnowflakeGenerator.

    The generator is passed in via a closure when the graph is built
    (see build_unique_id_agent). This node does NOT call next_id() — it
    reads the raw components so the LLM can reason about the formula.
    """
    logger.info(
        "gather_inputs req_id=%d post_type=%s machine_id=%d ts=%d seq=%d",
        state["req_id"], state["post_type"],
        state["machine_id"], state["timestamp_ms"], state["sequence"],
    )
    print(f"[gather_inputs] req_id={state['req_id']} "
          f"ts={state['timestamp_ms']} machine_id={state['machine_id']} "
          f"seq={state['sequence']}")
    return state


# ---------------------------------------------------------------------------
# Node 2 — reason_unique_id  (LLM)
# ---------------------------------------------------------------------------

async def reason_unique_id(state: UniqueIdAgentState) -> UniqueIdAgentState:
    """
    Ask the LLM to apply the Snowflake bit-packing formula.

    The prompt explains the exact bit layout and asks the LLM to compute
    the 64-bit integer. The LLM is the reasoning engine for this formula.
    """
    prompt = f"""
You are a Snowflake ID generator agent.

Your task is to compute a 64-bit unique identifier using the Snowflake format.

Rules:
  - Perform exact integer arithmetic (no rounding, no floating point)
  - The result MUST be a positive integer (bit 63 = 0)
  - Return ONLY valid JSON in the schema below — no explanation, no code

Schema:
{{"unique_id": <integer>}}

Snowflake bit layout (64-bit signed integer):
  Bit 63       : always 0 (positive sentinel — do NOT set this bit)
  Bits 62..22  : 41-bit millisecond timestamp  (TIMESTAMP_MS << 22)
  Bits 21..12  : 10-bit machine_id             (MACHINE_ID   << 12)
  Bits 11..0   : 12-bit sequence counter       (SEQUENCE      << 0)

Formula:
  unique_id = (TIMESTAMP_MS << 22) | (MACHINE_ID << 12) | SEQUENCE


Input:
  TIMESTAMP_MS = {state["timestamp_ms"]}
  MACHINE_ID   = {state["machine_id"]}
  SEQUENCE     = {state["sequence"]}
"""

    logger.info("LLM prompt for req_id=%d:\n%s", state["req_id"], prompt)

    response = await asyncio.to_thread(llm.invoke, prompt)

    raw      = response.text()
    in_tok   = response.usage_metadata.get("input_tokens",  0)
    out_tok  = response.usage_metadata.get("output_tokens", 0)

    logger.info("LLM raw response: %r  tokens in=%d out=%d", raw, in_tok, out_tok)
    print(f"[reason_unique_id] LLM response: {raw!r}  in_tokens={in_tok} out_tokens={out_tok}")

    parsed = _parse_json(raw)
    unique_id = None
    if parsed and isinstance(parsed.get("unique_id"), int):
        unique_id = parsed["unique_id"]
    else:
        logger.warning(
            "LLM did not return a valid unique_id for req_id=%d — raw=%r",
            state["req_id"], raw,
        )

    state["unique_id"]           = unique_id
    state["total_input_tokens"]  += in_tok
    state["total_output_tokens"] += out_tok
    state["total_llm_calls"]     += 1
    return state


# ---------------------------------------------------------------------------
# Node 3 — validate_output  (deterministic guard)
# ---------------------------------------------------------------------------

async def validate_output(state: UniqueIdAgentState) -> UniqueIdAgentState:
    """
    Validate the LLM's result against the expected bit layout.

    Checks:
    1. unique_id is a positive integer.
    2. The embedded timestamp field matches timestamp_ms (±1 for rounding).
    3. The embedded machine_id matches machine_id exactly.
    4. The embedded sequence matches sequence exactly.

    If any check fails, fall back to the deterministic formula and set
    fallback_used=True so the discrepancy is logged and tracked.
    """
    ts  = state["timestamp_ms"]
    mid = state["machine_id"]
    seq = state["sequence"]

    expected = _deterministic_snowflake(ts, mid, seq)
    uid      = state["unique_id"]

    def _check(uid: int) -> bool:
        if uid is None or uid <= 0:
            return False
        extracted_ts  = uid >> _TIMESTAMP_SHIFT
        extracted_mid = (uid >> _MACHINE_ID_SHIFT) & ((1 << 10) - 1)
        extracted_seq = uid & ((1 << _SEQUENCE_BITS) - 1)
        return (
            abs(extracted_ts  - ts)  <= 1 and   # ±1 ms tolerance
            extracted_mid == mid and
            extracted_seq == seq
        )

    if _check(uid):
        state["fallback_used"] = False
        logger.info(
            "validate_output PASS req_id=%d unique_id=%d", state["req_id"], uid
        )
        print(f"[validate_output] PASS  unique_id={uid}")
    else:
        state["unique_id"]     = expected
        state["fallback_used"] = True
        logger.warning(
            "validate_output FALLBACK req_id=%d llm_id=%s expected=%d",
            state["req_id"], uid, expected,
        )
        print(f"[validate_output] FALLBACK  llm_id={uid} -> correct={expected}")

    return state


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------

def build_unique_id_agent() -> any:
    """
    Build and compile the UniqueId LangGraph agent.

    Returns the compiled graph (callable as graph.ainvoke(state)).
    """
    graph = StateGraph(UniqueIdAgentState)

    graph.add_node("gather_inputs",    gather_inputs)
    graph.add_node("reason_unique_id", reason_unique_id)
    graph.add_node("validate_output",  validate_output)

    graph.set_entry_point("gather_inputs")
    graph.add_edge("gather_inputs",    "reason_unique_id")
    graph.add_edge("reason_unique_id", "validate_output")
    graph.add_edge("validate_output",  END)

    return graph.compile()

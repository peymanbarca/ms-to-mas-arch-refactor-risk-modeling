"""
currencyagent/agent.py

LangGraph currency conversion agent — replaces simple exchange calculation with
intelligent agentic currency conversion while keeping the exact same gRPC interface.

Graph topology:
─────────────────────────────────────────────────────────────────────────────
  ┌──────────────────┐
  │validate_currencies│  (deterministic) check currencies are supported
  └────────┬─────────┘
           │
     ┌─────▼──────┐  validation_error present?
     │   route    │──────────────────────────────────────────────────┐
     └─────┬──────┘  no                                              │ yes
           │                                                         │
  ┌────────▼──────────┐                                  ┌───────────▼──────────┐
  │  calculate_rate   │  (deterministic) compute        │  reject_conversion   │
  │                   │  EUR-based exchange rate        │  sets decision=FAILED│
  └────────┬──────────┘                                  └───────────┬──────────┘
           │                                                         │
  ┌────────▼──────────┐                                             │
  │ conversion_review │  (LLM) verify calculation &                 │
  │                   │  apply rounding/validation                  │
  └────────┬──────────┘                                             │
           │                                                         │
  ┌────────▼──────────┐◄────────────────────────────────────────────┘
  │ persist_conversion│  (deterministic) audit trail to MongoDB
  └────────┬──────────┘
           │
          END

Node roles
──────────
validate_currencies   Deterministic tool. Checks if both from_code and
                      to_code are in supported currencies. Sets
                      validation_error on failure.

calculate_rate        Deterministic tool. Performs the actual EUR-based
                      currency conversion using rates. Generates
                      conversion_id (UUID) and result amount.

conversion_review     LLM node (Ollama llama3). Reviews conversion
                      calculation, verifies rounding, and approves
                      or rejects the conversion.

reject_conversion     Deterministic shortcut for validation failures.
                      Sets decision=FAILED with reason.

persist_conversion    Deterministic tool. Writes conversion record to
                      MongoDB including rates, calculation, LLM review.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import re
import uuid
from typing import Any, Dict, Optional
import os

from langchain_ollama import ChatOllama
from langgraph.graph import END, StateGraph
from motor.motor_asyncio import AsyncIOMotorClient
from typing_extensions import TypedDict

logger = logging.getLogger("currencyagent")

# ── Ollama LLM (temperature=0 for deterministic conversion review) ─────────────
llm = ChatOllama(model="llama3.2:3b", temperature=0.0, reasoning=False)




# Global client (lazy-initialized)
_mongodb_client: AsyncIOMotorClient = None




# ════════════════════════════════════════════════════════════════════════════
# Agent state
# ════════════════════════════════════════════════════════════════════════════

class CurrencyConversionAgentState(TypedDict):
    """
    Shared state threaded through every node in the graph.

    Input fields (set before ainvoke):
        from_currency_code, from_units, from_nanos
        to_currency_code
        rates – dict of currency codes to EUR rates

    Intermediate fields (written by nodes):
        conversion_id      – UUID for this conversion
        validation_error   – set if currencies not supported
        euros_amount       – intermediate EUR amount
        converted_amount   – result before rounding
        rounded_amount     – final result after rounding

    Output fields (written by conversion_review / reject_conversion):
        decision           – {status: SUCCESS|FAILED, result: {...}, reason: str}

    Metrics (accumulated by conversion_review):
        total_input_tokens
        total_output_tokens
        total_llm_calls
    """
    # ── inputs ────────────────────────────────────────────────────────────────
    from_currency_code: str
    from_units:         int
    from_nanos:         int
    to_currency_code:   str
    rates:              dict

    # ── intermediate ──────────────────────────────────────────────────────────
    conversion_id:      Optional[str]
    validation_error:   Optional[str]
    euros_amount:       Optional[Dict[str, int]]
    converted_amount:   Optional[Dict[str, float]]
    rounded_amount:     Optional[Dict[str, int]]

    # ── output ────────────────────────────────────────────────────────────────
    decision:           Dict[str, Any]

    # ── metrics ───────────────────────────────────────────────────────────────
    total_input_tokens:  int
    total_output_tokens: int
    total_llm_calls:     int


# ════════════════════════════════════════════════════════════════════════════
# Node 1 – validate_currencies  (deterministic tool)
# ════════════════════════════════════════════════════════════════════════════

async def validate_currencies_node(state: CurrencyConversionAgentState) -> CurrencyConversionAgentState:
    """
    Deterministic tool node.

    Validates that both currencies are in the supported list.

    On success  → validation_error=None.
    On failure  → sets validation_error with description.
    """
    logger.info("[validate_currencies] validating | from=%s to=%s",
                state["from_currency_code"], state["to_currency_code"])

    from_code = state["from_currency_code"]
    to_code = state["to_currency_code"]
    rates = state["rates"]

    if from_code not in rates:
        error_msg = f"Unsupported currency: {from_code}"
        logger.warning("[validate_currencies] failed | reason=%s", error_msg)
        return {
            **state,
            "conversion_id": str(uuid.uuid4()),
            "validation_error": error_msg,
        }

    if to_code not in rates:
        error_msg = f"Unsupported currency: {to_code}"
        logger.warning("[validate_currencies] failed | reason=%s", error_msg)
        return {
            **state,
            "conversion_id": str(uuid.uuid4()),
            "validation_error": error_msg,
        }

    logger.info("[validate_currencies] passed | supported currencies")

    return {
        **state,
        "conversion_id": str(uuid.uuid4()),
        "validation_error": None,
    }


# ════════════════════════════════════════════════════════════════════════════
# Conditional router after validate_currencies
# ════════════════════════════════════════════════════════════════════════════

def route_after_validation(state: CurrencyConversionAgentState) -> str:
    """
    Conditional edge function — returns the name of the next node.

    If validation_error is set → go to reject_conversion.
    Otherwise                  → go to calculate_rate.
    """
    if state.get("validation_error"):
        logger.info("[route] currency validation failed → reject_conversion")
        return "reject_conversion"
    logger.info("[route] currency validation passed → calculate_rate")
    return "calculate_rate"


# ════════════════════════════════════════════════════════════════════════════
# Node 2a – calculate_rate  (deterministic tool)
# ════════════════════════════════════════════════════════════════════════════

def _carry(amount: dict) -> dict:
    """Helper to handle decimal carrying for currency amounts."""
    fraction_size = 10**9
    amount["nanos"] += (amount["units"] % 1) * fraction_size
    amount["units"] = int(amount["units"]) + int(amount["nanos"] // fraction_size)
    amount["nanos"] = amount["nanos"] % fraction_size
    return amount


async def calculate_rate_node(state: CurrencyConversionAgentState) -> CurrencyConversionAgentState:
    """
    Deterministic tool node — performs EUR-based currency conversion.

    Steps:
      1. Convert from_currency to EUR
      2. Convert EUR to to_currency
      3. Round final result

    Sets:
        euros_amount    – intermediate EUR amount
        converted_amount – result before rounding
        rounded_amount  – final result after rounding
    """
    logger.info("[calculate_rate] calculating | from=%s to=%s amount=%d.%d",
                state["from_currency_code"], state["to_currency_code"],
                state["from_units"], state["from_nanos"])

    from_code = state["from_currency_code"]
    to_code = state["to_currency_code"]
    rates = state["rates"]

    # Step 1: Convert to EUR
    from_rate = float(rates[from_code])
    euros = _carry({
        "units": state["from_units"] / from_rate,
        "nanos": state["from_nanos"] / from_rate
    })
    euros["nanos"] = round(euros["nanos"])

    # Step 2: Convert to target currency
    to_rate = float(rates[to_code])
    result = _carry({
        "units": euros["units"] * to_rate,
        "nanos": euros["nanos"] * to_rate
    })

    rounded = {
        "units": int(result["units"]),
        "nanos": int(round(result["nanos"])),
    }

    logger.info("[calculate_rate] calculated | result=%d.%d %s",
                rounded["units"], rounded["nanos"], to_code)

    return {
        **state,
        "euros_amount": euros,
        "converted_amount": result,
        "rounded_amount": rounded,
    }


# ════════════════════════════════════════════════════════════════════════════
# Node 2b – reject_conversion  (deterministic shortcut)
# ════════════════════════════════════════════════════════════════════════════

async def reject_conversion_node(state: CurrencyConversionAgentState) -> CurrencyConversionAgentState:
    """
    Deterministic shortcut node — reached when currency validation fails.

    Sets decision=FAILED with the validation error.
    """
    reason = state.get("validation_error", "Currency validation failed.")
    logger.info("[reject_conversion] setting decision=FAILED | reason=%s", reason)

    return {
        **state,
        "decision": {
            "status": "FAILED",
            "result": None,
            "reason": reason,
        },
    }


# ════════════════════════════════════════════════════════════════════════════
# Node 3 – conversion_review  (LLM / Ollama node)
# ════════════════════════════════════════════════════════════════════════════

def _parse_json_response(text: str) -> dict | None:
    """Extract the first JSON object from an LLM text response."""
    try:
        match = re.search(r"\{.*?\}", text, re.DOTALL)
        if match:
            return json.loads(match.group(0))
    except Exception as exc:
        logger.error("[conversion_review] JSON parse error: %s — raw: %s", exc, text)
    return None


async def conversion_review_node(state: CurrencyConversionAgentState) -> CurrencyConversionAgentState:
    """
    LLM node — reviews the currency conversion calculation.

    The LLM must return a JSON decision:
        {
          "status": "SUCCESS" | "REJECTED",
          "reason": "<brief explanation>"
        }

    Rule: Generally approve conversions with valid amounts.
    Reject only if amounts look suspicious or rounding is incorrect.

    Token usage is accumulated in state for observability.
    """
    from_units = state["from_units"]
    from_nanos = state["from_nanos"]
    to_units = state["rounded_amount"]["units"]
    to_nanos = state["rounded_amount"]["nanos"]

    from_cents = from_nanos // 10_000_000
    to_cents = to_nanos // 10_000_000

    prompt = f"""
You are a currency conversion verification agent for a payment system.

Your task is to verify that the currency conversion calculation is correct.

Rules:
- Status MUST be either "SUCCESS" or "REJECTED".
- Generally APPROVE conversions with reasonable amounts.
- Only REJECT if amounts look suspicious or calculation seems wrong.
- Rounding to nearest cent is acceptable.
- Return ONLY valid JSON. No markdown, no code blocks, no preamble.

Output schema:
{{
  "status": "SUCCESS" | "REJECTED",
  "reason": "<one sentence explanation>" // optional, only for REJECTED
}}

Conversion details:
  FROM_CURRENCY  : {state['from_currency_code']}
  FROM_AMOUNT    : {from_units}.{from_cents:02d}
  
  TO_CURRENCY    : {state['to_currency_code']}
  TO_AMOUNT      : {to_units}.{to_cents:02d}
  
  FROM_RATE      : {state['rates'].get(state['from_currency_code'], 'N/A')} (EUR)
  TO_RATE        : {state['rates'].get(state['to_currency_code'], 'N/A')} (EUR)
""".strip()

    logger.info("[conversion_review] invoking LLM | from=%s to=%s",
                state["from_currency_code"], state["to_currency_code"])

    response = await asyncio.to_thread(llm.invoke, prompt)

    raw        = response.text()
    in_tokens  = response.usage_metadata.get("input_tokens",  0)
    out_tokens = response.usage_metadata.get("output_tokens", 0)

    logger.info("[conversion_review] LLM raw response: %s", raw)
    logger.info("[conversion_review] tokens | in=%d out=%d", in_tokens, out_tokens)

    decision_json = _parse_json_response(raw)

    if not decision_json or decision_json.get("status") not in ("SUCCESS", "REJECTED"):
        logger.error("[conversion_review] invalid decision: %s", raw)
        raise ValueError(f"Invalid conversion decision from LLM: {raw!r}")

    status = decision_json.get("status", "REJECTED")
    
    if status == "SUCCESS":
        result = {
            "currency_code": state["to_currency_code"],
            "units": state["rounded_amount"]["units"],
            "nanos": state["rounded_amount"]["nanos"],
        }
    else:
        result = None

    logger.info("[conversion_review] decision=%s reason=%s",
                status, decision_json.get("reason", ""))

    return {
        **state,
        "decision": {
            "status": status,
            "result": result,
            "reason": decision_json.get("reason", ""),
        },
        "total_input_tokens":  state["total_input_tokens"]  + in_tokens,
        "total_output_tokens": state["total_output_tokens"] + out_tokens,
        "total_llm_calls":     state["total_llm_calls"]     + 1,
    }


# ════════════════════════════════════════════════════════════════════════════
# Node 4 – persist_conversion  (deterministic tool)
# ════════════════════════════════════════════════════════════════════════════

async def persist_conversion_node(state: CurrencyConversionAgentState) -> CurrencyConversionAgentState:
    """
    Deterministic tool node — persists conversion as log.

    Records:
        conversion_id    – UUID for this conversion
        from_currency    – source currency code
        to_currency      – target currency code
        from_amount      – source amount with units/nanos
        to_amount        – result amount with units/nanos
        rates            – EUR-based rates used
        euros_amount     – intermediate EUR calculation
        decision         – LLM review decision
        llm_metrics      – token usage
        created_at       – UTC timestamp
    """
    status = state["decision"].get("status", "REJECTED")
        
    from_cents = state["from_nanos"] // 10_000_000
    
    doc = {
        "_id":            state["conversion_id"],
        "status":         status,
        "from_currency":  state["from_currency_code"],
        "to_currency":    state["to_currency_code"],
        "from_amount": {
            "currency_code": state["from_currency_code"],
            "units":         state["from_units"],
            "nanos":         state["from_nanos"],
            "formatted":     f"{state['from_currency_code']} {state['from_units']}.{from_cents:02d}",
        },
        "to_amount":      state["decision"].get("result"),
        "rates": {
            "from_rate":  float(state["rates"].get(state["from_currency_code"], 0)),
            "to_rate":    float(state["rates"].get(state["to_currency_code"], 0)),
        },
        "euros_amount":   state.get("euros_amount"),
        "decision":       state["decision"],
        "llm_metrics": {
            "input_tokens":  state["total_input_tokens"],
            "output_tokens": state["total_output_tokens"],
            "llm_calls":     state["total_llm_calls"],
        },
        "created_at":     datetime.datetime.now(tz=datetime.timezone.utc),
    }

    logger.info("[log_conversion] | status=%s conversion_id=%s content=%s",
                status, state["conversion_id"], doc)


    return state


# ════════════════════════════════════════════════════════════════════════════
# Graph assembly
# ════════════════════════════════════════════════════════════════════════════

def build_currency_conversion_agent() -> Any:
    """
    Assemble and compile the LangGraph currency conversion agent.

    Returns the compiled graph, ready for ainvoke().
    """
    graph = StateGraph(CurrencyConversionAgentState)

    # Register nodes
    graph.add_node("validate_currencies",     validate_currencies_node)
    graph.add_node("calculate_rate",          calculate_rate_node)
    graph.add_node("reject_conversion",       reject_conversion_node)
    graph.add_node("conversion_review",       conversion_review_node)
    graph.add_node("persist_conversion",      persist_conversion_node)

    # Entry point
    graph.set_entry_point("validate_currencies")

    # Conditional edge after validation
    graph.add_conditional_edges(
        "validate_currencies",
        route_after_validation,
        {
            "calculate_rate":    "calculate_rate",
            "reject_conversion": "reject_conversion",
        },
    )

    # Happy path: calculate → review → persist
    graph.add_edge("calculate_rate",      "conversion_review")
    graph.add_edge("conversion_review",   "persist_conversion")

    # Rejection shortcut: reject → persist
    graph.add_edge("reject_conversion", "persist_conversion")

    # Terminal
    graph.add_edge("persist_conversion", END)

    compiled = graph.compile()
    logger.info("[CurrencyConversionAgent] graph compiled successfully")
    return compiled


# Singleton graph — built at import time
currency_conversion_graph = build_currency_conversion_agent()


# ════════════════════════════════════════════════════════════════════════════
# Public helper called by the gRPC servicer
# ════════════════════════════════════════════════════════════════════════════

async def run_currency_conversion_agent(
    from_currency_code: str,
    from_units:         int,
    from_nanos:         int,
    to_currency_code:   str,
    rates:              dict,
) -> CurrencyConversionAgentState:
    """
    Build initial state and invoke the compiled graph.

    Returns the final CurrencyConversionAgentState after all nodes have run.
    Raises ValueError if the LLM returns an unparseable decision.
    """
    initial_state: CurrencyConversionAgentState = {
        # inputs
        "from_currency_code": from_currency_code,
        "from_units":         from_units,
        "from_nanos":         from_nanos,
        "to_currency_code":   to_currency_code,
        "rates":              rates,
        # intermediate
        "conversion_id":      None,
        "validation_error":   None,
        "euros_amount":       None,
        "converted_amount":   None,
        "rounded_amount":     None,
        # output
        "decision": {},
        # metrics
        "total_input_tokens":  0,
        "total_output_tokens": 0,
        "total_llm_calls":     0,
    }

    logger.info(
        "[run_currency_conversion_agent] invoking graph | from=%s to=%s amount=%d.%d",
        from_currency_code, to_currency_code, from_units, from_nanos,
    )

    result: CurrencyConversionAgentState = await currency_conversion_graph.ainvoke(initial_state)

    logger.info(
        "[run_currency_conversion_agent] completed | status=%s result=%s llm_calls=%d",
        result["decision"].get("status"),
        result["decision"].get("result"),
        result["total_llm_calls"],
    )

    return result

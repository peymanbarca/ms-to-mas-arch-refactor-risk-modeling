"""
paymentservice/agent.py

LangGraph payment agent — replaces the simple charge() function call with a
full agentic graph while keeping the exact same gRPC Charge interface.

Graph topology:
─────────────────────────────────────────────────────────────────────────────
  ┌──────────────────┐
  │  validate_card   │  (deterministic) Luhn + expiry + CVV checks
  └────────┬─────────┘
           │
     ┌─────▼──────┐  validation_error present?
     │   route    │──────────────────────────────────────────────────┐
     └─────┬──────┘  no                                              │ yes
           │                                                         │
  ┌────────▼─────────┐                                   ┌───────────▼──────────┐
  │   call_psp       │  (deterministic) simulate PSP API  │  reject_card         │
  │                  │  → generates psp_tracking_id        │  sets decision=FAILED│
  └────────┬─────────┘                                   └───────────┬──────────┘
           │                                                         │
  ┌────────▼──────────┐                                             │
  │ payment_reasoning │  (LLM / Ollama llama3) decides              │
  │                   │  SUCCESS or FAILED from psp_tracking_id     │
  └────────┬──────────┘                                             │
           │                                                         │
  ┌────────▼──────────┐◄────────────────────────────────────────────┘
  │  persist_payment  │  (deterministic) writes to MongoDB
  └────────┬──────────┘
           │
          END

Node roles
──────────
validate_card      Deterministic tool. Runs Luhn, expiry, CVV checks from
                   card_validator.py.  Populates card_type / last_four on
                   success, or validation_error on failure.

call_psp           Deterministic tool. Simulates a real PSP API call with
                   network latency. Generates psp_tracking_id (UUID) and
                   sets transaction_id.

payment_reasoning  LLM node (Ollama llama3). Receives psp_tracking_id and
                   decides {status: SUCCESS|FAILED}.  Reasoning is the only
                   non-deterministic step — every other node is a pure tool.

reject_card        Deterministic shortcut for validation failures. Sets
                   decision=FAILED with the validation error as reason and
                   skips the PSP + LLM steps.

persist_payment    Deterministic tool. Writes the final payment document to
                   MongoDB regardless of SUCCESS or FAILED outcome.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import re
import time
import uuid
from typing import Any, Dict, Literal, Optional
import os

from langchain_ollama import ChatOllama
from langgraph.graph import END, StateGraph
from motor.motor_asyncio import AsyncIOMotorClient
from typing_extensions import TypedDict

from .card_validator import (
    CardValidationError,
    ChargeResult,
    detect_card_type,
    is_expired,
    is_valid_luhn,
)

logger = logging.getLogger("paymentservice.agent")

# ── Ollama LLM (mirrors sample: temperature=0 for deterministic auth) ─────────
llm = ChatOllama(model="llama3.2:3b", temperature=0.0, reasoning=False)


# ── MongoDB Configuration ─────────────────────────────────────────────────

MONGODB_URI = os.getenv("MONGODB_URI", "mongodb://localhost:27017")
MONGODB_DB = os.getenv("MONGODB_DB", "google_ms")

# Global client (lazy-initialized)
_mongodb_client: AsyncIOMotorClient = None


async def get_mongodb_client() -> AsyncIOMotorClient:
    """Get or create the MongoDB client."""
    global _mongodb_client
    if _mongodb_client is None:
        _mongodb_client = AsyncIOMotorClient(MONGODB_URI)
        # Verify connection
        await _mongodb_client.admin.command("ping")
        logger.info("Connected to MongoDB at %s", MONGODB_URI)
    return _mongodb_client


async def get_payment_transactions_collection():
    """Get the payment_transactions collection."""
    client = await get_mongodb_client()
    db = client[MONGODB_DB]
    collection = db["payment_transactions"]
    
    # Ensure indexes
    await collection.create_index("created_at")
    return collection

# ════════════════════════════════════════════════════════════════════════════
# Agent state
# ════════════════════════════════════════════════════════════════════════════

class PaymentAgentState(TypedDict):
    """
    Shared state threaded through every node in the graph.

    Input fields (set before ainvoke):
        card_number, card_cvv, exp_year, exp_month
        amount_currency_code, amount_units, amount_nanos

    Intermediate fields (written by nodes):
        card_type          – detected brand (Visa, MasterCard, …)
        last_four          – last 4 digits of sanitised card number
        validation_error   – set by validate_card on failure; None on success
        psp_tracking_id    – set by call_psp; None until that node runs
        transaction_id     – UUID generated alongside psp_tracking_id

    Output fields (written by payment_reasoning / reject_card):
        decision           – {"status": "SUCCESS"|"FAILED", "reason": str}

    Metrics (accumulated by payment_reasoning):
        total_input_tokens
        total_output_tokens
        total_llm_calls
    """
    # ── inputs ────────────────────────────────────────────────────────────────
    card_number:          str
    card_cvv:             int
    exp_year:             int
    exp_month:            int
    amount_currency_code: str
    amount_units:         int
    amount_nanos:         int

    # ── intermediate ──────────────────────────────────────────────────────────
    card_type:          Optional[str]
    last_four:          Optional[str]
    validation_error:   Optional[str]
    psp_tracking_id:    Optional[str]
    transaction_id:     Optional[str]

    # ── output ────────────────────────────────────────────────────────────────
    decision: Dict[str, Any]

    # ── metrics ───────────────────────────────────────────────────────────────
    total_input_tokens:  int
    total_output_tokens: int
    total_llm_calls:     int


# ════════════════════════════════════════════════════════════════════════════
# Node 1 – validate_card  (deterministic tool)
# ════════════════════════════════════════════════════════════════════════════

async def validate_card_node(state: PaymentAgentState) -> PaymentAgentState:
    """
    Deterministic tool node.

    Runs all card validations from card_validator.py:
      • Strip spaces/dashes
      • Luhn algorithm
      • Expiry check
      • CVV length check
      • Card type detection

    On success  → populates card_type, last_four; leaves validation_error=None.
    On failure  → sets validation_error; leaves card_type/last_four=None.
    """
    logger.info("[validate_card] starting validation | card_ending=%s",
                state["card_number"][-4:] if state["card_number"] else "????")

    card_number = state["card_number"].replace(" ", "").replace("-", "")

    try:
        if not card_number.isdigit():
            raise CardValidationError("Credit card info is invalid.")

        if not is_valid_luhn(card_number):
            raise CardValidationError("Credit card info is invalid.")

        if is_expired(state["exp_year"], state["exp_month"]):
            last_four = card_number[-4:]
            raise CardValidationError(
                f"The credit card (ending {last_four}) expired on "
                f"{state['exp_month']:02d}/{state['exp_year']}."
            )

        cvv_len = len(str(state["card_cvv"]))
        if cvv_len not in (3, 4):
            raise CardValidationError("Credit card CVV is invalid.")

        card_type = detect_card_type(card_number)
        last_four = card_number[-4:]

        logger.info("[validate_card] passed | type=%s last_four=%s",
                    card_type, last_four)

        return {
            **state,
            "card_type":        card_type,
            "last_four":        last_four,
            "validation_error": None,
        }

    except CardValidationError as exc:
        logger.warning("[validate_card] failed | reason=%s", exc)
        return {
            **state,
            "card_type":        None,
            "last_four":        card_number[-4:] if len(card_number) >= 4 else None,
            "validation_error": str(exc),
        }


# ════════════════════════════════════════════════════════════════════════════
# Conditional router after validate_card
# ════════════════════════════════════════════════════════════════════════════

def route_after_validation(state: PaymentAgentState) -> str:
    """
    Conditional edge function — returns the name of the next node.

    If validation_error is set → go to reject_card (skip PSP + LLM).
    Otherwise                  → go to call_psp.
    """
    if state.get("validation_error"):
        logger.info("[route] validation failed → reject_card")
        return "reject_card"
    logger.info("[route] validation passed → call_psp")
    return "call_psp"


# ════════════════════════════════════════════════════════════════════════════
# Node 2a – call_psp  (deterministic tool)
# ════════════════════════════════════════════════════════════════════════════

async def call_psp_node(state: PaymentAgentState) -> PaymentAgentState:
    """
    Deterministic tool node — simulates an external Payment Service Provider call.

    In a real system this would be an HTTPS call to Stripe / Adyen / Braintree.
    Here we simulate latency and generate a UUID tracking ID.

    Sets:
        psp_tracking_id – UUID from the PSP (non-null = PSP accepted the charge)
        transaction_id  – Internal transaction UUID (returned to caller)
    """
    logger.info("[call_psp] calling external PSP | amount=%s %d.%02d",
                state["amount_currency_code"],
                state["amount_units"],
                state["amount_nanos"] // 10_000_000)

    # Simulate PSP network latency (non-blocking)
    await asyncio.sleep(0.3)

    psp_tracking_id = str(uuid.uuid4())
    transaction_id  = str(uuid.uuid4())

    logger.info("[call_psp] PSP responded | psp_tracking_id=%s", psp_tracking_id)

    return {
        **state,
        "psp_tracking_id": psp_tracking_id,
        "transaction_id":  transaction_id,
    }


# ════════════════════════════════════════════════════════════════════════════
# Node 2b – reject_card  (deterministic shortcut for validation failures)
# ════════════════════════════════════════════════════════════════════════════

async def reject_card_node(state: PaymentAgentState) -> PaymentAgentState:
    """
    Deterministic shortcut node — reached only when validation_error is set.

    Bypasses PSP + LLM and directly sets decision=FAILED with the validation
    error as the reason.  Flows directly to persist_payment.
    """
    reason = state.get("validation_error", "Card validation failed.")
    logger.info("[reject_card] setting decision=FAILED | reason=%s", reason)

    return {
        **state,
        "psp_tracking_id": None,
        "transaction_id":  None,
        "decision": {
            "status": "FAILED",
            "reason": reason,
        },
    }


# ════════════════════════════════════════════════════════════════════════════
# Node 3 – payment_reasoning  (LLM / Ollama node)
# ════════════════════════════════════════════════════════════════════════════

def _parse_json_response(text: str) -> dict | None:
    """Extract the first JSON object from an LLM text response."""
    try:
        match = re.search(r"\{.*?\}", text, re.DOTALL)
        if match:
            return json.loads(match.group(0))
    except Exception as exc:
        logger.error("[payment_reasoning] JSON parse error: %s — raw: %s", exc, text)
    return None


async def payment_reasoning_node(state: PaymentAgentState) -> PaymentAgentState:
    """
    LLM node — the only non-deterministic step in the graph.

    Provides the LLM with:
      • PSP tracking ID (non-null = PSP accepted the charge)
      • Card brand and last four digits
      • Charge amount

    The LLM must return a JSON decision:
        { "status": "SUCCESS" | "FAILED", "reason": "<brief explanation>" }

    Rule embedded in the prompt (mirrors sample code):
      If PSP_TRACKING_ID is not null → status should be SUCCESS.
      If PSP_TRACKING_ID is null     → status should be FAILED.

    Token usage is accumulated in state for observability.
    """
    cents = state["amount_nanos"] // 10_000_000

    prompt = f"""
You are a payment authorization agent for an e-commerce platform.

Your task is to make a final authorization decision for a payment transaction.

Rules:
- Status MUST be either "SUCCESS" or "FAILED".
- If PSP_TRACKING_ID is not null, the payment was accepted by the PSP → status should be SUCCESS.
- If PSP_TRACKING_ID is null, the PSP rejected or did not respond → status should be FAILED.
- Only if the status is FAILED, include a brief, professional reason for your decision
- Return ONLY valid JSON. No markdown, no code blocks, no preamble.

Output schema:
{{
  "status": "SUCCESS" | "FAILED",
  "reason": "<one sentence explanation>" // optional, only for FAILED
}}

Transaction details:
  PSP_TRACKING_ID : {state["psp_tracking_id"]}
  CARD_TYPE       : {state.get("card_type", "Unknown")}
  CARD_LAST_FOUR  : {state.get("last_four", "????") }
  AMOUNT          : {state["amount_currency_code"]} {state["amount_units"]}.{cents:02d}
""".strip()

    logger.info("[payment_reasoning] invoking LLM | psp_tracking_id=%s",
                state["psp_tracking_id"])

    response = await asyncio.to_thread(llm.invoke, prompt)

    raw        = response.text()
    in_tokens  = response.usage_metadata.get("input_tokens",  0)
    out_tokens = response.usage_metadata.get("output_tokens", 0)

    logger.info("[payment_reasoning] LLM raw response: %s", raw)
    logger.info("[payment_reasoning] tokens | in=%d out=%d", in_tokens, out_tokens)

    decision = _parse_json_response(raw)

    if not decision or decision.get("status") not in ("SUCCESS", "FAILED"):
        logger.error("[payment_reasoning] invalid decision: %s", raw)
        raise ValueError(f"Invalid payment decision from LLM: {raw!r}")

    logger.info("[payment_reasoning] decision=%s reason=%s",
                decision["status"], decision.get("reason", ""))

    return {
        **state,
        "decision":           decision,
        "total_input_tokens":  state["total_input_tokens"]  + in_tokens,
        "total_output_tokens": state["total_output_tokens"] + out_tokens,
        "total_llm_calls":     state["total_llm_calls"]     + 1,
    }


# ════════════════════════════════════════════════════════════════════════════
# Node 4 – persist_payment  (deterministic tool)
# ════════════════════════════════════════════════════════════════════════════



async def persist_payment_node(state: PaymentAgentState) -> PaymentAgentState:
    """
    Deterministic tool node — persists the payment result to MongoDB.

    Runs for both SUCCESS and FAILED outcomes so every charge attempt is audited.

    Document schema:
        order_id (= transaction_id or "validation-failed-<uuid>")
        status                 – "SUCCESS" | "FAILED"
        card_type              – detected brand
        last_four              – masked card digits
        amount                 – { currency_code, units, nanos, formatted }
        psp_tracking_id        – UUID from PSP or null
        transaction_id         – internal UUID or null
        decision               – LLM decision dict
        llm_metrics            – { input_tokens, output_tokens, llm_calls }
        validation_error       – set if card was rejected before PSP
        created_at             – UTC timestamp
    """
    status       = state["decision"].get("status", "FAILED")
    cents        = state["amount_nanos"] // 10_000_000
    record_id    = state.get("transaction_id") or f"validation-failed-{uuid.uuid4()}"
    collection = await get_payment_transactions_collection()
    doc = {
        "_id":       record_id,
        "status":          status,
        "card_type":       state.get("card_type"),
        "last_four":       state.get("last_four"),
        "amount": {
            "currency_code": state["amount_currency_code"],
            "units":         state["amount_units"],
            "nanos":         state["amount_nanos"],
            "formatted":     f"{state['amount_currency_code']} {state['amount_units']}.{cents:02d}",
        },
        "psp_tracking_id":  state.get("psp_tracking_id"),
        "transaction_id":   state.get("transaction_id"),
        "decision":         state["decision"],
        "llm_metrics": {
            "input_tokens":  state["total_input_tokens"],
            "output_tokens": state["total_output_tokens"],
            "llm_calls":     state["total_llm_calls"],
        },
        "validation_error": state.get("validation_error"),
        "created_at":       datetime.datetime.now(tz=datetime.timezone.utc),
    }

    logger.info("[persist_payment] persisting | status=%s record_id=%s", status, record_id)

    try:
        await collection.insert_one(doc)
        logger.info("[persist_payment] persisted successfully | record_id=%s", record_id)
    except Exception as exc:
        # Non-fatal — never block the gRPC response for a DB write failure
        logger.error("[persist_payment] MongoDB write failed (non-fatal): %s", exc)
    return state


# ════════════════════════════════════════════════════════════════════════════
# Graph assembly
# ════════════════════════════════════════════════════════════════════════════

def build_payment_agent() -> Any:
    """
    Assemble and compile the LangGraph payment agent.

    Returns the compiled graph, ready for ainvoke().
    """
    graph = StateGraph(PaymentAgentState)

    # Register nodes
    graph.add_node("validate_card",       validate_card_node)
    graph.add_node("call_psp",            call_psp_node)
    graph.add_node("reject_card",         reject_card_node)
    graph.add_node("payment_reasoning",   payment_reasoning_node)
    graph.add_node("persist_payment",     persist_payment_node)

    # Entry point
    graph.set_entry_point("validate_card")

    # Conditional edge after validation
    graph.add_conditional_edges(
        "validate_card",
        route_after_validation,
        {
            "call_psp":    "call_psp",
            "reject_card": "reject_card",
        },
    )

    # Happy path: psp → reason → persist
    graph.add_edge("call_psp",          "payment_reasoning")
    graph.add_edge("payment_reasoning", "persist_payment")

    # Rejection shortcut: reject → persist
    graph.add_edge("reject_card", "persist_payment")

    # Terminal
    graph.add_edge("persist_payment", END)

    compiled = graph.compile()
    logger.info("[PaymentAgent] graph compiled successfully")
    return compiled


# Singleton graph — built at import time
payment_graph = build_payment_agent()


# ════════════════════════════════════════════════════════════════════════════
# Public helper called by the gRPC servicer
# ════════════════════════════════════════════════════════════════════════════

async def run_payment_agent(
    card_number:          str,
    card_cvv:             int,
    exp_year:             int,
    exp_month:            int,
    amount_currency_code: str,
    amount_units:         int,
    amount_nanos:         int,
) -> PaymentAgentState:
    """
    Build initial state and invoke the compiled graph.

    Returns the final PaymentAgentState after all nodes have run.
    Raises ValueError if the LLM returns an unparseable decision.
    """
    initial_state: PaymentAgentState = {
        # inputs
        "card_number":          card_number,
        "card_cvv":             card_cvv,
        "exp_year":             exp_year,
        "exp_month":            exp_month,
        "amount_currency_code": amount_currency_code,
        "amount_units":         amount_units,
        "amount_nanos":         amount_nanos,
        # intermediate
        "card_type":          None,
        "last_four":          None,
        "validation_error":   None,
        "psp_tracking_id":    None,
        "transaction_id":     None,
        # output
        "decision": {},
        # metrics
        "total_input_tokens":  0,
        "total_output_tokens": 0,
        "total_llm_calls":     0,
    }

    logger.info(
        "[run_payment_agent] invoking graph | currency=%s amount=%d.%02d",
        amount_currency_code, amount_units, amount_nanos // 10_000_000,
    )

    result: PaymentAgentState = await payment_graph.ainvoke(initial_state)

    logger.info(
        "[run_payment_agent] completed | status=%s transaction_id=%s llm_calls=%d",
        result["decision"].get("status"),
        result.get("transaction_id"),
        result["total_llm_calls"],
    )

    return result
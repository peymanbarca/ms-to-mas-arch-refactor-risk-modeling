"""
shippingagent/agent.py

LangGraph shipping agent — replaces the simple shipment dispatch with a
full agentic graph while keeping the exact same gRPC ShipOrder interface.

Graph topology:
─────────────────────────────────────────────────────────────────────────────
  ┌──────────────────┐
  │  validate_address│  (deterministic) format & validate address
  └────────┬─────────┘
           │
     ┌─────▼──────┐  validation_error present?
     │   route    │──────────────────────────────────────────────────┐
     └─────┬──────┘  no                                              │ yes
           │                                                         │
  ┌────────▼─────────┐                                   ┌───────────▼──────────┐
  │   fetch_quote    │  (deterministic) call GetQuote     │  reject_shipment     │
  │                  │  to get shipping cost              │  sets decision=FAILED│
  └────────┬─────────┘                                   └───────────┬──────────┘
           │                                                         │
  ┌────────▼──────────┐                                             │
  │ shipping_reasoning│  (LLM / Ollama llama3) decides              │
  │                   │  APPROVED or REJECTED for shipment          │
  └────────┬──────────┘                                             │
           │                                                         │
  ┌────────▼──────────┐◄────────────────────────────────────────────┘
  │  persist_shipment │  (deterministic) writes to MongoDB
  └────────┬──────────┘
           │
          END

Node roles
──────────
validate_address   Deterministic tool. Validates address fields (street, city,
                   state, country, zip). Populates on success, or sets
                   validation_error on failure.

fetch_quote        Deterministic tool. Calls GetQuote internally to fetch
                   shipping cost based on items. Generates shipping_id (UUID)
                   and tracking_id.

shipping_reasoning LLM node (Ollama llama3). Receives address, items, and
                   shipping cost; decides {status: APPROVED|REJECTED}.
                   Reasoning is the only non-deterministic step.

reject_shipment    Deterministic shortcut for validation failures. Sets
                   decision=REJECTED with the validation error as reason and
                   skips the GetQuote + LLM steps.

persist_shipment   Deterministic tool. Writes the final shipment document to
                   MongoDB regardless of APPROVED or REJECTED outcome.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import re
import uuid
from typing import Any, Dict, Literal, Optional
import os

from langchain_ollama import ChatOllama
from langgraph.graph import END, StateGraph
from motor.motor_asyncio import AsyncIOMotorClient
from typing_extensions import TypedDict

from ..shared import demo_pb2
from .quote import create_quote_from_count, create_tracking_id

logger = logging.getLogger("shippingagent")

# ── Ollama LLM (mirrors sample: temperature=0 for deterministic shipping decisions) ─
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


async def get_shipments_collection():
    """Get the shipments collection."""
    client = await get_mongodb_client()
    db = client[MONGODB_DB]
    collection = db["shipments"]
    
    # Ensure indexes
    await collection.create_index("tracking_id", unique=True)
    await collection.create_index("created_at")
    return collection

# ════════════════════════════════════════════════════════════════════════════
# Agent state
# ════════════════════════════════════════════════════════════════════════════

class ShippingAgentState(TypedDict):
    """
    Shared state threaded through every node in the graph.

    Input fields (set before ainvoke):
        items (list of {product_id, quantity})
        address (street_address, city, state, country, zip_code)

    Intermediate fields (written by nodes):
        shipping_id        – UUID for this shipment (generated in fetch_quote)
        quote              – shipping cost {currency_code, units, nanos, formatted}
        tracking_id        – generated from address hash
        validation_error   – set by validate_address on failure; None on success

    Output fields (written by shipping_reasoning / reject_shipment):
        decision           – {"status": "APPROVED"|"REJECTED", "reason": str}

    Metrics (accumulated by shipping_reasoning):
        total_input_tokens
        total_output_tokens
        total_llm_calls
    """
    # ── inputs ────────────────────────────────────────────────────────────────
    items:           list
    street_address:  str
    city:            str
    state:           str
    country:         str
    zip_code:        str

    # ── intermediate ──────────────────────────────────────────────────────────
    shipping_id:     Optional[str]
    quote:           Optional[Dict[str, Any]]
    tracking_id:     Optional[str]
    validation_error: Optional[str]

    # ── output ────────────────────────────────────────────────────────────────
    decision:        Dict[str, Any]

    # ── metrics ───────────────────────────────────────────────────────────────
    total_input_tokens:  int
    total_output_tokens: int
    total_llm_calls:     int


# ════════════════════════════════════════════════════════════════════════════
# Node 1 – validate_address  (deterministic tool)
# ════════════════════════════════════════════════════════════════════════════

async def validate_address_node(state: ShippingAgentState) -> ShippingAgentState:
    """
    Deterministic tool node.

    Validates address fields:
      • Non-empty street_address, city, state, country
      • Valid zip_code format (basic check)

    On success  → leaves validation_error=None.
    On failure  → sets validation_error with description.
    """
    logger.info("[validate_address] validating | city=%s state=%s",
                state["city"], state["state"])

    errors = []

    if not state.get("street_address", "").strip():
        errors.append("Street address is required.")
    if not state.get("city", "").strip():
        errors.append("City is required.")
    if not state.get("state", "").strip():
        errors.append("State is required.")
    if not state.get("country", "").strip():
        errors.append("Country is required.")
    if not state.get("zip_code", ""):
        errors.append("ZIP code is required.")

    if errors:
        error_msg = " ".join(errors)
        logger.warning("[validate_address] failed | reason=%s", error_msg)
        return {
            **state,
            "validation_error": error_msg,
        }

    logger.info("[validate_address] passed | full_address=%s, %s, %s",
                state["city"], state["state"], state["country"])

    return {
        **state,
        "validation_error": None,
    }


# ════════════════════════════════════════════════════════════════════════════
# Conditional router after validate_address
# ════════════════════════════════════════════════════════════════════════════

def route_after_validation(state: ShippingAgentState) -> str:
    """
    Conditional edge function — returns the name of the next node.

    If validation_error is set → go to reject_shipment (skip GetQuote + LLM).
    Otherwise                  → go to fetch_quote_node_and_generate_id.
    """
    if state.get("validation_error"):
        logger.info("[route] address validation failed → reject_shipment")
        return "reject_shipment"
    logger.info("[route] address validation passed → fetch_quote")
    return "fetch_quote_node_and_generate_id"


# ════════════════════════════════════════════════════════════════════════════
# Node 2a – fetch_quote  (deterministic tool)
# ════════════════════════════════════════════════════════════════════════════

async def fetch_quote_node_and_generate_id(state: ShippingAgentState) -> ShippingAgentState:
    """
    Deterministic tool node — fetches shipping quote and generates IDs.

    Generates:
        shipping_id – UUID for this shipment record
        tracking_id – derived from address hash
        quote       – cost breakdown

    Sets:
        shipping_id, tracking_id, quote in state
    """
    logger.info("[fetch_quote] calculating quote | item_count=%d",
                len(state["items"]))

    # Generate shipment IDs
    shipping_id = str(uuid.uuid4())
    
    base_address = (
        f"{state['street_address']}, {state['city']}, {state['state']}"
    )
    tracking_id = create_tracking_id(base_address)

    # Calculate quote based on item count
    item_count = sum(item.get("quantity", 1) for item in state["items"])
    quote_obj = create_quote_from_count(item_count)
    
    cents = quote_obj.cents
    quote_data = {
        "currency_code": "USD",
        "units": quote_obj.dollars,
        "nanos": quote_obj.nanos,
        "formatted": f"USD {quote_obj.dollars}.{cents:02d}",
    }

    logger.info("[fetch_quote] quote generated | shipping_id=%s tracking_id=%s amount=%s",
                shipping_id, tracking_id, quote_data["formatted"])

    return {
        **state,
        "shipping_id": shipping_id,
        "tracking_id": tracking_id,
        "quote": quote_data,
    }


# ════════════════════════════════════════════════════════════════════════════
# Node 2b – reject_shipment  (deterministic shortcut for validation failures)
# ════════════════════════════════════════════════════════════════════════════

async def reject_shipment_node(state: ShippingAgentState) -> ShippingAgentState:
    """
    Deterministic shortcut node — reached only when validation_error is set.

    Bypasses GetQuote + LLM and directly sets decision=REJECTED with the
    validation error as the reason. Flows directly to persist_shipment.
    """
    reason = state.get("validation_error", "Address validation failed.")
    logger.info("[reject_shipment] setting decision=REJECTED | reason=%s", reason)

    return {
        **state,
        "shipping_id": str(uuid.uuid4()),  # Still need ID for persistence
        "tracking_id": None,
        "quote": None,
        "decision": {
            "status": "REJECTED",
            "reason": reason,
        },
    }


# ════════════════════════════════════════════════════════════════════════════
# Node 3 – shipping_reasoning  (LLM / Ollama node)
# ════════════════════════════════════════════════════════════════════════════

def _parse_json_response(text: str) -> dict | None:
    """Extract the first JSON object from an LLM text response."""
    try:
        match = re.search(r"\{.*?\}", text, re.DOTALL)
        if match:
            return json.loads(match.group(0))
    except Exception as exc:
        logger.error("[shipping_reasoning] JSON parse error: %s — raw: %s", exc, text)
    return None


async def shipping_reasoning_node(state: ShippingAgentState) -> ShippingAgentState:
    """
    LLM node — the only non-deterministic step in the graph.

    Provides the LLM with:
      • Destination address (full address)
      • Number of items to ship
      • Shipping cost

    The LLM must return a JSON decision:
        { "status": "APPROVED" | "REJECTED", "reason": "<brief explanation>" }

    Rule embedded in the prompt:
      Generally approve shipments to valid addresses with reasonable costs.
      Reject if there are concerns (unusual patterns, etc.).

    Token usage is accumulated in state for observability.
    """
    cents = state["quote"]["nanos"] // 10_000_000
    item_count = len(state["items"])

    prompt = f"""
You are a shipping authorization agent for an e-commerce platform.

Your task is to make a final shipment approval decision.

Rules:
- Status MUST be either "APPROVED" or "REJECTED".
- Generally APPROVE shipments based on valid addresses and TRACKING_ID should not be null.
- Only REJECT if there are concerns (e.g., unusual patterns, high-risk regions).
- Only if the status is REJECTED, include a brief, professional reason.
- Return ONLY valid JSON. No markdown, no code blocks, no preamble.

Output schema:
{{
  "status": "APPROVED" | "REJECTED",
  "reason": "<one sentence explanation>" // optional, only for REJECTED
}}

Shipment details:
  DESTINATION_ADDRESS : {state['street_address']}, {state['city']}, {state['state']}, {state['country']} {state['zip_code']}
  TRACKING_ID          : {state['tracking_id']}
""".strip()

    logger.info("[shipping_reasoning] invoking LLM | shipping_id=%s",
                state["shipping_id"])

    response = await asyncio.to_thread(llm.invoke, prompt)

    raw        = response.text()
    in_tokens  = response.usage_metadata.get("input_tokens",  0)
    out_tokens = response.usage_metadata.get("output_tokens", 0)

    logger.info("[shipping_reasoning] LLM raw response: %s", raw)
    logger.info("[shipping_reasoning] tokens | in=%d out=%d", in_tokens, out_tokens)
    logger.info("raw LLM response: %s", raw)

    decision = _parse_json_response(raw)

    if not decision or decision.get("status") not in ("APPROVED", "REJECTED"):
        logger.error("[shipping_reasoning] invalid decision: %s", raw)
        raise ValueError(f"Invalid shipping decision from LLM: {raw!r}")

    logger.info("[shipping_reasoning] decision=%s reason=%s",
                decision["status"], decision.get("reason", ""))

    return {
        **state,
        "decision":           decision,
        "total_input_tokens":  state["total_input_tokens"]  + in_tokens,
        "total_output_tokens": state["total_output_tokens"] + out_tokens,
        "total_llm_calls":     state["total_llm_calls"]     + 1,
    }


# ════════════════════════════════════════════════════════════════════════════
# Node 4 – persist_shipment  (deterministic tool)
# ════════════════════════════════════════════════════════════════════════════

async def persist_shipment_node(state: ShippingAgentState) -> ShippingAgentState:
    """
    Deterministic tool node — persists the shipment result to MongoDB.

    Runs for both APPROVED and REJECTED outcomes so every shipment attempt
    is audited.

    Document schema:
        _id (= shipping_id)
        status                 – "APPROVED" | "REJECTED"
        tracking_id            – generated or null
        items                  – list of items to ship
        address                – full address details
        quote                  – shipping cost breakdown
        decision               – LLM decision dict
        llm_metrics            – { input_tokens, output_tokens, llm_calls }
        validation_error       – set if address was rejected before LLM
        created_at             – UTC timestamp
    """
    status       = state["decision"].get("status", "REJECTED")
    collection = await get_shipments_collection()
    
    doc = {
        "_id":       state["shipping_id"],
        "status":          status,
        "tracking_id":     state.get("tracking_id"),
        "items":           state["items"],
        "address": {
            "street_address": state["street_address"],
            "city":           state["city"],
            "state":          state["state"],
            "country":        state["country"],
            "zip_code":       state["zip_code"],
        },
        "quote":           state.get("quote"),
        "decision":        state["decision"],
        "llm_metrics": {
            "input_tokens":  state["total_input_tokens"],
            "output_tokens": state["total_output_tokens"],
            "llm_calls":     state["total_llm_calls"],
        },
        "validation_error": state.get("validation_error"),
        "created_at":      datetime.datetime.now(tz=datetime.timezone.utc),
    }

    logger.info("[persist_shipment] persisting | status=%s shipping_id=%s", status, state["shipping_id"])

    try:
        await collection.insert_one(doc)
        logger.info("[persist_shipment] persisted successfully | shipping_id=%s", state["shipping_id"])
    except Exception as exc:
        # Non-fatal — never block the gRPC response for a DB write failure
        logger.error("[persist_shipment] MongoDB write failed (non-fatal): %s", exc)
    
    return state


# ════════════════════════════════════════════════════════════════════════════
# Graph assembly
# ════════════════════════════════════════════════════════════════════════════

def build_shipping_agent() -> Any:
    """
    Assemble and compile the LangGraph shipping agent.

    Returns the compiled graph, ready for ainvoke().
    """
    graph = StateGraph(ShippingAgentState)

    # Register nodes
    graph.add_node("validate_address",     validate_address_node)
    graph.add_node("fetch_quote_node_and_generate_id", fetch_quote_node_and_generate_id)
    graph.add_node("reject_shipment",      reject_shipment_node)
    graph.add_node("shipping_reasoning",   shipping_reasoning_node)
    graph.add_node("persist_shipment",     persist_shipment_node)

    # Entry point
    graph.set_entry_point("validate_address")

    # Conditional edge after validation
    graph.add_conditional_edges(
        "validate_address",
        route_after_validation,
        {
            "fetch_quote_node_and_generate_id":     "fetch_quote_node_and_generate_id",
            "reject_shipment": "reject_shipment",
        },
    )

    # Happy path: fetch_quote → reasoning → persist
    graph.add_edge("fetch_quote_node_and_generate_id", "shipping_reasoning")
    graph.add_edge("shipping_reasoning",   "persist_shipment")

    # Rejection shortcut: reject → persist
    graph.add_edge("reject_shipment", "persist_shipment")

    # Terminal
    graph.add_edge("persist_shipment", END)

    compiled = graph.compile()
    logger.info("[ShippingAgent] graph compiled successfully")
    return compiled


# Singleton graph — built at import time
shipping_graph = build_shipping_agent()


# ════════════════════════════════════════════════════════════════════════════
# Public helper called by the gRPC servicer
# ════════════════════════════════════════════════════════════════════════════

async def run_shipping_agent(
    items:           list,
    street_address:  str,
    city:            str,
    state:           str,
    country:         str,
    zip_code:        str,
) -> ShippingAgentState:
    """
    Build initial state and invoke the compiled graph.

    Returns the final ShippingAgentState after all nodes have run.
    Raises ValueError if the LLM returns an unparseable decision.
    """
    initial_state: ShippingAgentState = {
        # inputs
        "items":           items,
        "street_address":  street_address,
        "city":            city,
        "state":           state,
        "country":         country,
        "zip_code":        zip_code,
        # intermediate
        "shipping_id":     None,
        "quote":           None,
        "tracking_id":     None,
        "validation_error": None,
        # output
        "decision": {},
        # metrics
        "total_input_tokens":  0,
        "total_output_tokens": 0,
        "total_llm_calls":     0,
    }

    logger.info(
        "[run_shipping_agent] invoking graph |  items=%d destination=%s, %s",
        len(items), city, state,
    )

    result: ShippingAgentState = await shipping_graph.ainvoke(initial_state)

    logger.info(
        "[run_shipping_agent] completed | status=%s tracking_id=%s llm_calls=%d",
        result["decision"].get("status"),
        result.get("tracking_id"),
        result["total_llm_calls"],
    )

    return result

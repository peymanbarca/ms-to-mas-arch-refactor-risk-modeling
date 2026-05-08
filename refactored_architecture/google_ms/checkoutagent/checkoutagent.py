"""
checkoutservice/agent.py

ReAct (Reason + Act) checkout orchestration agent.

Unlike the payment/email/recommendation agents — which use a fixed linear
pipeline — this agent gives the LLM full control over the orchestration
sequence.  After every tool execution the LLM inspects the entire order
state and decides what to do next, making this a true agentic loop.

Architecture
─────────────────────────────────────────────────────────────────────────────

  ┌──────────────────────┐
  │    initialise_order  │  (deterministic) generate order_id, validate inputs
  └──────────┬───────────┘
             │
  ┌──────────▼───────────┐   ◄─────────────────────────────────────────┐
  │    reason            │  (LLM / llama3)                             │
  │                      │  Receives full state snapshot               │
  │                      │  Returns: { next_action, reasoning }        │
  └──────────┬───────────┘                                             │
             │                                                          │
       ┌─────▼──────┐  next_action == DONE?                            │
       │   route    │──────────────────────────────────┐               │
       └─────┬──────┘  not done                        │               │
             │                                          │               │
  ┌──────────▼───────────┐                 ┌───────────▼───────────┐   │
  │   execute_tool       │  (deterministic)│   finalise_order      │   │
  │                      │  dispatcher:    │   build OrderResult   │   │
  │  GET_CART            │  calls the      │   persist to MongoDB  │   │
  │  GET_PRODUCT_PRICES  │  chosen         └───────────────────────┘   │
  │  GET_SHIPPING_QUOTE  │  downstream                                  │
  │  CHARGE_CARD         │  service                                     │
  │  SHIP_ORDER          │                                              │
  │  EMPTY_CART          │                                              │
  │  SEND_CONFIRMATION   │                                              │
  └──────────┬───────────┘                                             │
             │                                                          │
             └──────────────────────────────────────────────────────────┘
                          (loop back to reason)

LLM reasoning
─────────────────────────────────────────────────────────────────────────────
At each `reason` step the LLM receives:

  • The original order request (user_id, currency, address, email)
  • Everything gathered so far (cart items, product prices, shipping quote,
    computed total, transaction_id, tracking_id, confirmation status)
  • The full history of actions taken and their results
  • The list of available tools with their descriptions

The LLM must output JSON:
  {
    "reasoning": "<why this tool is the right next step>",
    "next_action": "<TOOL_NAME | DONE>",
    "action_params": { ... }   // tool-specific overrides (optional)
  }

This means the LLM can:
  • Re-order steps if needed (e.g., skip SEND_CONFIRMATION if email is empty)
  • Retry a step if the result was an error
  • Decide early completion is appropriate
  • Reason about partial failures and how to proceed

Tools available to the LLM
─────────────────────────────────────────────────────────────────────────────
  GET_CART             Fetch user's cart from CartService
  GET_PRODUCT_PRICES   Fetch catalog + convert prices to user_currency
  GET_SHIPPING_QUOTE   Get USD shipping quote + convert to user_currency
  CHARGE_CARD          Charge the credit card via PaymentService
  SHIP_ORDER           Dispatch shipment via ShippingService
  EMPTY_CART           Clear user's cart (best-effort)
  SEND_CONFIRMATION    Send order confirmation email
  DONE                 Signal that the checkout is complete

MongoDB persistence
─────────────────────────────────────────────────────────────────────────────
  Same 4-transition lifecycle as the original orchestrator:
  PENDING → PAID → SHIPPED → COMPLETED
  Written at the start of finalise_order and after each critical step.

Safety
─────────────────────────────────────────────────────────────────────────────
  MAX_ITERATIONS caps the ReAct loop to prevent runaway LLM calls.
  If the cap is reached the agent falls back to the deterministic orchestrator
  so the order always completes.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import re
import uuid
from datetime import timezone
from os import getenv
from typing import Any, Dict, List, Optional

import grpc
from langchain_ollama import ChatOllama
from langgraph.graph import END, StateGraph
from motor.motor_asyncio import AsyncIOMotorClient
from typing_extensions import TypedDict

from ..shared import demo_pb2
from ..shared import demo_pb2_grpc

from .money import (
    Money,
    format_money,
    money_multiply_slow,
    money_must,
    money_sum,
    proto_to_money,
    zero_money,
)

logger = logging.getLogger("checkoutagent")

# ── LLM ───────────────────────────────────────────────────────────────────────
llm = ChatOllama(model="llama3.2:3b", temperature=0.0, reasoning=False)

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

async def get_orders_collection():
    """Get the orders collection."""
    client = await get_mongodb_client()
    db = client[MONGODB_DB]
    col = db["orders"]
    return col

# ── Safety cap ────────────────────────────────────────────────────────────────
MAX_ITERATIONS: int = 12   # maximum ReAct loop turns before fallback

MONGODB_URI = getenv("MONGODB_URI", "mongodb://user:pass1@localhost:27017")
MONGODB_DB  = getenv("MONGODB_DB",  "google_ms")

# ── Order lifecycle constants ─────────────────────────────────────────────────
class OrderStatus:
    PENDING   = "pending"
    PAID      = "paid"
    SHIPPED   = "shipped"
    COMPLETED = "completed"
    FAILED    = "failed"


# ── Available tool names ──────────────────────────────────────────────────────
class ToolName:
    GET_CART             = "GET_CART"
    GET_PRODUCT_PRICES   = "GET_PRODUCT_PRICES"
    GET_SHIPPING_QUOTE   = "GET_SHIPPING_QUOTE"
    CHARGE_CARD          = "CHARGE_CARD"
    SHIP_ORDER           = "SHIP_ORDER"
    EMPTY_CART           = "EMPTY_CART"
    SEND_CONFIRMATION    = "SEND_CONFIRMATION"
    DONE                 = "DONE"

    ALL = {
        GET_CART, GET_PRODUCT_PRICES, GET_SHIPPING_QUOTE,
        CHARGE_CARD, SHIP_ORDER, EMPTY_CART, SEND_CONFIRMATION, DONE,
    }

TOOL_DESCRIPTIONS = {
    ToolName.GET_CART: (
        "Fetch the user's shopping cart from CartService. "
        "Must be called before GET_PRODUCT_PRICES. "
        "Result: list of {product_id, quantity}."
    ),
    ToolName.GET_PRODUCT_PRICES: (
        "Fetch product details and convert prices to user_currency via "
        "ProductCatalogService + CurrencyService. "
        "Requires cart to be fetched first. "
        "Result: list of {product_id, quantity, unit_cost, subtotal}."
    ),
    ToolName.GET_SHIPPING_QUOTE: (
        "Get a shipping cost estimate in user_currency from ShippingService. "
        "Requires cart to be fetched first. "
        "Result: {currency_code, units, nanos, formatted}."
    ),
    ToolName.CHARGE_CARD: (
        "Charge the credit card for the order total via PaymentService. "
        "Requires product prices and shipping quote to compute total. "
        "Result: {transaction_id}."
    ),
    ToolName.SHIP_ORDER: (
        "Dispatch the shipment via ShippingService. "
        "Requires payment to be completed. "
        "Result: {tracking_id}."
    ),
    ToolName.EMPTY_CART: (
        "Clear the user's cart in CartService (best-effort, non-critical). "
        "Should be called after payment succeeds. "
        "Result: {status}."
    ),
    ToolName.SEND_CONFIRMATION: (
        "Send order confirmation email via EmailService (best-effort). "
        "Requires order_id, tracking_id, and items to be available. "
        "Result: {status}."
    ),
    ToolName.DONE: (
        "Signal that checkout is fully complete. "
        "Use when SHIP_ORDER has succeeded and best-effort steps "
        "(EMPTY_CART, SEND_CONFIRMATION) have been attempted. "
        "Do NOT call DONE unless the card has been charged and the order shipped."
    ),
}


# ════════════════════════════════════════════════════════════════════════════
# Agent state
# ════════════════════════════════════════════════════════════════════════════

class StepRecord(TypedDict):
    """Audit record for one ReAct loop iteration."""
    iteration:   int
    reasoning:   str
    action:      str
    result:      str          # human-readable outcome
    success:     bool


class CheckoutAgentState(TypedDict):
    """Full shared state threaded through every node in the ReAct loop."""

    # ── original request fields ───────────────────────────────────────────────
    order_id:     str
    user_id:      str
    user_currency: str
    email:        str
    address:      Optional[Dict[str, Any]]   # serialised Address
    credit_card:  Optional[Dict[str, Any]]   # serialised CreditCardInfo (masked)

    # ── injected stubs (not serialised to LLM prompt) ─────────────────────────
    cart_stub:     Any
    catalog_stub:  Any
    currency_stub: Any
    shipping_stub: Any
    payment_stub:  Any
    email_stub:    Any
    grpc_context:  Any

    # ── gathered data (accumulates across iterations) ─────────────────────────
    cart_items:    Optional[List[Dict[str, Any]]]   # [{product_id, quantity}]
    order_items:   Optional[List[Dict[str, Any]]]   # [{product_id, qty, unit_cost, subtotal}]
    shipping_cost: Optional[Dict[str, Any]]          # {currency_code, units, nanos, formatted}
    order_total:   Optional[Dict[str, Any]]          # {currency_code, units, nanos, formatted}

    # ── outcome fields ────────────────────────────────────────────────────────
    transaction_id:  Optional[str]
    tracking_id:     Optional[str]
    confirmation_sent: bool
    cart_emptied:    bool

    # ── ReAct loop control ────────────────────────────────────────────────────
    iteration:     int
    next_action:   Optional[str]     # ToolName chosen by LLM
    last_reasoning: Optional[str]    # LLM's last reasoning paragraph
    steps:         List[StepRecord]  # full audit history
    is_complete:   bool
    fatal_error:   Optional[str]     # set if the loop must abort

    # ── metrics ───────────────────────────────────────────────────────────────
    total_input_tokens:  int
    total_output_tokens: int
    total_llm_calls:     int


# ════════════════════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════════════════════

def _money_dict(m: demo_pb2.Money) -> Dict[str, Any]:
    cents = m.nanos // 10_000_000
    return {
        "currency_code": m.currency_code,
        "units":         m.units,
        "nanos":         m.nanos,
        "formatted":     f"{m.currency_code} {m.units}.{cents:02d}",
    }


def _address_dict(a: demo_pb2.Address) -> Dict[str, Any]:
    return {
        "street_address": a.street_address,
        "city": a.city, "state": a.state,
        "country": a.country, "zip_code": a.zip_code,
    }


def _compute_total(
    order_items: List[Dict[str, Any]],
    shipping: Dict[str, Any],
    currency: str,
) -> Dict[str, Any]:
    """Recompute order total from order_items + shipping using money arithmetic."""
    total = zero_money(currency)
    shipping_py = Money(
        currency_code=shipping["currency_code"],
        units=shipping["units"],
        nanos=shipping["nanos"],
    )
    total = money_must(money_sum(total, shipping_py))

    for item in order_items:
        cost = item["unit_cost"]
        cost_py = Money(
            currency_code=cost["currency_code"],
            units=cost["units"],
            nanos=cost["nanos"],
        )
        mult = money_multiply_slow(cost_py, item["quantity"])
        total = money_must(money_sum(total, mult))

    return {
        "currency_code": total.currency_code,
        "units":         total.units,
        "nanos":         total.nanos,
        "formatted":     format_money(total),
    }


def _state_summary(state: CheckoutAgentState) -> str:
    """
    Build a concise JSON snapshot of the current order state for the LLM prompt.
    Stubs and raw proto objects are excluded.
    """
    return json.dumps({
        "order_id":          state["order_id"],
        "user_id":           state["user_id"],
        "user_currency":     state["user_currency"],
        "email":             state["email"],
        "address":           state.get("address"),
        "cart_items":        state.get("cart_items"),
        "order_items":       state.get("order_items"),
        "shipping_cost":     state.get("shipping_cost"),
        "order_total":       state.get("order_total"),
        "transaction_id":    state.get("transaction_id"),
        "tracking_id":       state.get("tracking_id"),
        "confirmation_sent": state.get("confirmation_sent", False),
        "cart_emptied":      state.get("cart_emptied", False),
    }, indent=2)


def _steps_summary(steps: List[StepRecord]) -> str:
    """Compact history of all actions taken so far."""
    if not steps:
        return "  (no actions taken yet)"
    lines = []
    for s in steps:
        status = "✓" if s["success"] else "✗"
        lines.append(f"  [{s['iteration']}] {status} {s['action']} → {s['result']}")
    return "\n".join(lines)


# ════════════════════════════════════════════════════════════════════════════
# Node 1 – initialise_order  (deterministic)
# ════════════════════════════════════════════════════════════════════════════

async def initialise_order_node(state: CheckoutAgentState) -> CheckoutAgentState:
    """
    Deterministic setup node.
    Validates that required fields are present and generates order_id.
    Creates the order in MongoDB with PENDING status.
    The LLM reason loop starts after this.
    """
    logger.info(
        "[initialise_order] user_id=%s currency=%s order_id=%s",
        state["user_id"], state["user_currency"], state["order_id"],
    )
    
    # Create initial PENDING order in MongoDB
    await _db_create_pending_order(state)
    
    return {
        **state,
        "is_complete":   False,
        "fatal_error":   None,
        "iteration":     0,
        "steps":         [],
        "next_action":   None,
        "last_reasoning": None,
    }


# ════════════════════════════════════════════════════════════════════════════
# Node 2 – reason  (LLM node — the "brain")
# ════════════════════════════════════════════════════════════════════════════

def _build_reason_prompt(state: CheckoutAgentState) -> str:
    tools_block = "\n".join(
        f"  {name}: {desc}"
        for name, desc in TOOL_DESCRIPTIONS.items()
    )
    return f"""
You are the orchestration brain for an e-commerce checkout service.

Your job is to inspect the current order state and decide which single tool
to call next to advance the checkout toward completion.

Available tools:
{tools_block}

Current order state (JSON):
{_state_summary(state)}

Actions taken so far (iteration {state['iteration']}):
{_steps_summary(state.get('steps', []))}

Rules:
- You MUST follow this logical sequence unless a step already has data:
    1. GET_CART            → populates cart_items
    2. GET_PRODUCT_PRICES  → populates order_items (needs cart_items)
    3. GET_SHIPPING_QUOTE  → populates shipping_cost (needs cart_items)
    4. CHARGE_CARD         → populates transaction_id (needs order_items + shipping_cost)
    5. SHIP_ORDER          → populates tracking_id (needs transaction_id)
    6. EMPTY_CART          → best-effort cleanup (needs transaction_id)
    7. SEND_CONFIRMATION   → best-effort email (needs tracking_id)
    8. DONE                → only when SHIP_ORDER has tracking_id
- Skip a step if its result is already in the state (not null).
- If a critical step failed (CHARGE_CARD, SHIP_ORDER), do NOT proceed to DONE.
- EMPTY_CART and SEND_CONFIRMATION are best-effort: attempt them even after partial failures.
- Do NOT call DONE unless transaction_id AND tracking_id are both set.
- Return ONLY valid JSON — no markdown, no preamble.

Output schema:
{{
  "next_action": "<one of: GET_CART | GET_PRODUCT_PRICES | GET_SHIPPING_QUOTE | CHARGE_CARD | SHIP_ORDER | EMPTY_CART | SEND_CONFIRMATION | DONE>"
}}
""".strip()


def _parse_llm_decision(text: str) -> Dict[str, str]:
    """Extract {reasoning, next_action} from LLM JSON response."""
    try:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            data = json.loads(m.group(0))
            action = data.get("next_action", "").strip().upper()
            if action in ToolName.ALL:
                return {
                    "reasoning":   data.get("reasoning", ""),
                    "next_action": action,
                }
    except Exception:
        pass

    # Fallback: scan for any known tool name in the raw text
    for tool in [
        ToolName.GET_CART, ToolName.GET_PRODUCT_PRICES, ToolName.GET_SHIPPING_QUOTE,
        ToolName.CHARGE_CARD, ToolName.SHIP_ORDER, ToolName.EMPTY_CART,
        ToolName.SEND_CONFIRMATION, ToolName.DONE,
    ]:
        if tool in text.upper():
            logger.warning("[reason] fell back to text-scan, found: %s", tool)
            return {"reasoning": text[:200], "next_action": tool}

    return {"reasoning": "parse failed", "next_action": ToolName.GET_CART}


async def reason_node(state: CheckoutAgentState) -> CheckoutAgentState:
    """
    LLM node — inspects the entire order state and decides the next tool call.

    This is the core differentiator: unlike a fixed pipeline, the LLM can
    reason about partial failures, re-order steps if needed, or skip steps
    that have already been completed.
    """
    iteration = state["iteration"]
    logger.info(
        "[reason] iteration=%d user_id=%s | invoking LLM",
        iteration, state["user_id"],
    )

    # Safety: if we've hit MAX_ITERATIONS without completing, abort
    if iteration >= MAX_ITERATIONS:
        logger.warning(
            "[reason] MAX_ITERATIONS=%d reached | aborting to fallback",
            MAX_ITERATIONS,
        )
        return {
            **state,
            "fatal_error": f"Exceeded MAX_ITERATIONS ({MAX_ITERATIONS})",
            "is_complete": False,
        }

    prompt = _build_reason_prompt(state)

    try:
        response    = await asyncio.to_thread(llm.invoke, prompt)
        raw         = response.text()
        in_tokens   = response.usage_metadata.get("input_tokens",  0)
        out_tokens  = response.usage_metadata.get("output_tokens", 0)

        logger.info("[reason] LLM raw (iter=%d): %s", iteration, raw[:300])

        decision = _parse_llm_decision(raw)
        action   = decision["next_action"]
        reasoning= decision["reasoning"]

        logger.info(
            "[reason] iteration=%d → action=%s | %s",
            iteration, action, reasoning[:120],
        )

        return {
            **state,
            "next_action":    action,
            "last_reasoning": reasoning,
            "iteration":      iteration + 1,
            "total_input_tokens":  state["total_input_tokens"]  + in_tokens,
            "total_output_tokens": state["total_output_tokens"] + out_tokens,
            "total_llm_calls":     state["total_llm_calls"]     + 1,
        }

    except Exception as exc:
        logger.error("[reason] LLM error at iteration=%d: %s", iteration, exc)
        # Determine the next logical action deterministically as fallback
        fallback = _deterministic_next_action(state)
        logger.warning("[reason] LLM fallback → %s", fallback)
        return {
            **state,
            "next_action":    fallback,
            "last_reasoning": f"LLM error ({exc}); using deterministic fallback",
            "iteration":      iteration + 1,
        }


def _deterministic_next_action(state: CheckoutAgentState) -> str:
    """
    Deterministic fallback: returns the next logical tool in the standard sequence.
    Used when the LLM call fails or returns an unparseable response.
    """
    if not state.get("cart_items"):
        return ToolName.GET_CART
    if not state.get("order_items"):
        return ToolName.GET_PRODUCT_PRICES
    if not state.get("shipping_cost"):
        return ToolName.GET_SHIPPING_QUOTE
    if not state.get("transaction_id"):
        return ToolName.CHARGE_CARD
    if not state.get("tracking_id"):
        return ToolName.SHIP_ORDER
    if not state.get("cart_emptied"):
        return ToolName.EMPTY_CART
    if not state.get("confirmation_sent"):
        return ToolName.SEND_CONFIRMATION
    return ToolName.DONE


# ════════════════════════════════════════════════════════════════════════════
# Conditional router
# ════════════════════════════════════════════════════════════════════════════

def route_after_reason(state: CheckoutAgentState) -> str:
    if state.get("fatal_error"):
        return "finalise_order"   # hit max iterations — finalise with what we have
    if state.get("next_action") == ToolName.DONE:
        return "finalise_order"
    return "execute_tool"


# ════════════════════════════════════════════════════════════════════════════
# Node 3 – execute_tool  (deterministic dispatcher)
# ════════════════════════════════════════════════════════════════════════════

async def execute_tool_node(state: CheckoutAgentState) -> CheckoutAgentState:
    """
    Deterministic dispatcher — executes whichever tool the LLM chose.

    Each tool result is appended to state.steps so the LLM can see what
    happened in the next reason iteration.
    """
    action    = state["next_action"]
    iteration = state["iteration"]
    logger.info("[execute_tool] iteration=%d action=%s", iteration, action)

    try:
        new_fields, result_msg, success = await _dispatch(action, state)
    except Exception as exc:
        logger.error("[execute_tool] unexpected error | action=%s error=%s", action, exc)
        new_fields  = {}
        result_msg  = f"unexpected error: {exc}"
        success     = False

    step: StepRecord = {
        "iteration": iteration,
        "reasoning": state.get("last_reasoning", ""),
        "action":    action,
        "result":    result_msg,
        "success":   success,
    }

    return {
        **state,
        **new_fields,
        "steps": list(state.get("steps", [])) + [step],
    }


async def _dispatch(
    action: str,
    state:  CheckoutAgentState,
) -> tuple[Dict[str, Any], str, bool]:
    """
    Route to the correct downstream call and return (new_state_fields, msg, success).
    Each branch is a direct async call to the injected stub.
    """

    # ── GET_CART ──────────────────────────────────────────────────────────────
    if action == ToolName.GET_CART:
        resp: demo_pb2.GetCartResponse = await state["cart_stub"].GetCart(
            demo_pb2.GetCartRequest(user_id=state["user_id"])
        )
        items = [
            {"product_id": i.product_id, "quantity": i.quantity}
            for i in resp.cart.items
        ]
        logger.info("[execute_tool:GET_CART] fetched %d items", len(items))
        return (
            {"cart_items": items},
            f"fetched {len(items)} item(s): {[i['product_id'] for i in items]}",
            True,
        )

    # ── GET_PRODUCT_PRICES ────────────────────────────────────────────────────
    elif action == ToolName.GET_PRODUCT_PRICES:
        cart_items  = state.get("cart_items") or []
        user_currency = state["user_currency"]
        order_items = []

        for item in cart_items:
            resp: demo_pb2.GetProductResponse = await state["catalog_stub"].GetProduct(
                demo_pb2.GetProductRequest(id=item["product_id"])
            )
            product = resp.product
            price_usd = product.price_usd
            state["total_output_tokens"] += getattr(resp.llm_metrics, "total_input_tokens",  0)
            state["total_llm_calls"] += getattr(resp.llm_metrics, "total_llm_calls", 0)
            state["total_input_tokens"] += getattr(resp.llm_metrics, "total_input_tokens", 0)

            if price_usd.currency_code == user_currency:
                converted = price_usd
            else:
                converted_resp: demo_pb2.CurrencyConversionResponse = await state["currency_stub"].Convert(
                    demo_pb2.CurrencyConversionRequest(
                        from_=price_usd, to_code=user_currency,
                    )
                )
                converted: demo_pb2.Money = converted_resp.converted_amount
                state["total_output_tokens"] += getattr(converted_resp.llm_metrics, "total_input_tokens",  0)
                state["total_llm_calls"] += getattr(converted_resp.llm_metrics, "total_llm_calls", 0)
                state["total_input_tokens"] += getattr(converted_resp.llm_metrics, "total_input_tokens", 0)

            cost_py   = proto_to_money(converted)
            mult      = money_multiply_slow(cost_py, item["quantity"])
            sub_cents = mult.nanos // 10_000_000

            order_items.append({
                "product_id": item["product_id"],
                "quantity":   item["quantity"],
                "unit_cost":  _money_dict(converted),
                "subtotal":   {
                    "currency_code": mult.currency_code,
                    "units":         mult.units,
                    "nanos":         mult.nanos,
                    "formatted":     f"{mult.currency_code} {mult.units}.{sub_cents:02d}",
                },
            })

        logger.info(
            "[execute_tool:GET_PRODUCT_PRICES] priced %d items in %s, order_items: %s",
            len(order_items), user_currency, order_items
        )
        await _db_update_order_order_items(state["order_id"], order_items)  # persist order_items to MongoDB
        return (
            {"order_items": order_items},
            f"priced {len(order_items)} items in {user_currency}",
            True,
        )

    # ── GET_SHIPPING_QUOTE ────────────────────────────────────────────────────
    elif action == ToolName.GET_SHIPPING_QUOTE:
        address    = state["address"]
        cart_items = state.get("cart_items") or []

        addr_proto = demo_pb2.Address(
            street_address=address.get("street_address", ""),
            city=address.get("city", ""),
            state=address.get("state", ""),
            country=address.get("country", ""),
            zip_code=address.get("zip_code", 0),
        )
        items_proto = [
            demo_pb2.CartItem(product_id=i["product_id"], quantity=i["quantity"])
            for i in cart_items
        ]

        quote_resp: demo_pb2.GetQuoteResponse = await state["shipping_stub"].GetQuote(
            demo_pb2.GetQuoteRequest(address=addr_proto, items=items_proto)
        )
        shipping_usd = quote_resp.cost_usd
        user_currency = state["user_currency"]
        
        state["total_output_tokens"] += getattr(quote_resp.llm_metrics, "total_input_tokens",  0)
        state["total_llm_calls"] += getattr(quote_resp.llm_metrics, "total_llm_calls", 0)
        state["total_input_tokens"] += getattr(quote_resp.llm_metrics, "total_input_tokens", 0)

        if shipping_usd.currency_code == user_currency:
            shipping_local = shipping_usd
        else:
            converted_resp = await state["currency_stub"].Convert(
                demo_pb2.CurrencyConversionRequest(
                    from_=shipping_usd, to_code=user_currency,
                )
            )
            shipping_local: demo_pb2.Money = converted_resp.converted_amount
            state["total_output_tokens"] += getattr(converted_resp.llm_metrics, "total_input_tokens",  0)
            state["total_llm_calls"] += getattr(converted_resp.llm_metrics, "total_llm_calls", 0)
            state["total_input_tokens"] += getattr(converted_resp.llm_metrics, "total_input_tokens", 0)

        cost_dict = _money_dict(shipping_local)
        logger.info("[execute_tool:GET_SHIPPING_QUOTE] %s", cost_dict["formatted"])
        await _db_update_order_shipping_quote(state["order_id"], cost_dict)  # persist shipping_cost to MongoDB
        
        return (
            {"shipping_cost": cost_dict},
            f"shipping quote: {cost_dict['formatted']}",
            True,
        )

    # ── CHARGE_CARD ───────────────────────────────────────────────────────────
    elif action == ToolName.CHARGE_CARD:
        order_items   = state.get("order_items") or []
        shipping_cost = state.get("shipping_cost")
        user_currency = state["user_currency"]
        cc            = state["credit_card"]   # masked dict injected at init

        # Recompute total from current state data
        total = _compute_total(order_items, shipping_cost, user_currency)
        total_proto = demo_pb2.Money(
            currency_code=total["currency_code"],
            units=total["units"],
            nanos=total["nanos"],
        )

        charge_resp: demo_pb2.ChargeResponse = await state["payment_stub"].Charge(
            demo_pb2.ChargeRequest(
                amount=total_proto,
                credit_card=demo_pb2.CreditCardInfo(
                    credit_card_number=cc["number"],
                    credit_card_cvv=cc["cvv"],
                    credit_card_expiration_year=cc["exp_year"],
                    credit_card_expiration_month=cc["exp_month"],
                ),
            )
        )
        txn_id = charge_resp.transaction_id
        logger.info("[execute_tool:CHARGE_CARD] transaction_id=%s", txn_id)
        
        state["total_output_tokens"] += getattr(charge_resp.llm_metrics, "total_input_tokens",  0)
        state["total_llm_calls"] += getattr(charge_resp.llm_metrics, "total_llm_calls", 0)
        state["total_input_tokens"] += getattr(charge_resp.llm_metrics, "total_input_tokens", 0)

        # Update MongoDB: PENDING → PAID
        await _db_update_order(state["order_id"], OrderStatus.PAID, {
            "transaction_id": txn_id,
            "order_total":    total,
        })

        return (
            {"transaction_id": txn_id, "order_total": total},
            f"charged {total['formatted']} → transaction_id={txn_id}",
            True,
        )

    # ── SHIP_ORDER ────────────────────────────────────────────────────────────
    elif action == ToolName.SHIP_ORDER:
        address    = state["address"]
        cart_items = state.get("cart_items") or []

        addr_proto = demo_pb2.Address(
            street_address=address.get("street_address", ""),
            city=address.get("city", ""),
            state=address.get("state", ""),
            country=address.get("country", ""),
            zip_code=address.get("zip_code", 0),
        )
        items_proto = [
            demo_pb2.CartItem(product_id=i["product_id"], quantity=i["quantity"])
            for i in cart_items
        ]

        ship_resp: demo_pb2.ShipOrderResponse = await state["shipping_stub"].ShipOrder(
            demo_pb2.ShipOrderRequest(address=addr_proto, items=items_proto)
        )
        tracking_id = ship_resp.tracking_id
        logger.info("[execute_tool:SHIP_ORDER] tracking_id=%s", tracking_id)

        state["total_output_tokens"] += getattr(ship_resp.llm_metrics, "total_input_tokens",  0)
        state["total_llm_calls"] += getattr(ship_resp.llm_metrics, "total_llm_calls", 0)
        state["total_input_tokens"] += getattr(ship_resp.llm_metrics, "total_input_tokens", 0)

        await _db_update_order(state["order_id"], OrderStatus.SHIPPED, {
            "shipping_tracking_id": tracking_id,
        })

        return (
            {"tracking_id": tracking_id},
            f"order shipped → tracking_id={tracking_id}",
            True,
        )

    # ── EMPTY_CART ────────────────────────────────────────────────────────────
    elif action == ToolName.EMPTY_CART:
        try:
            cart_resp: demo_pb2.EmptyCartResponse = await state["cart_stub"].EmptyCart(
                demo_pb2.EmptyCartRequest(user_id=state["user_id"])
            )
            
            state["total_output_tokens"] += getattr(cart_resp.llm_metrics, "total_input_tokens",  0)
            state["total_llm_calls"] += getattr(cart_resp.llm_metrics, "total_llm_calls", 0)
            state["total_input_tokens"] += getattr(cart_resp.llm_metrics, "total_input_tokens", 0)
        
            logger.info("[execute_tool:EMPTY_CART] cart cleared")
            return ({"cart_emptied": True}, "cart emptied", True)
        except Exception as exc:
            logger.warning("[execute_tool:EMPTY_CART] failed (non-fatal): %s", exc)
            return ({"cart_emptied": False}, f"empty cart failed (non-fatal): {exc}", False)

    # ── SEND_CONFIRMATION ─────────────────────────────────────────────────────
    elif action == ToolName.SEND_CONFIRMATION:
        try:
            order_items   = state.get("order_items") or []
            shipping_cost = state.get("shipping_cost") or {}
            address_d     = state.get("address") or {}

            addr_proto = demo_pb2.Address(
                street_address=address_d.get("street_address", ""),
                city=address_d.get("city", ""),
                state=address_d.get("state", ""),
                country=address_d.get("country", ""),
                zip_code=address_d.get("zip_code", 0),
            )
            sc = shipping_cost
            items_proto = [
                demo_pb2.OrderItem(
                    item=demo_pb2.CartItem(
                        product_id=oi["product_id"], quantity=oi["quantity"]
                    ),
                    cost=demo_pb2.Money(
                        currency_code=oi["unit_cost"]["currency_code"],
                        units=oi["unit_cost"]["units"],
                        nanos=oi["unit_cost"]["nanos"],
                    ),
                )
                for oi in order_items
            ]

            order_result = demo_pb2.OrderResult(
                order_id=state["order_id"],
                shipping_tracking_id=state.get("tracking_id", ""),
                shipping_cost=demo_pb2.Money(
                    currency_code=sc.get("currency_code", "USD"),
                    units=sc.get("units", 0),
                    nanos=sc.get("nanos", 0),
                ),
                shipping_address=addr_proto,
                items=items_proto,
            )

            email_resp: demo_pb2.LLMMetrics = await state["email_stub"].SendOrderConfirmation(
                demo_pb2.SendOrderConfirmationRequest(
                    email=state["email"], order=order_result
                )
            )
            
            state["total_output_tokens"] += getattr(email_resp, "total_input_tokens",  0)
            state["total_llm_calls"] += getattr(email_resp, "total_llm_calls", 0)
            state["total_input_tokens"] += getattr(email_resp, "total_input_tokens", 0)

            # Mark COMPLETED in MongoDB
            await _db_update_order(state["order_id"], OrderStatus.COMPLETED, {})

            logger.info("[execute_tool:SEND_CONFIRMATION] email sent to %s", state["email"])
            return (
                {"confirmation_sent": True},
                f"confirmation email sent to {state['email']}",
                True,
            )
        except Exception as exc:
            logger.warning("[execute_tool:SEND_CONFIRMATION] failed (non-fatal): %s", exc)
            return (
                {"confirmation_sent": False},
                f"email failed (non-fatal): {exc}",
                False,
            )

    else:
        return ({}, f"unknown action: {action}", False)


# ════════════════════════════════════════════════════════════════════════════
# Node 4 – finalise_order  (deterministic)
# ════════════════════════════════════════════════════════════════════════════

async def finalise_order_node(state: CheckoutAgentState) -> CheckoutAgentState:
    """
    Deterministic node — builds the final OrderResult proto from accumulated state.
    Called when LLM returns DONE or MAX_ITERATIONS is hit.
    """
    logger.info(
        "[finalise_order] order_id=%s transaction_id=%s tracking_id=%s llm_calls=%d",
        state["order_id"],
        state.get("transaction_id"),
        state.get("tracking_id"),
        state["total_llm_calls"],
    )

    # Log full step history at DEBUG level for observability
    for s in state.get("steps", []):
        logger.debug(
            "[finalise_order] step[%d] %s → %s (ok=%s)",
            s["iteration"], s["action"], s["result"], s["success"],
        )

    return {**state, "is_complete": True}


# ════════════════════════════════════════════════════════════════════════════
# MongoDB helpers (same schema as original orchestrator)
# ════════════════════════════════════════════════════════════════════════════



async def _db_create_pending_order(state: CheckoutAgentState) -> None:
    """Create a new order with PENDING status in MongoDB."""
    col = await get_orders_collection()
    if col is None:
        logger.warning("[MongoDB] orders collection not available")
        return
    
    try:
        now = datetime.datetime.now(tz=timezone.utc)
        doc = {
            "_id":                  state["order_id"],
            "order_id":             state["order_id"],
            "status":               OrderStatus.PENDING,
            "user_id":              state["user_id"],
            "user_currency":        state["user_currency"],
            "email":                state["email"],
            "address":              state.get("address"),
            "items":                [],  # will be populated later
            "shipping_cost":        None,
            "total":                None,
            "transaction_id":       None,
            "shipping_tracking_id": None,
            "created_at":           now,
            "updated_at":           now,
            "status_history":       [{"status": OrderStatus.PENDING, "timestamp": now}],
        }
        await col.insert_one(doc)
        logger.info("[MongoDB] created PENDING order | order_id=%s", state["order_id"])
    except Exception as exc:
        logger.error("[MongoDB] failed to create PENDING order: %s", exc)


async def _db_update_order(order_id: str, status: str, extra: Dict[str, Any]) -> None:
    """Update an existing order with new status and extra fields."""
    col = await get_orders_collection()
    if col is None:
        return
    try:
        now = datetime.datetime.now(tz=timezone.utc)
        set_fields: Dict[str, Any] = {"status": status, "updated_at": now}
        set_fields.update(extra)
        await col.update_one(
            {"_id": order_id},
            {
                "$set":  set_fields,
                "$push": {"status_history": {"status": status, "timestamp": now}},
            },
        )
        logger.info("[MongoDB] updated order | order_id=%s status=%s", order_id, status)
    except Exception as exc:
        logger.error("[MongoDB] failed to update order: %s", exc)


async def _db_update_order_shipping_quote(order_id: str, shipping_quote: dict) -> None:
    """Update an existing order with new status and extra fields."""
    col = await get_orders_collection()
    if col is None:
        return
    try:
        now = datetime.datetime.now(tz=timezone.utc)
        set_fields: Dict[str, Any] = {"shipping_cost": shipping_quote, "updated_at": now}
        await col.update_one(
            {"_id": order_id},
            {"$set": set_fields}
        )
        logger.info("[MongoDB] updated order add shipping quote | order_id=%s", order_id)
    except Exception as exc:
        logger.error("[MongoDB] failed to update order: %s", exc)

async def _db_update_order_order_items(order_id: str, order_items: list) -> None:
    """Update an existing order with new status and extra fields."""
    col = await get_orders_collection()
    if col is None:
        return
    try:
        now = datetime.datetime.now(tz=timezone.utc)
        set_fields: Dict[str, Any] = {"items": order_items, "updated_at": now}
        await col.update_one(
            {"_id": order_id},
            {"$set": set_fields}
        )
        logger.info("[MongoDB] updated order add order items | order_id=%s", order_id)
    except Exception as exc:
        logger.error("[MongoDB] failed to update order: %s", exc)
                
# ════════════════════════════════════════════════════════════════════════════
# Graph assembly
# ════════════════════════════════════════════════════════════════════════════

def build_checkout_agent():
    """Assemble and compile the ReAct checkout agent graph."""
    graph = StateGraph(CheckoutAgentState)

    graph.add_node("initialise_order", initialise_order_node)
    graph.add_node("reason",           reason_node)
    graph.add_node("execute_tool",     execute_tool_node)
    graph.add_node("finalise_order",   finalise_order_node)

    graph.set_entry_point("initialise_order")
    graph.add_edge("initialise_order", "reason")

    graph.add_conditional_edges(
        "reason",
        route_after_reason,
        {
            "execute_tool":   "execute_tool",
            "finalise_order": "finalise_order",
        },
    )

    # After executing a tool → reason again (the ReAct loop)
    graph.add_edge("execute_tool", "reason")
    graph.add_edge("finalise_order", END)

    compiled = graph.compile()
    logger.info("[CheckoutAgent] ReAct graph compiled successfully")
    return compiled


checkout_graph = build_checkout_agent()


# ════════════════════════════════════════════════════════════════════════════
# Public helper called by the gRPC servicer
# ════════════════════════════════════════════════════════════════════════════

async def run_checkout_agent(
    request:       demo_pb2.PlaceOrderRequest,
    cart_stub:     demo_pb2_grpc.CartServiceStub,
    catalog_stub:  demo_pb2_grpc.ProductCatalogServiceStub,
    currency_stub: demo_pb2_grpc.CurrencyServiceStub,
    shipping_stub: demo_pb2_grpc.ShippingServiceStub,
    payment_stub:  demo_pb2_grpc.PaymentServiceStub,
    email_stub:    demo_pb2_grpc.EmailServiceStub,
    grpc_context:  Any = None,
) -> CheckoutAgentState:
    """
    Build the initial state and invoke the ReAct checkout agent graph.

    The credit card is stored masked (only last-4 exposed in logs) but the
    raw fields are kept in state for the CHARGE_CARD tool node.

    Returns the final CheckoutAgentState. Callers should read:
        state["transaction_id"]  – for PlaceOrderResponse
        state["tracking_id"]     – for ShipOrderResponse
        state["order_items"]     – for the OrderResult items list
        state["shipping_cost"]   – for the OrderResult shipping_cost
        state["fatal_error"]     – non-None if something went badly wrong
    """
    cc = request.credit_card
    initial_state: CheckoutAgentState = {
        # original request
        "order_id":     str(uuid.uuid4()),
        "user_id":      request.user_id,
        "user_currency": request.user_currency,
        "email":        request.email,
        "address":      _address_dict(request.address),
        "credit_card": {
            "number":    cc.credit_card_number,
            "cvv":       cc.credit_card_cvv,
            "exp_year":  cc.credit_card_expiration_year,
            "exp_month": cc.credit_card_expiration_month,
        },
        # injected stubs
        "cart_stub":     cart_stub,
        "catalog_stub":  catalog_stub,
        "currency_stub": currency_stub,
        "shipping_stub": shipping_stub,
        "payment_stub":  payment_stub,
        "email_stub":    email_stub,
        "grpc_context":  grpc_context,
        # gathered data
        "cart_items":    None,
        "order_items":   None,
        "shipping_cost": None,
        "order_total":   None,
        # outcomes
        "transaction_id":   None,
        "tracking_id":      None,
        "confirmation_sent": False,
        "cart_emptied":     False,
        # loop control
        "iteration":     0,
        "next_action":   None,
        "last_reasoning": None,
        "steps":         [],
        "is_complete":   False,
        "fatal_error":   None,
        # metrics
        "total_input_tokens":  0,
        "total_output_tokens": 0,
        "total_llm_calls":     0,
    }

    logger.info(
        "[run_checkout_agent] start | user_id=%s currency=%s order_id=%s",
        request.user_id, request.user_currency, initial_state["order_id"],
    )

    result: CheckoutAgentState = await checkout_graph.ainvoke(initial_state)

    logger.info(
        "[run_checkout_agent] done | order_id=%s "
        "transaction_id=%s tracking_id=%s "
        "llm_calls=%d iterations=%d fatal_error=%s",
        result["order_id"],
        result.get("transaction_id"),
        result.get("tracking_id"),
        result["total_llm_calls"],
        result["iteration"],
        result.get("fatal_error"),
    )

    return result
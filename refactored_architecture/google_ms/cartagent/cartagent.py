"""
cartservice/agent.py

LangGraph cart agent — replaces the simple add/get/empty cart operations with a
full agentic graph while keeping the exact same gRPC interface.

Graph topology:
─────────────────────────────────────────────────────────────────────────────
  ┌──────────────────────┐
  │  validate_request    │  (deterministic) validate user_id, product_id, qty
  └────────┬─────────────┘
           │
     ┌─────▼──────┐  validation_error present?
     │   route    │──────────────────────────────────────────────────┐
     └─────┬──────┘  no                                              │ yes
           │                                                         │
  ┌────────▼─────────┐                                   ┌───────────▼──────────┐
  │  apply_operation │  (deterministic) add/get/empty     │  reject_operation    │
  │  (fetch+execute) │  MongoDB operation                  │  sets decision=FAILED│
  └────────┬─────────┘                                   └───────────┬──────────┘
           │                                                         │
  ┌────────▼──────────┐                                             │
  │ operation_reasoning│  (LLM / Ollama llama3) validates            │
  │                   │  consistency of cart state                   │
  └────────┬──────────┘                                             │
           │                                                         │
  ┌────────▼──────────┐◄────────────────────────────────────────────┘
  │ persist_operation │  (deterministic) writes to MongoDB audit trail
  └────────┬──────────┘
           │
          END

Node roles
──────────
validate_request       Deterministic tool. Validates user_id, product_id, qty.
                       On success: leaves validation_error=None.
                       On failure: sets validation_error.

apply_operation        Deterministic tool. Executes the cart operation:
                       - ADD_ITEM: add product to cart (increment qty)
                       - GET_CART: fetch cart items
                       - EMPTY_CART: clear all items
                       Stores result in operation_result.

operation_reasoning    LLM node (Ollama llama3). Reviews the operation:
                       - For ADD_ITEM: validates product is real, qty > 0
                       - For GET_CART: validates response consistency
                       - For EMPTY_CART: validates user_id is valid
                       Decides APPROVED or REJECTED based on operation validity.

reject_operation       Deterministic shortcut for validation failures.
                       Sets decision=FAILED with validation error as reason.

persist_operation      Deterministic tool. Writes operation audit trail to
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

logger = logging.getLogger("cartservice.agent")

# ── Ollama LLM (temperature=0 for deterministic decision-making) ─────────────
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


async def get_cart_operations_collection():
    """Get the cart_operations collection for audit trail."""
    client = await get_mongodb_client()
    db = client[MONGODB_DB]
    collection = db["cart_operations"]
    
    # Ensure indexes
    await collection.create_index("operation_id", unique=True)
    await collection.create_index("user_id")
    await collection.create_index("created_at")
    return collection


async def get_carts_collection():
    """Get the carts collection for actual cart data."""
    client = await get_mongodb_client()
    db = client[MONGODB_DB]
    collection = db["carts"]
    return collection


# ════════════════════════════════════════════════════════════════════════════
# Agent state
# ════════════════════════════════════════════════════════════════════════════

class CartAgentState(TypedDict):
    """
    Shared state threaded through every node in the cart agent graph.

    Input fields:
        operation_type   – "ADD_ITEM" | "GET_CART" | "EMPTY_CART"
        user_id          – user identifier
        product_id       – (for ADD_ITEM)
        quantity         – (for ADD_ITEM)

    Intermediate fields:
        validation_error – set by validate_request on failure; None on success
        operation_result – result from apply_operation node

    Output fields:
        decision         – {"status": "APPROVED"|"REJECTED", "reason": str}

    Metrics:
        total_input_tokens, total_output_tokens, total_llm_calls
    """
    # ── inputs ────────────────────────────────────────────────────────────────
    operation_id:    str
    operation_type:  Literal["ADD_ITEM", "GET_CART", "EMPTY_CART"]
    user_id:         str
    product_id:      Optional[str]
    quantity:        Optional[int]

    # ── intermediate ──────────────────────────────────────────────────────────
    validation_error:  Optional[str]
    operation_result:  Optional[Dict[str, Any]]

    # ── output ────────────────────────────────────────────────────────────────
    decision: Dict[str, Any]

    # ── metrics ───────────────────────────────────────────────────────────────
    total_input_tokens:  int
    total_output_tokens: int
    total_llm_calls:     int


# ════════════════════════════════════════════════════════════════════════════
# Node 1 – validate_request  (deterministic tool)
# ════════════════════════════════════════════════════════════════════════════

async def validate_request_node(state: CartAgentState) -> CartAgentState:
    """
    Deterministic validation node.

    Validates:
      • user_id is non-empty
      • operation_type is valid
      • For ADD_ITEM: product_id and quantity are valid
      • For EMPTY_CART: only user_id required
      • For GET_CART: only user_id required

    On success: validation_error=None
    On failure: validation_error set with reason
    """
    logger.info("[validate_request] validating | operation=%s user=%s",
                state["operation_type"], state["user_id"])

    user_id = state.get("user_id", "").strip()
    operation_type = state.get("operation_type", "")

    # Validate user_id
    if not user_id:
        logger.warning("[validate_request] empty user_id")
        return {
            **state,
            "validation_error": "User ID is required.",
        }

    # Validate operation_type
    valid_ops = {"ADD_ITEM", "GET_CART", "EMPTY_CART"}
    if operation_type not in valid_ops:
        logger.warning("[validate_request] invalid operation: %s", operation_type)
        return {
            **state,
            "validation_error": f"Invalid operation: {operation_type}",
        }

    # Validate operation-specific fields
    if operation_type == "ADD_ITEM":
        product_id = state.get("product_id", "").strip()
        quantity = state.get("quantity", 0)

        if not product_id:
            logger.warning("[validate_request] empty product_id for ADD_ITEM")
            return {
                **state,
                "validation_error": "Product ID is required for ADD_ITEM.",
            }

        if not isinstance(quantity, int) or quantity <= 0:
            logger.warning("[validate_request] invalid quantity for ADD_ITEM: %s", quantity)
            return {
                **state,
                "validation_error": "Quantity must be a positive integer.",
            }

    logger.info("[validate_request] passed validation")
    return {
        **state,
        "validation_error": None,
    }


# ════════════════════════════════════════════════════════════════════════════
# Conditional router after validate_request
# ════════════════════════════════════════════════════════════════════════════

def route_after_validation(state: CartAgentState) -> str:
    """
    Conditional edge function.

    If validation_error is set → go to reject_operation.
    Otherwise                  → go to apply_operation.
    """
    if state.get("validation_error"):
        logger.info("[route] validation failed → reject_operation")
        return "reject_operation"
    logger.info("[route] validation passed → apply_operation")
    return "apply_operation"


# ════════════════════════════════════════════════════════════════════════════
# Conditional router after apply_operation (skip reasoning for GET_CART)
# ════════════════════════════════════════════════════════════════════════════

def route_after_apply(state: CartAgentState) -> str:
    """
    Conditional edge function — skip LLM reasoning for GET_CART operations.

    GET_CART operations bypass reasoning and go directly to persistence.
    ADD_ITEM and EMPTY_CART operations proceed to LLM reasoning.
    """
    operation_type = state.get("operation_type", "")
    
    if operation_type == "GET_CART":
        logger.info("[route] GET_CART operation → skip reasoning, go to persist")
        # For GET_CART, set a default APPROVED decision
        return "auto_approve"
    
    logger.info("[route] %s operation → reasoning", operation_type)
    return "operation_reasoning"


# ════════════════════════════════════════════════════════════════════════════
# Node 2a – apply_operation  (deterministic tool)
# ════════════════════════════════════════════════════════════════════════════

async def apply_operation_node(state: CartAgentState) -> CartAgentState:
    """
    Deterministic tool node — executes the cart operation against MongoDB.

    Operations:
      • ADD_ITEM: add product to user's cart (increment quantity)
      • GET_CART: fetch user's current cart items
      • EMPTY_CART: clear all items from user's cart

    Stores the result in operation_result for LLM review.
    """
    operation_type = state["operation_type"]
    user_id = state["user_id"]
    operation_id = state["operation_id"]

    logger.info("[apply_operation] executing %s | user=%s operation_id=%s",
                operation_type, user_id, operation_id)

    collection = await get_carts_collection()
    cart_key = f"cart:{user_id}"
    result = {}

    try:
        if operation_type == "ADD_ITEM":
            product_id = state["product_id"]
            quantity = state["quantity"]

            # Fetch current cart
            cart_doc = await collection.find_one({"_id": cart_key})
            items = cart_doc.get("items", {}) if cart_doc else {}

            # Update quantity
            current_qty = int(items.get(product_id, 0))
            new_qty = current_qty + quantity
            items[product_id] = new_qty

            # Save back to MongoDB
            if items:
                await collection.update_one(
                    {"_id": cart_key},
                    {"$set": {"items": items}},
                    upsert=True
                )
            else:
                await collection.delete_one({"_id": cart_key})

            result = {
                "operation": "ADD_ITEM",
                "product_id": product_id,
                "quantity_added": quantity,
                "new_total_quantity": new_qty,
                "success": True,
            }
            logger.info("[apply_operation] ADD_ITEM completed | product=%s new_qty=%d",
                        product_id, new_qty)

        elif operation_type == "GET_CART":
            cart_doc = await collection.find_one({"_id": cart_key})
            items = cart_doc.get("items", {}) if cart_doc else {}

            result = {
                "operation": "GET_CART",
                "items": items,
                "item_count": len(items),
                "success": True,
            }
            logger.info("[apply_operation] GET_CART completed | items=%d",
                        len(items))

        elif operation_type == "EMPTY_CART":
            await collection.delete_one({"_id": cart_key})

            result = {
                "operation": "EMPTY_CART",
                "success": True,
            }
            logger.info("[apply_operation] EMPTY_CART completed")

    except Exception as exc:
        logger.error("[apply_operation] error executing %s: %s",
                     operation_type, exc)
        result = {
            "operation": operation_type,
            "success": False,
            "error": str(exc),
        }

    return {
        **state,
        "operation_result": result,
    }


# ════════════════════════════════════════════════════════════════════════════
# Node 2b – reject_operation  (deterministic shortcut)
# ════════════════════════════════════════════════════════════════════════════

async def reject_operation_node(state: CartAgentState) -> CartAgentState:
    """
    Deterministic shortcut node — reached when validation_error is set.

    Bypasses apply_operation + LLM and directly sets decision=REJECTED.
    """
    reason = state.get("validation_error", "Request validation failed.")
    logger.info("[reject_operation] setting decision=REJECTED | reason=%s", reason)

    return {
        **state,
        "operation_result": None,
        "decision": {
            "status": "REJECTED",
            "reason": reason,
        },
    }


# ════════════════════════════════════════════════════════════════════════════
# Node 2c – auto_approve  (deterministic shortcut for GET_CART)
# ════════════════════════════════════════════════════════════════════════════

async def auto_approve_node(state: CartAgentState) -> CartAgentState:
    """
    Deterministic node — auto-approves GET_CART operations without LLM reasoning.

    GET_CART is a read-only operation, so it bypasses LLM review and is
    automatically approved if the operation succeeded.
    """
    operation_result = state.get("operation_result", {})
    success = operation_result.get("success", False)

    if success:
        decision = {
            "status": "APPROVED",
            "reason": "GET_CART operation completed successfully (no reasoning required)",
        }
        logger.info("[auto_approve] GET_CART approved without reasoning")
    else:
        decision = {
            "status": "REJECTED",
            "reason": f"GET_CART operation failed: {operation_result.get('error', 'unknown error')}",
        }
        logger.warning("[auto_approve] GET_CART rejected due to operation failure")

    return {
        **state,
        "decision": decision,
    }


# ════════════════════════════════════════════════════════════════════════════
# Node 3 – operation_reasoning  (LLM / Ollama node)
# ════════════════════════════════════════════════════════════════════════════

def _parse_json_response(text: str) -> dict | None:
    """Extract the first JSON object from an LLM text response."""
    try:
        match = re.search(r"\{.*?\}", text, re.DOTALL)
        if match:
            return json.loads(match.group(0))
    except Exception as exc:
        logger.error("[operation_reasoning] JSON parse error: %s", exc)
    return None


async def operation_reasoning_node(state: CartAgentState) -> CartAgentState:
    """
    LLM node — validates cart operations ADD_ITEM and EMPTY_CART only.

    GET_CART operations are skipped and auto-approved (see auto_approve_node).

    The LLM reviews:
      • For ADD_ITEM: is the product valid? is quantity reasonable (1-1000)?
      • For EMPTY_CART: is the request valid?

    Returns:
        { "status": "APPROVED" | "REJECTED", "reason": "<explanation>" }

    Token usage is accumulated for observability.
    """
    operation_type = state["operation_type"]
    result = state.get("operation_result", {})

    # Build operation details for LLM
    operation_details = json.dumps(result, indent=2)

    if operation_type == "ADD_ITEM":
        validation_rules = """- For ADD_ITEM operations:
  * Product ID must not be empty
  * Quantity must be between 1 and 1000
  * Operation must have succeeded (success=true)"""
    elif operation_type == "EMPTY_CART":
        validation_rules = """- For EMPTY_CART operations:
  * Operation must have succeeded (success=true)"""
    else:
        validation_rules = "- Operation must have succeeded"

    prompt = f"""
You are a cart operation validation agent for an e-commerce platform.

Your task is to approve or reject a cart operation based on business logic.

Rules:
- Status MUST be either "APPROVED" or "REJECTED".
{validation_rules}
- If any rule is violated, set status to REJECTED with a brief reason.
- Do not generate python code.
- Return ONLY valid JSON. No markdown, no code blocks, no preamble.

Output schema:
{{
  "status": "APPROVED" | "REJECTED",
  "reason": "<one sentence explanation if REJECTED>"
}}

Operation details:
  TYPE: {operation_type}
  DATA: {operation_details}
""".strip()

    logger.info("[operation_reasoning] invoking LLM | operation=%s",
                operation_type)

    response = await asyncio.to_thread(llm.invoke, prompt)

    raw = response.text()
    in_tokens = response.usage_metadata.get("input_tokens", 0)
    out_tokens = response.usage_metadata.get("output_tokens", 0)

    logger.info("[operation_reasoning] LLM response: %s", raw)
    logger.info("[operation_reasoning] tokens | in=%d out=%d", in_tokens, out_tokens)

    decision = _parse_json_response(raw)

    if not decision or decision.get("status") not in ("APPROVED", "REJECTED"):
        logger.error("[operation_reasoning] invalid decision: %s", raw)
        raise ValueError(f"Invalid cart operation decision from LLM: {raw!r}")

    logger.info("[operation_reasoning] decision=%s reason=%s",
                decision["status"], decision.get("reason", ""))

    return {
        **state,
        "decision": decision,
        "total_input_tokens": state["total_input_tokens"] + in_tokens,
        "total_output_tokens": state["total_output_tokens"] + out_tokens,
        "total_llm_calls": state["total_llm_calls"] + 1,
    }


# ════════════════════════════════════════════════════════════════════════════
# Node 4 – persist_operation  (deterministic tool)
# ════════════════════════════════════════════════════════════════════════════

async def persist_operation_node(state: CartAgentState) -> CartAgentState:
    """
    Deterministic tool node — persists the cart operation to MongoDB audit trail.

    Runs for both APPROVED and REJECTED outcomes so every operation is audited.

    Document schema:
        operation_id         – UUID
        operation_type       – ADD_ITEM | GET_CART | EMPTY_CART
        user_id              – user identifier
        product_id           – (for ADD_ITEM)
        quantity             – (for ADD_ITEM)
        operation_result     – result from apply_operation
        decision             – LLM decision dict
        llm_metrics          – { input_tokens, output_tokens, llm_calls }
        validation_error     – if validation failed
        created_at           – UTC timestamp
    """
    operation_id = state["operation_id"]
    operation_type = state["operation_type"]
    user_id = state["user_id"]
    status = state["decision"].get("status", "REJECTED")

    collection = await get_cart_operations_collection()

    doc = {
        "_id": operation_id,
        "operation_id": operation_id,
        "operation_type": operation_type,
        "user_id": user_id,
        "product_id": state.get("product_id"),
        "quantity": state.get("quantity"),
        "operation_result": state.get("operation_result"),
        "decision": state["decision"],
        "llm_metrics": {
            "input_tokens": state["total_input_tokens"],
            "output_tokens": state["total_output_tokens"],
            "llm_calls": state["total_llm_calls"],
        },
        "validation_error": state.get("validation_error"),
        "created_at": datetime.datetime.now(tz=datetime.timezone.utc),
    }

    logger.info("[persist_operation] persisting | status=%s operation_id=%s",
                status, operation_id)

    try:
        await collection.insert_one(doc)
        logger.info("[persist_operation] persisted successfully | operation_id=%s",
                    operation_id)
    except Exception as exc:
        # Non-fatal — never block the gRPC response for a DB write failure
        logger.error("[persist_operation] MongoDB write failed (non-fatal): %s", exc)

    return state


# ════════════════════════════════════════════════════════════════════════════
# Graph assembly
# ════════════════════════════════════════════════════════════════════════════

def build_cart_agent() -> Any:
    """
    Assemble and compile the LangGraph cart agent.

    Graph topology:
      validate_request → apply_operation → route_after_apply
                      ↓                      ├→ operation_reasoning → persist_operation
                   reject_operation        └→ auto_approve → persist_operation

    Returns the compiled graph, ready for ainvoke().
    """
    graph = StateGraph(CartAgentState)

    # Register nodes
    graph.add_node("validate_request", validate_request_node)
    graph.add_node("apply_operation", apply_operation_node)
    graph.add_node("reject_operation", reject_operation_node)
    graph.add_node("auto_approve", auto_approve_node)
    graph.add_node("operation_reasoning", operation_reasoning_node)
    graph.add_node("persist_operation", persist_operation_node)

    # Entry point
    graph.set_entry_point("validate_request")

    # Conditional edge after validation
    graph.add_conditional_edges(
        "validate_request",
        route_after_validation,
        {
            "apply_operation": "apply_operation",
            "reject_operation": "reject_operation",
        },
    )

    # Conditional edge after apply_operation (GET_CART → auto_approve, others → reasoning)
    graph.add_conditional_edges(
        "apply_operation",
        route_after_apply,
        {
            "operation_reasoning": "operation_reasoning",
            "auto_approve": "auto_approve",
        },
    )

    # Both reasoning and auto_approve paths converge at persistence
    graph.add_edge("operation_reasoning", "persist_operation")
    graph.add_edge("auto_approve", "persist_operation")

    # Rejection shortcut: reject → persist
    graph.add_edge("reject_operation", "persist_operation")

    # Terminal
    graph.add_edge("persist_operation", END)

    compiled = graph.compile()
    logger.info("[CartAgent] graph compiled successfully")
    return compiled


# Singleton graph — built at import time
cart_graph = build_cart_agent()


# ════════════════════════════════════════════════════════════════════════════
# Public helper called by the gRPC servicer
# ════════════════════════════════════════════════════════════════════════════

async def run_cart_agent(
    operation_type: Literal["ADD_ITEM", "GET_CART", "EMPTY_CART"],
    user_id: str,
    product_id: Optional[str] = None,
    quantity: Optional[int] = None,
) -> CartAgentState:
    """
    Build initial state and invoke the compiled graph.

    Returns the final CartAgentState after all nodes have run.
    Raises ValueError if the LLM returns an unparseable decision.
    """
    operation_id = str(uuid.uuid4())

    initial_state: CartAgentState = {
        # inputs
        "operation_id": operation_id,
        "operation_type": operation_type,
        "user_id": user_id,
        "product_id": product_id,
        "quantity": quantity,
        # intermediate
        "validation_error": None,
        "operation_result": None,
        # output
        "decision": {},
        # metrics
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "total_llm_calls": 0,
    }

    logger.info(
        "[run_cart_agent] invoking graph | operation=%s user=%s operation_id=%s",
        operation_type, user_id, operation_id,
    )

    result: CartAgentState = await cart_graph.ainvoke(initial_state)

    logger.info(
        "[run_cart_agent] completed | status=%s operation_id=%s llm_calls=%d",
        result["decision"].get("status"),
        operation_id,
        result["total_llm_calls"],
    )

    return result

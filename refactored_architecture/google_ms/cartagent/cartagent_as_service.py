"""

Replaces the original C# cartservice.
- gRPC server on port 7070  (same as original)
- FastAPI HTTP server on port 8070  (health + REST proxy)
- Cart state stored in MongoDB
- Now using agentic cart operations via LangGraph + Ollama LLM
"""

import asyncio
import logging
import os
import sys

import grpc
from motor.motor_asyncio import AsyncIOMotorClient
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from ..shared import demo_pb2
from ..shared import demo_pb2_grpc
from ..shared.base_service import make_health_app, run_service
from .cartagent import run_cart_agent

logger = logging.getLogger(__name__)

MONGODB_URI = os.getenv("MONGODB_URI", "mongodb://localhost:27017")
MONGODB_DB = os.getenv("MONGODB_DB", "google_ms")
GRPC_PORT  = int(os.getenv("PORT", "5054"))

# ── MongoDB client and connection ─────────────────────────────────────────

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

async def get_carts_collection():
    """Get the carts collection."""
    client = await get_mongodb_client()
    db = client[MONGODB_DB]
    collection = db["carts"]
    
    # Ensure unique index on _id (cart key)
    # await collection.create_index("_id", unique=True)
    return collection


# ── gRPC Servicer ───────────────────────────────────────────────────────────

class CartServicer(demo_pb2_grpc.CartServiceServicer):
    """gRPC implementation – identical wire interface to the original C# service."""

    # RPCs -------------------------------------------------------------------
    async def AddItem(self, request, context):
        """Add item to cart."""
        try:
            user_id = request.user_id
            product_id = request.item.product_id
            quantity = request.item.quantity

            logger.info("AddItem called | user=%s product=%s qty=%d",
                        user_id, product_id, quantity)

            # Invoke cart agent for intelligent item addition
            state = await run_cart_agent(
                operation_type="ADD_ITEM",
                user_id=user_id,
                product_id=product_id,
                quantity=quantity,
            )

            decision_status = state.get("decision", {}).get("status", "REJECTED")
            logger.info("AddItem agent decision: %s | user=%s product=%s",
                        decision_status, user_id, product_id)

            return demo_pb2.AddItemResponse(llm_metrics=demo_pb2.LLMMetrics(
                total_input_tokens=state.get("total_input_tokens", 0),
                total_output_tokens=state.get("total_output_tokens", 0),
                total_llm_calls=state.get("total_llm_calls", 0),
            ))

        except Exception as e:
            logger.error("AddItem failed: %s", str(e), exc_info=True)
            if context:
                context.set_details(f"Failed to add item: {str(e)}")
            return demo_pb2.AddItemResponse(llm_metrics=demo_pb2.LLMMetrics())

    async def GetCart(self, request, context):
        """Get user's cart."""
        try:
            user_id = request.user_id
            logger.info("GetCart called | user=%s", user_id)

            # Invoke cart agent for cart retrieval
            state = await run_cart_agent(
                operation_type="GET_CART",
                user_id=user_id,
            )

            # Extract items from operation result
            operation_result = state.get("operation_result", {})
            items_dict = operation_result.get("items", {})

            # Convert to proto CartItems
            cart_items = [
                demo_pb2.CartItem(product_id=pid, quantity=int(qty))
                for pid, qty in items_dict.items()
            ]

            logger.info("GetCart returning %d items | user=%s", len(cart_items), user_id)
            return demo_pb2.GetCartResponse(cart=demo_pb2.Cart(user_id=user_id, items=cart_items), llm_metrics=demo_pb2.LLMMetrics())

        except Exception as e:
            logger.error("GetCart failed: %s", str(e), exc_info=True)
            if context:
                context.set_details(f"Failed to get cart: {str(e)}")
            return demo_pb2.GetCartResponse(cart=demo_pb2.Cart(user_id=user_id, items=[]), llm_metrics=demo_pb2.LLMMetrics())

    async def EmptyCart(self, request, context):
        """Empty user's cart."""
        try:
            user_id = request.user_id
            logger.info("EmptyCart called | user=%s", user_id)

            # Invoke cart agent for cart emptying
            state = await run_cart_agent(
                operation_type="EMPTY_CART",
                user_id=user_id,
            )

            decision_status = state.get("decision", {}).get("status", "REJECTED")
            logger.info("EmptyCart agent decision: %s | user=%s",
                        decision_status, user_id)

            return demo_pb2.EmptyCartResponse(llm_metrics=demo_pb2.LLMMetrics(
                total_input_tokens=state.get("total_input_tokens", 0),
                total_output_tokens=state.get("total_output_tokens", 0),
                total_llm_calls=state.get("total_llm_calls", 0),
            ))

        except Exception as e:
            logger.error("EmptyCart failed: %s", str(e), exc_info=True)
            if context:
                context.set_details(f"Failed to empty cart: {str(e)}")
            return demo_pb2.EmptyCartResponse(llm_metrics=demo_pb2.LLMMetrics())


# ── FastAPI (REST proxy + health) ────────────────────────────────────────────

app = make_health_app("cartagent")

class AddItemBody(BaseModel):
    product_id: str
    quantity: int = 1

@app.post("/cart/{user_id}/items", summary="Add item to cart (REST proxy)")
async def rest_add_item(user_id: str, body: AddItemBody):
    servicer = CartServicer()
    req = demo_pb2.AddItemRequest(
        user_id=user_id,
        item=demo_pb2.CartItem(product_id=body.product_id, quantity=body.quantity),
    )
    await servicer.AddItem(req, None)
    return {"status": "ok"}

@app.get("/cart/{user_id}", summary="Get cart (REST proxy)")
async def rest_get_cart(user_id: str):
    servicer = CartServicer()
    result = await servicer.GetCart(demo_pb2.GetCartRequest(user_id=user_id), None)
    return {
        "user_id": result.user_id,
        "items": [{"product_id": i.product_id, "quantity": i.quantity} for i in result.items],
    }

@app.delete("/cart/{user_id}", summary="Empty cart (REST proxy)")
async def rest_empty_cart(user_id: str):
    servicer = CartServicer()
    await servicer.EmptyCart(demo_pb2.EmptyCartRequest(user_id=user_id), None)
    return {"status": "ok"}


# ── Entry point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    run_service(
        demo_pb2_grpc.add_CartServiceServicer_to_server,
        CartServicer(),
        GRPC_PORT,
        app,
    )
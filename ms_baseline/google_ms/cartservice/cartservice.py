"""
cartservice/main.py

Replaces the original C# cartservice.
- gRPC server on port 7070  (same as original)
- FastAPI HTTP server on port 8070  (health + REST proxy)
- Cart state stored in MongoDB
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

logger = logging.getLogger(__name__)

MONGODB_URI = os.getenv("MONGODB_URI", "mongodb://user:pass1@localhost:27017")
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

    async def _get_items(self, user_id: str) -> list[dict]:
        """Get cart items from MongoDB."""
        key = f"cart:{user_id}"
        collection = await get_carts_collection()
        cart_doc = await collection.find_one({"_id": key})
        
        if cart_doc is None:
            return []
        
        # Return items as list of dicts with product_id and quantity
        items = cart_doc.get("items", {})
        return [{"product_id": pid, "quantity": qty} for pid, qty in items.items()]

    async def _save_cart(self, user_id: str, items: dict) -> None:
        """Save cart items to MongoDB."""
        key = f"cart:{user_id}"
        collection = await get_carts_collection()
        
        if not items:
            # If no items, delete the document
            await collection.delete_one({"_id": key})
        else:
            # Upsert cart document
            await collection.update_one(
                {"_id": key},
                {"$set": {"items": items}},
                upsert=True
            )

    # RPCs -------------------------------------------------------------------
    async def AddItem(self, request, context):
        """Add item to cart."""
        try:
            key = f"cart:{request.user_id}"
            pid = request.item.product_id
            qty = request.item.quantity
            
            collection = await get_carts_collection()
            
            # Get current items
            cart_doc = await collection.find_one({"_id": key})
            items = cart_doc.get("items", {}) if cart_doc else {}
            
            # Update quantity
            current_qty = int(items.get(pid, 0))
            items[pid] = current_qty + qty
            
            # Save back
            await self._save_cart(request.user_id, items)
            
            logger.info("AddItem user=%s product=%s qty=%d", request.user_id, pid, qty)
            return demo_pb2.Empty()
        except Exception as e:
            logger.error("AddItem failed: %s", str(e), exc_info=True)
            if context:
                context.set_details(f"Failed to add item: {str(e)}")
            return demo_pb2.Empty()

    async def GetCart(self, request, context):
        """Get user's cart."""
        try:
            items = await self._get_items(request.user_id)
            cart_items = [
                demo_pb2.CartItem(product_id=i["product_id"], quantity=i["quantity"])
                for i in items
            ]
            return demo_pb2.Cart(user_id=request.user_id, items=cart_items)
        except Exception as e:
            logger.error("GetCart failed: %s", str(e), exc_info=True)
            if context:
                context.set_details(f"Failed to get cart: {str(e)}")
            return demo_pb2.Cart(user_id=request.user_id, items=[])

    async def EmptyCart(self, request, context):
        """Empty user's cart."""
        try:
            key = f"cart:{request.user_id}"
            collection = await get_carts_collection()
            await collection.delete_one({"_id": key})
            logger.info("EmptyCart user=%s", request.user_id)
            return demo_pb2.Empty()
        except Exception as e:
            logger.error("EmptyCart failed: %s", str(e), exc_info=True)
            if context:
                context.set_details(f"Failed to empty cart: {str(e)}")
            return demo_pb2.Empty()


# ── FastAPI (REST proxy + health) ────────────────────────────────────────────

app = make_health_app("cartservice")

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
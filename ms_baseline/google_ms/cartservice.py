"""
cartservice/main.py

Replaces the original C# cartservice.
- gRPC server on port 7070  (same as original)
- FastAPI HTTP server on port 8070  (health + REST proxy)
- Cart state stored in Redis (same as original)
"""

import asyncio
import logging
import os
import sys

import grpc
import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from shared import demo_pb2
from shared import demo_pb2_grpc
from shared.base_service import make_health_app, run_service

logger = logging.getLogger(__name__)

REDIS_ADDR = os.getenv("REDIS_ADDR", "localhost:6385")
GRPC_PORT  = int(os.getenv("PORT", "7070"))

# ── Redis client ────────────────────────────────────────────────────────────

def _redis_client() -> aioredis.Redis:
    host, port = REDIS_ADDR.rsplit(":", 1)
    return aioredis.Redis(host=host, port=int(port), decode_responses=True, password="1")


# ── gRPC Servicer ───────────────────────────────────────────────────────────

class CartServicer(demo_pb2_grpc.CartServiceServicer):
    """gRPC implementation – identical wire interface to the original C# service."""

    def __init__(self):
        self.redis = _redis_client()

    # helpers ----------------------------------------------------------------
    async def _get_items(self, user_id: str) -> list[dict]:
        raw = await self.redis.hgetall(f"cart:{user_id}")
        return [{"product_id": pid, "quantity": int(qty)} for pid, qty in raw.items()]

    # RPCs -------------------------------------------------------------------
    async def AddItem(self, request, context):
        key = f"cart:{request.user_id}"
        pid = request.item.product_id
        current = int(await self.redis.hget(key, pid) or 0)
        await self.redis.hset(key, pid, current + request.item.quantity)
        logger.info("AddItem user=%s product=%s qty=%d", request.user_id, pid, request.item.quantity)
        return demo_pb2.Empty()

    async def GetCart(self, request, context):
        items = await self._get_items(request.user_id)
        cart_items = [
            demo_pb2.CartItem(product_id=i["product_id"], quantity=i["quantity"])
            for i in items
        ]
        return demo_pb2.Cart(user_id=request.user_id, items=cart_items)

    async def EmptyCart(self, request, context):
        await self.redis.delete(f"cart:{request.user_id}")
        logger.info("EmptyCart user=%s", request.user_id)
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
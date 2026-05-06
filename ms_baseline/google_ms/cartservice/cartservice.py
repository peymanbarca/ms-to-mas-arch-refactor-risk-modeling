"""
cartservice/main.py

Replaces the original C# cartservice.
- gRPC server on port 7070  (same as original)
- FastAPI HTTP server on port 8070  (health + REST proxy)
- Cart state stored in MongoDB

Proto response types (updated):
    AddItem(AddItemRequest)     → AddItemResponse   { llm_metrics }
    GetCart(GetCartRequest)     → GetCartResponse   { cart, llm_metrics }
    EmptyCart(EmptyCartRequest) → EmptyCartResponse { llm_metrics }

Root cause of the "returns None" bug
─────────────────────────────────────
The gRPC framework serialises the value your servicer method returns.
If the return type does not match the proto-registered response class the
framework gets None from protobuf and sends back an empty/null response.

Fixes applied:
  • GetCart   → return GetCartResponse(cart=Cart(...), llm_metrics=...)
                NOT Cart(...) directly
  • AddItem   → return AddItemResponse(llm_metrics=...)
                NOT Empty()
  • EmptyCart → return EmptyCartResponse(llm_metrics=...)
                NOT Empty()
  • REST /cart/{user_id} unwraps resp.cart before serialising to JSON
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
MONGODB_DB  = os.getenv("MONGODB_DB",  "google_ms")
GRPC_PORT   = int(os.getenv("PORT", "5054"))




# ── MongoDB helpers ───────────────────────────────────────────────────────────

_mongodb_client: AsyncIOMotorClient | None = None


async def get_mongodb_client() -> AsyncIOMotorClient:
    """Get or create the singleton MongoDB client."""
    global _mongodb_client
    if _mongodb_client is None:
        _mongodb_client = AsyncIOMotorClient(MONGODB_URI)
        await _mongodb_client.admin.command("ping")
        logger.info("Connected to MongoDB at %s", MONGODB_URI)
    return _mongodb_client


async def get_carts_collection():
    """Return the carts collection."""
    client = await get_mongodb_client()
    return client[MONGODB_DB]["carts"]


# ── gRPC Servicer ─────────────────────────────────────────────────────────────

class CartServicer(demo_pb2_grpc.CartServiceServicer):
    """
    gRPC implementation – wire-compatible with the original C# cartservice,
    updated to return the new proto response wrapper types.
    """

    # ── private MongoDB helpers ───────────────────────────────────────────────

    async def _get_items(self, user_id: str) -> dict[str, int]:
        """Return current cart as { product_id: quantity }. Empty dict if none."""
        col      = await get_carts_collection()
        cart_doc = await col.find_one({"_id": f"cart:{user_id}"})
        if cart_doc is None:
            return {}
        return cart_doc.get("items", {})

    async def _save_cart(self, user_id: str, items: dict[str, int]) -> None:
        """
        Upsert cart document.
        Deletes the document when items is empty (keeps collection lean,
        consistent with the original Redis DEL behaviour).
        """
        key = f"cart:{user_id}"
        col = await get_carts_collection()
        if not items:
            await col.delete_one({"_id": key})
        else:
            await col.update_one(
                {"_id": key},
                {"$set": {"items": items}},
                upsert=True,
            )

    # ── AddItem ───────────────────────────────────────────────────────────────

    async def AddItem(
        self,
        request: demo_pb2.AddItemRequest,
        context: grpc.aio.ServicerContext,
    ) -> demo_pb2.AddItemResponse:
        """
        FIX: return AddItemResponse(llm_metrics=...) instead of Empty().

        Proto: rpc AddItem(AddItemRequest) returns (AddItemResponse) {}
        """
        try:
            pid   = request.item.product_id
            qty   = request.item.quantity
            items = await self._get_items(request.user_id)
            items[pid] = int(items.get(pid, 0)) + qty
            await self._save_cart(request.user_id, items)

            logger.info(
                "AddItem user=%s product=%s qty=%d total_qty=%d",
                request.user_id, pid, qty, items[pid],
            )
            return demo_pb2.AddItemResponse(llm_metrics=demo_pb2.LLMMetrics(
                total_input_tokens=-1,
                total_output_tokens=-1,
                total_llm_calls=-1,
            ))

        except Exception as exc:
            logger.error("AddItem failed: %s", exc, exc_info=True)
            if context:
                await context.abort(
                    grpc.StatusCode.INTERNAL,
                    f"Failed to add item: {exc}",
                )
            return demo_pb2.AddItemResponse(llm_metrics=_empty_metrics())

    # ── GetCart ───────────────────────────────────────────────────────────────

    async def GetCart(
        self,
        request: demo_pb2.GetCartRequest,
        context: grpc.aio.ServicerContext,
    ) -> demo_pb2.GetCartResponse:
        """
        FIX: return GetCartResponse(cart=Cart(...), llm_metrics=...)
             instead of Cart(...) directly.

        Proto: rpc GetCart(GetCartRequest) returns (GetCartResponse) {}
               message GetCartResponse { Cart cart = 1; LLMMetrics llm_metrics = 2; }

        Returning Cart directly caused the gRPC framework to receive a value
        whose message descriptor doesn't match GetCartResponse.  The framework
        cannot serialise a mismatched type, so it produces an empty/None response
        on the wire — which is the bug the caller observed.
        """
        try:
            items_dict = await self._get_items(request.user_id)
            cart_items = [
                demo_pb2.CartItem(product_id=pid, quantity=qty)
                for pid, qty in items_dict.items()
            ]
            cart = demo_pb2.Cart(user_id=request.user_id, items=cart_items)

            logger.info(
                "GetCart user=%s items=%d",
                request.user_id, len(cart_items),
            )

            # ✓ Correct: wrap Cart inside GetCartResponse
            return demo_pb2.GetCartResponse(
                cart=cart,
                llm_metrics=demo_pb2.LLMMetrics(
                total_input_tokens=-1,
                total_output_tokens=-1,
                total_llm_calls=-1,
            ))

        except Exception as exc:
            logger.error("GetCart failed: %s", exc, exc_info=True)
            if context:
                await context.abort(
                    grpc.StatusCode.INTERNAL,
                    f"Failed to get cart: {exc}",
                )
            # Still return the correct wrapper type on error
            return demo_pb2.GetCartResponse(
                cart=demo_pb2.Cart(user_id=request.user_id, items=[]),
                llm_metrics=_empty_metrics(),
            )

    # ── EmptyCart ─────────────────────────────────────────────────────────────

    async def EmptyCart(
        self,
        request: demo_pb2.EmptyCartRequest,
        context: grpc.aio.ServicerContext,
    ) -> demo_pb2.EmptyCartResponse:
        """
        FIX: return EmptyCartResponse(llm_metrics=...) instead of Empty().

        Proto: rpc EmptyCart(EmptyCartRequest) returns (EmptyCartResponse) {}
        """
        try:
            col    = await get_carts_collection()
            result = await col.delete_one({"_id": f"cart:{request.user_id}"})

            logger.info(
                "EmptyCart user=%s deleted=%s",
                request.user_id,
                result.deleted_count > 0,
            )
            return demo_pb2.EmptyCartResponse(llm_metrics=demo_pb2.LLMMetrics(
                total_input_tokens=-1,
                total_output_tokens=-1,
                total_llm_calls=-1,
            ))

        except Exception as exc:
            logger.error("EmptyCart failed: %s", exc, exc_info=True)
            if context:
                await context.abort(
                    grpc.StatusCode.INTERNAL,
                    f"Failed to empty cart: {exc}",
                )
            return demo_pb2.EmptyCartResponse(llm_metrics=_empty_metrics())


# ── FastAPI REST layer ────────────────────────────────────────────────────────

app = make_health_app("cartservice")


class AddItemBody(BaseModel):
    product_id: str
    quantity:   int = 1


@app.post("/cart/{user_id}/items", summary="Add item to cart (REST proxy)")
async def rest_add_item(user_id: str, body: AddItemBody):
    servicer = CartServicer()
    await servicer.AddItem(
        demo_pb2.AddItemRequest(
            user_id=user_id,
            item=demo_pb2.CartItem(product_id=body.product_id, quantity=body.quantity),
        ),
        None,
    )
    return {"status": "ok"}


@app.get("/cart/{user_id}", summary="Get cart (REST proxy)")
async def rest_get_cart(user_id: str):
    servicer = CartServicer()
    # ✓ Unwrap GetCartResponse.cart before serialising to JSON
    resp: demo_pb2.GetCartResponse = await servicer.GetCart(
        demo_pb2.GetCartRequest(user_id=user_id), None
    )
    cart = resp.cart
    return {
        "user_id": cart.user_id,
        "items": [
            {"product_id": i.product_id, "quantity": i.quantity}
            for i in cart.items
        ],
    }


@app.delete("/cart/{user_id}", summary="Empty cart (REST proxy)")
async def rest_empty_cart(user_id: str):
    servicer = CartServicer()
    await servicer.EmptyCart(
        demo_pb2.EmptyCartRequest(user_id=user_id), None
    )
    return {"status": "ok"}


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    run_service(
        demo_pb2_grpc.add_CartServiceServicer_to_server,
        CartServicer(),
        GRPC_PORT,
        app,
    )
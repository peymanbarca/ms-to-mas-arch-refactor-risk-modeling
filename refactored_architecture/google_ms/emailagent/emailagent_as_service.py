"""
emailservice/main.py

Python / FastAPI + gRPC server for EmailService.

gRPC port  : 5056  (env PORT)
HTTP port  : 6056  (env HTTP_PORT)

Startup sequence:
    1. MongoDB client created → agent.db wired
    2. gRPC server starts    → EmailService + HealthService registered
    3. FastAPI HTTP starts   → /health, /ready, POST /send-confirmation

FastAPI REST endpoints:
    GET  /health              – liveness probe
    GET  /ready               – readiness probe
    POST /send-confirmation   – REST proxy for SendOrderConfirmation RPC
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

import grpc
from fastapi import FastAPI, HTTPException
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))
from ..shared import demo_pb2
from ..shared import demo_pb2_grpc
from ..shared.base_service import make_health_app
from .servicer import EmailServicer
from . import emailagent as _email_agent

logger = logging.getLogger("emailagent")

GRPC_PORT = int(os.getenv("PORT", "5056"))



# ════════════════════════════════════════════════════════════════════════════
# FastAPI application
# ════════════════════════════════════════════════════════════════════════════

app = make_health_app("emailagent")

_servicer = EmailServicer()


# ── Pydantic request model ────────────────────────────────────────────────────

class AddressIn(BaseModel):
    street_address: str = ""
    city:    str = ""
    state:   str = ""
    country: str = ""
    zip_code: int = 0


class MoneyIn(BaseModel):
    currency_code: str = "USD"
    units: int = 0
    nanos: int = 0


class OrderItemIn(BaseModel):
    product_id: str
    quantity:   int = 1
    cost:       MoneyIn = MoneyIn()


class OrderResultIn(BaseModel):
    order_id:             str
    shipping_tracking_id: str = ""
    shipping_cost:        MoneyIn = MoneyIn()
    shipping_address:     AddressIn = AddressIn()
    items:                list[OrderItemIn] = []


class SendConfirmationIn(BaseModel):
    email: str
    order: OrderResultIn


def _build_order_proto(r: OrderResultIn) -> demo_pb2.OrderResult:
    """Convert REST Pydantic model → proto OrderResult."""
    items = [
        demo_pb2.OrderItem(
            item=demo_pb2.CartItem(
                product_id=it.product_id, quantity=it.quantity
            ),
            cost=demo_pb2.Money(
                currency_code=it.cost.currency_code,
                units=it.cost.units,
                nanos=it.cost.nanos,
            ),
        )
        for it in r.items
    ]
    return demo_pb2.OrderResult(
        order_id=r.order_id,
        shipping_tracking_id=r.shipping_tracking_id,
        shipping_cost=demo_pb2.Money(
            currency_code=r.shipping_cost.currency_code,
            units=r.shipping_cost.units,
            nanos=r.shipping_cost.nanos,
        ),
        shipping_address=demo_pb2.Address(
            street_address=r.shipping_address.street_address,
            city=r.shipping_address.city,
            state=r.shipping_address.state,
            country=r.shipping_address.country,
            zip_code=r.shipping_address.zip_code,
        ),
        items=items,
    )


# ── POST /send-confirmation ───────────────────────────────────────────────────

@app.post(
    "/send-confirmation",
    summary="Send order confirmation email (REST proxy for SendOrderConfirmation RPC)",
    description=(
        "Runs the LangGraph email agent:\n\n"
        "1. Prepare order data\n"
        "2. Generate personalised message (Ollama llama3)\n"
        "3. Render Jinja2 HTML template\n"
        "4. Send / log email\n"
        "5. Persist audit log to MongoDB\n\n"
        "Returns send_status and LLM metrics."
    ),
)
async def post_send_confirmation(body: SendConfirmationIn):
    order_proto = _build_order_proto(body.order)
    try:
        final_state = await _email_agent.run_email_agent(
            recipient_email=body.email,
            order_proto=order_proto,
        )
    except Exception as exc:
        logger.error("POST /send-confirmation error: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))

    if final_state.get("error") and "Template rendering" in (final_state.get("error") or ""):
        raise HTTPException(status_code=500, detail=final_state["error"])

    cents = body.order.shipping_cost.nanos // 10_000_000
    return {
        "order_id":            body.order.order_id,
        "recipient_email":     body.email,
        "send_status":         final_state.get("send_status"),
        "personalised_message": final_state.get("personalised_message"),
        "llm_metrics": {
            "input_tokens":  final_state["total_input_tokens"],
            "output_tokens": final_state["total_output_tokens"],
            "llm_calls":     final_state["total_llm_calls"],
        },
        "error": final_state.get("error"),
    }


# ════════════════════════════════════════════════════════════════════════════
# gRPC server builder
# ════════════════════════════════════════════════════════════════════════════

def _build_grpc_server(servicer: EmailServicer, port: int) -> grpc.aio.Server:
    server = grpc.aio.server()
    demo_pb2_grpc.add_EmailServiceServicer_to_server(servicer, server)
    # health_pb2_grpc.add_HealthServicer_to_server(servicer, server)
    SERVICE_NAMES = (
        demo_pb2.DESCRIPTOR.services_by_name["EmailService"].full_name,
        # grpc_reflection.SERVICE_NAME,
    )
    # grpc_reflection.enable_server_reflection(SERVICE_NAMES, server)
    server.add_insecure_port(f"[::]:{port}")
    return server


# ════════════════════════════════════════════════════════════════════════════
# Entry point
# ════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(
        level=logging.INFO,
        format='{"severity":"%(levelname)s","time":"%(asctime)s",'
               '"name":"%(name)s","message":"%(message)s"}',
    )

    http_port = int(os.getenv("HTTP_PORT", GRPC_PORT + 1000))

    async def _main() -> None:


        grpc_server = _build_grpc_server(_servicer, GRPC_PORT)
        await grpc_server.start()
        logger.info("EmailService gRPC started on port %d", GRPC_PORT)
        logger.info("EmailService HTTP started on port %d", http_port)

        http_cfg = uvicorn.Config(
            app, host="0.0.0.0", port=http_port,
            log_level="info", access_log=False,
        )

        try:
            await asyncio.gather(
                grpc_server.wait_for_termination(),
                uvicorn.Server(http_cfg).serve(),
            )
        finally:
            logger.info("MongoDB connection closed")

    asyncio.run(_main())
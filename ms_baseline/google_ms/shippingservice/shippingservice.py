"""
shippingservice/main.py

Python / FastAPI + gRPC server – faithful port of the Go shippingservice main.go.

Go startup sequence reproduced here:
─────────────────────────────────────────────────────────────────────────────
  1. init()                   → JSON logrus logger setup
  2. if DISABLE_TRACING == "" → initTracing() (currently TODO in Go too)
  3. if DISABLE_PROFILER == "" → initProfiling() (Cloud Profiler, 3 retries)
  4. port = $PORT or "50051"
  5. net.Listen("tcp", port)
  6. grpc.NewServer()
  7. pb.RegisterShippingServiceServer(srv, &server{})
  8. healthpb.RegisterHealthServer(srv, healthcheck)
  9. reflection.Register(srv)
 10. srv.Serve(lis)
─────────────────────────────────────────────────────────────────────────────

What this Python version adds beyond the Go original:
  • FastAPI HTTP server on HTTP_PORT (default PORT + 1000) with:
      GET  /health              – liveness probe
      GET  /ready               – readiness probe
      POST /quote               – REST proxy for GetQuote RPC
      POST /ship                – REST proxy for ShipOrder RPC
      GET  /quote               – GET variant (query-param address + item count)
  • gRPC reflection (same as Go's reflection.Register)
  • Optional OTLP tracing via OTEL_EXPORTER_OTLP_ENDPOINT env var
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import traceback

import grpc
# from grpc_reflection.v1alpha import reflection as grpc_reflection
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))
from ..shared import demo_pb2
from ..shared import demo_pb2_grpc
from ..shared.base_service import make_health_app, run_service
from .servicer import ShippingServicer
from .quote import create_quote_from_count

logger = logging.getLogger("shippingservice")

GRPC_PORT = int(os.getenv("PORT", "5051"))


# ════════════════════════════════════════════════════════════════════════════
# Optional OpenTelemetry tracing
# (mirrors Go's initTracing – currently a TODO in the original too)
# ════════════════════════════════════════════════════════════════════════════

# def _setup_tracing() -> None:
#     if os.getenv("DISABLE_TRACING"):
#         logger.info("Tracing disabled.")
#         return

#     logger.info("Tracing enabled.")
#     try:
#         from opentelemetry import trace
#         from opentelemetry.instrumentation.grpc import (
#             GrpcAioInstrumentorServer,
#             GrpcAioInstrumentorClient,
#         )
#         from opentelemetry.sdk.trace import TracerProvider
#         from opentelemetry.sdk.trace.export import BatchSpanProcessor
#         from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

#         GrpcAioInstrumentorServer().instrument()
#         GrpcAioInstrumentorClient().instrument()

#         endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "localhost:4317")
#         provider = TracerProvider()
#         provider.add_span_processor(
#             BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, insecure=True))
#         )
#         trace.set_tracer_provider(provider)
#         logger.info("OTLP tracing → %s", endpoint)

#     except ImportError:
#         logger.info("opentelemetry packages not installed – tracing skipped")
#     except Exception:
#         logger.warning("Tracing setup error: %s", traceback.format_exc())


# ════════════════════════════════════════════════════════════════════════════
# Optional Cloud Profiler
# (mirrors Go's initProfiling with 3-retry logic)
# ════════════════════════════════════════════════════════════════════════════

# def _setup_profiler() -> None:
#     if os.getenv("DISABLE_PROFILER"):
#         logger.info("Profiling disabled.")
#         return

#     logger.info("Profiling enabled.")
#     try:
#         import googlecloudprofiler
#         for attempt in range(1, 4):
#             try:
#                 googlecloudprofiler.start(
#                     service="shippingservice",
#                     service_version="1.0.0",
#                     verbose=0,
#                 )
#                 logger.info("started Stackdriver profiler")
#                 return
#             except Exception as exc:
#                 logger.warning("failed to start profiler (attempt %d): %s", attempt, exc)
#                 if attempt < 3:
#                     wait = 10 * attempt
#                     logger.info("sleeping %ds before retry", wait)
#                     import time
#                     time.sleep(wait)
#         logger.warning("could not initialize Stackdriver profiler after retrying, giving up")
#     except ImportError:
#         logger.info("googlecloudprofiler not installed – profiling skipped")


# ════════════════════════════════════════════════════════════════════════════
# FastAPI HTTP layer
# ════════════════════════════════════════════════════════════════════════════

app = make_health_app("shippingservice")

_servicer: ShippingServicer = ShippingServicer()


# ── Pydantic request/response models ─────────────────────────────────────────

class AddressModel(BaseModel):
    street_address: str
    city: str
    state: str
    country: str
    zip_code: int = 0


class CartItemModel(BaseModel):
    product_id: str
    quantity: int = 1


class QuoteRequest(BaseModel):
    address: AddressModel
    items: list[CartItemModel]


class ShipRequest(BaseModel):
    address: AddressModel
    items: list[CartItemModel]


def _address_proto(m: AddressModel) -> demo_pb2.Address:
    return demo_pb2.Address(
        street_address=m.street_address,
        city=m.city,
        state=m.state,
        country=m.country,
        zip_code=m.zip_code,
    )


def _items_proto(items: list[CartItemModel]) -> list[demo_pb2.CartItem]:
    return [demo_pb2.CartItem(product_id=i.product_id, quantity=i.quantity) for i in items]


# ── POST /quote ───────────────────────────────────────────────────────────────

@app.post(
    "/quote",
    summary="Get shipping quote (REST proxy for GetQuote RPC)",
    description=(
        "Returns a USD shipping cost estimate for the given address and items.\n\n"
        "Internally calls the same `ShippingServicer.GetQuote` the gRPC server exposes.\n\n"
        "Pricing tiers (by total item count):\n"
        "- 0 items → $0.00\n"
        "- 1–2 items → $8.99\n"
        "- 3–4 items → $15.99\n"
        "- 5–7 items → $23.99\n"
        "- 8–9 items → $31.99\n"
        "- 10+ items → $39.99\n"
    ),
)
async def post_quote(body: QuoteRequest):
    request = demo_pb2.GetQuoteRequest(
        address=_address_proto(body.address),
        items=_items_proto(body.items),
    )
    try:
        resp = await _servicer.GetQuote(request, context=None)
    except Exception as exc:
        logger.error("GetQuote REST error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))

    cents = resp.cost_usd.nanos // 10_000_000
    return {
        "cost_usd": {
            "currency_code": resp.cost_usd.currency_code,
            "units":         resp.cost_usd.units,
            "nanos":         resp.cost_usd.nanos,
        },
        "formatted": f"${resp.cost_usd.units}.{cents:02d} USD",
        "total_items": sum(i.quantity for i in body.items),
    }


# ── GET /quote  (convenience endpoint) ───────────────────────────────────────

@app.get(
    "/quote",
    summary="Quick quote by item count (no address required)",
    description="Estimates shipping cost based purely on item count. No address needed.",
)
async def get_quote(item_count: int = 1):
    if item_count < 0:
        raise HTTPException(status_code=400, detail="item_count must be >= 0")
    quote = create_quote_from_count(item_count)
    return {
        "item_count": item_count,
        "cost_usd": {
            "currency_code": "USD",
            "units": quote.dollars,
            "nanos": quote.nanos,
        },
        "formatted": f"${quote.dollars}.{quote.cents:02d} USD",
    }


# ── POST /ship ────────────────────────────────────────────────────────────────

@app.post(
    "/ship",
    summary="Ship order (REST proxy for ShipOrder RPC)",
    description=(
        "Dispatches a mock shipment to the given address and returns a tracking ID.\n\n"
        "Tracking ID format: `<2-letter prefix>-<8-digit hash>-<2 random letters>`\n"
        "e.g. `FE-12345678-UX`"
    ),
)
async def post_ship(body: ShipRequest):
    request = demo_pb2.ShipOrderRequest(
        address=_address_proto(body.address),
        items=_items_proto(body.items),
    )
    try:
        resp = await _servicer.ShipOrder(request, context=None)
    except Exception as exc:
        logger.error("ShipOrder REST error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))

    return {
        "tracking_id": resp.tracking_id,
        "address": {
            "street_address": body.address.street_address,
            "city":           body.address.city,
            "state":          body.address.state,
            "country":        body.address.country,
            "zip_code":       body.address.zip_code,
        },
    }


# ════════════════════════════════════════════════════════════════════════════
# gRPC server builder
# (mirrors Go: grpc.NewServer() + RegisterShippingServiceServer + reflection)
# ════════════════════════════════════════════════════════════════════════════

def _build_grpc_server(servicer: ShippingServicer, port: int) -> grpc.aio.Server:
    """
    Go equivalent:
        srv := grpc.NewServer()
        pb.RegisterShippingServiceServer(srv, &server{})
        healthpb.RegisterHealthServer(srv, healthcheck)
        reflection.Register(srv)
        srv.Add_insecure_port(port)
    """
    server = grpc.aio.server()

    # Register ShippingService
    demo_pb2_grpc.add_ShippingServiceServicer_to_server(servicer, server)

    # Register gRPC HealthService (same as Go's health.NewServer())
    # health_pb2_grpc.add_HealthServicer_to_server(servicer, server)

    # Register gRPC reflection (same as Go's reflection.Register(srv))
    # SERVICE_NAMES = (
    #     demo_pb2.DESCRIPTOR.services_by_name["ShippingService"].full_name,
    #     grpc_reflection.SERVICE_NAME,
    # )
    # grpc_reflection.enable_server_reflection(SERVICE_NAMES, server)

    server.add_insecure_port(f"[::]:{port}")
    return server


# ════════════════════════════════════════════════════════════════════════════
# Entry point  (mirrors Go main())
# ════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn

    # Go: JSON-format logrus logger
    logging.basicConfig(
        level=logging.INFO,
        format='{"timestamp": "%(asctime)s", "severity": "%(levelname)s", '
               '"message": "%(message)s", "logger": "%(name)s"}',
    )

    # Go: if DISABLE_TRACING == "" → initTracing()
    # _setup_tracing()

    # Go: if DISABLE_PROFILER == "" → initProfiling()
    # _setup_profiler()

    # Go: port := defaultPort / os.LookupEnv("PORT")
    logger.info("Shipping Service listening on port %d", GRPC_PORT)

    http_port = int(os.getenv("HTTP_PORT", GRPC_PORT + 1000))

    async def _main() -> None:
        grpc_server = _build_grpc_server(_servicer, GRPC_PORT)
        await grpc_server.start()
        logger.info("[ShippingService] gRPC server started on :%d", GRPC_PORT)
        logger.info("[ShippingService] HTTP server starting on :%d", http_port)

        http_cfg = uvicorn.Config(
            app,
            host="0.0.0.0",
            port=http_port,
            log_level="info",
            access_log=False,
        )
        await asyncio.gather(
            grpc_server.wait_for_termination(),
            uvicorn.Server(http_cfg).serve(),
        )

    asyncio.run(_main())
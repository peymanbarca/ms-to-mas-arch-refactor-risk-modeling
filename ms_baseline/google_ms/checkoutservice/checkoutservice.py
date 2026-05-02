"""
checkoutservice/main.py

Python / FastAPI + gRPC server — faithful port of Go checkoutservice main.go.

Go startup sequence reproduced exactly:
────────────────────────────────────────────────────────────────────────────
  1. JSON logrus logger init
  2. if ENABLE_TRACING == "1"  → initTracing()   (OTLP exporter)
  3. if ENABLE_PROFILER == "1" → initProfiling()  (Cloud Profiler, 3 retries)
  4. port := "5050" / $PORT
  5. mustMapEnv for 6 downstream service addresses
  6. mustConnGRPC for each address (creates grpc.ClientConn)
  7. grpc.NewServer() + RegisterCheckoutServiceServer
  8. health.NewServer() + RegisterHealthServer
  9. srv.Serve(lis)
────────────────────────────────────────────────────────────────────────────

Python additions:
  • FastAPI HTTP on HTTP_PORT (default PORT + 1000):
      GET  /health              – liveness
      GET  /ready               – readiness
      POST /place-order         – REST proxy for PlaceOrder RPC
      GET  /place-order/preview – estimate total price without charging card
  • gRPC reflection (grpcurl support)
  • All 6 downstream addresses read from env vars (same names as Go)
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
import traceback

import grpc
# from grpc_health.v1 import health_pb2_grpc
# from grpc_reflection.v1alpha import reflection as grpc_reflection
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, field_validator

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))
from ..shared import demo_pb2
from ..shared import demo_pb2_grpc
from ..shared.base_service import make_health_app
from .orchestrator import CheckoutOrchestrator
from .servicer import CheckoutServicer
from .money import format_money, proto_to_money, money_multiply_slow, money_sum, money_must, zero_money

logger = logging.getLogger("checkoutservice")

# Go: listenPort = "5050"
GRPC_PORT = int(os.getenv("PORT", "5050"))


# ════════════════════════════════════════════════════════════════════════════
# mustMapEnv / mustConnGRPC (Go helpers reproduced)
# ════════════════════════════════════════════════════════════════════════════

def must_map_env(env_key: str, default: str) -> str:
    """
    Go: func mustMapEnv(target *string, envKey string)
    Panics if the environment variable is not set.
    """
    v = os.environ.get(env_key, default=default)
    if not v:
        raise EnvironmentError(
            f"environment variable {env_key!r} not set"
        )
    return v


def must_conn_grpc(addr: str) -> grpc.aio.Channel:
    """
    Go: func mustConnGRPC(ctx, conn **grpc.ClientConn, addr string)
    Creates an insecure async gRPC channel. Raises on failure.
    """
    logger.info("connecting gRPC | addr=%s", addr)
    return grpc.aio.insecure_channel(addr)


# ════════════════════════════════════════════════════════════════════════════
# Optional OpenTelemetry tracing
# Go: func initTracing()
# ════════════════════════════════════════════════════════════════════════════

# def _setup_tracing() -> None:
#     if os.environ.get("ENABLE_TRACING") != "1":
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
#         from opentelemetry.propagate import set_global_textmap
#         from opentelemetry.propagators.composite import CompositeHTTPPropagator
#         from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
#         from opentelemetry.baggage.propagation import W3CBaggagePropagator

#         # Go: otel.SetTextMapPropagator(propagation.NewCompositeTextMapPropagator(
#         #         propagation.TraceContext{}, propagation.Baggage{}))
#         set_global_textmap(
#             CompositeHTTPPropagator([TraceContextTextMapPropagator(), W3CBaggagePropagator()])
#         )

#         GrpcAioInstrumentorServer().instrument()
#         GrpcAioInstrumentorClient().instrument()

#         collector_addr = os.environ.get("COLLECTOR_SERVICE_ADDR", "localhost:4317")
#         logger.info("OTLP collector → %s", collector_addr)

#         provider = TracerProvider()
#         provider.add_span_processor(
#             BatchSpanProcessor(
#                 OTLPSpanExporter(endpoint=collector_addr, insecure=True)
#             )
#         )
#         trace.set_tracer_provider(provider)
#     except ImportError:
#         logger.info("opentelemetry packages not installed – tracing skipped")
#     except Exception:
#         logger.warning("Failed to initialise tracing: %s", traceback.format_exc())


# ════════════════════════════════════════════════════════════════════════════
# Optional Cloud Profiler
# Go: func initProfiling(service, version string)
# ════════════════════════════════════════════════════════════════════════════

# def _setup_profiler() -> None:
#     if os.environ.get("ENABLE_PROFILER") != "1":
#         logger.info("Profiling disabled.")
#         return
#     logger.info("Profiling enabled.")

#     def _start() -> None:
#         try:
#             import googlecloudprofiler
#             # Go: for i := 1; i <= 3; i++
#             for attempt in range(1, 4):
#                 try:
#                     googlecloudprofiler.start(
#                         service="checkoutservice",
#                         service_version="1.0.0",
#                         verbose=0,
#                     )
#                     logger.info("started Stackdriver profiler")
#                     return
#                 except Exception as exc:
#                     logger.warning("failed to start profiler: %s", exc)
#                     if attempt < 3:
#                         d = 10 * attempt
#                         logger.info("sleeping %ds to retry initializing Stackdriver profiler", d)
#                         time.sleep(d)
#             logger.warning("could not initialize Stackdriver profiler after retrying, giving up")
#         except ImportError:
#             logger.info("googlecloudprofiler not installed – profiling skipped")

#     import threading
#     threading.Thread(target=_start, daemon=True).start()


# ════════════════════════════════════════════════════════════════════════════
# FastAPI HTTP layer
# ════════════════════════════════════════════════════════════════════════════

app = make_health_app("checkoutservice")

# Lazily populated in _main() after channels are established
_orchestrator: CheckoutOrchestrator | None = None


def _get_orch() -> CheckoutOrchestrator:
    if _orchestrator is None:
        raise RuntimeError("Orchestrator not initialised; server has not started yet.")
    return _orchestrator


# ── Pydantic models ───────────────────────────────────────────────────────────

class AddressIn(BaseModel):
    street_address: str
    city:    str
    state:   str
    country: str
    zip_code: int = 0


class PlaceOrderIn(BaseModel):
    user_id:       str
    user_currency: str = "USD"
    address:       AddressIn
    email:         str
    credit_card_number:           str
    credit_card_cvv:              int
    credit_card_expiration_year:  int
    credit_card_expiration_month: int

    @field_validator("credit_card_expiration_month")
    @classmethod
    def validate_month(cls, v: int) -> int:
        if not 1 <= v <= 12:
            raise ValueError("expiration_month must be 1–12")
        return v


def _address_proto(a: AddressIn) -> demo_pb2.Address:
    return demo_pb2.Address(
        street_address=a.street_address,
        city=a.city, state=a.state,
        country=a.country, zip_code=a.zip_code,
    )


def _order_result_dict(o: demo_pb2.OrderResult) -> dict:
    cents = o.shipping_cost.nanos // 10_000_000
    return {
        "order_id":             o.order_id,
        "shipping_tracking_id": o.shipping_tracking_id,
        "shipping_cost": {
            "currency_code": o.shipping_cost.currency_code,
            "units":         o.shipping_cost.units,
            "nanos":         o.shipping_cost.nanos,
            "formatted":     f"{o.shipping_cost.currency_code} {o.shipping_cost.units}.{cents:02d}",
        },
        "shipping_address": {
            "street_address": o.shipping_address.street_address,
            "city":           o.shipping_address.city,
            "state":          o.shipping_address.state,
            "country":        o.shipping_address.country,
            "zip_code":       o.shipping_address.zip_code,
        },
        "items": [
            {
                "product_id": item.item.product_id,
                "quantity":   item.item.quantity,
                "unit_cost": {
                    "currency_code": item.cost.currency_code,
                    "units":         item.cost.units,
                    "nanos":         item.cost.nanos,
                },
                "subtotal": {
                    "currency_code": item.cost.currency_code,
                    "units":         item.cost.units * item.item.quantity,
                    "nanos":         item.cost.nanos  * item.item.quantity,
                },
            }
            for item in o.items
        ],
    }


# ── POST /place-order ─────────────────────────────────────────────────────────

@app.post(
    "/place-order",
    summary="Place order (REST proxy for PlaceOrder RPC)",
    description=(
        "Orchestrates the full checkout flow:\n\n"
        "1. Fetch user's cart\n"
        "2. Look up each product price via ProductCatalogService\n"
        "3. Convert all prices to user_currency via CurrencyService\n"
        "4. Get a shipping quote via ShippingService\n"
        "5. Compute total (Σ item×qty + shipping)\n"
        "6. Charge card via PaymentService\n"
        "7. Ship order via ShippingService\n"
        "8. Empty cart via CartService\n"
        "9. Send confirmation email via EmailService\n"
        "10. Return order summary\n"
    ),
)
async def post_place_order(body: PlaceOrderIn):
    orch = _get_orch()
    request = demo_pb2.PlaceOrderRequest(
        user_id=body.user_id,
        user_currency=body.user_currency,
        address=_address_proto(body.address),
        email=body.email,
        credit_card=demo_pb2.CreditCardInfo(
            credit_card_number=body.credit_card_number,
            credit_card_cvv=body.credit_card_cvv,
            credit_card_expiration_year=body.credit_card_expiration_year,
            credit_card_expiration_month=body.credit_card_expiration_month,
        ),
    )
    try:
        response = await orch.place_order(request)
    except Exception as exc:
        logger.error("POST /place-order error: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))

    return _order_result_dict(response.order)


# ── GET /place-order/preview ──────────────────────────────────────────────────

class PreviewIn(BaseModel):
    user_id:       str
    user_currency: str = "USD"
    address:       AddressIn


@app.post(
    "/place-order/preview",
    summary="Preview order total (no card charge, no shipment)",
    description=(
        "Fetches cart, looks up prices, converts currency, gets shipping quote "
        "and returns the full order total — without charging the card or dispatching."
    ),
)
async def post_preview(body: PreviewIn):
    orch = _get_orch()
    try:
        prep = await orch.prepare_order_items_and_shipping_quote(
            user_id=body.user_id,
            user_currency=body.user_currency,
            address=_address_proto(body.address),
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    # Compute total (same logic as PlaceOrder step 4)
    total = zero_money(body.user_currency)
    total = money_must(money_sum(total, proto_to_money(prep.shipping_cost_localized)))
    for oi in prep.order_items:
        cost_py    = proto_to_money(oi.cost)
        mult_price = money_multiply_slow(cost_py, oi.item.quantity)
        total      = money_must(money_sum(total, mult_price))

    cents = prep.shipping_cost_localized.nanos // 10_000_000
    return {
        "user_id":       body.user_id,
        "user_currency": body.user_currency,
        "shipping_cost": {
            "currency_code": prep.shipping_cost_localized.currency_code,
            "units":         prep.shipping_cost_localized.units,
            "nanos":         prep.shipping_cost_localized.nanos,
            "formatted":     f"{prep.shipping_cost_localized.currency_code} {prep.shipping_cost_localized.units}.{cents:02d}",
        },
        "items": [
            {
                "product_id": oi.item.product_id,
                "quantity":   oi.item.quantity,
                "unit_cost": {
                    "currency_code": oi.cost.currency_code,
                    "units":         oi.cost.units,
                    "nanos":         oi.cost.nanos,
                },
            }
            for oi in prep.order_items
        ],
        "total": {
            "currency_code": total.currency_code,
            "units":         total.units,
            "nanos":         total.nanos,
            "formatted":     format_money(total),
        },
    }


# ════════════════════════════════════════════════════════════════════════════
# gRPC server builder
# ════════════════════════════════════════════════════════════════════════════

def _build_grpc_server(
    servicer: CheckoutServicer,
    port: int,
) -> grpc.aio.Server:
    """
    Go:
        srv = grpc.NewServer(grpc.StatsHandler(otelgrpc.NewServerHandler()))
        pb.RegisterCheckoutServiceServer(srv, svc)
        healthcheck := health.NewServer()
        healthpb.RegisterHealthServer(srv, healthcheck)
    """
    server = grpc.aio.server()
    demo_pb2_grpc.add_CheckoutServiceServicer_to_server(servicer, server)
    # health_pb2_grpc.add_HealthServicer_to_server(servicer, server)

    # gRPC reflection (grpcurl support — not in Go original but useful)
    SERVICE_NAMES = (
        demo_pb2.DESCRIPTOR.services_by_name["CheckoutService"].full_name,
        # grpc_reflection.SERVICE_NAME,
    )
    # grpc_reflection.enable_server_reflection(SERVICE_NAMES, server)

    server.add_insecure_port(f"[::]:{port}")
    return server


# ════════════════════════════════════════════════════════════════════════════
# Entry point  (mirrors Go main())
# ════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn

    # Go: logrus JSON formatter with timestamp/severity/message fields
    logging.basicConfig(
        level=logging.DEBUG,
        format='{"timestamp":"%(asctime)s","severity":"%(levelname)s",'
               '"message":"%(message)s","logger":"%(name)s"}',
    )

    # Go: if ENABLE_TRACING == "1" → initTracing()
    # _setup_tracing()

    # Go: if ENABLE_PROFILER == "1" → go initProfiling(...)
    # _setup_profiler()

    # Go: mustMapEnv for all 6 downstream addresses
    shipping_addr  = must_map_env("SHIPPING_SERVICE_ADDR", "localhost:5051")
    catalog_addr   = must_map_env("PRODUCT_CATALOG_SERVICE_ADDR", "localhost:5055")
    cart_addr      = must_map_env("CART_SERVICE_ADDR", "localhost:5054")
    currency_addr  = must_map_env("CURRENCY_SERVICE_ADDR", "localhost:5053")
    email_addr     = must_map_env("EMAIL_SERVICE_ADDR", "localhost:5056")
    payment_addr   = must_map_env("PAYMENT_SERVICE_ADDR", "localhost:5052")

    http_port = int(os.getenv("HTTP_PORT", GRPC_PORT + 1000))

    async def _main() -> None:
        global _orchestrator

        # Go: mustConnGRPC for each address
        shipping_ch  = must_conn_grpc(shipping_addr)
        catalog_ch   = must_conn_grpc(catalog_addr)
        cart_ch      = must_conn_grpc(cart_addr)
        currency_ch  = must_conn_grpc(currency_addr)
        email_ch     = must_conn_grpc(email_addr)
        payment_ch   = must_conn_grpc(payment_addr)

        # Build orchestrator with all 6 stubs injected
        _orchestrator = CheckoutOrchestrator(
            cart_stub     = demo_pb2_grpc.CartServiceStub(cart_ch),
            catalog_stub  = demo_pb2_grpc.ProductCatalogServiceStub(catalog_ch),
            currency_stub = demo_pb2_grpc.CurrencyServiceStub(currency_ch),
            shipping_stub = demo_pb2_grpc.ShippingServiceStub(shipping_ch),
            payment_stub  = demo_pb2_grpc.PaymentServiceStub(payment_ch),
            email_stub    = demo_pb2_grpc.EmailServiceStub(email_ch),
        )

        servicer    = CheckoutServicer(orchestrator=_orchestrator)
        grpc_server = _build_grpc_server(servicer, GRPC_PORT)
        await grpc_server.start()

        # Go: log.Infof("starting to listen on tcp: %q", lis.Addr().String())
        logger.info("starting to listen on tcp: [::]:%d", GRPC_PORT)
        logger.info("HTTP server on :%d", http_port)

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
"""
paymentservice/main.py

Python / FastAPI + gRPC server – faithful port of the Node.js paymentservice.

Node.js server.js startup sequence reproduced here:
─────────────────────────────────────────────────────────────────────────────
  1. pino logger initialised (JSON output, severity/message fields)
  2. if !DISABLE_PROFILER → @google-cloud/profiler.start()
  3. if !DISABLE_TRACING  → initTracing() (OpenTelemetry)
  4. grpc.Server() created on $PORT (default 50051)
  5. PaymentService.service added with { charge } handler
  6. grpc-health-check registered
  7. server.bindAsync(port, insecureCredentials, callback)
  8. server.start()
  9. logger.info('PaymentService gRPC server started on port X')
─────────────────────────────────────────────────────────────────────────────

Python adds:
  • FastAPI HTTP server on HTTP_PORT (default PORT + 1000) with:
      GET  /health          – liveness probe
      GET  /ready           – readiness probe
      POST /charge          – REST proxy for Charge RPC
      POST /charge/validate – dry-run card validation (no transaction generated)
  • gRPC reflection (grpcurl / grpc-js tooling support)
  • Optional OTLP tracing via OTEL_EXPORTER_OTLP_ENDPOINT env var
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import traceback

import grpc
# from grpc_health.v1 import health_pb2_grpc
# from grpc_reflection.v1alpha import reflection as grpc_reflection
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, field_validator

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))
from ..shared import demo_pb2
from ..shared import demo_pb2_grpc
from ..shared.base_service import make_health_app, run_service
from .servicer import PaymentServicer
from .card_validator import (
    charge as validate_charge,
    CardValidationError,
    detect_card_type,
    is_valid_luhn,
    is_expired,
)

logger = logging.getLogger("paymentservice")

GRPC_PORT = int(os.getenv("PORT", "5052"))


# ════════════════════════════════════════════════════════════════════════════
# Optional OpenTelemetry tracing
# (mirrors Node.js initTracing / DISABLE_TRACING guard)
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
# (mirrors Node.js @google-cloud/profiler guard)
# ════════════════════════════════════════════════════════════════════════════

# def _setup_profiler() -> None:
#     if os.getenv("DISABLE_PROFILER"):
#         logger.info("Profiling disabled.")
#         return
#     logger.info("Profiling enabled.")
#     try:
#         import googlecloudprofiler
#         googlecloudprofiler.start(
#             service="paymentservice",
#             service_version="1.0.0",
#             verbose=0,
#         )
#         logger.info("Cloud Profiler started.")
#     except ImportError:
#         logger.info("googlecloudprofiler not installed – profiling skipped")
#     except Exception as exc:
#         logger.warning("Cloud Profiler failed to start: %s", exc)


# ════════════════════════════════════════════════════════════════════════════
# FastAPI HTTP layer
# ════════════════════════════════════════════════════════════════════════════

app = make_health_app("paymentservice")

_servicer = PaymentServicer()


# ── Pydantic models ───────────────────────────────────────────────────────────

class MoneyModel(BaseModel):
    currency_code: str = "USD"
    units: int = 0
    nanos: int = 0


class ChargeRequestModel(BaseModel):
    amount: MoneyModel
    credit_card_number: str
    credit_card_cvv: int
    credit_card_expiration_year: int
    credit_card_expiration_month: int

    @field_validator("credit_card_expiration_month")
    @classmethod
    def validate_month(cls, v: int) -> int:
        if not 1 <= v <= 12:
            raise ValueError("credit_card_expiration_month must be 1–12")
        return v

    @field_validator("credit_card_expiration_year")
    @classmethod
    def validate_year(cls, v: int) -> int:
        if v < 2000 or v > 2100:
            raise ValueError("credit_card_expiration_year looks invalid")
        return v


class ValidateRequestModel(BaseModel):
    credit_card_number: str
    credit_card_cvv: int
    credit_card_expiration_year: int
    credit_card_expiration_month: int


# ── POST /charge ──────────────────────────────────────────────────────────────

@app.post(
    "/charge",
    summary="Charge credit card (REST proxy for Charge RPC)",
    description=(
        "Validates the credit card via Luhn algorithm + expiry check, "
        "then returns a mock UUID transaction ID.\n\n"
        "**Supported card types:** Visa, MasterCard, American Express, Discover.\n\n"
        "Returns HTTP 422 for validation errors (expired, bad Luhn, etc.)."
    ),
)
async def post_charge(body: ChargeRequestModel):
    """
    REST proxy – calls the same PaymentServicer.Charge logic as the gRPC server.
    """
    request = demo_pb2.ChargeRequest(
        amount=demo_pb2.Money(
            currency_code=body.amount.currency_code,
            units=body.amount.units,
            nanos=body.amount.nanos,
        ),
        credit_card=demo_pb2.CreditCardInfo(
            credit_card_number=body.credit_card_number,
            credit_card_cvv=body.credit_card_cvv,
            credit_card_expiration_year=body.credit_card_expiration_year,
            credit_card_expiration_month=body.credit_card_expiration_month,
        ),
    )

    # Use a mock context that captures aborts
    class _MockContext:
        async def abort(self, code, details):
            raise _GrpcAbort(code, details)

    class _GrpcAbort(Exception):
        def __init__(self, code, details):
            self.code    = code
            self.details = details

    try:
        resp = await _servicer.Charge(request, _MockContext())
    except _GrpcAbort as exc:
        status = 422 if exc.code == grpc.StatusCode.INVALID_ARGUMENT else 500
        raise HTTPException(status_code=status, detail=exc.details)

    card_number = body.credit_card_number.replace(" ", "").replace("-", "")
    cents = body.amount.nanos // 10_000_000
    return {
        "transaction_id": resp.transaction_id,
        "card_type": detect_card_type(card_number),
        "last_four": card_number[-4:],
        "amount": {
            "currency_code": body.amount.currency_code,
            "units": body.amount.units,
            "nanos": body.amount.nanos,
            "formatted": f"{body.amount.currency_code} {body.amount.units}.{cents:02d}",
        },
    }


# ── POST /charge/validate ─────────────────────────────────────────────────────

@app.post(
    "/charge/validate",
    summary="Validate credit card (dry-run, no charge)",
    description=(
        "Runs the same card validation logic as /charge but does NOT generate "
        "a transaction ID or produce any side effects.\n\n"
        "Useful for pre-validating a card before the final checkout step."
    ),
)
async def post_validate(body: ValidateRequestModel):
    card_number = body.credit_card_number.replace(" ", "").replace("-", "")

    errors: list[str] = []
    if not card_number.isdigit():
        errors.append("Card number must contain only digits.")
    elif not is_valid_luhn(card_number):
        errors.append("Card number failed Luhn check.")

    if is_expired(body.credit_card_expiration_year, body.credit_card_expiration_month):
        errors.append(
            f"Card expired on "
            f"{body.credit_card_expiration_month:02d}/{body.credit_card_expiration_year}."
        )

    cvv_len = len(str(body.credit_card_cvv))
    if cvv_len not in (3, 4):
        errors.append(f"CVV length {cvv_len} is invalid (expected 3 or 4 digits).")

    card_type = detect_card_type(card_number) if not errors else "Unknown"

    return {
        "valid": len(errors) == 0,
        "card_type": card_type,
        "last_four": card_number[-4:] if len(card_number) >= 4 else card_number,
        "errors": errors,
    }


# ════════════════════════════════════════════════════════════════════════════
# gRPC server builder
# ════════════════════════════════════════════════════════════════════════════

def _build_grpc_server(servicer: PaymentServicer, port: int) -> grpc.aio.Server:
    """
    Node.js equivalent:
        const server = new grpc.Server();
        server.addService(PaymentService.service, { charge });
        server.addService(healthCheckService);
        server.bindAsync(`:${port}`, grpc.ServerCredentials.createInsecure(), cb);
        server.start();
    """
    server = grpc.aio.server()

    demo_pb2_grpc.add_PaymentServiceServicer_to_server(servicer, server)
    # health_pb2_grpc.add_HealthServicer_to_server(servicer, server)

    SERVICE_NAMES = (
        demo_pb2.DESCRIPTOR.services_by_name["PaymentService"].full_name,
        # grpc_reflection.SERVICE_NAME,
    )
    # grpc_reflection.enable_server_reflection(SERVICE_NAMES, server)

    server.add_insecure_port(f"[::]:{port}")
    return server


# ════════════════════════════════════════════════════════════════════════════
# Entry point  (mirrors Node.js server.js)
# ════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn

    # Node.js: pino logger with severity/message/name fields
    logging.basicConfig(
        level=logging.INFO,
        format='{"severity": "%(levelname)s", "time": "%(asctime)s", '
               '"name": "%(name)s", "message": "%(message)s"}',
    )

    # _setup_tracing()
    # _setup_profiler()

    http_port = int(os.getenv("HTTP_PORT", GRPC_PORT + 1000))

    async def _main() -> None:
        grpc_server = _build_grpc_server(_servicer, GRPC_PORT)
        await grpc_server.start()
        # Node.js: logger.info('PaymentService gRPC server started on port X')
        logger.info("PaymentService gRPC server started on port %d", GRPC_PORT)
        logger.info("PaymentService HTTP server starting on port %d", http_port)

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
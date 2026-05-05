from __future__ import annotations

import asyncio
import logging
import os
import sys
import traceback

import grpc
from fastapi import FastAPI, HTTPException, Query

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))
from ..shared import demo_pb2
from ..shared import demo_pb2_grpc
from ..shared.base_service import make_health_app, run_service
from .servicer import RecommendationServicer

logger = logging.getLogger("recommendationagent")

GRPC_PORT = int(os.getenv("PORT", "5058"))


# ════════════════════════════════════════════════════════════════════════════
# OpenTelemetry tracing setup
# (mirrors original GrpcInstrumentorClient/Server + OTLPSpanExporter block)
# ════════════════════════════════════════════════════════════════════════════

# def _setup_tracing() -> None:
#     """
#     Original:
#         grpc_client_instrumentor = GrpcInstrumentorClient(); .instrument()
#         grpc_server_instrumentor = GrpcInstrumentorServer(); .instrument()
#         if os.environ["ENABLE_TRACING"] == "1":
#             trace.set_tracer_provider(TracerProvider())
#             otel_endpoint = os.getenv("COLLECTOR_SERVICE_ADDR", "localhost:4317")
#             trace.get_tracer_provider().add_span_processor(
#                 BatchSpanProcessor(OTLPSpanExporter(endpoint=otel_endpoint, insecure=True))
#             )
#     """
#     try:
#         from opentelemetry import trace
#         from opentelemetry.instrumentation.grpc import (
#             GrpcAioInstrumentorClient,
#             GrpcAioInstrumentorServer,
#         )
#         from opentelemetry.sdk.trace import TracerProvider
#         from opentelemetry.sdk.trace.export import BatchSpanProcessor
#         from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

#         # Auto-instrument all gRPC client + server calls
#         GrpcAioInstrumentorClient().instrument()
#         GrpcAioInstrumentorServer().instrument()
#         logger.info("gRPC OpenTelemetry instrumentation enabled")

#         if os.environ.get("ENABLE_TRACING") == "1":
#             otel_endpoint = os.getenv("COLLECTOR_SERVICE_ADDR", "localhost:4317")
#             provider = TracerProvider()
#             provider.add_span_processor(
#                 BatchSpanProcessor(
#                     OTLPSpanExporter(endpoint=otel_endpoint, insecure=True)
#                 )
#             )
#             trace.set_tracer_provider(provider)
#             logger.info("OTLP tracing → %s", otel_endpoint)
#         else:
#             logger.info("Tracing disabled (ENABLE_TRACING != 1)")

#     except ImportError:
#         logger.info("opentelemetry packages not installed – tracing disabled")
#     except Exception:
#         logger.warning(
#             "Exception on tracing setup: %s – tracing disabled",
#             traceback.format_exc(),
#         )


# ════════════════════════════════════════════════════════════════════════════
# FastAPI HTTP layer
# ════════════════════════════════════════════════════════════════════════════

app = make_health_app("recommendationagent")

# Lazy singleton – built after catalog channel is ready
_servicer: RecommendationServicer | None = None


def _get_servicer() -> RecommendationServicer:
    if _servicer is None:
        raise RuntimeError(
            "RecommendationServicer not initialised; "
            "ensure PRODUCT_CATALOG_SERVICE_ADDR is set and the server has started."
        )
    return _servicer


@app.get(
    "/recommendations",
    summary="List product recommendations (REST proxy for ListRecommendations RPC)",
    description=(
        "Returns up to 5 product IDs the user might like, "
        "excluding products already in `product_id` query params.\n\n"
        "Internally calls the same `RecommendationServicer.ListRecommendations` "
        "that the gRPC server exposes – no double-hop."
    ),
)
async def get_recommendations(
    user_id: str = Query(..., description="Current user identifier"),
    product_id: list[str] = Query(
        default=[],
        description="Product IDs to exclude (already in cart / recently viewed)",
    ),
):
    """
    REST equivalent of the gRPC ListRecommendations call.

    Example:
        GET /recommendations?user_id=u1&product_id=OLJCESPC7Z&product_id=2ZYFJ3GM2N
    """
    svc = _get_servicer()
    request = demo_pb2.ListRecommendationsRequest(
        user_id=user_id,
        product_ids=product_id,
    )
    try:
        response = await svc.ListRecommendations(request, context=None)
    except Exception as exc:
        logger.error("ListRecommendations REST error: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc))

    return {
        "user_id": user_id,
        "excluded_product_ids": product_id,
        "recommended_product_ids": list(response.product_ids),
        "count": len(response.product_ids),
    }


# ════════════════════════════════════════════════════════════════════════════
# gRPC server factory
# (mirrors original grpc.server setup but uses grpc.aio)
# ════════════════════════════════════════════════════════════════════════════

def _build_grpc_server(
    servicer: RecommendationServicer,
    port: int,
) -> grpc.aio.Server:
    """
    Original:
        server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
        demo_pb2_grpc.add_RecommendationServiceServicer_to_server(service, server)
        health_pb2_grpc.add_HealthServicer_to_server(service, server)
        server.add_insecure_port('[::]:' + port)
        server.start()
    """
    server = grpc.aio.server()
    demo_pb2_grpc.add_RecommendationServiceServicer_to_server(servicer, server)
    server.add_insecure_port(f"[::]:{port}")
    return server


# ════════════════════════════════════════════════════════════════════════════
# Entry point  (mirrors original __main__ block)
# ════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    # # ── Original: logger.info("initializing recommendationservice") ──────────
    # logger.info("initializing recommendationservice")

    # # ── Original: initStackdriverProfiling() ─────────────────────────────────
    # if "DISABLE_PROFILER" not in os.environ:
    #     logger.info("Profiler disabled (googlecloudprofiler removed in PR#3196).")
    # else:
    #     logger.info("Profiler disabled via env var.")

    # ── Original: GrpcInstrumentor + TracerProvider ───────────────────────────
    # _setup_tracing()

    # ── Original: server.start() + blockUntilShutdown ───────────────────────
    http_port = int(os.getenv("HTTP_PORT", GRPC_PORT + 1000))

    async def _main() -> None:

        # ── Original: catalog_addr check ────────────────────────────────────────
        catalog_addr = os.environ.get("PRODUCT_CATALOG_SERVICE_ADDR", "localhost:5055")
        if not catalog_addr:
            raise EnvironmentError(
                "PRODUCT_CATALOG_SERVICE_ADDR environment variable not set"
            )
        logger.info("product catalog address: %s", catalog_addr)

        # ── Original: channel = grpc.insecure_channel(catalog_addr) ─────────────
        # Original: product_catalog_stub = ProductCatalogServiceStub(channel)
        catalog_channel = grpc.aio.insecure_channel(catalog_addr)
        catalog_stub = demo_pb2_grpc.ProductCatalogServiceStub(catalog_channel)

        # ── Build servicer (inject stub) ─────────────────────────────────────────
        _servicer = RecommendationServicer(product_catalog_stub=catalog_stub)

        grpc_server = _build_grpc_server(_servicer, GRPC_PORT)
        await grpc_server.start()
        logger.info("listening on port: %d", GRPC_PORT)

        http_config = uvicorn.Config(
            app,
            host="0.0.0.0",
            port=http_port,
            log_level="info",
            access_log=False,
        )
        http_server = uvicorn.Server(http_config)

        await asyncio.gather(
            grpc_server.wait_for_termination(),
            http_server.serve(),
        )

    asyncio.run(_main())
"""
adservice/main.py

Python / FastAPI + gRPC replacement for the Java AdService.

┌─────────────────────────────────────────────────────────────────────────┐
│  What the Java service does (AdService.java)                            │
│                                                                         │
│  • Starts a gRPC server on $PORT (default 9555).                        │
│  • Registers AdServiceImpl (inner class) as the only gRPC handler.      │
│  • Registers a gRPC HealthService so Kubernetes can probe it.           │
│  • Optionally exports metrics to Prometheus (:9090) and traces to       │
│    Stackdriver / Jaeger (we keep /metrics via prometheus-client and      │
│    honour the same env vars for optional Jaeger export).                │
│                                                                         │
│  What this Python service does                                          │
│                                                                         │
│  • Same gRPC interface on port 9555 (env PORT).                         │
│  • FastAPI HTTP server on port 10555 (env HTTP_PORT) with:              │
│      GET  /health           – health probe (mirrors gRPC health check)  │
│      GET  /ready            – readiness probe                           │
│      GET  /ads?key=...      – REST proxy for GetAds                     │
│      GET  /ads/random       – convenience: always return random ads     │
│      GET  /catalog          – list all categories + ad counts           │
│      GET  /metrics          – Prometheus text exposition                │
└─────────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import logging
import os
import sys
import time
from typing import Optional

from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import PlainTextResponse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))
from ..shared import demo_pb2
from ..shared import demo_pb2_grpc
from ..shared.base_service  import make_health_app, run_service

from .ad_catalog import (
    ADS_BY_CATEGORY,
    ALL_ADS,
    MAX_ADS_TO_SERVE,
    get_ads_by_category,
    get_ads_by_key,
    get_random_ads,
)
from .servicer import AdServicer

logger = logging.getLogger(__name__)

GRPC_PORT = int(os.getenv("PORT", "9555"))

# # ── Prometheus metrics (optional – mirrors Java PrometheusStatsCollector) ───
# try:
#     from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST
#
#     _ad_requests_total = Counter(
#         "app_ads_ad_requests_total",
#         "Total GetAds requests",
#         ["request_type"],          # "targeted" or "random"
#     )
#     _ad_response_size = Histogram(
#         "app_ads_ad_response_size",
#         "Number of ads returned per request",
#         buckets=[0, 1, 2, 3, 4, 5],
#     )
#     _PROMETHEUS_AVAILABLE = True
#     logger.info("Prometheus metrics enabled")
# except ImportError:
#     _PROMETHEUS_AVAILABLE = False
#     logger.info("prometheus_client not installed – /metrics endpoint disabled")
#
#
# def _record_metrics(request_type: str, ad_count: int) -> None:
#     if not _PROMETHEUS_AVAILABLE:
#         return
#     _ad_requests_total.labels(request_type=request_type).inc()
#     _ad_response_size.observe(ad_count)


# ════════════════════════════════════════════════════════════════════════════
# FastAPI application
# ════════════════════════════════════════════════════════════════════════════

app = make_health_app("adservice")

# ── shared servicer instance (avoids re-creating per request) ────────────────
_servicer = AdServicer()


# ── helper ───────────────────────────────────────────────────────────────────

def _ad_to_dict(ad: demo_pb2.Ad) -> dict:
    return {"redirect_url": ad.redirect_url, "text": ad.text}


# ── /ads  ─────────────────────────────────────────────────────────────────────

@app.get(
    "/ads",
    summary="Get contextual ads (REST proxy for GetAds RPC)",
    description=(
        "Pass one or more `key` query params (category names or product keywords).\n\n"
        "Returns up to MAX_ADS_TO_SERVE ads matching those keys.\n"
        "If no keys are given, or no ads match, random ads are returned.\n\n"
        "**Mirrors the Java gRPC GetAds behaviour exactly.**"
    ),
    response_description="List of ads",
)
async def get_ads(
    key: list[str] = Query(
        default=[],
        description="Context key(s). E.g. ?key=photography&key=kitchen",
    )
):
    """
    REST proxy – internally calls the same AdServicer.GetAds logic.

    Example requests:
        GET /ads                          → random ads (no context)
        GET /ads?key=photography          → photography ads
        GET /ads?key=kitchen&key=cycling  → kitchen + cycling ads
    """
    request = demo_pb2.AdRequest(context_keys=key)

    # We call the servicer directly (no network hop) to stay DRY.
    response: demo_pb2.AdResponse = await _servicer.GetAds(request, context=None)
    ads = [_ad_to_dict(ad) for ad in response.ads]

    # request_type = "targeted" if key else "random"
    # _record_metrics(request_type, len(ads))

    return {
        "context_keys": key,
        "ads": ads,
        "count": len(ads),
    }


# ── /ads/random ───────────────────────────────────────────────────────────────

@app.get(
    "/ads/random",
    summary="Get random ads (no context)",
    description="Always returns MAX_ADS_TO_SERVE randomly selected ads.",
)
async def get_random_ads_endpoint(
    n: int = Query(default=MAX_ADS_TO_SERVE, ge=1, le=10,
                   description="Number of random ads to return"),
):
    """
    Equivalent to calling GetAds with an empty context_keys list,
    mirroring Java's getDefaultAds() / getRandomAds() methods.
    """
    entries = get_random_ads(n)
    ads = [{"redirect_url": e.redirect_url, "text": e.text} for e in entries]
    # _record_metrics("random", len(ads))
    return {"ads": ads, "count": len(ads)}


# ── /catalog ──────────────────────────────────────────────────────────────────

@app.get(
    "/catalog",
    summary="List all ad categories and their ads",
    description=(
        "Returns the full in-memory ad catalog, grouped by category.\n\n"
        "Useful for debugging which context keys trigger which ads."
    ),
)
async def list_catalog():
    """
    Debug / admin endpoint.  Not present in the original Java service;
    added here to make the ad catalog inspectable without a gRPC client.
    """
    catalog = {
        category: [
            {"redirect_url": ad.redirect_url, "text": ad.text}
            for ad in ads
        ]
        for category, ads in ADS_BY_CATEGORY.items()
    }
    return {
        "total_ads": len(ALL_ADS),
        "categories": list(ADS_BY_CATEGORY.keys()),
        "max_ads_to_serve": MAX_ADS_TO_SERVE,
        "catalog": catalog,
    }


# ── /metrics ──────────────────────────────────────────────────────────────────

# @app.get(
#     "/metrics",
#     summary="Prometheus metrics",
#     response_class=PlainTextResponse,
#     description="Exposes Prometheus-format metrics (mirrors Java PrometheusStatsCollector).",
#     include_in_schema=_PROMETHEUS_AVAILABLE,
# )
# async def metrics():
#     if not _PROMETHEUS_AVAILABLE:
#         raise HTTPException(status_code=404, detail="prometheus_client not installed")
#     return PlainTextResponse(
#         content=generate_latest().decode("utf-8"),
#         media_type=CONTENT_TYPE_LATEST,
#     )


# ════════════════════════════════════════════════════════════════════════════
# Entry point  (mirrors Java AdService.main)
# ════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    """
    Java main() sequence reproduced in Python:

        1. initializeAds()            → ad_catalog module-level init (already done)
        2. RpcViews.registerAllViews()→ _PROMETHEUS_AVAILABLE check above
        3. LoggingTraceExporter       → standard Python logging configured below
        4. initStackdriver()          → skipped (GCP-specific, optional)
        5. PrometheusStatsCollector   → prometheus_client if available
        6. JaegerTraceExporter        → honoured via OTEL_EXPORTER_JAEGER_ENDPOINT env var
        7. service.start()            → run_service() below
        8. service.blockUntilShutdown() → asyncio event loop inside run_service()
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    # Optional: OpenTelemetry Jaeger export (replaces Java JaegerTraceExporter)
    jaeger_endpoint = os.getenv("OTEL_EXPORTER_JAEGER_ENDPOINT")
    if jaeger_endpoint:
        try:
            from opentelemetry import trace
            from opentelemetry.exporter.jaeger.thrift import JaegerExporter
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            provider = TracerProvider()
            provider.add_span_processor(BatchSpanProcessor(JaegerExporter()))
            trace.set_tracer_provider(provider)
            logger.info("Jaeger tracing enabled → %s", jaeger_endpoint)
        except ImportError:
            logger.warning("opentelemetry-exporter-jaeger not installed; skipping Jaeger export")

    logger.info("AdService starting | gRPC port=%d", GRPC_PORT)
    logger.info("Ad catalog loaded | total_ads=%d | categories=%s",
                len(ALL_ADS), list(ADS_BY_CATEGORY.keys()))

    run_service(
        demo_pb2_grpc.add_AdServiceServicer_to_server,
        _servicer,
        GRPC_PORT,
        app,
    )
"""
shared/base_service.py

Common runner that starts a gRPC server and a FastAPI HTTP server
side-by-side inside a single asyncio event loop.

Usage in each service:
    from shared.base_service import run_service
    run_service(grpc_servicer_adder, servicer_instance, grpc_port, app)
"""

import asyncio
import logging
import os
import signal

import grpc
import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

# ── helpers ────────────────────────────────────────────────────────────────

def make_health_app(service_name: str) -> FastAPI:
    """Return a minimal FastAPI app with /health and /ready endpoints."""
    app = FastAPI(title=service_name, version="1.0.0")

    @app.get("/health", tags=["observability"])
    async def health():
        return JSONResponse({"status": "healthy", "service": service_name})

    @app.get("/ready", tags=["observability"])
    async def ready():
        return JSONResponse({"status": "ready", "service": service_name})

    return app


async def _serve_grpc(adder_fn, servicer, port: int, max_workers: int = 10):
    server = grpc.aio.server()
    adder_fn(servicer, server)
    listen_addr = f"[::]:{port}"
    server.add_insecure_port(listen_addr)
    await server.start()
    logger.info("gRPC server listening on %s", listen_addr)

    async def _shutdown():
        await server.stop(grace=5)

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: asyncio.ensure_future(_shutdown()))

    await server.wait_for_termination()


async def _serve_http(app: FastAPI, port: int):
    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=port,
        log_level="info",
        access_log=False,
    )
    server = uvicorn.Server(config)
    await server.serve()


def run_service(adder_fn, servicer, grpc_port: int, app: FastAPI):
    """
    Block until both the gRPC server and the FastAPI HTTP server exit.

    :param adder_fn:   The generated add_XxxServicer_to_server function.
    :param servicer:   An instance of the gRPC servicer class.
    :param grpc_port:  Port for the gRPC server.
    :param app:        A FastAPI application (health + optional REST proxy).
    """
    http_port = int(os.getenv("HTTP_PORT", grpc_port + 1000))

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    async def _main():
        await asyncio.gather(
            _serve_grpc(adder_fn, servicer, grpc_port),
            _serve_http(app, http_port),
        )

    asyncio.run(_main())
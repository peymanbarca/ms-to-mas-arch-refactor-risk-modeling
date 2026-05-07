"""
recommendationservice/client.py

Async gRPC client for RecommendationService.

The original repo only had the server (recommendation_server.py); the
frontend called it directly using a generated stub.  This module wraps
that stub in a clean async client class with:
  • typed method signatures
  • error normalisation to RecommendationClientError
  • async-context-manager support for proper channel lifecycle

Usage
─────
Short-lived (test / script):

    async with RecommendationClient("recommendationservice:8080") as client:
        product_ids = await client.list_recommendations(
            user_id="user-abc",
            product_ids=["OLJCESPC7Z", "2ZYFJ3GM2N"],
        )
        print(product_ids)

Long-lived (held by FastAPI lifespan):

    client = RecommendationClient()
    product_ids = await client.list_recommendations("user-abc", [])

Standalone CLI:

    python -m recommendationservice.client user-123 PROD1 PROD2
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

import grpc

from ..shared import demo_pb2
from ..shared import demo_pb2_grpc

logger = logging.getLogger("recommendationagent-client")

_DEFAULT_ADDR = os.getenv("RECOMMENDATION_SERVICE_ADDR", "localhost:5058")


# ── Error type ───────────────────────────────────────────────────────────────

class RecommendationClientError(Exception):
    """Raised when the RecommendationService RPC call fails."""

    def __init__(self, rpc: str, code: grpc.StatusCode, details: str) -> None:
        self.rpc = rpc
        self.code = code
        self.details = details
        super().__init__(f"[RecommendationService.{rpc}] {code.name}: {details}")


# ── Client class ─────────────────────────────────────────────────────────────

class RecommendationClient:
    """
    Async gRPC client for RecommendationAgent (port 5058).

    Wraps the generated stub and translates gRPC errors into
    RecommendationClientError so callers don't need to import grpc.
    """

    def __init__(self, address: str | None = None) -> None:
        self._address = address or _DEFAULT_ADDR
        self._channel = grpc.aio.insecure_channel(self._address)
        self._stub = demo_pb2_grpc.RecommendationServiceStub(self._channel)
        logger.info("RecommendationClient → %s", self._address)

    # ── public API ───────────────────────────────────────────────────────────

    async def list_recommendations(
        self,
        user_id: str,
        product_ids: list[str],
    ) -> list[str]:
        """
        ListRecommendations RPC – returns product IDs the user might like.

        Mirrors the original server's algorithm:
          all_catalog_products MINUS already_seen → random sample up to 5.

        Args:
            user_id:     Current user identifier.
            product_ids: Products already in the user's cart / recently viewed.
                         These are excluded from recommendations.

        Returns:
            List of recommended product ID strings (up to 5).

        Raises:
            RecommendationClientError: on any gRPC transport or server error.
        """
        logger.info(
            "ListRecommendations | user_id=%s | exclude_count=%d",
            user_id,
            len(product_ids),
        )

        request = demo_pb2.ListRecommendationsRequest(
            user_id=user_id,
            product_ids=product_ids,
        )

        try:
            response: demo_pb2.ListRecommendationsResponse = (
                await self._stub.ListRecommendations(request)
            )
        except grpc.aio.AioRpcError as exc:
            logger.error(
                "ListRecommendations failed | code=%s | details=%s",
                exc.code(),
                exc.details(),
            )
            raise RecommendationClientError(
                "ListRecommendations", exc.code(), exc.details()
            ) from exc

        recommended = list(response.product_ids)
        llm_metrics = response.llm_metrics
        logger.info(
            "ListRecommendations returned %d product(s) for user=%s: %s",
            len(recommended),
            user_id,
            recommended,
        )
        logger.info(
            "ListRecommendations LLM metrics | user_id=%s: input_tokens=%d, output_tokens=%d, llm_calls=%d",
            user_id,
            llm_metrics.total_input_tokens,
            llm_metrics.total_output_tokens,
            llm_metrics.total_llm_calls,
        )
        return recommended

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def close(self) -> None:
        await self._channel.close()
        logger.info("RecommendationClient channel closed | address=%s", self._address)

    async def __aenter__(self) -> "RecommendationClient":
        return self

    async def __aexit__(self, *_) -> None:
        await self.close()


# ── Standalone CLI ───────────────────────────────────────────────────────────

async def _run_cli(address: str, user_id: str, product_ids: list[str]) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    print(f"\nRecommendationClient → {address}")
    print(f"  user_id     : {user_id}")
    print(f"  exclude_ids : {product_ids}")
    print()

    async with RecommendationClient(address) as client:
        try:
            recs = await client.list_recommendations(user_id, product_ids)
        except RecommendationClientError as exc:
            print(f"ERROR: {exc}")
            sys.exit(1)

    if recs:
        print(f"Recommendations ({len(recs)}):")
        for pid in recs:
            print(f"  • {pid}")
    else:
        print("No recommendations returned.")


if __name__ == "__main__":
    """
    Usage:
        python -m recommendationservice.client <user_id> [product_id ...]

    Examples:
        python -m recommendationservice.client user-abc
        python -m recommendationservice.client user-abc OLJCESPC7Z 2ZYFJ3GM2N
        RECOMMENDATION_SERVICE_ADDR=recs:5058 python -m recommendationservice.client u1
    """
    if len(sys.argv) < 2:
        print("Usage: python -m recommendationservice.client <user_id> [product_id ...]")
        sys.exit(1)

    _addr    = os.getenv("RECOMMENDATION_SERVICE_ADDR", "localhost:5058")
    _user_id = sys.argv[1]
    _pids    = sys.argv[2:]

    asyncio.run(_run_cli(_addr, _user_id, _pids))
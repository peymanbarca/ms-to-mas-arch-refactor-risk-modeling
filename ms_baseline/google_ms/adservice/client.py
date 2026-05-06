"""
adservice/client.py

Python port of Java's AdServiceClient.java  (the standalone test/demo client).

Original Java behaviour:
  • Creates a ManagedChannel to the ad service host:port.
  • Calls getAds() with one or more context keys.
  • Prints the returned ads to stdout.
  • Shuts down the channel on exit.

This module provides two things:
  1. AdServiceClient  – async class usable in production code (e.g. frontend).
  2. A __main__ block that replicates the Java standalone client behaviour,
     runnable as:  python -m adservice.client [context_key ...]
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

import grpc

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))
from ..shared import demo_pb2
from ..shared import demo_pb2_grpc

logger = logging.getLogger(__name__)

# ── default target (matches Kubernetes service name + Java client default) ───
_DEFAULT_ADDR = os.getenv("AD_SERVICE_ADDR", "localhost:5057")


class AdServiceClient:
    """
    Async gRPC client for AdService.

    Mirrors the Java AdServiceClient channel lifecycle:
      • Lazily creates a single shared channel per instance.
      • Exposes get_ads() for programmatic use.
      • Supports async-context-manager protocol for clean channel shutdown.

    Usage
    -----
    Short-lived (matches Java try-with-resources style):

        async with AdServiceClient("adservice:9555") as client:
            ads = await client.get_ads(["photography", "kitchen"])
            for ad in ads:
                print(ad["text"], "→", ad["redirect_url"])

    Long-lived (singleton held by a FastAPI lifespan):

        client = AdServiceClient()
        # ... later:
        ads = await client.get_ads(["cycling"])
        await client.close()
    """

    def __init__(self, address: str | None = None) -> None:
        self._address = address or _DEFAULT_ADDR
        self._channel = grpc.aio.insecure_channel(self._address)
        self._stub = demo_pb2_grpc.AdServiceStub(self._channel)
        logger.info("AdServiceClient → %s", self._address)

    # ── public API ───────────────────────────────────────────────────────────

    async def get_ads(self, context_keys: list[str]) -> list[dict]:
        """
        Java equivalent:
            blockingStub.getAds(AdRequest.newBuilder().addAllContextKeys(keys).build())

        Args:
            context_keys: Category / keyword strings sent as context.
                          Pass [] to receive random default ads (mirrors Java behaviour).

        Returns:
            List of ad dicts:  [{"redirect_url": str, "text": str}, ...]

        Raises:
            grpc.aio.AioRpcError: on any transport or server-side gRPC error.
        """
        logger.info(
            "AdServiceClient.get_ads | address=%s | keys=%s",
            self._address,
            context_keys,
        )

        request = demo_pb2.AdRequest(context_keys=context_keys)

        try:
            response: demo_pb2.AdResponse = await self._stub.GetAds(request)
        except grpc.aio.AioRpcError as exc:
            logger.error(
                "GetAds RPC failed | code=%s | details=%s",
                exc.code(),
                exc.details(),
            )
            raise

        ads = [
            {"redirect_url": ad.redirect_url, "text": ad.text}
            for ad in response.ads
        ]
        logger.info("AdServiceClient received %d ad(s)", len(ads))
        logger.info("LLM metrics received | total_input_tokens=%d | total_output_tokens=%d | total_llm_calls=%d",
                    response.llm_metrics.total_input_tokens,
                    response.llm_metrics.total_output_tokens,
                    response.llm_metrics.total_llm_calls)
        return ads

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def close(self) -> None:
        """
        Java equivalent:  channel.shutdown().awaitTermination(5, SECONDS)
        """
        await self._channel.close()
        logger.info("AdServiceClient channel closed | address=%s", self._address)

    async def __aenter__(self) -> "AdServiceClient":
        return self

    async def __aexit__(self, *_) -> None:
        await self.close()


# ── Standalone CLI client (mirrors Java AdServiceClient.main) ────────────────

async def _run_client(address: str, context_keys: list[str]) -> None:
    """
    Replicates the Java standalone client:
      logger.info("Get Ads with context " + keys)
      AdResponse response = blockingStub.getAds(...)
      logger.info("Ads: " + response.getAdsList())
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    async with AdServiceClient(address) as client:
        logger.info("Get Ads with context %s ...", context_keys)
        try:
            ads = await client.get_ads(context_keys)
        except grpc.aio.AioRpcError as exc:
            logger.error("RPC failed: %s – %s", exc.code(), exc.details())
            sys.exit(1)

        if ads:
            for ad in ads:
                # Java: logger.info("Ads: " + ad.getText() + " " + ad.getRedirectUrl())
                logger.info("Ad: %s  →  %s", ad["text"], ad["redirect_url"])
        else:
            logger.info("No ads returned.")

        logger.info("Exiting AdServiceClient...")


if __name__ == "__main__":
    """
    Usage:
        python -m adservice.client [context_key ...]

    Examples:
        python -m adservice.client                    # random ads (no context)
        python -m adservice.client photography        # single key
        python -m adservice.client kitchen cycling    # multiple keys
        AD_SERVICE_ADDR=adservice:9555 python -m adservice.client camera
    """
    _addr = os.getenv("AD_SERVICE_ADDR", "localhost:5057")
    _keys = sys.argv[1:]  # all CLI args become context keys

    if not _keys:
        print("No context keys provided – will receive random default ads.\n")

    asyncio.run(_run_client(_addr, _keys))
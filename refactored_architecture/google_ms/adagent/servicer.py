"""
adservice/servicer.py

Python port of Java's AdService.AdServiceImpl inner class.

Now using agentic ad selection via LangGraph + Ollama LLM.

Original Java logic (census-ecosystem + main-branch):
  ┌──────────────────────────────────────────────────────────────────┐
  │  getAds(AdRequest req, StreamObserver<AdResponse> observer)      │
  │    if req.contextKeys is non-empty:                              │
  │      for each key → look up ad in cacheMap / adsMap              │
  │      if nothing found → fall back to getDefaultAds()            │
  │    else:                                                         │
  │      ads = getDefaultAds()                                       │
  │    return AdResponse(ads)                                        │
  └──────────────────────────────────────────────────────────────────┘

The Python version now delegates ad selection to the ad request agent,
which uses LLM ranking + MongoDB persistence while maintaining identical
gRPC interface and fallback behavior.
"""

from __future__ import annotations

import logging
import os
import sys
import asyncio

import grpc

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))
from ..shared import demo_pb2
from ..shared import demo_pb2_grpc

from .ad_catalog import (
    AdEntry,
    get_ads_by_category,
    get_ads_by_key,
    get_random_ads,
    ADS_BY_CATEGORY,
    MAX_ADS_TO_SERVE,
)
from .adagent import run_ad_request_agent

logger = logging.getLogger(__name__)


def _to_proto(entry: AdEntry) -> demo_pb2.Ad:
    """Convert an AdEntry dataclass to a proto Ad message."""
    return demo_pb2.Ad(redirect_url=entry.redirect_url, text=entry.text)


class AdServicer(demo_pb2_grpc.AdServiceServicer):
    """
    Async gRPC servicer – wire-compatible replacement for the Java AdServiceImpl.

    Strategy (matches Java main-branch behaviour):
      1. If context_keys present → collect ads by category for each key.
         Each key is tried as a *category* first (ImmutableListMultimap strategy),
         then as a direct *keyword* key (HashMap strategy).
      2. If ads list is still empty → serve MAX_ADS_TO_SERVE random ads.
    """

    # ── RPC ─────────────────────────────────────────────────────────────────

    async def GetAds(
        self,
        request: demo_pb2.AdRequest,
        context: grpc.aio.ServicerContext,
    ) -> demo_pb2.AdResponse:
        """
        Java equivalent:
            public void getAds(AdRequest req, StreamObserver<AdResponse> obs)

        Now delegated to agentic ad selection via LangGraph.
        The agent uses LLM ranking + MongoDB persistence while maintaining
        identical gRPC interface and fallback behavior.
        """
        context_keys: list[str] = list(request.context_keys)
        logger.info(
            "GetAds called | context_keys_count=%d | keys=%s",
            len(context_keys),
            context_keys,
        )

        # Prepare catalog for agent
        # Convert ADS_BY_CATEGORY to format agent expects: category -> [{"redirect_url": ..., "text": ...}, ...]
        catalog = {}
        for category, ads in ADS_BY_CATEGORY.items():
            catalog[category] = [
                {"redirect_url": ad.redirect_url, "text": ad.text}
                for ad in ads
            ]

        # Invoke agent for intelligent ad selection
        try:
            response = await run_ad_request_agent(
                context_keys=context_keys,
                catalog=catalog,
                max_ads=MAX_ADS_TO_SERVE,
            )
            
            # Convert agent results to proto
            selected_ads = response.get("ads", [])
            ads = [
                demo_pb2.Ad(
                    redirect_url=ad["redirect_url"],
                    text=ad["text"]
                )
                for ad in selected_ads
            ]
            llm_metrics = response.get("llm_metrics", {})
            
            logger.info("GetAds returning %d ad(s) via agent", len(ads))
            return demo_pb2.AdResponse(ads=ads, llm_metrics=llm_metrics)

        except Exception as e:
            logger.error("Agent error during ad selection: %s", e)
            # Graceful fallback: return random ads
            logger.info("Falling back to random ads due to agent error")
            ads = [_to_proto(a) for a in get_random_ads(MAX_ADS_TO_SERVE)]
            return demo_pb2.AdResponse(ads=ads, llm_metrics=demo_pb2.LLMMetrics(
                total_input_tokens=-1,
                total_output_tokens=-1,
                total_llm_calls=-1))
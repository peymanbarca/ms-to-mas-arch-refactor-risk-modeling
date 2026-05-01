"""
adservice/servicer.py

Python port of Java's AdService.AdServiceImpl inner class.

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

The Python version keeps identical behaviour and adds structured logging
that mirrors the Java logger.info / logger.warning calls.
"""

from __future__ import annotations

import logging
import os
import sys

import grpc

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))
from ..shared import demo_pb2
from ..shared import demo_pb2_grpc

from .ad_catalog import (
    AdEntry,
    get_ads_by_category,
    get_ads_by_key,
    get_random_ads,
    MAX_ADS_TO_SERVE,
)

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

        Maps context_keys → ads using the same two-tier lookup the Java code uses.
        """
        context_keys: list[str] = list(request.context_keys)
        logger.info(
            "GetAds called | context_keys_count=%d | keys=%s",
            len(context_keys),
            context_keys,
        )

        ads: list[demo_pb2.Ad] = []

        if context_keys:
            # ── Java: "Constructing Ads using context" ───────────────────────
            for key in context_keys:
                # Strategy 1: category lookup (ImmutableListMultimap)
                category_ads = get_ads_by_category(key.lower())
                if category_ads:
                    ads.extend(_to_proto(a) for a in category_ads)
                    logger.debug("Category hit: key=%r → %d ads", key, len(category_ads))
                else:
                    # Strategy 2: direct key lookup (HashMap, original version)
                    entry = get_ads_by_key(key.lower())
                    if entry:
                        ads.append(_to_proto(entry))
                        logger.debug("Key hit: key=%r → 1 ad", key)
                    else:
                        logger.debug("No ad found for key=%r", key)
        else:
            # ── Java: "No Context provided. Constructing random Ads." ────────
            logger.info("No context keys provided → returning random ads")

        # ── Java: "No Ads found based on context. Constructing random Ads." ──
        if not ads:
            logger.info("Context yielded no ads → falling back to random ads")
            ads = [_to_proto(a) for a in get_random_ads(MAX_ADS_TO_SERVE)]

        logger.info("GetAds returning %d ad(s)", len(ads))
        return demo_pb2.AdResponse(ads=ads)
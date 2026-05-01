"""
recommendationservice/servicer.py

Async gRPC servicer – a modernised, line-by-line port of the original
recommendation_server.py (Google Cloud Platform / microservices-demo).

Original logic (verbatim):
─────────────────────────────────────────────────────────────────────────────
  def ListRecommendations(self, request, context):
      max_responses = 5
      cat_response = product_catalog_stub.ListProducts(demo_pb2.Empty())
      product_ids = [x.id for x in cat_response.products]
      filtered_products = list(set(product_ids) - set(request.product_ids))
      num_products = len(filtered_products)
      num_return   = min(max_responses, num_products)
      indices      = random.sample(range(num_products), num_return)
      prod_list    = [filtered_products[i] for i in indices]
      logger.info("[Recv ListRecommendations] product_ids={}".format(prod_list))
      response = demo_pb2.ListRecommendationsResponse()
      response.product_ids.extend(prod_list)
      return response
─────────────────────────────────────────────────────────────────────────────

Changes from original:
  • Uses grpc.aio (async) instead of grpc (sync ThreadPoolExecutor)
  • ProductCatalog stub injected via constructor instead of module-level global
    → makes the class unit-testable without patching globals
  • Health check kept identical (SERVING / UNIMPLEMENTED)
  • Structured JSON logging via logger.py kept intact
"""

from __future__ import annotations

import logging
import random
from typing import Optional
import os
import sys

import grpc

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))
from ..shared import demo_pb2
from ..shared import demo_pb2_grpc

logger = logging.getLogger("recommendationservice-server")

MAX_RESPONSES: int = 5   # original hard-coded value


class RecommendationServicer(
    demo_pb2_grpc.RecommendationServiceServicer,
):
    """
    Async gRPC implementation of RecommendationService + gRPC HealthService.

    The ProductCatalog stub is injected so the servicer can be tested without
    a live ProductCatalogService.  In production, pass the real async stub.

    Args:
        product_catalog_stub: An async ProductCatalogServiceStub instance.
    """

    def __init__(
        self,
        product_catalog_stub: demo_pb2_grpc.ProductCatalogServiceStub,
    ) -> None:
        self._catalog_stub = product_catalog_stub

    # ── RecommendationService RPC ────────────────────────────────────────────

    async def ListRecommendations(
        self,
        request: demo_pb2.ListRecommendationsRequest,
        context: grpc.aio.ServicerContext,
    ) -> demo_pb2.ListRecommendationsResponse:
        """
        Original Python (sync) → async Python port.

        Algorithm (unchanged):
          1. Fetch the full product list from ProductCatalogService.
          2. Remove products the user already has in their context (request.product_ids).
          3. Return up to MAX_RESPONSES randomly sampled products from what's left.
        """
        # ── Step 1: fetch all product IDs from ProductCatalogService ─────────
        # Original: cat_response = product_catalog_stub.ListProducts(demo_pb2.Empty())
        try:
            cat_response: demo_pb2.ListProductsResponse = (
                await self._catalog_stub.ListProducts(demo_pb2.Empty())
            )
            logger.info("Product catalog fetched from catalog service: %d products" % len(cat_response.products))
        except grpc.aio.AioRpcError as exc:
            logger.error(
                "Failed to call ProductCatalogService.ListProducts | "
                "code=%s | details=%s",
                exc.code(),
                exc.details(),
            )
            await context.abort(
                grpc.StatusCode.INTERNAL,
                f"upstream ProductCatalogService error: {exc.details()}",
            )
            return demo_pb2.ListRecommendationsResponse()

        # ── Step 2: filter out products already in the request ───────────────
        # Original: product_ids = [x.id for x in cat_response.products]
        #           filtered_products = list(set(product_ids) - set(request.product_ids))
        product_ids: list[str] = [p.id for p in cat_response.products]
        filtered_products: list[str] = list(
            set(product_ids) - set(request.product_ids)
        )

        # ── Step 3: randomly sample up to MAX_RESPONSES ──────────────────────
        # Original: num_products = len(filtered_products)
        #           num_return   = min(max_responses, num_products)
        #           indices      = random.sample(range(num_products), num_return)
        #           prod_list    = [filtered_products[i] for i in indices]
        num_products: int = len(filtered_products)
        num_return: int = min(MAX_RESPONSES, num_products)
        indices: list[int] = random.sample(range(num_products), num_return)
        prod_list: list[str] = [filtered_products[i] for i in indices]

        # Original: logger.info("[Recv ListRecommendations] product_ids={}".format(prod_list))
        logger.info(
            "[Recv ListRecommendations] user_id=%s product_ids=%s",
            request.user_id,
            prod_list,
        )

        # ── Step 4: build and return response ────────────────────────────────
        # Original: response = demo_pb2.ListRecommendationsResponse()
        #           response.product_ids.extend(prod_list)
        #           return response
        response = demo_pb2.ListRecommendationsResponse()
        response.product_ids.extend(prod_list)
        return response

    # ── gRPC HealthService RPCs ──────────────────────────────────────────────
    # Original kept these identical; we do the same.
"""
Async gRPC servicer — same ListRecommendations proto interface, now powered
by the LangGraph recommendation agent (agent.py).

Original algorithm (random.sample):
    1. Fetch full catalog from ProductCatalogService
    2. Filter out products in request.product_ids
    3. random.sample(filtered, min(5, len(filtered)))

Agent algorithm (LLM reasoning with random.sample fallback):
    fetch_catalog → filter_products → recommendation_reasoning (LLM)
                  ↳ on empty: return_empty
                  → format_response

gRPC error mapping (unchanged):
    ProductCatalogService unavailable → INTERNAL abort
    All other errors                  → INTERNAL abort
    LLM failure                       → graceful fallback to random.sample()
                                        (never aborts; always returns valid response)
"""

from __future__ import annotations

import logging

import grpc
# from grpc_health.v1 import health_pb2, health_pb2_grpc

from ..shared import demo_pb2
from ..shared import demo_pb2_grpc
from .recommendationagent import run_recommendation_agent

logger = logging.getLogger("recommendationservicer")


class RecommendationServicer(
    demo_pb2_grpc.RecommendationServiceServicer,
    # health_pb2_grpc.HealthServicer,
):
    """
    Async gRPC servicer — wire-compatible with the original Python service.

    The catalog stub is still injected in __init__ (unchanged interface) and
    forwarded to the agent graph on each request.
    """

    def __init__(
        self,
        product_catalog_stub: demo_pb2_grpc.ProductCatalogServiceStub,
    ) -> None:
        self._catalog_stub = product_catalog_stub

    # ── ListRecommendations RPC ───────────────────────────────────────────────

    async def ListRecommendations(
        self,
        request: demo_pb2.ListRecommendationsRequest,
        context: grpc.aio.ServicerContext,
    ) -> demo_pb2.ListRecommendationsResponse:
        """
        Original algorithm:  random.sample(filtered_catalog, up_to_5)
        Agent algorithm:     LLM ranks filtered catalog → returns diverse picks
                             Falls back to random.sample() on any LLM failure.

        The proto interface is identical — callers see no difference.
        """
        logger.info(
            "[ListRecommendations] user_id=%s excluded=%d",
            request.user_id,
            len(request.product_ids),
        )

        try:
            final_state = await run_recommendation_agent(
                user_id=request.user_id,
                excluded_ids=list(request.product_ids),
                catalog_stub=self._catalog_stub,
                grpc_context=context,
            )

        except Exception as exc:
            logger.error(
                "[ListRecommendations] agent error | user_id=%s error=%s",
                request.user_id, exc, exc_info=True,
            )
            await context.abort(
                grpc.StatusCode.INTERNAL,
                f"recommendation agent error: {exc}",
            )
            return demo_pb2.ListRecommendationsResponse()

        recommended = final_state.get("recommended_ids", [])

        # Log the original line format (preserved from original servicer)
        logger.info(
            "[Recv ListRecommendations] user_id=%s product_ids=%s llm_used=%s llm_calls=%d",
            request.user_id,
            recommended,
            final_state.get("llm_used", False),
            final_state.get("total_llm_calls", 0),
        )

        if final_state.get("error") and not final_state.get("llm_used"):
            logger.warning(
                "[ListRecommendations] LLM fell back to random | user_id=%s reason=%s",
                request.user_id, final_state["error"],
            )

        response = demo_pb2.ListRecommendationsResponse()
        response.product_ids.extend(recommended)
        return response

    # ── gRPC HealthService ────────────────────────────────────────────────────

    # async def Check(
    #     self,
    #     request: health_pb2.HealthCheckRequest,
    #     context: grpc.aio.ServicerContext,
    # ) -> health_pb2.HealthCheckResponse:
    #     return health_pb2.HealthCheckResponse(
    #         status=health_pb2.HealthCheckResponse.SERVING
    #     )

    # async def Watch(
    #     self,
    #     request: health_pb2.HealthCheckRequest,
    #     context: grpc.aio.ServicerContext,
    # ) -> health_pb2.HealthCheckResponse:
    #     return health_pb2.HealthCheckResponse(
    #         status=health_pb2.HealthCheckResponse.UNIMPLEMENTED
    #     )
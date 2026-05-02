"""
checkoutservice/servicer.py

Async gRPC servicer — wraps CheckoutOrchestrator and maps gRPC error codes
exactly as the Go checkoutService struct methods do.

Go method signatures reproduced:
    PlaceOrder(ctx, req) → (PlaceOrderResponse, error)
    Check(ctx, req)      → (HealthCheckResponse, nil)
    Watch(req, ws)       → error(Unimplemented)
"""

from __future__ import annotations

import logging

import grpc
# from grpc_health.v1 import health_pb2, health_pb2_grpc

from ..shared import demo_pb2
from ..shared import demo_pb2_grpc
from .orchestrator import CheckoutOrchestrator

logger = logging.getLogger("checkoutservice")


class CheckoutServicer(
    demo_pb2_grpc.CheckoutServiceServicer,
    # health_pb2_grpc.HealthServicer,
):
    """
    gRPC servicer that delegates all business logic to CheckoutOrchestrator.

    The orchestrator is injected so the servicer itself stays thin and testable.
    """

    def __init__(self, orchestrator: CheckoutOrchestrator) -> None:
        self._orch = orchestrator

    # ── PlaceOrder RPC ────────────────────────────────────────────────────────
    async def PlaceOrder(
        self,
        request: demo_pb2.PlaceOrderRequest,
        context: grpc.aio.ServicerContext,
    ) -> demo_pb2.PlaceOrderResponse:
        """
        Go:
            func (cs *checkoutService) PlaceOrder(ctx, req) (*pb.PlaceOrderResponse, error)

        Error mapping (identical to Go):
            prepareOrderItemsAndShippingQuoteFromCart failure → INTERNAL
            chargeCard failure                                → INTERNAL
            shipOrder failure                                 → UNAVAILABLE
        """
        try:
            return await self._orch.place_order(request)

        except _ShipOrderError as exc:
            # Go: return nil, status.Errorf(codes.Unavailable, "shipping error: %+v", err)
            logger.error("[PlaceOrder] shipping error: %s", exc)
            await context.abort(grpc.StatusCode.UNAVAILABLE, f"shipping error: {exc}")

        except _ChargeCardError as exc:
            # Go: return nil, status.Errorf(codes.Internal, "failed to charge card: %+v", err)
            logger.error("[PlaceOrder] charge card error: %s", exc)
            await context.abort(grpc.StatusCode.INTERNAL, f"failed to charge card: {exc}")

        except Exception as exc:
            # Go: return nil, status.Errorf(codes.Internal, err.Error())
            logger.error("[PlaceOrder] internal error: %s", exc, exc_info=True)
            await context.abort(grpc.StatusCode.INTERNAL, str(exc))

        return demo_pb2.PlaceOrderResponse()

    # ── Health RPCs ───────────────────────────────────────────────────────────
    # async def Check(
    #     self,
    #     request: health_pb2.HealthCheckRequest,
    #     context: grpc.aio.ServicerContext,
    # ) -> health_pb2.HealthCheckResponse:
    #     """Go: return &healthpb.HealthCheckResponse{Status: SERVING}, nil"""
    #     return health_pb2.HealthCheckResponse(
    #         status=health_pb2.HealthCheckResponse.SERVING
    #     )

    # async def Watch(
    #     self,
    #     request: health_pb2.HealthCheckRequest,
    #     context: grpc.aio.ServicerContext,
    # ) -> None:
    #     """Go: return status.Errorf(codes.Unimplemented, "health check via Watch not implemented")"""
    #     await context.abort(
    #         grpc.StatusCode.UNIMPLEMENTED,
    #         "health check via Watch not implemented",
    #     )


# ── Sentinel error types so servicer can map to correct gRPC status codes ────
class _ChargeCardError(Exception):
    pass

class _ShipOrderError(Exception):
    pass
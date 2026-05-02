"""
shippingservice/servicer.py

Async gRPC servicer – direct Python port of the Go shippingservice server struct.

Go originals (main.go):
─────────────────────────────────────────────────────────────────────────────
  type server struct {
      pb.UnimplementedShippingServiceServer
  }

  func (s *server) GetQuote(ctx context.Context, in *pb.GetQuoteRequest) (*pb.GetQuoteResponse, error) {
      log.Info("[GetQuote] received request")
      defer log.Info("[GetQuote] completed request")

      count := 0
      for _, item := range in.Items {
          count += int(item.Quantity)
      }
      quote := CreateQuoteFromCount(count)

      return &pb.GetQuoteResponse{
          CostUsd: &pb.Money{
              CurrencyCode: "USD",
              Units:        int64(quote.Dollars),
              Nanos:        int32(quote.Cents * 10000000),
          },
      }, nil
  }

  func (s *server) ShipOrder(ctx context.Context, in *pb.ShipOrderRequest) (*pb.ShipOrderResponse, error) {
      log.Info("[ShipOrder] received request")
      defer log.Info("[ShipOrder] completed request")

      baseAddress := fmt.Sprintf("%s, %s, %s",
          in.Address.StreetAddress, in.Address.City, in.Address.State)
      id := CreateTrackingId(baseAddress)

      return &pb.ShipOrderResponse{TrackingId: id}, nil
  }

  func (s *server) Check(...) { return SERVING }
  func (s *server) Watch(...)  { return UNIMPLEMENTED }
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import logging

import grpc
# from grpc_health.v1 import health_pb2, health_pb2_grpc

from ..shared import demo_pb2
from ..shared import demo_pb2_grpc
from .quote import create_quote_from_count, create_tracking_id

logger = logging.getLogger("shippingservice")


class ShippingServicer(
    demo_pb2_grpc.ShippingServiceServicer,
    # health_pb2_grpc.HealthServicer,
):
    """
    Async gRPC servicer – wire-compatible replacement for the Go server struct.

    Implements:
      • ShippingService  (GetQuote, ShipOrder)
      • gRPC HealthService  (Check, Watch)
    """

    # ── GetQuote ─────────────────────────────────────────────────────────────

    async def GetQuote(
        self,
        request: demo_pb2.GetQuoteRequest,
        context: grpc.aio.ServicerContext,
    ) -> demo_pb2.GetQuoteResponse:
        """
        Go:  func (s *server) GetQuote(...)

        Produces a USD shipping quote based on total item count.

        Steps (identical to Go):
          1. Sum all item quantities from the request.
          2. Call CreateQuoteFromCount to get tiered price.
          3. Return Money{USD, dollars, cents * 10_000_000}.
        """
        # Go: log.Info("[GetQuote] received request")
        logger.info("[GetQuote] received request")

        # Go: count := 0
        #     for _, item := range in.Items { count += int(item.Quantity) }
        count: int = sum(item.quantity for item in request.items)

        # Go: quote := CreateQuoteFromCount(count)
        quote = create_quote_from_count(count)

        logger.info(
            "[GetQuote] item_count=%d → $%d.%02d",
            count, quote.dollars, quote.cents,
        )

        # Go: log.Info("[GetQuote] completed request")
        logger.info("[GetQuote] completed request")

        # Go: return &pb.GetQuoteResponse{
        #       CostUsd: &pb.Money{
        #           CurrencyCode: "USD",
        #           Units:        int64(quote.Dollars),
        #           Nanos:        int32(quote.Cents * 10000000),
        #       },
        #     }
        return demo_pb2.GetQuoteResponse(
            cost_usd=demo_pb2.Money(
                currency_code="USD",
                units=quote.dollars,
                nanos=quote.nanos,   # cents * 10_000_000
            )
        )

    # ── ShipOrder ────────────────────────────────────────────────────────────

    async def ShipOrder(
        self,
        request: demo_pb2.ShipOrderRequest,
        context: grpc.aio.ServicerContext,
    ) -> demo_pb2.ShipOrderResponse:
        """
        Go:  func (s *server) ShipOrder(...)

        Mocks shipment dispatch and returns a tracking ID.

        Steps (identical to Go):
          1. Build a base-address string from the request address fields.
          2. Call CreateTrackingId to generate a mock tracking ID.
          3. Return ShipOrderResponse with that tracking ID.
        """
        # Go: log.Info("[ShipOrder] received request")
        logger.info("[ShipOrder] received request")

        # Go: baseAddress := fmt.Sprintf("%s, %s, %s",
        #         in.Address.StreetAddress, in.Address.City, in.Address.State)
        base_address = (
            f"{request.address.street_address}, "
            f"{request.address.city}, "
            f"{request.address.state}"
        )

        # Go: id := CreateTrackingId(baseAddress)
        tracking_id = create_tracking_id(base_address)

        logger.info(
            "[ShipOrder] base_address=%r → tracking_id=%s",
            base_address,
            tracking_id,
        )

        # Go: log.Info("[ShipOrder] completed request")
        logger.info("[ShipOrder] completed request")

        # Go: return &pb.ShipOrderResponse{TrackingId: id}
        return demo_pb2.ShipOrderResponse(tracking_id=tracking_id)

    # ── gRPC HealthService ───────────────────────────────────────────────────

    # async def Check(
    #     self,
    #     request: health_pb2.HealthCheckRequest,
    #     context: grpc.aio.ServicerContext,
    # ) -> health_pb2.HealthCheckResponse:
    #     # Go: return &healthpb.HealthCheckResponse{Status: healthpb.HealthCheckResponse_SERVING}
    #     return health_pb2.HealthCheckResponse(
    #         status=health_pb2.HealthCheckResponse.SERVING
    #     )

    # async def Watch(
    #     self,
    #     request: health_pb2.HealthCheckRequest,
    #     context: grpc.aio.ServicerContext,
    # ) -> health_pb2.HealthCheckResponse:
    #     # Go: return status.Errorf(codes.Unimplemented, "health check via Watch not implemented")
    #     await context.abort(
    #         grpc.StatusCode.UNIMPLEMENTED,
    #         "health check via Watch not implemented",
    #     )
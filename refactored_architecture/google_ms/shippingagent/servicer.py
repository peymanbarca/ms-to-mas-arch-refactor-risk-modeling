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
from .shippingagent import run_shipping_agent

logger = logging.getLogger("shippingservicer")


class ShippingServicer(
    demo_pb2_grpc.ShippingServiceServicer,
    # health_pb2_grpc.HealthServicer,
):
    """
    Async gRPC servicer – wire-compatible replacement for the Go server struct.

    Implements:
      • ShippingService  (GetQuote, ShipOrder)
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
            ), llm_metrics=demo_pb2.LLMMetrics() 
        )

    # ── ShipOrder ────────────────────────────────────────────────────────────

    async def ShipOrder(
        self,
        request: demo_pb2.ShipOrderRequest,
        context: grpc.aio.ServicerContext,
    ) -> demo_pb2.ShipOrderResponse:
        """
        gRPC ShipOrder endpoint – now powered by ShippingAgent (LangGraph).

        Flow:
          1. Extract address and items from gRPC request.
          2. Invoke run_shipping_agent() – orchestrates the agentic workflow:
             • validate_address: check all address fields are provided
             • fetch_quote:      calculate shipping cost and generate IDs
             • shipping_reasoning: LLM decision (APPROVED or REJECTED)
             • persist_shipment:  write result to MongoDB
          3. Return tracking_id in ShipOrderResponse (same interface as before).

        Key difference from baseline servicer:
          • Shipment decision now goes through LLM reasoning (non-deterministic).
          • Only approved shipments get a tracking_id (REJECTED ones return placeholder).
          • Full audit trail stored in MongoDB for every decision.
          • Token metrics included in MongoDB document for observability.
        """
        logger.info("[ShipOrder] received request")

        # Extract address and items
        items_list = [
            {"product_id": item.product_id, "quantity": item.quantity}
            for item in request.items
        ]

        # Invoke the shipping agent
        agent_result = await run_shipping_agent(
            items=items_list,
            street_address=request.address.street_address,
            city=request.address.city,
            state=request.address.state,
            country=request.address.country,
            zip_code=request.address.zip_code,
        )

        # Extract decision
        decision_status = agent_result["decision"].get("status", "REJECTED")
        tracking_id = agent_result.get("tracking_id") or "REJECTED"

        logger.info(
            "[ShipOrder] agent decision=%s | tracking_id=%s",
            decision_status, tracking_id,
        )
        logger.info("[ShipOrder] completed request")

        # Return same gRPC response (backward compatible)
        return demo_pb2.ShipOrderResponse(tracking_id=tracking_id, llm_metrics=demo_pb2.LLMMetrics(
            total_input_tokens=agent_result.get("total_input_tokens", 0),
            total_output_tokens=agent_result.get("total_output_tokens", 0),
            total_llm_calls=agent_result.get("total_llm_calls", 0),
        ))

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
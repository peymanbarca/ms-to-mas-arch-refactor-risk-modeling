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
import uuid
from datetime import datetime
from os import getenv

import grpc
from motor.motor_asyncio import AsyncIOMotorClient
# from grpc_health.v1 import health_pb2, health_pb2_grpc

from ..shared import demo_pb2
from ..shared import demo_pb2_grpc
from .quote import create_quote_from_count, create_tracking_id

logger = logging.getLogger("shippingservice")

# MongoDB configuration
MONGODB_URI = getenv("MONGODB_URI", "mongodb://user:pass1@localhost:27017")
MONGODB_DB = getenv("MONGODB_DB", "google_ms")

# Global MongoDB client (lazy-initialized)
_mongodb_client: AsyncIOMotorClient | None = None


async def get_mongodb_client() -> AsyncIOMotorClient:
    """Get or create MongoDB client (lazy initialization)."""
    global _mongodb_client
    if _mongodb_client is None:
        _mongodb_client = AsyncIOMotorClient(MONGODB_URI)
    return _mongodb_client


async def get_shipments_collection():
    """Get shipments collection with auto-created indexes."""
    client = await get_mongodb_client()
    db = client[MONGODB_DB]
    collection = db["shipments"]
    
    # Create indexes
    await collection.create_index("tracking_id", unique=True)
    await collection.create_index("created_at")
    
    return collection


async def save_shipment_record(
    shipping_id: str,
    request_data: dict,
    quote: dict,
    tracking_id: str,
) -> None:
    """
    Persist shipment record to MongoDB.
    
    Args:
        shipping_id: UUID for this shipment
        request_data: Address and order details from request
        quote: Quote cost details
        tracking_id: Generated tracking ID
    """
    try:
        collection = await get_shipments_collection()
        
        document = {
            "_id": shipping_id,
            "status": "shipped",
            "request": request_data,
            "quote": quote,
            "tracking_id": tracking_id,
            "created_at": datetime.utcnow(),
        }
        
        await collection.insert_one(document)
        logger.info(
            "[MongoDB] Shipment persisted: shipping_id=%s, tracking_id=%s",
            shipping_id,
            tracking_id,
        )
    except Exception as exc:
        logger.error(
            "[MongoDB] Failed to persist shipment: shipping_id=%s, error=%s",
            shipping_id,
            str(exc),
            exc_info=True,
        )


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
            )
        )

    # ── ShipOrder ────────────────────────────────────────────────────────────

    async def ShipOrder(
            self,
            request: demo_pb2.ShipOrderRequest,
            context: grpc.aio.ServicerContext,
        ) -> demo_pb2.ShipOrderResponse:
            """
            Go: func (s *server) ShipOrder(ctx, in) (*pb.ShipOrderResponse, error)
    
            Extended flow (Go steps + MongoDB persistence):
    
            1. Generate a UUID for this shipment record.
            2. Build base-address string (mirrors Go fmt.Sprintf).
            3. Generate tracking ID from the address hash.
            4. Call GetQuote internally to get the shipping cost.
                ──────────────────────────────────────────────────────────
                WHY: ShipOrderRequest proto = { address, items } only.
                    There is no cost_usd field on this message.
                    We reuse the same address + items to compute the
                    cost so the MongoDB record is complete and accurate,
                    without requiring the caller to send the cost twice.
                ──────────────────────────────────────────────────────────
            5. Persist the full shipment document to MongoDB (best-effort).
            6. Return ShipOrderResponse{ tracking_id }.
            """
            logger.info("[ShipOrder] received request")
    
            # ── Step 1: unique shipment ID ────────────────────────────────────────
            shipping_id = str(uuid.uuid4())
    
            # ── Step 2: build base address string ─────────────────────────────────
            # Go: baseAddress := fmt.Sprintf("%s, %s, %s",
            #         in.Address.StreetAddress, in.Address.City, in.Address.State)
            base_address = (
                f"{request.address.street_address}, "
                f"{request.address.city}, "
                f"{request.address.state}"
            )
    
            # ── Step 3: generate tracking ID ──────────────────────────────────────
            # Go: id := CreateTrackingId(baseAddress)
            tracking_id = create_tracking_id(base_address)
            logger.info(
                "[ShipOrder] base_address=%r → tracking_id=%s",
                base_address, tracking_id,
            )
    
            # ── Step 4: fetch shipping cost via internal GetQuote call ────────────
            #
            # ShipOrderRequest has { address, items } but NO cost_usd field.
            # We call our own GetQuote with the same address + items to get an
            # accurate cost for MongoDB persistence.  This is a direct method call
            # (no network round-trip) — identical in semantics to how checkoutservice
            # calls GetQuote before calling ShipOrder over the network.
            quote_request = demo_pb2.GetQuoteRequest(
                address=request.address,
                items=list(request.items),
            )
            quote_response: demo_pb2.GetQuoteResponse = await self.GetQuote(
                quote_request, context
            )
    
            cost = quote_response.cost_usd
            cents = cost.nanos // 10_000_000
            logger.info(
                "[ShipOrder] quote fetched | %s %d.%02d",
                cost.currency_code, cost.units, cents,
            )
    
            # ── Step 5: persist to MongoDB (best-effort, never blocks response) ───
            request_data = {
                "address": {
                    "street_address": request.address.street_address,
                    "city":           request.address.city,
                    "state":          request.address.state,
                    "country":        request.address.country,
                    "zip_code":       request.address.zip_code,
                },
                "items": [
                    {"product_id": item.product_id, "quantity": item.quantity}
                    for item in request.items
                ],
            }
            cost_usd_data = {
                "currency_code": cost.currency_code,
                "units":         cost.units,
                "nanos":         cost.nanos,
                "formatted":     f"{cost.currency_code} {cost.units}.{cents:02d}",
            }
    
            try:
                await save_shipment_record(
                    shipping_id=shipping_id,
                    request_data=request_data,
                    quote=cost_usd_data,
                    tracking_id=tracking_id,
                )
            except Exception as exc:
                logger.warning(
                    "[ShipOrder] MongoDB persistence failed (non-fatal) | "
                    "shipping_id=%s error=%s",
                    shipping_id, exc,
                )
    
            # ── Step 6: return response ────────────────────────────────────────────
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
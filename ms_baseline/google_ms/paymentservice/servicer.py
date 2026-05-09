"""
paymentservice/servicer.py

Async gRPC servicer – direct Python port of Node.js server.js + charge.js.

Node.js original (server.js):
─────────────────────────────────────────────────────────────────────────────
  server.addService(paymentServiceProto.PaymentService.service, {
    charge: async (call, callback) => {
      logger.info('PaymentService#Charge called with request', { request: call.request });
      try {
        const response = await charge(call.request);
        logger.info(`PaymentService#Charge returning response`, { response });
        callback(null, response);
      } catch (err) {
        logger.warn(err);
        callback(err);
      }
    }
  });
─────────────────────────────────────────────────────────────────────────────

The gRPC HealthService (Check / Watch) is also registered here, matching
the Node.js server's grpc-health-check registration.

MongoDB Integration:
─────────────────────────────────────────────────────────────────────────────
- Each charge request generates a UUID payment_id
- Response persisted to MongoDB under collection "payment_transactions"
- On success: stores transaction_id and charge details
- On error: stores error field with exception details
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime

import grpc
from motor.motor_asyncio import AsyncIOMotorClient
# from grpc_health.v1 import health_pb2, health_pb2_grpc

from ..shared import demo_pb2
from ..shared import demo_pb2_grpc
from .card_validator import charge, CardValidationError

logger = logging.getLogger("paymentservice")

# ── MongoDB Configuration ─────────────────────────────────────────────────

MONGODB_URI = os.getenv("MONGODB_URI", "mongodb://localhost:27017")
MONGODB_DB = os.getenv("MONGODB_DB", "google_ms")

# Global client (lazy-initialized)
_mongodb_client: AsyncIOMotorClient = None


async def get_mongodb_client() -> AsyncIOMotorClient:
    """Get or create the MongoDB client."""
    global _mongodb_client
    if _mongodb_client is None:
        _mongodb_client = AsyncIOMotorClient(MONGODB_URI)
        # Verify connection
        await _mongodb_client.admin.command("ping")
        logger.info("Connected to MongoDB at %s", MONGODB_URI)
    return _mongodb_client


async def get_payment_transactions_collection():
    """Get the payment_transactions collection."""
    client = await get_mongodb_client()
    db = client[MONGODB_DB]
    collection = db["payment_transactions"]
    
    # Ensure indexes
    await collection.create_index("created_at")
    return collection


async def save_charge_transaction(
    payment_id: str,
    request_data: dict,
    response_data: dict = None,
    error: str = None,
    error_code: str = None,
) -> None:
    """
    Persist charge transaction to MongoDB.
    
    Args:
        payment_id: UUID generated for this payment
        request_data: Credit card and amount information
        response_data: Successful charge response (transaction_id, etc.)
        error: Error message (if charge failed)
        error_code: gRPC error code (if charge failed)
    """
    collection = await get_payment_transactions_collection()
    
    document = {
        "_id": payment_id,
        "payment_id": payment_id,
        "created_at": datetime.utcnow(),
        "request": request_data,
    }
    
    if response_data:
        document["response"] = response_data
        document["status"] = "success"
    elif error:
        document["error"] = error
        document["error_code"] = error_code
        document["status"] = "failed"
    
    try:
        await collection.insert_one(document)
        logger.debug(f"Transaction persisted | payment_id={payment_id} status={document.get('status')}")
    except Exception as e:
        logger.error(f"Failed to persist transaction | payment_id={payment_id} error={str(e)}", exc_info=True)


class PaymentServicer(
    demo_pb2_grpc.PaymentServiceServicer,
    # health_pb2_grpc.HealthServicer,
):
    """
    Async gRPC servicer – wire-compatible replacement for the Node.js PaymentService.

    Implements:
      • PaymentService  (Charge)
      • gRPC HealthService  (Check, Watch)
    """

    # ── Charge RPC ────────────────────────────────────────────────────────────

    async def Charge(
        self,
        request: demo_pb2.ChargeRequest,
        context: grpc.aio.ServicerContext,
    ) -> demo_pb2.ChargeResponse:
        """
        JS equivalent:
            server.addService(..., { charge: async (call, callback) => { ... } })

        Validates the credit card, then returns a mock transaction_id (UUID v4).
        All transactions are persisted to MongoDB with a generated payment_id (UUID).

        gRPC error codes mirror the Node.js error propagation:
          • CardValidationError → INVALID_ARGUMENT  (same semantics as JS callback(err))
          • Unexpected errors   → INTERNAL
        """
        # Generate unique payment_id for this transaction
        payment_id = str(uuid.uuid4())
        
        cc = request.credit_card
        amt = request.amount

        # JS: logger.info('PaymentService#Charge called with request', { request })
        logger.info(
            "PaymentService#Charge called | payment_id=%s currency=%s units=%d nanos=%d "
            "card_ending=%s exp=%02d/%d",
            payment_id,
            amt.currency_code,
            amt.units,
            amt.nanos,
            cc.credit_card_number[-4:] if cc.credit_card_number else "????",
            cc.credit_card_expiration_month,
            cc.credit_card_expiration_year,
        )

        # Prepare request data for persistence
        request_data = {
            "amount": {
                "currency_code": amt.currency_code,
                "units": amt.units,
                "nanos": amt.nanos,
            },
            "credit_card": {
                "number_ending": cc.credit_card_number[-4:] if cc.credit_card_number else "????",
                "expiration_month": cc.credit_card_expiration_month,
                "expiration_year": cc.credit_card_expiration_year,
            }
        }

        try:
            result = charge(
                credit_card_number=cc.credit_card_number,
                credit_card_cvv=cc.credit_card_cvv,
                credit_card_expiration_year=cc.credit_card_expiration_year,
                credit_card_expiration_month=cc.credit_card_expiration_month,
                amount_currency_code=amt.currency_code,
                amount_units=amt.units,
                amount_nanos=amt.nanos,
            )
        except CardValidationError as exc:
            # JS: callback(err)  with grpc INVALID_ARGUMENT
            logger.warning("Charge rejected | payment_id=%s reason=%s", payment_id, exc)
            
            # Persist failed transaction to MongoDB
            await save_charge_transaction(
                payment_id=payment_id,
                request_data=request_data,
                error=str(exc),
                error_code="INVALID_ARGUMENT",
            )
            
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
            return demo_pb2.ChargeResponse()

        except Exception as exc:
            # Unexpected – map to INTERNAL
            logger.error("Charge unexpected error | payment_id=%s error=%s", payment_id, exc, exc_info=True)
            
            # Persist failed transaction to MongoDB
            await save_charge_transaction(
                payment_id=payment_id,
                request_data=request_data,
                error=str(exc),
                error_code="INTERNAL",
            )
            
            await context.abort(grpc.StatusCode.INTERNAL, f"Unexpected error: {exc}")
            return demo_pb2.ChargeResponse()

        # JS: logger.info('PaymentService#Charge returning response', { response })
        logger.info(
            "PaymentService#Charge success | payment_id=%s card_type=%s last_four=%s transaction_id=%s",
            payment_id,
            result.card_type,
            result.last_four,
            result.transaction_id,
        )

        # Prepare response data for persistence
        response_data = {
            "transaction_id": result.transaction_id,
            "card_type": result.card_type,
            "last_four": result.last_four,
        }
        
        # Persist successful transaction to MongoDB
        await save_charge_transaction(
            payment_id=payment_id,
            request_data=request_data,
            response_data=response_data,
        )

        # JS: callback(null, response)  where response = { transaction_id: uuidv4() }
        return demo_pb2.ChargeResponse(transaction_id=result.transaction_id, llm_metrics=demo_pb2.LLMMetrics(
            total_input_tokens=-1,
            total_output_tokens=-1,
            total_llm_calls=-1
        ))

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
    #     await context.abort(
    #         grpc.StatusCode.UNIMPLEMENTED,
    #         "health check via Watch not implemented",
    #     )
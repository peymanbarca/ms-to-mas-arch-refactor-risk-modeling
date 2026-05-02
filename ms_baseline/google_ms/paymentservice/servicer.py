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
"""

from __future__ import annotations

import logging

import grpc
# from grpc_health.v1 import health_pb2, health_pb2_grpc

from ..shared import demo_pb2
from ..shared import demo_pb2_grpc
from .card_validator import charge, CardValidationError

logger = logging.getLogger("paymentservice")


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

        gRPC error codes mirror the Node.js error propagation:
          • CardValidationError → INVALID_ARGUMENT  (same semantics as JS callback(err))
          • Unexpected errors   → INTERNAL
        """
        cc = request.credit_card
        amt = request.amount

        # JS: logger.info('PaymentService#Charge called with request', { request })
        logger.info(
            "PaymentService#Charge called | currency=%s units=%d nanos=%d "
            "card_ending=%s exp=%02d/%d",
            amt.currency_code,
            amt.units,
            amt.nanos,
            cc.credit_card_number[-4:] if cc.credit_card_number else "????",
            cc.credit_card_expiration_month,
            cc.credit_card_expiration_year,
        )

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
            logger.warning("Charge rejected | reason=%s", exc)
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
            return demo_pb2.ChargeResponse()

        except Exception as exc:
            # Unexpected – map to INTERNAL
            logger.error("Charge unexpected error | error=%s", exc, exc_info=True)
            await context.abort(grpc.StatusCode.INTERNAL, f"Unexpected error: {exc}")
            return demo_pb2.ChargeResponse()

        # JS: logger.info('PaymentService#Charge returning response', { response })
        logger.info(
            "PaymentService#Charge success | card_type=%s last_four=%s transaction_id=%s",
            result.card_type,
            result.last_four,
            result.transaction_id,
        )

        # JS: callback(null, response)  where response = { transaction_id: uuidv4() }
        return demo_pb2.ChargeResponse(transaction_id=result.transaction_id)

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
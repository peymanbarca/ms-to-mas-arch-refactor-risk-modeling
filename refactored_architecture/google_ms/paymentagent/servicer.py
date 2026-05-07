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
from .paymentagent import run_payment_agent

logger = logging.getLogger("paymentagent")



class PaymentServicer(
    demo_pb2_grpc.PaymentServiceServicer,
    # health_pb2_grpc.HealthServicer,
):
    """
    Async gRPC servicer — wire-compatible with the Node.js PaymentService.
 
    The only internal change: charge() is now the LangGraph agent graph, which
    adds PSP simulation, LLM-based authorization (Ollama llama3), and MongoDB
    persistence while keeping the identical proto interface.
    """
 
    # ── Charge RPC ────────────────────────────────────────────────────────────
 
    async def Charge(
        self,
        request: demo_pb2.ChargeRequest,
        context: grpc.aio.ServicerContext,
    ) -> demo_pb2.ChargeResponse:
        """
        gRPC Charge RPC — identical proto interface, agent-powered internals.
 
        Agent graph steps:
            validate_card → [call_psp → payment_reasoning] or [reject_card]
                         → persist_payment
 
        Maps agent output to gRPC response / error codes:
            decision.status == SUCCESS  → ChargeResponse(transaction_id)
            decision.status == FAILED
                with validation_error   → abort(INVALID_ARGUMENT)
                without validation_error → abort(INTERNAL)
            Graph exception             → abort(INTERNAL)
        """
        cc  = request.credit_card
        amt = request.amount
 
        logger.info(
            "PaymentService#Charge called | currency=%s units=%d nanos=%d "
            "card_ending=%s exp=%02d/%d",
            amt.currency_code, amt.units, amt.nanos,
            cc.credit_card_number[-4:] if cc.credit_card_number else "????",
            cc.credit_card_expiration_month, cc.credit_card_expiration_year,
        )
 
        try:
            final_state = await run_payment_agent(
                card_number          = cc.credit_card_number,
                card_cvv             = cc.credit_card_cvv,
                exp_year             = cc.credit_card_expiration_year,
                exp_month            = cc.credit_card_expiration_month,
                amount_currency_code = amt.currency_code,
                amount_units         = amt.units,
                amount_nanos         = amt.nanos,
            )
 
        except Exception as exc:
            logger.error("Charge agent error | error=%s", exc, exc_info=True)
            await context.abort(
                grpc.StatusCode.INTERNAL,
                f"Payment agent error: {exc}",
            )
            return demo_pb2.ChargeResponse()
 
        decision         = final_state.get("decision", {})
        status           = decision.get("status", "FAILED")
        reason           = decision.get("reason", "")
        validation_error = final_state.get("validation_error")
 
        if status == "SUCCESS":
            transaction_id = final_state.get("transaction_id", "")
            logger.info(
                "PaymentService#Charge success | card_type=%s last_four=%s "
                "transaction_id=%s llm_calls=%d",
                final_state.get("card_type"),
                final_state.get("last_four"),
                transaction_id,
                final_state.get("total_llm_calls", 0),
            )
            return demo_pb2.ChargeResponse(transaction_id=transaction_id, llm_metrics=demo_pb2.LLMMetrics(
                total_input_tokens=final_state.get("total_input_tokens", 0),
                total_output_tokens=final_state.get("total_output_tokens", 0),
                total_llm_calls=final_state.get("total_llm_calls", 0),
            ))
 
        # FAILED branch
        if validation_error:
            logger.warning("Charge rejected (validation) | reason=%s", validation_error)
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, validation_error)
        else:
            logger.warning("Charge rejected (agent decision) | reason=%s", reason)
            await context.abort(
                grpc.StatusCode.INTERNAL,
                f"Payment declined: {reason}",
            )
        return demo_pb2.ChargeResponse()

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
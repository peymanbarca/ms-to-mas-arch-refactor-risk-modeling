"""
emailservice/servicer.py

Async gRPC servicer for EmailService — identical proto interface, now
powered by the LangGraph email agent (agent.py).

Proto interface (unchanged):
    SendOrderConfirmation(SendOrderConfirmationRequest) → Empty

Agent graph replaces the original dummy implementation:
    prepare_order_data → personalise_message (LLM) → render_template
    → send_email → persist_email_log

gRPC error mapping:
    Template rendering failure  → INTERNAL
    All other errors            → INTERNAL  (best-effort; email is non-critical)
    send_status == "failed"     → logged as WARNING, gRPC still returns Empty
                                  (mirrors original: email failure ≠ order failure)
"""

from __future__ import annotations

import logging

import grpc

from ..shared import demo_pb2
from ..shared import demo_pb2_grpc
from .emailagent import run_email_agent

logger = logging.getLogger("emailservicer")


class EmailServicer(
    demo_pb2_grpc.EmailServiceServicer,
    # health_pb2_grpc.HealthSeervicer,
):
    """
    Async gRPC servicer — wire-compatible with the original Python EmailService.

    Internal change: the agent graph replaces the bare Jinja2 render + log.
    The gRPC interface is identical: SendOrderConfirmation returns Empty().
    """

    # ── SendOrderConfirmation RPC ─────────────────────────────────────────────

    async def SendOrderConfirmation(
        self,
        request: demo_pb2.SendOrderConfirmationRequest,
        context: grpc.aio.ServicerContext,
    ) -> demo_pb2.LLMMetrics:
        """
        Original dummy implementation:
            - render Jinja2 template
            - log the HTML
            - return Empty()

        Agent-powered implementation:
            prepare_order_data
            → personalise_message  (Ollama llama3)
            → render_template      (Jinja2, items list fully populated)
            → send_email           (log today; SendGrid / SES / SMTP via env var)
            → persist_email_log    (MongoDB audit record)
            → return Empty()

        gRPC behaviour mirrors original:
            • Template error → INTERNAL abort
            • All other failures → logged as WARNING, Empty() returned
              (email is best-effort; it must never fail a checkout)
        """
        email    = request.email
        order    = request.order

        logger.info(
            "EmailService#SendOrderConfirmation | to=%s order_id=%s",
            email, order.order_id,
        )

        try:
            final_state = await run_email_agent(
                recipient_email=email,
                order_proto=order,
            )

        except Exception as exc:
            logger.error(
                "EmailService#SendOrderConfirmation agent error | to=%s error=%s",
                email, exc, exc_info=True,
            )
            if context:
                await context.abort(
                    grpc.StatusCode.INTERNAL,
                    f"Email agent error: {exc}",
                )
            return demo_pb2.LLMMetrics()

        # Template rendering failure → INTERNAL (same as original TemplateError branch)
        if final_state.get("error") and "Template rendering" in (final_state.get("error") or ""):
            logger.error(
                "EmailService#SendOrderConfirmation template error | to=%s error=%s",
                email, final_state["error"],
            )
            if context:
                await context.abort(
                    grpc.StatusCode.INTERNAL,
                    "An error occurred when preparing the confirmation mail.",
                )
            return demo_pb2.LLMMetrics()

        # Delivery failure → warn but still return Empty() (email is non-critical)
        send_status = final_state.get("send_status", "unknown")
        if send_status == "failed":
            logger.warning(
                "EmailService#SendOrderConfirmation delivery failed (non-fatal) | "
                "to=%s order_id=%s error=%s",
                email, order.order_id, final_state.get("error"),
            )
        else:
            logger.info(
                "EmailService#SendOrderConfirmation success | "
                "to=%s order_id=%s status=%s llm_calls=%d",
                email, order.order_id, send_status,
                final_state.get("total_llm_calls", 0),
            )

        return demo_pb2.LLMMetrics(total_input_tokens=final_state.get("total_input_tokens", 0),
                                   total_output_tokens=final_state.get("total_output_tokens", 0),
                                   total_llm_calls=final_state.get("total_llm_calls", 0))

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
    # ) -> None:
    #     await context.abort(
    #         grpc.StatusCode.UNIMPLEMENTED,
    #         "health check via Watch not implemented",
    #     )
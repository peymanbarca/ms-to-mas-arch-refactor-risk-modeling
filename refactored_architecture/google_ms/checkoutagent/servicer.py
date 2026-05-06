"""
checkoutservice/servicer.py

Async gRPC servicer — identical PlaceOrder proto interface, now powered by
the ReAct checkout agent (agent.py) instead of the deterministic orchestrator.

The agent replaces the fixed pipeline with LLM-driven dynamic reasoning:
    LLM decides which tool to call at each iteration based on full state.

gRPC error mapping (preserved):
    transaction_id missing after agent completes → INTERNAL  (payment failed)
    tracking_id missing after payment succeeds   → UNAVAILABLE  (shipping failed)
    Agent-level exception                        → INTERNAL
"""

from __future__ import annotations

import logging

import grpc
from fastapi import HTTPException
# from grpc_health.v1 import health_pb2, health_pb2_grpc

from ..shared import demo_pb2
from ..shared import demo_pb2_grpc
from ..shared.metrics import build_llm_metrics

from .checkoutagent import run_checkout_agent

logger = logging.getLogger("checkoutservicer")


class CheckoutServicer(
    demo_pb2_grpc.CheckoutServiceServicer,
    # health_pb2_grpc.HealthServicer,
):
    """
    gRPC servicer that delegates all checkout orchestration to the ReAct agent.
    All 6 downstream stubs are injected in __init__ and forwarded to the agent.
    """

    def __init__(
        self,
        cart_stub:     demo_pb2_grpc.CartServiceStub,
        catalog_stub:  demo_pb2_grpc.ProductCatalogServiceStub,
        currency_stub: demo_pb2_grpc.CurrencyServiceStub,
        shipping_stub: demo_pb2_grpc.ShippingServiceStub,
        payment_stub:  demo_pb2_grpc.PaymentServiceStub,
        email_stub:    demo_pb2_grpc.EmailServiceStub,
    ) -> None:
        self._cart     = cart_stub
        self._catalog  = catalog_stub
        self._currency = currency_stub
        self._shipping = shipping_stub
        self._payment  = payment_stub
        self._email    = email_stub

    async def PlaceOrder(
        self,
        request: demo_pb2.PlaceOrderRequest,
        context: grpc.aio.ServicerContext = None,
    ) -> demo_pb2.PlaceOrderResponse:
        logger.info(
            "[PlaceOrder] user_id=%s currency=%s",
            request.user_id, request.user_currency,
        )

        try:
            final_state = await run_checkout_agent(
                request=request,
                cart_stub=self._cart,
                catalog_stub=self._catalog,
                currency_stub=self._currency,
                shipping_stub=self._shipping,
                payment_stub=self._payment,
                email_stub=self._email,
                grpc_context=context,
            )
        except Exception as exc:
            logger.error("[PlaceOrder] agent error: %s", exc, exc_info=True)
            if context:
                await context.abort(grpc.StatusCode.INTERNAL, f"checkout agent error: {exc}")
            else:
                raise  # Re-raise for REST callers
            return demo_pb2.PlaceOrderResponse()

        if not final_state.get("transaction_id"):
            msg = final_state.get("fatal_error") or "payment was not completed"
            if context:
                await context.abort(grpc.StatusCode.INTERNAL, f"failed to charge card: {msg}")
            else:
                raise HTTPException(status_code=500, detail=f"failed to charge card: {msg}")
            return demo_pb2.PlaceOrderResponse()

        if not final_state.get("tracking_id"):
            if context:
                await context.abort(
                    grpc.StatusCode.UNAVAILABLE, "shipping error: order was not dispatched"
                )
            else:
                raise HTTPException(status_code=500, detail="shipping error: order was not dispatched")
            return demo_pb2.PlaceOrderResponse()

        # Build OrderResult
        order_items_proto = []
        for oi in (final_state.get("order_items") or []):
            uc = oi["unit_cost"]
            order_items_proto.append(
                demo_pb2.OrderItem(
                    item=demo_pb2.CartItem(
                        product_id=oi["product_id"], quantity=oi["quantity"]
                    ),
                    cost=demo_pb2.Money(
                        currency_code=uc["currency_code"],
                        units=uc["units"],
                        nanos=uc["nanos"],
                    ),
                )
            )

        sc   = final_state.get("shipping_cost") or {}
        addr = final_state.get("address") or {}
        order_result = demo_pb2.OrderResult(
            order_id=final_state["order_id"],
            shipping_tracking_id=final_state["tracking_id"],
            shipping_cost=demo_pb2.Money(
                currency_code=sc.get("currency_code", request.user_currency),
                units=sc.get("units", 0),
                nanos=sc.get("nanos", 0),
            ),
            shipping_address=demo_pb2.Address(
                street_address=addr.get("street_address", ""),
                city=addr.get("city", ""),
                state=addr.get("state", ""),
                country=addr.get("country", ""),
                zip_code=addr.get("zip_code", 0),
            ),
            items=order_items_proto,
        )

        logger.info(
            "[PlaceOrder] success | order_id=%s llm_calls=%d iterations=%d",
            final_state["order_id"],
            final_state["total_llm_calls"],
            final_state["iteration"],
        )
        
        # Build LLM metrics
        llm_metrics = build_llm_metrics(
            total_input_tokens=final_state.get("total_input_tokens", 0),
            total_output_tokens=final_state.get("total_output_tokens", 0),
            total_llm_calls=final_state.get("total_llm_calls", 0),
        )
        
        return demo_pb2.PlaceOrderResponse(order=order_result, llm_metrics=llm_metrics)

    # async def Check(self, request, context):
    #     return health_pb2.HealthCheckResponse(
    #         status=health_pb2.HealthCheckResponse.SERVING
    #     )

    # async def Watch(self, request, context):
    #     await context.abort(
    #         grpc.StatusCode.UNIMPLEMENTED, "health check via Watch not implemented"
    #     )
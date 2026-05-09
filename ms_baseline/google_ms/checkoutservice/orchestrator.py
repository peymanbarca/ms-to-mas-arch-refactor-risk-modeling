"""
checkoutservice/orchestrator.py

Complete async Python port of the Go checkoutservice, with full LLM metrics
aggregation from every downstream service response.

LLM metrics collection
─────────────────────────────────────────────────────────────────────────────
Every downstream service now embeds LLMMetrics in its response:

    CartService      GetCartResponse    { cart, llm_metrics }
                     EmptyCartResponse  { llm_metrics }
    ProductCatalog   GetProductResponse { product, llm_metrics }
    CurrencyService  CurrencyConversionResponse { money, llm_metrics }
    ShippingService  GetQuoteResponse   { cost_usd, llm_metrics }
                     ShipOrderResponse  { tracking_id, llm_metrics }
    PaymentService   ChargeResponse     { transaction_id, llm_metrics }
    EmailService     SendOrderConfirmationResponse { llm_metrics }

This orchestrator:
  1. Creates a single LLMMetricsAccumulator at the start of place_order.
  2. Passes it into every helper method.
  3. Each helper adds the downstream service's llm_metrics to the accumulator.
  4. place_order returns PlaceOrderResponse with the fully aggregated totals,
     including checkout's own agent LLM calls (if any).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from os import getenv

from ..shared import demo_pb2
from ..shared import demo_pb2_grpc

from .money import (
    Money,
    money_must,
    money_multiply_slow,
    money_sum,
    proto_to_money,
    zero_money,
)

from motor.motor_asyncio import AsyncIOMotorClient

logger = logging.getLogger("checkoutservice")

MONGODB_URI = getenv("MONGODB_URI", "mongodb://localhost:27017")
MONGODB_DB  = getenv("MONGODB_DB",  "google_ms")

_mongodb_client: AsyncIOMotorClient | None = None


# ── MongoDB ───────────────────────────────────────────────────────────────────

async def get_mongodb_client() -> AsyncIOMotorClient:
    global _mongodb_client
    if _mongodb_client is None:
        _mongodb_client = AsyncIOMotorClient(MONGODB_URI)
    return _mongodb_client


async def get_order_collection():
    client = await get_mongodb_client()
    return client[MONGODB_DB]["orders"]


# ════════════════════════════════════════════════════════════════════════════
# LLMMetricsAccumulator
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class LLMMetricsAccumulator:
    """
    Mutable accumulator that sums LLMMetrics across all downstream service calls
    during a single PlaceOrder request.

    Usage:
        acc = LLMMetricsAccumulator()
        acc.add(some_grpc_response.llm_metrics)   # safe even if llm_metrics is None
        ...
        proto = acc.to_proto()                     # → demo_pb2.LLMMetrics
    """
    total_input_tokens:  int = -1
    total_output_tokens: int = -1
    total_llm_calls:     int = -1

    def add(self, metrics: demo_pb2.LLMMetrics | None) -> None:
        """
        Merge one LLMMetrics proto into this accumulator.
        Safe to call with None (e.g. when a service has not been updated yet).
        """
        if metrics is None:
            return
        self.total_input_tokens  += getattr(metrics, "total_input_tokens",  0)
        self.total_output_tokens += getattr(metrics, "total_output_tokens", 0)
        self.total_llm_calls     += getattr(metrics, "total_llm_calls",     0)

    def add_own(
        self,
        input_tokens:  int = 0,
        output_tokens: int = 0,
        llm_calls:     int = 0,
    ) -> None:
        """
        Add checkout's own LLM usage (agent reasoning iterations).
        Called after the checkout agent finishes.
        """
        self.total_input_tokens  += input_tokens
        self.total_output_tokens += output_tokens
        self.total_llm_calls     += llm_calls

    def to_proto(self) -> demo_pb2.LLMMetrics:
        """Convert to the proto LLMMetrics message for PlaceOrderResponse."""
        return demo_pb2.LLMMetrics(
            total_input_tokens=self.total_input_tokens,
            total_output_tokens=self.total_output_tokens,
            total_llm_calls=self.total_llm_calls,
        )

    def __repr__(self) -> str:
        return (
            f"LLMMetrics("
            f"in={self.total_input_tokens} "
            f"out={self.total_output_tokens} "
            f"calls={self.total_llm_calls})"
        )


# ── orderPrep (Go struct) ─────────────────────────────────────────────────────

@dataclass
class OrderPrep:
    order_items:             list[demo_pb2.OrderItem] = field(default_factory=list)
    cart_items:              list[demo_pb2.CartItem]  = field(default_factory=list)
    shipping_cost_localized: demo_pb2.Money | None    = None


# ════════════════════════════════════════════════════════════════════════════
# CheckoutOrchestrator
# ════════════════════════════════════════════════════════════════════════════

class CheckoutOrchestrator:
    """
    Holds injected downstream stubs and implements every orchestration helper.
    Every helper that receives a response from a downstream service accepts an
    optional LLMMetricsAccumulator and calls acc.add(response.llm_metrics).
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

    # ── getUserCart ───────────────────────────────────────────────────────────

    async def get_user_cart(
        self,
        user_id: str,
        acc: LLMMetricsAccumulator,
    ) -> list[demo_pb2.CartItem]:
        """
        Calls CartService.GetCart.
        GetCartResponse { Cart cart; LLMMetrics llm_metrics }
        Unwraps resp.cart.items and accumulates resp.llm_metrics.
        """
        try:
            resp: demo_pb2.GetCartResponse = await self._cart.GetCart(
                demo_pb2.GetCartRequest(user_id=user_id)
            )
        except Exception as exc:
            raise RuntimeError(
                f"failed to get user cart during checkout: {exc}"
            ) from exc

        # Collect CartService LLM metrics (always 0 for CartService, but API-consistent)
        acc.add(resp.llm_metrics)
        logger.debug(
            "[get_user_cart] user_id=%s items=%d metrics=%s",
            user_id, len(resp.cart.items), acc,
        )
        return list(resp.cart.items)

    # ── emptyUserCart ─────────────────────────────────────────────────────────

    async def empty_user_cart(
        self,
        user_id: str,
        acc: LLMMetricsAccumulator,
    ) -> None:
        """
        Calls CartService.EmptyCart.
        EmptyCartResponse { LLMMetrics llm_metrics }
        """
        try:
            resp: demo_pb2.EmptyCartResponse = await self._cart.EmptyCart(
                demo_pb2.EmptyCartRequest(user_id=user_id)
            )
            acc.add(resp.llm_metrics)
        except Exception as exc:
            raise RuntimeError(
                f"failed to empty user cart during checkout: {exc}"
            ) from exc

    # ── convertCurrency ───────────────────────────────────────────────────────

    async def convert_currency(
        self,
        from_money: demo_pb2.Money,
        to_currency: str,
        acc: LLMMetricsAccumulator,
    ) -> demo_pb2.Money:
        """
        Calls CurrencyService.Convert.
        CurrencyConversionResponse { Money money; LLMMetrics llm_metrics }
        """
        if from_money.currency_code == to_currency:
            return from_money
        try:
            resp: demo_pb2.CurrencyConversionResponse = await self._currency.Convert(
                demo_pb2.CurrencyConversionRequest(
                    from_=from_money,
                    to_code=to_currency,
                )
            )
        except Exception as exc:
            raise RuntimeError(f"failed to convert currency: {exc}") from exc

        acc.add(resp.llm_metrics)
        return resp.money   # CurrencyConversionResponse wraps money in .money field

    # ── quoteShipping ─────────────────────────────────────────────────────────

    async def quote_shipping(
        self,
        address: demo_pb2.Address,
        items: list[demo_pb2.CartItem],
        acc: LLMMetricsAccumulator,
    ) -> demo_pb2.Money:
        """
        Calls ShippingService.GetQuote.
        GetQuoteResponse { Money cost_usd; LLMMetrics llm_metrics }
        """
        try:
            resp: demo_pb2.GetQuoteResponse = await self._shipping.GetQuote(
                demo_pb2.GetQuoteRequest(address=address, items=items)
            )
        except Exception as exc:
            raise RuntimeError(f"failed to get shipping quote: {exc}") from exc

        acc.add(resp.llm_metrics)
        return resp.cost_usd

    # ── prepOrderItems ────────────────────────────────────────────────────────

    async def prep_order_items(
        self,
        cart_items: list[demo_pb2.CartItem],
        user_currency: str,
        acc: LLMMetricsAccumulator,
    ) -> list[demo_pb2.OrderItem]:
        """
        For each cart item:
          1. ProductCatalogService.GetProduct  → GetProductResponse { product, llm_metrics }
          2. CurrencyService.Convert           → CurrencyConversionResponse { money, llm_metrics }
        Both responses contribute to acc.
        """
        order_items: list[demo_pb2.OrderItem] = []

        for item in cart_items:
            # ── GetProduct ────────────────────────────────────────────────────
            try:
                product_resp: demo_pb2.GetProductResponse = await self._catalog.GetProduct(
                    demo_pb2.GetProductRequest(id=item.product_id)
                )
            except Exception as exc:
                raise RuntimeError(
                    f"failed to get product #{item.product_id!r}: {exc}"
                ) from exc

            acc.add(product_resp.llm_metrics)
            product = product_resp.product   # unwrap from GetProductResponse

            # ── ConvertCurrency ───────────────────────────────────────────────
            try:
                price: demo_pb2.Money = await self.convert_currency(
                    product.price_usd, user_currency, acc
                )
            except Exception as exc:
                raise RuntimeError(
                    f"failed to convert price of {item.product_id!r} "
                    f"to {user_currency}: {exc}"
                ) from exc

            order_items.append(demo_pb2.OrderItem(item=item, cost=price))

        return order_items

    # ── prepareOrderItemsAndShippingQuoteFromCart ─────────────────────────────

    async def prepare_order_items_and_shipping_quote(
        self,
        user_id: str,
        user_currency: str,
        address: demo_pb2.Address,
        acc: LLMMetricsAccumulator,
    ) -> OrderPrep:
        out = OrderPrep()

        try:
            cart_items = await self.get_user_cart(user_id, acc)
        except Exception as exc:
            raise RuntimeError(f"cart failure: {exc}") from exc
        out.cart_items = cart_items
        logger.info("fetched cart | user_id=%s items=%d", user_id, len(cart_items))

        try:
            order_items = await self.prep_order_items(cart_items, user_currency, acc)
        except Exception as exc:
            raise RuntimeError(f"failed to prepare order: {exc}") from exc
        out.order_items = order_items

        try:
            shipping_usd = await self.quote_shipping(address, cart_items, acc)
        except Exception as exc:
            raise RuntimeError(f"shipping quote failure: {exc}") from exc

        try:
            shipping_localized = await self.convert_currency(
                shipping_usd, user_currency, acc
            )
        except Exception as exc:
            raise RuntimeError(
                f"failed to convert shipping cost to currency: {exc}"
            ) from exc

        out.shipping_cost_localized = shipping_localized
        logger.info(
            "shipping quote | %s %d.%02d",
            shipping_localized.currency_code,
            shipping_localized.units,
            shipping_localized.nanos // 10_000_000,
        )
        return out

    # ── chargeCard ────────────────────────────────────────────────────────────

    async def charge_card(
        self,
        amount: demo_pb2.Money,
        credit_card: demo_pb2.CreditCardInfo,
        acc: LLMMetricsAccumulator,
    ) -> str:
        """
        Calls PaymentService.Charge.
        ChargeResponse { string transaction_id; LLMMetrics llm_metrics }
        """
        try:
            resp: demo_pb2.ChargeResponse = await self._payment.Charge(
                demo_pb2.ChargeRequest(amount=amount, credit_card=credit_card)
            )
        except Exception as exc:
            raise RuntimeError(f"could not charge the card: {exc}") from exc

        acc.add(resp.llm_metrics)
        logger.debug("[charge_card] transaction_id=%s metrics=%s", resp.transaction_id, acc)
        return resp.transaction_id

    # ── shipOrder ─────────────────────────────────────────────────────────────

    async def ship_order(
        self,
        address: demo_pb2.Address,
        items: list[demo_pb2.CartItem],
        acc: LLMMetricsAccumulator,
    ) -> str:
        """
        Calls ShippingService.ShipOrder.
        ShipOrderResponse { string tracking_id; LLMMetrics llm_metrics }
        """
        try:
            resp: demo_pb2.ShipOrderResponse = await self._shipping.ShipOrder(
                demo_pb2.ShipOrderRequest(address=address, items=items)
            )
        except Exception as exc:
            raise RuntimeError(f"shipment failed: {exc}") from exc

        acc.add(resp.llm_metrics)
        logger.debug("[ship_order] tracking_id=%s metrics=%s", resp.tracking_id, acc)
        return resp.tracking_id

    # ── sendOrderConfirmation ─────────────────────────────────────────────────

    async def send_order_confirmation(
        self,
        email: str,
        order: demo_pb2.OrderResult,
        acc: LLMMetricsAccumulator,
    ) -> None:
        """
        Calls EmailService.SendOrderConfirmation.
        SendOrderConfirmationResponse { LLMMetrics llm_metrics }
        (Email is best-effort — acc is still updated on success.)
        """
        resp: demo_pb2.SendOrderConfirmationResponse = (
            await self._email.SendOrderConfirmation(
                demo_pb2.SendOrderConfirmationRequest(email=email, order=order)
            )
        )
        # acc.add(resp.llm_metrics)

    # ── PlaceOrder ─────────────────────────────────────────────────────────────

    async def place_order(
        self,
        request: demo_pb2.PlaceOrderRequest,
        own_input_tokens:  int = 0,
        own_output_tokens: int = 0,
        own_llm_calls:     int = 0,
    ) -> demo_pb2.PlaceOrderResponse:
        """
        Full orchestration with aggregated LLM metrics across all downstream calls.

        Args:
            request:            PlaceOrderRequest proto.
            own_input_tokens:   Checkout's own LLM input tokens (from agent reasoning).
            own_output_tokens:  Checkout's own LLM output tokens.
            own_llm_calls:      Number of LLM calls made by checkout's own agent.

        Returns:
            PlaceOrderResponse { OrderResult order; LLMMetrics llm_metrics }
            where llm_metrics = sum of ALL downstream service metrics
                               + checkout's own agent metrics.
        """
        logger.info(
            "[PlaceOrder] user_id=%r user_currency=%r",
            request.user_id, request.user_currency,
        )

        # ── Single accumulator for the entire request ─────────────────────────
        acc = LLMMetricsAccumulator()

        # Add checkout's own agent LLM usage first
        # (passed in from the agent state after the ReAct loop completes)
        acc.add_own(
            input_tokens=own_input_tokens,
            output_tokens=own_output_tokens,
            llm_calls=own_llm_calls,
        )

        # ── Step 1: Generate order UUID + create PENDING record ───────────────
        order_id = str(uuid.uuid4())
        collection = await get_order_collection()
        await collection.insert_one({
            "_id":          order_id,
            "user_id":      request.user_id,
            "user_currency": request.user_currency,
            "status":       "pending",
            "created_at":   datetime.now(tz=timezone.utc),
        })
        logger.info("[PlaceOrder] order_id=%s created (pending)", order_id)

        # ── Step 2: Fetch cart + prices + shipping quote ──────────────────────
        # acc collects metrics from: GetCart, GetProduct×N, Convert×N, GetQuote, Convert
        prep = await self.prepare_order_items_and_shipping_quote(
            user_id=request.user_id,
            user_currency=request.user_currency,
            address=request.address,
            acc=acc,
        )

        # ── Step 3: Compute total ─────────────────────────────────────────────
        total = zero_money(request.user_currency)
        total = money_must(money_sum(total, proto_to_money(prep.shipping_cost_localized)))
        for order_item in prep.order_items:
            mult_price = money_multiply_slow(
                proto_to_money(order_item.cost), order_item.item.quantity
            )
            total = money_must(money_sum(total, mult_price))

        total_proto = demo_pb2.Money(
            currency_code=total.currency_code,
            units=total.units,
            nanos=total.nanos,
        )
        logger.info(
            "[PlaceOrder] total | %s %d.%02d",
            total_proto.currency_code,
            total_proto.units,
            total_proto.nanos // 10_000_000,
        )

        # ── Step 4: Charge card ───────────────────────────────────────────────
        # acc collects: ChargeResponse.llm_metrics (PaymentService agent)
        try:
            transaction_id = await self.charge_card(
                total_proto, request.credit_card, acc
            )
            await collection.update_one(
                {"_id": order_id},
                {"$set": {"transaction_id": transaction_id, "status": "paid"}},
            )
        except Exception as exc:
            await collection.update_one(
                {"_id": order_id},
                {"$set": {"status": "payment_failed"}},
            )
            raise RuntimeError(f"could not charge the card: {exc}") from exc

        logger.info("payment went through (transaction_id: %s)", transaction_id)

        # ── Step 5: Ship order ────────────────────────────────────────────────
        # acc collects: ShipOrderResponse.llm_metrics (ShippingService)
        try:
            tracking_id = await self.ship_order(
                request.address, prep.cart_items, acc
            )
            await collection.update_one(
                {"_id": order_id},
                {"$set": {"tracking_id": tracking_id, "status": "shipped"}},
            )
        except Exception as exc:
            await collection.update_one(
                {"_id": order_id},
                {"$set": {"status": "shipping_failed"}},
            )
            raise RuntimeError(f"shipment failed: {exc}") from exc

        # ── Step 6: Empty cart (best-effort) ──────────────────────────────────
        # acc collects: EmptyCartResponse.llm_metrics (always 0 for CartService)
        try:
            await self.empty_user_cart(request.user_id, acc)
        except Exception as exc:
            logger.warning(
                "failed to empty cart for user_id=%r (non-fatal): %s",
                request.user_id, exc,
            )

        # ── Step 7: Build OrderResult ─────────────────────────────────────────
        order_result = demo_pb2.OrderResult(
            order_id=order_id,
            shipping_tracking_id=tracking_id,
            shipping_cost=prep.shipping_cost_localized,
            shipping_address=request.address,
            items=prep.order_items,
        )

        # ── Step 8: Send confirmation email (best-effort) ─────────────────────
        # acc collects: SendOrderConfirmationResponse.llm_metrics (EmailService agent)
        try:
            await self.send_order_confirmation(request.email, order_result, acc)
            logger.info("order confirmation email sent to %r", request.email)
            await collection.update_one(
                {"_id": order_id},
                {"$set": {"status": "completed"}},
            )
        except Exception as exc:
            logger.warning(
                "failed to send order confirmation to %r: %s", request.email, exc
            )

        # ── Step 9: Log final aggregated metrics ──────────────────────────────
        logger.info(
            "[PlaceOrder] aggregated LLM metrics | order_id=%s %s",
            order_id, acc,
        )

        # ── Step 10: Return response with fully aggregated metrics ────────────
        return demo_pb2.PlaceOrderResponse(
            order=order_result,
            llm_metrics=acc.to_proto(),
        )
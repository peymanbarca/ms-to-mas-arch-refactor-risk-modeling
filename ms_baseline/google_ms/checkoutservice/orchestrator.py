"""
checkoutservice/orchestrator.py

Complete async Python port of every downstream call and the PlaceOrder
orchestration from the Go checkoutservice/main.go.

Mirrors every Go method on checkoutService struct:
  • getUserCart
  • emptyUserCart
  • quoteShipping
  • convertCurrency
  • prepOrderItems
  • prepareOrderItemsAndShippingQuoteFromCart
  • chargeCard
  • shipOrder
  • sendOrderConfirmation
  • PlaceOrder  (top-level orchestration called by the gRPC servicer)

All downstream stubs are injected in __init__ — identical to how Go uses
the checkoutService struct fields — so tests can swap in mocks without
any real servers.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime

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
from os import getenv

logger = logging.getLogger("checkoutservice")

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

async def get_order_collection():
    """Get order collection with auto-created indexes."""
    client = await get_mongodb_client()
    db = client[MONGODB_DB]
    collection = db["orders"]
    
    return collection
    

# ── orderPrep (Go struct) ─────────────────────────────────────────────────────
@dataclass
class OrderPrep:
    """
    Go:
        type orderPrep struct {
            orderItems            []*pb.OrderItem
            cartItems             []*pb.CartItem
            shippingCostLocalized *pb.Money
        }
    """
    order_items:             list[demo_pb2.OrderItem] = field(default_factory=list)
    cart_items:              list[demo_pb2.CartItem]  = field(default_factory=list)
    shipping_cost_localized: demo_pb2.Money | None    = None


# ── CheckoutOrchestrator ──────────────────────────────────────────────────────
class CheckoutOrchestrator:
    """
    Holds injected downstream stubs and implements every orchestration helper.

    Args:
        cart_stub:     CartServiceStub
        catalog_stub:  ProductCatalogServiceStub
        currency_stub: CurrencyServiceStub
        shipping_stub: ShippingServiceStub
        payment_stub:  PaymentServiceStub
        email_stub:    EmailServiceStub
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
    async def get_user_cart(self, user_id: str) -> list[demo_pb2.CartItem]:
        """
        Go:
            cart, err := pb.NewCartServiceClient(cs.cartSvcConn).
                GetCart(ctx, &pb.GetCartRequest{UserId: userID})
            if err != nil { return nil, fmt.Errorf("failed to get user cart during checkout: %+v", err) }
            return cart.GetItems(), nil
        """
        try:
            resp: demo_pb2.Cart = await self._cart.GetCart(
                demo_pb2.GetCartRequest(user_id=user_id)
            )
        except Exception as exc:
            raise RuntimeError(f"failed to get user cart during checkout: {exc}") from exc
        return list(resp.cart.items)

    # ── emptyUserCart ─────────────────────────────────────────────────────────
    async def empty_user_cart(self, user_id: str) -> None:
        """
        Go:
            if _, err := pb.NewCartServiceClient(cs.cartSvcConn).
                EmptyCart(ctx, &pb.EmptyCartRequest{UserId: userID}); err != nil {
                return fmt.Errorf("failed to empty user cart during checkout: %+v", err)
            }
        """
        try:
            await self._cart.EmptyCart(
                demo_pb2.EmptyCartRequest(user_id=user_id)
            )
        except Exception as exc:
            raise RuntimeError(f"failed to empty user cart during checkout: {exc}") from exc

    # ── quoteShipping ─────────────────────────────────────────────────────────
    async def quote_shipping(
        self,
        address: demo_pb2.Address,
        items: list[demo_pb2.CartItem],
    ) -> demo_pb2.Money:
        """
        Go:
            shippingQuote, err := pb.NewShippingServiceClient(cs.shippingSvcConn).
                GetQuote(ctx, &pb.GetQuoteRequest{Address: address, Items: items})
            if err != nil { return nil, fmt.Errorf("failed to get shipping quote: %+v", err) }
            return shippingQuote.GetCostUsd(), nil
        """
        try:
            resp: demo_pb2.GetQuoteResponse = await self._shipping.GetQuote(
                demo_pb2.GetQuoteRequest(address=address, items=items)
            )
        except Exception as exc:
            raise RuntimeError(f"failed to get shipping quote: {exc}") from exc
        return resp.cost_usd

    # ── convertCurrency ───────────────────────────────────────────────────────
    async def convert_currency(
        self,
        from_money: demo_pb2.Money,
        to_currency: str,
    ) -> demo_pb2.Money:
        """
        Go:
            result, err := pb.NewCurrencyServiceClient(cs.currencySvcConn).
                Convert(ctx, &pb.CurrencyConversionRequest{From: from, ToCode: toCurrency})
            if err != nil { return nil, fmt.Errorf("failed to convert currency: %+v", err) }
            return result, err
        """
        if from_money.currency_code == to_currency:
            return from_money
        try:
            result: demo_pb2.Money = await self._currency.Convert(
                demo_pb2.CurrencyConversionRequest(
                    from_=from_money,
                    to_code=to_currency,
                )
            )
        except Exception as exc:
            raise RuntimeError(f"failed to convert currency: {exc}") from exc
        return result

    # ── prepOrderItems ────────────────────────────────────────────────────────
    async def prep_order_items(
        self,
        cart_items: list[demo_pb2.CartItem],
        user_currency: str,
    ) -> list[demo_pb2.OrderItem]:
        """
        Go:
            cl := pb.NewProductCatalogServiceClient(cs.productCatalogSvcConn)
            for i, item := range items {
                product, err := cl.GetProduct(ctx, &pb.GetProductRequest{Id: item.GetProductId()})
                if err != nil { return nil, fmt.Errorf("failed to get product #%q", ...) }
                price, err := cs.convertCurrency(ctx, product.GetPriceUsd(), userCurrency)
                if err != nil { return nil, fmt.Errorf("failed to convert price of %q to %s", ...) }
                out[i] = &pb.OrderItem{Item: item, Cost: price}
            }
        """
        order_items: list[demo_pb2.OrderItem] = []
        for item in cart_items:
            try:
                product: demo_pb2.GetProductResponse = await self._catalog.GetProduct(
                    demo_pb2.GetProductRequest(id=item.product_id)
                )
            except Exception as exc:
                raise RuntimeError(
                    f"failed to get product #{item.product_id!r}: {exc}"
                ) from exc

            try:
                convert_resp: demo_pb2.CurrencyConversionResponse = await self.convert_currency(
                    product.product.price_usd, user_currency
                )
                price = convert_resp.money
            except Exception as exc:
                raise RuntimeError(
                    f"failed to convert price of {item.product_id!r} to {user_currency}: {exc}"
                ) from exc

            order_items.append(demo_pb2.OrderItem(item=item, cost=price))

        return order_items

    # ── prepareOrderItemsAndShippingQuoteFromCart ─────────────────────────────
    async def prepare_order_items_and_shipping_quote(
        self,
        user_id: str,
        user_currency: str,
        address: demo_pb2.Address,
    ) -> OrderPrep:
        """
        Go:
            cartItems, err := cs.getUserCart(ctx, userID)
            if err != nil { return out, fmt.Errorf("cart failure: %+v", err) }

            orderItems, err := cs.prepOrderItems(ctx, cartItems, userCurrency)
            if err != nil { return out, fmt.Errorf("failed to prepare order: %+v", err) }

            shippingUSD, err := cs.quoteShipping(ctx, address, cartItems)
            if err != nil { return out, fmt.Errorf("shipping quote failure: %+v", err) }

            shippingPrice, err := cs.convertCurrency(ctx, shippingUSD, userCurrency)
            if err != nil { return out, fmt.Errorf("failed to convert shipping cost to currency: %+v", err) }

            out.shippingCostLocalized = shippingPrice
            out.cartItems             = cartItems
            out.orderItems            = orderItems
        """
        out = OrderPrep()

        # Step 1 – getUserCart
        try:
            cart_items = await self.get_user_cart(user_id)
        except Exception as exc:
            raise RuntimeError(f"cart failure: {exc}") from exc
        out.cart_items = cart_items
        logger.info("fetched cart | user_id=%s items=%d", user_id, len(cart_items))

        # Step 2 – prepOrderItems
        try:
            order_items = await self.prep_order_items(cart_items, user_currency)
        except Exception as exc:
            raise RuntimeError(f"failed to prepare order: {exc}") from exc
        out.order_items = order_items

        # Step 3 – quoteShipping (always in USD from ShippingService)
        try:
            shipping_usd: demo_pb2.Money = await self.quote_shipping(address, cart_items)
        except Exception as exc:
            raise RuntimeError(f"shipping quote failure: {exc}") from exc

        # Step 4 – convertCurrency (USD → user_currency)
        try:
            shipping_localized: demo_pb2.Money = await self.convert_currency(
                shipping_usd, user_currency
            )
        except Exception as exc:
            raise RuntimeError(f"failed to convert shipping cost to currency: {exc}") from exc

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
    ) -> str:
        """
        Go:
            paymentResp, err := pb.NewPaymentServiceClient(cs.paymentSvcConn).
                Charge(ctx, &pb.ChargeRequest{Amount: amount, CreditCard: paymentInfo})
            if err != nil { return "", fmt.Errorf("could not charge the card: %+v", err) }
            return paymentResp.GetTransactionId(), nil
        """
        try:
            resp: demo_pb2.ChargeResponse = await self._payment.Charge(
                demo_pb2.ChargeRequest(amount=amount, credit_card=credit_card)
            )
        except Exception as exc:
            raise RuntimeError(f"could not charge the card: {exc}") from exc
        return resp.transaction_id

    # ── shipOrder ─────────────────────────────────────────────────────────────
    async def ship_order(
        self,
        address: demo_pb2.Address,
        items: list[demo_pb2.CartItem],
    ) -> str:
        """
        Go:
            resp, err := pb.NewShippingServiceClient(cs.shippingSvcConn).
                ShipOrder(ctx, &pb.ShipOrderRequest{Address: address, Items: items})
            if err != nil { return "", fmt.Errorf("shipment failed: %+v", err) }
            return resp.GetTrackingId(), nil
        """
        try:
            resp: demo_pb2.ShipOrderResponse = await self._shipping.ShipOrder(
                demo_pb2.ShipOrderRequest(address=address, items=items)
            )
        except Exception as exc:
            raise RuntimeError(f"shipment failed: {exc}") from exc
        return resp.tracking_id

    # ── sendOrderConfirmation ─────────────────────────────────────────────────
    async def send_order_confirmation(
        self,
        email: str,
        order: demo_pb2.OrderResult,
    ) -> None:
        """
        Go:
            _, err := pb.NewEmailServiceClient(cs.emailSvcConn).
                SendOrderConfirmation(ctx, &pb.SendOrderConfirmationRequest{
                    Email: email, Order: order})
            return err

        Note: caller uses best-effort — a failure here only logs a warning.
        """
        await self._email.SendOrderConfirmation(
            demo_pb2.SendOrderConfirmationRequest(email=email, order=order)
        )

    # ── PlaceOrder ─────────────────────────────────────────────────────────────
    async def place_order(
        self,
        request: demo_pb2.PlaceOrderRequest,
    ) -> demo_pb2.PlaceOrderResponse:
        """
        Full orchestration — direct async port of Go PlaceOrder.

        Go flow (verbatim):
            1. log user_id + user_currency
            2. orderID, _ := uuid.NewUUID()
            3. prep, err := cs.prepareOrderItemsAndShippingQuoteFromCart(...)
               → abort with INTERNAL on error
            4. total := Money{userCurrency, 0, 0}
               total  = Must(Sum(total, *prep.shippingCostLocalized))
               for _, it := range prep.orderItems {
                   multPrice = MultiplySlow(*it.Cost, qty)
                   total     = Must(Sum(total, multPrice))
               }
            5. txID, err := cs.chargeCard(ctx, &total, req.CreditCard)
               → abort with INTERNAL on error
            6. log "payment went through (transaction_id: %s)"
            7. shippingTrackingID, err := cs.shipOrder(ctx, req.Address, prep.cartItems)
               → abort with UNAVAILABLE on error
            8. _ = cs.emptyUserCart(ctx, req.UserId)   ← error ignored
            9. orderResult := &pb.OrderResult{...}
           10. if err := cs.sendOrderConfirmation(...); err != nil { log.Warnf } else { log.Infof }
           11. return &pb.PlaceOrderResponse{Order: orderResult}, nil
        """
        # Step 1 – Go: log.Infof("[PlaceOrder] user_id=%q user_currency=%q", ...)
        logger.info(
            "[PlaceOrder] user_id=%r user_currency=%r",
            request.user_id, request.user_currency,
        )

        # Step 2 – Go: orderID, err := uuid.NewUUID()
        order_id = str(uuid.uuid4())
        collection = await get_order_collection()
        
        # generate new order record with status pending in db
        order_record = {
            "_id": order_id,
            "user_id": request.user_id,
            "user_currency": request.user_currency,
            "status": "pending",
            "created_at": datetime.utcnow(),
        }
        await collection.insert_one(order_record)

        # Step 3 – Go: prep, err := cs.prepareOrderItemsAndShippingQuoteFromCart(...)
        prep = await self.prepare_order_items_and_shipping_quote(
            user_id=request.user_id,
            user_currency=request.user_currency,
            address=request.address,
        )

        # Step 4 – Go: total := pb.Money{CurrencyCode: req.UserCurrency, Units: 0, Nanos: 0}
        #              total  = money.Must(money.Sum(total, *prep.shippingCostLocalized))
        #              for _, it := range prep.orderItems {
        #                  multPrice := money.MultiplySlow(*it.Cost, uint32(it.GetItem().GetQuantity()))
        #                  total      = money.Must(money.Sum(total, multPrice))
        #              }
        total = zero_money(request.user_currency)

        shipping_py = proto_to_money(prep.shipping_cost_localized)
        total = money_must(money_sum(total, shipping_py))

        for order_item in prep.order_items:
            cost_py   = proto_to_money(order_item.cost)
            mult_price = money_multiply_slow(cost_py, order_item.item.quantity)
            total      = money_must(money_sum(total, mult_price))

        total_proto = demo_pb2.Money(
            currency_code=total.currency_code,
            units=total.units,
            nanos=total.nanos,
        )
        logger.info(
            "[PlaceOrder] order total | %s %d.%02d",
            total_proto.currency_code,
            total_proto.units,
            total_proto.nanos // 10_000_000,
        )

        # Step 5 – Go: txID, err := cs.chargeCard(ctx, &total, req.CreditCard)
        try:
            transaction_id = await self.charge_card(total_proto, request.credit_card)
            # update order record with transaction_id and status paid in db
            await collection.update_one(
                {"_id": order_id},
                {"$set": {"transaction_id": transaction_id, "status": "paid"}}
            )
        except Exception as exc:
            # update order record with status payment_failed in db
            await collection.update_one(
                {"_id": order_id},
                {"$set": {"status": "payment_failed"}}
            )
            raise RuntimeError(f"could not charge the card: {exc}") from exc

        # Step 6 – Go: log.Infof("payment went through (transaction_id: %s)", txID)
        logger.info("payment went through (transaction_id: %s)", transaction_id)

        # Step 7 – Go: shippingTrackingID, err := cs.shipOrder(ctx, req.Address, prep.cartItems)
        try:
            tracking_id = await self.ship_order(request.address, prep.cart_items)
            # update order record with tracking_id and status shipped in db
            await collection.update_one(
                {"_id": order_id},
                {"$set": {"tracking_id": tracking_id, "status": "shipped"}}
            )
        except Exception as exc:
            # update order record with status shipping_failed in db
            await collection.update_one(
                {"_id": order_id},
                {"$set": {"status": "shipping_failed"}}
            )
            raise RuntimeError(f"shipment failed: {exc}") from exc

        # Step 8 – Go: _ = cs.emptyUserCart(ctx, req.UserId)   (error silently ignored)
        try:
            await self.empty_user_cart(request.user_id)
        except Exception as exc:
            logger.warning(
                "failed to empty cart for user_id=%r (non-fatal): %s",
                request.user_id, exc,
            )

        # Step 9 – Go: orderResult := &pb.OrderResult{...}
        order_result = demo_pb2.OrderResult(
            order_id=order_id,
            shipping_tracking_id=tracking_id,
            shipping_cost=prep.shipping_cost_localized,
            shipping_address=request.address,
            items=prep.order_items,
        )

        # Step 10 – Go: if err := cs.sendOrderConfirmation(...); err != nil { log.Warnf } else { log.Infof }
        try:
            await self.send_order_confirmation(request.email, order_result)
            logger.info("order confirmation email sent to %r", request.email)
        except Exception as exc:
            logger.warning(
                "failed to send order confirmation to %r: %s", request.email, exc
            )
        # update order record with status completed in db
        await collection.update_one(
            {"_id": order_id},
            {"$set": {"status": "completed"}}
        )

        # Step 11 – Go: return &pb.PlaceOrderResponse{Order: orderResult}, nil
        return demo_pb2.PlaceOrderResponse(order=order_result)
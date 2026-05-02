"""
checkoutservice/client.py

Async gRPC client for CheckoutService.

The Go frontend calls PlaceOrder like this:
    resp, err := pb.NewCheckoutServiceClient(conn).PlaceOrder(ctx, &pb.PlaceOrderRequest{...})

This module wraps that call in a clean Python async client with:
  • Typed method signature with plain-dict input/output
  • CheckoutClientError for uniform error handling
  • Async-context-manager support for channel lifecycle
  • A standalone CLI for manual end-to-end testing

Usage
─────
Short-lived:

    async with CheckoutClient("checkoutservice:5050") as client:
        result = await client.place_order(
            user_id="user-123",
            user_currency="USD",
            address={
                "street_address": "1600 Amphitheatre Pkwy",
                "city": "Mountain View",
                "state": "CA",
                "country": "US",
                "zip_code": 94043,
            },
            email="test@example.com",
            credit_card_number="4111111111111111",
            credit_card_cvv=123,
            credit_card_expiration_year=2030,
            credit_card_expiration_month=1,
        )
        print(result["order_id"])

Long-lived (singleton in a frontend service):

    client = CheckoutClient()
    result = await client.place_order(...)

CLI:

    python -m checkoutservice.client \\
        user-123 USD "1 Main St" Springfield IL US 62701 test@example.com \\
        4111111111111111 123 2030 1
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

import grpc

from ..shared import demo_pb2
from ..shared import demo_pb2_grpc

logger = logging.getLogger("checkoutservice-client")

_DEFAULT_ADDR = os.getenv("CHECKOUT_SERVICE_ADDR", "localhost:5050")


# ── Error ──────────────────────────────────────────────────────────────────────
class CheckoutClientError(Exception):
    """Raised when a CheckoutService PlaceOrder RPC call fails."""

    def __init__(self, code: grpc.StatusCode, details: str) -> None:
        self.code    = code
        self.details = details
        super().__init__(f"[CheckoutService.PlaceOrder] {code.name}: {details}")


# ── Helpers ───────────────────────────────────────────────────────────────────
def _build_address(d: dict) -> demo_pb2.Address:
    return demo_pb2.Address(
        street_address=d.get("street_address", ""),
        city=d.get("city", ""),
        state=d.get("state", ""),
        country=d.get("country", ""),
        zip_code=int(d.get("zip_code", 0)),
    )


def _order_result_to_dict(o: demo_pb2.OrderResult) -> dict:
    return {
        "order_id":             o.order_id,
        "shipping_tracking_id": o.shipping_tracking_id,
        "shipping_cost": {
            "currency_code": o.shipping_cost.currency_code,
            "units":         o.shipping_cost.units,
            "nanos":         o.shipping_cost.nanos,
        },
        "shipping_address": {
            "street_address": o.shipping_address.street_address,
            "city":           o.shipping_address.city,
            "state":          o.shipping_address.state,
            "country":        o.shipping_address.country,
            "zip_code":       o.shipping_address.zip_code,
        },
        "items": [
            {
                "product_id": item.item.product_id,
                "quantity":   item.item.quantity,
                "cost": {
                    "currency_code": item.cost.currency_code,
                    "units":         item.cost.units,
                    "nanos":         item.cost.nanos,
                },
            }
            for item in o.items
        ],
    }


# ── Client class ──────────────────────────────────────────────────────────────
class CheckoutClient:
    """
    Async gRPC client for CheckoutService (port 5050).

    Wraps the PlaceOrder RPC and translates gRPC errors into CheckoutClientError.
    """

    def __init__(self, address: str | None = None) -> None:
        self._address = address or _DEFAULT_ADDR
        self._channel = grpc.aio.insecure_channel(self._address)
        self._stub    = demo_pb2_grpc.CheckoutServiceStub(self._channel)
        logger.info("CheckoutClient → %s", self._address)

    # ── place_order ───────────────────────────────────────────────────────────
    async def place_order(
        self,
        user_id: str,
        user_currency: str,
        address: dict,
        email: str,
        credit_card_number: str,
        credit_card_cvv: int,
        credit_card_expiration_year: int,
        credit_card_expiration_month: int,
    ) -> dict:
        """
        PlaceOrder RPC — orchestrates the full checkout flow via CheckoutService.

        Go frontend equivalent:
            pb.NewCheckoutServiceClient(conn).PlaceOrder(ctx, &pb.PlaceOrderRequest{
                UserId:       userID,
                UserCurrency: userCurrency,
                Address:      address,
                Email:        email,
                CreditCard:   creditCard,
            })

        Args:
            user_id:                     Authenticated user identifier.
            user_currency:               ISO 4217 code, e.g. "USD", "EUR".
            address:                     Delivery address dict with keys:
                                         street_address, city, state, country, zip_code.
            email:                       Email for order confirmation.
            credit_card_number:          PAN string (Luhn-valid test numbers work).
            credit_card_cvv:             3 or 4-digit CVV.
            credit_card_expiration_year: 4-digit year.
            credit_card_expiration_month: 1–12 month.

        Returns:
            dict with keys:
                order_id, shipping_tracking_id, shipping_cost (Money dict),
                shipping_address (Address dict), items (list of OrderItem dicts).

        Raises:
            CheckoutClientError:
                INTERNAL    → cart/catalog/currency/payment failure
                UNAVAILABLE → shipping failure
        """
        logger.info(
            "CheckoutClient.place_order | user_id=%s currency=%s email=%s",
            user_id, user_currency, email,
        )

        request = demo_pb2.PlaceOrderRequest(
            user_id=user_id,
            user_currency=user_currency,
            address=_build_address(address),
            email=email,
            credit_card=demo_pb2.CreditCardInfo(
                credit_card_number=credit_card_number,
                credit_card_cvv=int(credit_card_cvv),
                credit_card_expiration_year=int(credit_card_expiration_year),
                credit_card_expiration_month=int(credit_card_expiration_month),
            ),
        )

        try:
            response: demo_pb2.PlaceOrderResponse = await self._stub.PlaceOrder(request)
        except grpc.aio.AioRpcError as exc:
            logger.error(
                "PlaceOrder failed | code=%s | details=%s",
                exc.code(), exc.details(),
            )
            raise CheckoutClientError(exc.code(), exc.details()) from exc

        result = _order_result_to_dict(response.order)
        logger.info(
            "PlaceOrder success | order_id=%s tracking_id=%s",
            result["order_id"],
            result["shipping_tracking_id"],
        )
        return result

    # ── lifecycle ─────────────────────────────────────────────────────────────
    async def close(self) -> None:
        await self._channel.close()
        logger.info("CheckoutClient channel closed | address=%s", self._address)

    async def __aenter__(self) -> "CheckoutClient":
        return self

    async def __aexit__(self, *_) -> None:
        await self.close()


# ── Standalone CLI ────────────────────────────────────────────────────────────
async def _run_cli(
    address: str,
    user_id: str,
    user_currency: str,
    street: str,
    city: str,
    state: str,
    country: str,
    zip_code: int,
    email: str,
    card_number: str,
    cvv: int,
    exp_year: int,
    exp_month: int,
) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    print(f"\nCheckoutClient → {address}")
    print(f"  user_id      : {user_id}")
    print(f"  currency     : {user_currency}")
    print(f"  ship to      : {street}, {city}, {state} {zip_code}, {country}")
    print(f"  email        : {email}")
    print(f"  card ending  : ...{card_number[-4:]}")
    print()

    addr = {
        "street_address": street,
        "city":     city,
        "state":    state,
        "country":  country,
        "zip_code": zip_code,
    }

    async with CheckoutClient(address) as client:
        try:
            result = await client.place_order(
                user_id=user_id,
                user_currency=user_currency,
                address=addr,
                email=email,
                credit_card_number=card_number,
                credit_card_cvv=cvv,
                credit_card_expiration_year=exp_year,
                credit_card_expiration_month=exp_month,
            )
        except CheckoutClientError as exc:
            print(f"✗ PlaceOrder failed [{exc.code.name}]: {exc.details}")
            sys.exit(1)

    cost  = result["shipping_cost"]
    cents = cost["nanos"] // 10_000_000
    print(f"✓ Order placed successfully!")
    print(f"  order_id     : {result['order_id']}")
    print(f"  tracking_id  : {result['shipping_tracking_id']}")
    print(f"  shipping cost: {cost['currency_code']} {cost['units']}.{cents:02d}")
    print(f"  items        : {len(result['items'])}")


if __name__ == "__main__":
    """
    Usage:
        python -m checkoutservice.client \\
            <user_id> <currency> <street> <city> <state> <country> <zip> <email> \\
            <card_number> <cvv> <exp_year> <exp_month>

    Example (Visa test number):
        python -m checkoutservice.client \\
            user-abc USD "1600 Amphitheatre Pkwy" "Mountain View" CA US 94043 \\
            test@example.com 4111111111111111 123 2030 1

    Override address:
        CHECKOUT_SERVICE_ADDR=checkoutservice:5050 python -m checkoutservice.client ...
    """
    if len(sys.argv) != 13:
        print(
            "Usage: python -m checkoutservice.client "
            "<user_id> <currency> <street> <city> <state> <country> <zip> <email> "
            "<card_number> <cvv> <exp_year> <exp_month>"
        )
        sys.exit(1)

    (_, _uid, _cur, _street, _city, _state, _country,
     _zip, _email, _card, _cvv, _year, _month) = sys.argv

    asyncio.run(_run_cli(
        address      = os.getenv("CHECKOUT_SERVICE_ADDR", "localhost:5050"),
        user_id      = _uid,
        user_currency= _cur,
        street       = _street,
        city         = _city,
        state        = _state,
        country      = _country,
        zip_code     = int(_zip),
        email        = _email,
        card_number  = _card,
        cvv          = int(_cvv),
        exp_year     = int(_year),
        exp_month    = int(_month),
    ))
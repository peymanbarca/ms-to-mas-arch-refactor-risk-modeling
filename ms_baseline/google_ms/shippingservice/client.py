"""
shippingservice/client.py

Async gRPC client for ShippingService.

The original repo only exposed the Go server; the Go checkoutservice called it
directly using a generated stub:

    Go (checkoutservice/main.go):
        func (cs *checkoutService) shipOrder(...) (string, error) {
            resp, err := pb.NewShippingServiceClient(cs.shippingSvcConn).ShipOrder(ctx,
                &pb.ShipOrderRequest{Address: addr, Items: items})
            ...
            return resp.GetTrackingId(), nil
        }

        func (cs *checkoutService) getShippingQuote(...) (*pb.Money, error) {
            resp, err := pb.NewShippingServiceClient(cs.shippingSvcConn).GetQuote(ctx,
                &pb.GetQuoteRequest{Address: addr, Items: items})
            ...
            return resp.GetCostUsd(), nil
        }

This module wraps those stub calls in a clean Python async client with:
  • Typed method signatures with plain dicts in/out (no proto objects required)
  • ShippingClientError for uniform error handling
  • Async-context-manager support for proper channel lifecycle

Usage
─────
Short-lived (script / test):

    async with ShippingClient("shippingservice:50051") as client:
        quote = await client.get_quote(
            address={"street_address": "1600 Amphitheatre Pkwy",
                     "city": "Mountain View", "state": "CA",
                     "country": "US", "zip_code": 94043},
            items=[{"product_id": "OLJCESPC7Z", "quantity": 2}],
        )
        print(quote)   # {"currency_code": "USD", "units": 8, "nanos": 990000000}

        tracking = await client.ship_order(address=..., items=...)
        print(tracking)  # "FE-12345678-UX"

Long-lived (checkout service):

    client = ShippingClient()
    quote  = await client.get_quote(address, items)
    tid    = await client.ship_order(address, items)

Standalone CLI:

    python -m shippingservice.client quote  "1 Main St" "Springfield" "IL" "US" 62701 2
    python -m shippingservice.client ship   "1 Main St" "Springfield" "IL" "US" 62701 2
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

import grpc

from ..shared import demo_pb2
from ..shared import demo_pb2_grpc

logger = logging.getLogger("shippingservice-client")

_DEFAULT_ADDR = os.getenv("SHIPPING_SERVICE_ADDR", "localhost:5051")


# ── Error type ────────────────────────────────────────────────────────────────

class ShippingClientError(Exception):
    """Raised when a ShippingService RPC call fails."""

    def __init__(self, rpc: str, code: grpc.StatusCode, details: str) -> None:
        self.rpc     = rpc
        self.code    = code
        self.details = details
        super().__init__(f"[ShippingService.{rpc}] {code.name}: {details}")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _build_address(d: dict) -> demo_pb2.Address:
    return demo_pb2.Address(
        street_address=d.get("street_address", ""),
        city=d.get("city", ""),
        state=d.get("state", ""),
        country=d.get("country", ""),
        zip_code=int(d.get("zip_code", 0)),
    )


def _build_items(items: list[dict]) -> list[demo_pb2.CartItem]:
    return [
        demo_pb2.CartItem(
            product_id=i["product_id"],
            quantity=int(i.get("quantity", 1)),
        )
        for i in items
    ]


def _money_to_dict(m: demo_pb2.Money) -> dict:
    return {
        "currency_code": m.currency_code,
        "units":         m.units,
        "nanos":         m.nanos,
    }


# ── Client class ──────────────────────────────────────────────────────────────

class ShippingClient:
    """
    Async gRPC client for ShippingService (port 50051).

    Mirrors the Go checkoutservice caller pattern:
      • get_quote()  → Go: shippingServiceClient.GetQuote()
      • ship_order() → Go: shippingServiceClient.ShipOrder()
    """

    def __init__(self, address: str | None = None) -> None:
        self._address = address or _DEFAULT_ADDR
        self._channel = grpc.aio.insecure_channel(self._address)
        self._stub    = demo_pb2_grpc.ShippingServiceStub(self._channel)
        logger.info("ShippingClient → %s", self._address)

    # ── GetQuote ─────────────────────────────────────────────────────────────

    async def get_quote(
        self,
        address: dict,
        items: list[dict],
    ) -> dict:
        """
        GetQuote RPC – estimate shipping cost for a list of cart items.

        Go equivalent (checkoutservice):
            pb.NewShippingServiceClient(conn).GetQuote(ctx,
                &pb.GetQuoteRequest{Address: addr, Items: items})

        Args:
            address: {
                "street_address": str,
                "city": str,
                "state": str,
                "country": str,
                "zip_code": int,
            }
            items: [{"product_id": str, "quantity": int}, ...]

        Returns:
            Money dict: {"currency_code": "USD", "units": int, "nanos": int}
            e.g. {"currency_code": "USD", "units": 8, "nanos": 990000000} = $8.99

        Raises:
            ShippingClientError: on any transport or server error.
        """
        total_items = sum(i.get("quantity", 1) for i in items)
        logger.info(
            "GetQuote | address=%s,%s | total_items=%d",
            address.get("city"), address.get("state"), total_items,
        )

        try:
            response: demo_pb2.GetQuoteResponse = await self._stub.GetQuote(
                demo_pb2.GetQuoteRequest(
                    address=_build_address(address),
                    items=_build_items(items),
                )
            )
        except grpc.aio.AioRpcError as exc:
            logger.error(
                "GetQuote failed | code=%s | details=%s",
                exc.code(), exc.details(),
            )
            raise ShippingClientError("GetQuote", exc.code(), exc.details()) from exc

        result = _money_to_dict(response.cost_usd)
        dollars = result["units"]
        cents   = result["nanos"] // 10_000_000
        logger.info("GetQuote → $%d.%02d USD", dollars, cents)
        return result

    # ── ShipOrder ────────────────────────────────────────────────────────────

    async def ship_order(
        self,
        address: dict,
        items: list[dict],
    ) -> str:
        """
        ShipOrder RPC – dispatch a shipment and return a tracking ID.

        Go equivalent (checkoutservice):
            pb.NewShippingServiceClient(conn).ShipOrder(ctx,
                &pb.ShipOrderRequest{Address: addr, Items: items})

        Args:
            address: Delivery address dict (same shape as get_quote).
            items:   Cart items to ship.

        Returns:
            tracking_id (str), e.g. "FE-12345678-UX"

        Raises:
            ShippingClientError: on any transport or server error.
        """
        logger.info(
            "ShipOrder | address=%s,%s,%s",
            address.get("street_address"),
            address.get("city"),
            address.get("state"),
        )

        try:
            response: demo_pb2.ShipOrderResponse = await self._stub.ShipOrder(
                demo_pb2.ShipOrderRequest(
                    address=_build_address(address),
                    items=_build_items(items),
                )
            )
        except grpc.aio.AioRpcError as exc:
            logger.error(
                "ShipOrder failed | code=%s | details=%s",
                exc.code(), exc.details(),
            )
            raise ShippingClientError("ShipOrder", exc.code(), exc.details()) from exc

        logger.info("ShipOrder → tracking_id=%s", response.tracking_id)
        return response.tracking_id

    # ── lifecycle ─────────────────────────────────────────────────────────────

    async def close(self) -> None:
        await self._channel.close()
        logger.info("ShippingClient channel closed | address=%s", self._address)

    async def __aenter__(self) -> "ShippingClient":
        return self

    async def __aexit__(self, *_) -> None:
        await self.close()


# ── Standalone CLI ────────────────────────────────────────────────────────────

async def _run_cli(
    command: str,
    address: str,
    street: str,
    city: str,
    state: str,
    country: str,
    zip_code: int,
    quantity: int,
) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    addr = {
        "street_address": street,
        "city": city,
        "state": state,
        "country": country,
        "zip_code": zip_code,
    }
    items = [{"product_id": "DEMO-PRODUCT", "quantity": quantity}]

    async with ShippingClient(address) as client:
        if command == "quote":
            try:
                quote = await client.get_quote(addr, items)
                cents = quote["nanos"] // 10_000_000
                print(f"\nShipping quote: ${quote['units']}.{cents:02d} {quote['currency_code']}")
            except ShippingClientError as exc:
                print(f"ERROR: {exc}")
                sys.exit(1)

        elif command == "ship":
            try:
                tid = await client.ship_order(addr, items)
                print(f"\nOrder shipped! Tracking ID: {tid}")
            except ShippingClientError as exc:
                print(f"ERROR: {exc}")
                sys.exit(1)

        else:
            print(f"Unknown command: {command!r}. Use 'quote' or 'ship'.")
            sys.exit(1)


if __name__ == "__main__":
    """
    Usage:
        python -m shippingservice.client <command> <street> <city> <state> <country> <zip> <qty>

    Commands: quote | ship

    Examples:
        python -m shippingservice.client quote "1600 Amphitheatre Pkwy" "Mountain View" CA US 94043 3
        python -m shippingservice.client ship  "1600 Amphitheatre Pkwy" "Mountain View" CA US 94043 3
        SHIPPING_SERVICE_ADDR=shippingservice:50051 python -m shippingservice.client quote ...
    """
    if len(sys.argv) < 8:
        print("Usage: python -m shippingservice.client <quote|ship> "
              "<street> <city> <state> <country> <zip> <qty>")
        sys.exit(1)

    _, cmd, _street, _city, _state, _country, _zip, _qty = sys.argv
    _addr = os.getenv("SHIPPING_SERVICE_ADDR", "localhost:50051")

    asyncio.run(_run_cli(
        command  = cmd,
        address  = _addr,
        street   = _street,
        city     = _city,
        state    = _state,
        country  = _country,
        zip_code = int(_zip),
        quantity = int(_qty),
    ))
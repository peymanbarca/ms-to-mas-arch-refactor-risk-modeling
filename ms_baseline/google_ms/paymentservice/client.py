"""
paymentservice/client.py

Async gRPC client for PaymentService.

The original Node.js repo had no standalone client file; the Go checkoutservice
called PaymentService directly:

    Go (checkoutservice/main.go):
        func (cs *checkoutService) chargeCard(ctx context.Context,
            amount *pb.Money, paymentInfo *pb.CreditCardInfo) (string, error) {
            resp, err := pb.NewPaymentServiceClient(cs.paymentSvcConn).Charge(ctx,
                &pb.ChargeRequest{Amount: amount, CreditCard: paymentInfo})
            return resp.GetTransactionId(), err
        }

This module provides:
  1. PaymentClient  – async class for production use (e.g. checkoutservice).
  2. __main__ block – CLI test client, mirrors Node.js manual test patterns.

Usage
─────
Short-lived:

    async with PaymentClient("paymentservice:50051") as client:
        txn_id = await client.charge(
            amount={"currency_code": "USD", "units": 100, "nanos": 0},
            credit_card_number="4111111111111111",
            credit_card_cvv=123,
            credit_card_expiration_year=2030,
            credit_card_expiration_month=1,
        )
        print(txn_id)

Long-lived (checkout service singleton):

    client = PaymentClient()
    txn_id = await client.charge(amount, number, cvv, year, month)

CLI:

    python -m paymentservice.client 4111111111111111 123 2030 1 USD 99 0
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

import grpc

from ..shared import demo_pb2
from ..shared import demo_pb2_grpc

logger = logging.getLogger("paymentservice-client")

_DEFAULT_ADDR = os.getenv("PAYMENT_SERVICE_ADDR", "localhost:5052")


# ── Error type ────────────────────────────────────────────────────────────────

class PaymentClientError(Exception):
    """Raised when a PaymentService Charge RPC call fails."""

    def __init__(self, code: grpc.StatusCode, details: str) -> None:
        self.code    = code
        self.details = details
        super().__init__(f"[PaymentService.Charge] {code.name}: {details}")


# ── Client class ──────────────────────────────────────────────────────────────

class PaymentClient:
    """
    Async gRPC client for PaymentService (port 50051).

    Exposes a single charge() method that wraps the Charge RPC and raises
    PaymentClientError on any gRPC transport or server-side failure.
    """

    def __init__(self, address: str | None = None) -> None:
        self._address = address or _DEFAULT_ADDR
        self._channel = grpc.aio.insecure_channel(self._address)
        self._stub    = demo_pb2_grpc.PaymentServiceStub(self._channel)
        logger.info("PaymentClient → %s", self._address)

    # ── Charge ────────────────────────────────────────────────────────────────

    async def charge(
        self,
        amount: dict,
        credit_card_number: str,
        credit_card_cvv: int,
        credit_card_expiration_year: int,
        credit_card_expiration_month: int,
    ) -> str:
        """
        Charge RPC – validate and charge a credit card, return transaction ID.

        Go equivalent (checkoutservice):
            pb.NewPaymentServiceClient(conn).Charge(ctx, &pb.ChargeRequest{
                Amount:     amount,
                CreditCard: creditCard,
            })

        Args:
            amount: {
                "currency_code": str,   e.g. "USD"
                "units":         int,   e.g. 100
                "nanos":         int,   e.g. 0
            }
            credit_card_number:          PAN string, spaces/dashes allowed.
            credit_card_cvv:             3 or 4-digit CVV as integer.
            credit_card_expiration_year: 4-digit expiry year.
            credit_card_expiration_month: 1–12 expiry month.

        Returns:
            transaction_id (str) – UUID v4 string on success.

        Raises:
            PaymentClientError:
                • code=INVALID_ARGUMENT  → card validation failed (expired, bad Luhn, etc.)
                • code=UNAVAILABLE/etc.  → transport failure
        """
        logger.info(
            "PaymentClient.charge | currency=%s units=%d card_ending=%s",
            amount.get("currency_code", "?"),
            amount.get("units", 0),
            str(credit_card_number).replace(" ", "")[-4:],
        )

        request = demo_pb2.ChargeRequest(
            amount=demo_pb2.Money(
                currency_code=amount.get("currency_code", "USD"),
                units=int(amount.get("units", 0)),
                nanos=int(amount.get("nanos", 0)),
            ),
            credit_card=demo_pb2.CreditCardInfo(
                credit_card_number=credit_card_number,
                credit_card_cvv=int(credit_card_cvv),
                credit_card_expiration_year=int(credit_card_expiration_year),
                credit_card_expiration_month=int(credit_card_expiration_month),
            ),
        )

        try:
            response: demo_pb2.ChargeResponse = await self._stub.Charge(request)
        except grpc.aio.AioRpcError as exc:
            logger.error(
                "Charge RPC failed | code=%s | details=%s",
                exc.code(), exc.details(),
            )
            raise PaymentClientError(exc.code(), exc.details()) from exc

        logger.info("Charge success | transaction_id=%s, llm_metrics =%s", response.transaction_id, response.llm_metrics)
        return response.transaction_id

    # ── lifecycle ─────────────────────────────────────────────────────────────

    async def close(self) -> None:
        await self._channel.close()
        logger.info("PaymentClient channel closed | address=%s", self._address)

    async def __aenter__(self) -> "PaymentClient":
        return self

    async def __aexit__(self, *_) -> None:
        await self.close()


# ── Standalone CLI ────────────────────────────────────────────────────────────

async def _run_cli(
    address: str,
    card_number: str,
    cvv: int,
    exp_year: int,
    exp_month: int,
    currency: str,
    units: int,
    nanos: int,
) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    amount = {"currency_code": currency, "units": units, "nanos": nanos}
    cents = nanos // 10_000_000

    print(f"\nPaymentClient → {address}")
    print(f"  Card   : {'*' * (len(card_number.replace(' ', '')) - 4)}{card_number.replace(' ', '')[-4:]}")
    print(f"  CVV    : {'*' * len(str(cvv))}")
    print(f"  Expiry : {exp_month:02d}/{exp_year}")
    print(f"  Amount : {currency} {units}.{cents:02d}\n")

    async with PaymentClient(address) as client:
        try:
            txn_id = await client.charge(
                amount=amount,
                credit_card_number=card_number,
                credit_card_cvv=cvv,
                credit_card_expiration_year=exp_year,
                credit_card_expiration_month=exp_month,
            )
            print(f"✓ Charge successful! Transaction ID: {txn_id}")
        except PaymentClientError as exc:
            print(f"✗ Charge failed [{exc.code.name}]: {exc.details}")
            sys.exit(1)


if __name__ == "__main__":
    """
    Usage:
        python -m paymentservice.client <card_number> <cvv> <exp_year> <exp_month> \\
                                         <currency> <units> <nanos>

    Examples (Luhn-valid test numbers):
        # Visa – success
        python -m paymentservice.client 4111111111111111 123 2030 1 USD 100 0

        # Mastercard – success
        python -m paymentservice.client 5500005555555559 123 2030 6 EUR 25 990000000

        # Expired card – INVALID_ARGUMENT
        python -m paymentservice.client 4111111111111111 123 2020 1 USD 50 0

        # Bad Luhn – INVALID_ARGUMENT
        python -m paymentservice.client 4111111111111112 123 2030 1 USD 10 0

    Env var override:
        PAYMENT_SERVICE_ADDR=paymentservice:50051 python -m paymentservice.client ...
    """
    if len(sys.argv) != 8:
        print("Usage: python -m paymentservice.client "
              "<card_number> <cvv> <exp_year> <exp_month> <currency> <units> <nanos>")
        sys.exit(1)

    _, _card, _cvv, _year, _month, _curr, _units, _nanos = sys.argv
    _addr = os.getenv("PAYMENT_SERVICE_ADDR", "localhost:5052")

    asyncio.run(_run_cli(
        address    = _addr,
        card_number= _card,
        cvv        = int(_cvv),
        exp_year   = int(_year),
        exp_month  = int(_month),
        currency   = _curr,
        units      = int(_units),
        nanos      = int(_nanos),
    ))
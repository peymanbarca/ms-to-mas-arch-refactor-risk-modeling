"""
notification_service.py — RetailBen
────────────────────────────────────
Single-file FastAPI service.  Port 8009.

Exposes one REST endpoint:
    POST /notify

Called by the Order Service (or any other service) whenever an email
needs to be sent.  Email delivery is handled by MockEmailProvider, which
simulates an external SMTP/SendGrid-style API.

To wire up a real provider later, replace MockEmailProvider.deliver()
with your SDK call (e.g. boto3 SES, sendgrid.SendGridAPIClient, smtplib).

Run:
    pip install fastapi uvicorn
    uvicorn notification_service:app --port 8009 --reload
"""

import asyncio
import logging
import random
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr

logger = logging.getLogger("notification")
logging.basicConfig(
    filename='./logs/notification_service.log',
    level=logging.INFO,  # Log all messages with severity DEBUG or higher
    format='%(asctime)s - %(levelname)s - %(message)s'  # Define the message format
)


# ─────────────────────────────────────────────────────────────────────────────
# In-memory log (swap for a MongoDB collection in production)
# ─────────────────────────────────────────────────────────────────────────────

_notification_log: list[dict] = []


# ─────────────────────────────────────────────────────────────────────────────
# Mock external email provider
# ─────────────────────────────────────────────────────────────────────────────

class MockEmailProvider:
    """
    Pretends to be an external email delivery API (SendGrid, SES, Mailgun …).

    deliver() always returns a structured result so the caller can log the
    outcome without needing to catch exceptions.

    Simulates:
      - 20–80 ms network latency
      - 5 % transient failure rate (useful for testing circuit-breaker logic)
    """

    FAILURE_RATE = 0

    async def deliver(self, *, to: str, subject: str, body: str) -> dict:
        # Simulate provider round-trip latency
        await asyncio.sleep(random.uniform(0.02, 0.08))

        if random.random() < self.FAILURE_RATE:
            logger.warning("[MockEmailProvider] ✗ delivery failed  to=%s", to)
            return {"success": False, "message_id": None,
                    "error": "Simulated transient provider failure"}

        message_id = f"mock-{uuid.uuid4().hex[:10]}@retailben.local"
        logger.info(
            "\n┌── MOCK EMAIL ─────────────────────────────────┐"
            "\n│  To      : %s"
            "\n│  Subject : %s"
            "\n│  Msg-ID  : %s"
            "\n├───────────────────────────────────────────────┤"
            "\n%s"
            "\n└───────────────────────────────────────────────┘",
            to, subject, message_id,
            "\n".join(f"│  {line}" for line in body.splitlines()),
        )
        return {"success": True, "message_id": message_id, "error": None}


_provider = MockEmailProvider()


# ─────────────────────────────────────────────────────────────────────────────
# Schemas
# ─────────────────────────────────────────────────────────────────────────────

class NotificationType(Enum):
    ORDER_PLACED     = "order.placed"
    ORDER_CONFIRMED  = "order.confirmed"
    PAYMENT_SUCCESS  = "payment.success"
    PAYMENT_FAILED   = "payment.failed"
    ORDER_SHIPPED    = "order.shipped"
    ORDER_DELIVERED  = "order.delivered"
    PROMOTION        = "promotion"
    CUSTOM           = "custom"


class NotifyRequest(BaseModel):
    to:                EmailStr
    recipient_name:    str = "Customer"
    notification_type: NotificationType
    # Caller passes whatever context fields are relevant
    # (order_id, amount, tracking_number, promo_title, …)
    context:           dict[str, Any] = {}


class NotifyResponse(BaseModel):
    notification_id:   str
    to:                str
    notification_type: str
    subject:           str
    status:            str          # "sent" | "failed"
    message_id:        str | None
    error:             str | None
    sent_at:           datetime


# ─────────────────────────────────────────────────────────────────────────────
# Email content builders
# ─────────────────────────────────────────────────────────────────────────────

def _subject(ntype: str, ctx: dict) -> str:
    oid = ctx.get("order_id", "")
    mapping = {
        NotificationType.ORDER_PLACED:    f"Order #{oid} Received – Thank You!",
        NotificationType.ORDER_CONFIRMED: f"Order #{oid} Confirmed",
        NotificationType.PAYMENT_SUCCESS: f"Payment Confirmed for Order #{oid}",
        NotificationType.PAYMENT_FAILED:  f"Action Required: Payment Failed for Order #{oid}",
        NotificationType.ORDER_SHIPPED:   f"Your Order #{oid} Is On Its Way!",
        NotificationType.ORDER_DELIVERED: f"Order #{oid} Delivered",
        NotificationType.PROMOTION:       ctx.get("promo_title", "A Special Offer for You"),
        NotificationType.CUSTOM:          ctx.get("subject", "Message from RetailBen"),
    }
    return mapping.get(ntype, "RetailBen Notification")


def _body(ntype: str, name: str, ctx: dict) -> str:
    oid = ctx.get("order_id", "N/A")
    templates = {
        NotificationType.ORDER_PLACED: (
            f"Hi {name},\n\n"
            f"We received your order #{oid}.\n"
            f"Total: ${ctx.get('total', 'N/A')}\n"
            f"Items: {ctx.get('items', 'N/A')}\n\n"
            f"We'll send another email once it's confirmed.\n\nRetailBen Team"
        ),
        NotificationType.ORDER_CONFIRMED: (
            f"Hi {name},\n\n"
            f"Great news — order #{oid} is confirmed and being prepared.\n"
            f"Expected dispatch: {ctx.get('expected_dispatch', 'Soon')}\n\nRetailBen Team"
        ),
        NotificationType.PAYMENT_SUCCESS: (
            f"Hi {name},\n\n"
            f"We received your payment of ${ctx.get('amount', 'N/A')} "
            f"for order #{oid}.\n"
            f"Transaction ID: {ctx.get('transaction_id', 'N/A')}\n\nRetailBen Team"
        ),
        NotificationType.PAYMENT_FAILED: (
            f"Hi {name},\n\n"
            f"Your payment for order #{oid} could not be processed.\n"
            f"Reason: {ctx.get('reason', 'Unknown')}\n"
            f"Please update your payment method and try again.\n\nRetailBen Team"
        ),
        NotificationType.ORDER_SHIPPED: (
            f"Hi {name},\n\n"
            f"Order #{oid} has been handed to {ctx.get('carrier', 'our carrier')}.\n"
            f"Tracking number: {ctx.get('tracking_number', 'N/A')}\n\nRetailBen Team"
        ),
        NotificationType.ORDER_DELIVERED: (
            f"Hi {name},\n\n"
            f"Order #{oid} has been delivered. Enjoy!\n"
            f"If anything's wrong, just reply to this email.\n\nRetailBen Team"
        ),
        NotificationType.PROMOTION: (
            f"Hi {name},\n\n"
            f"{ctx.get('promo_title', 'Special Offer')}!\n\n"
            f"{ctx.get('promo_description', '')}\n"
            f"Promo code : {ctx.get('promo_code', 'N/A')}\n"
            f"Discount   : {ctx.get('discount_percent', 0)}%\n"
            f"Valid until: {ctx.get('valid_until', 'N/A')}\n\nRetailBen Team"
        ),
        NotificationType.CUSTOM: (
            ctx.get("body", f"Hi {name},\n\nYou have a message from RetailBen.")
        ),
    }
    return templates.get(ntype, f"Hi {name},\n\nNew notification from RetailBen.")


# ─────────────────────────────────────────────────────────────────────────────
# App
# ─────────────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Notification Service",
    description="Sends transactional emails on behalf of RetailBen services.",
    version="1.0.0",
)

app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])


@app.get("/health")
async def health():
    return {"status": "ok", "service": "notification-service", "port": 8009}


@app.post("/notify", response_model=NotifyResponse, status_code=201)
async def notify(req: NotifyRequest):
    """
    Send an email notification.

    Called by the Order Service (or any peer service) via HTTP POST.

    Example — Order Service calling after order placement:

        import httpx
        httpx.post("http://notification-service:8009/notify", json={
            "to": "alice@example.com",
            "recipient_name": "Alice",
            "notification_type": "order.placed",
            "context": {
                "order_id": "ORD-001",
                "total": "49.99",
                "items": "2x Widget, 1x Gadget"
            }
        })
    """
    subject = _subject(req.notification_type, req.context)
    body    = _body(req.notification_type, req.recipient_name, req.context)

    result  = await _provider.deliver(to=req.to, subject=subject, body=body)

    record = {
        "notification_id":   str(uuid.uuid4()),
        "to":                req.to,
        "notification_type": req.notification_type,
        "subject":           subject,
        "status":            "sent" if result["success"] else "failed",
        "message_id":        result["message_id"],
        "error":             result["error"],
        "sent_at":           datetime.now(timezone.utc),
    }
    _notification_log.append(record)

    if not result["success"]:
        # Still return 201 with status="failed" so callers can log and
        # decide whether to retry; don't raise 500 (delivery is best-effort).
        logger.error("Notification delivery failed for %s — %s", req.to, result["error"])

    return NotifyResponse(**record)


@app.get("/notifications", response_model=list[NotifyResponse])
async def list_notifications(to: str | None = None):
    """Return the in-memory notification log, optionally filtered by recipient."""
    records = _notification_log if not to else [r for r in _notification_log if r["to"] == to]
    return [NotifyResponse(**r) for r in reversed(records)]
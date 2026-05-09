"""
subscription_service.py — RetailBen
─────────────────────────────────────
Single-file FastAPI service.  Port 8010.

Three REST endpoints:

    GET  /catalogue
        Returns the full promo catalogue, marking each code as active
        or expired so the frontend can show only subscribable promos.

    POST /subscriptions
        A user buys a subscription tied to a promotion code.
        The promo code must exist in PROMO_CATALOGUE and must not have
        expired.  Duplicate (user_id + promo_code) is rejected with 409.
        Persisted in MongoDB → user_subscriptions collection.

    GET  /subscriptions/{user_id}
        Returns all active, non-expired subscriptions for a user.
        Expiry is evaluated at query time — no cleanup job needed.

Run:
    pip install fastapi uvicorn motor pydantic[email]
    uvicorn subscription_service:app --port 8010 --reload
"""

import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Annotated

from fastapi import FastAPI, HTTPException, Path
from fastapi.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorCollection
from pydantic import BaseModel, EmailStr, Field
import logging

logger = logging.getLogger("subscription")
logging.basicConfig(
    filename='./logs/subscription_service.log',
    level=logging.INFO,  # Log all messages with severity DEBUG or higher
    format='%(asctime)s - %(levelname)s - %(message)s'  # Define the message format
)

# ─────────────────────────────────────────────────────────────────────────────
# Promotion catalogue
# ─────────────────────────────────────────────────────────────────────────────

PROMO_CATALOGUE: dict[str, dict] = {
    "SUMMER20": {
        "title":            "Summer Sale",
        "description":      "20% off all orders this summer.",
        "discount_percent": 20.0,
        "expires_at":       datetime(2026, 8, 31, 23, 59, 59, tzinfo=timezone.utc),
    },
    "WELCOME10": {
        "title":            "Welcome Discount",
        "description":      "10% off your first order.",
        "discount_percent": 10.0,
        "expires_at":       datetime(2026, 12, 31, 23, 59, 59, tzinfo=timezone.utc),
    },
    "FLASH50": {
        "title":            "Flash Deal",
        "description":      "50% off for the next 24 hours!",
        "discount_percent": 50.0,
        "expires_at":       datetime(2026, 6, 1, 23, 59, 59, tzinfo=timezone.utc),
    },
    "LOYALTY15": {
        "title":            "Loyalty Reward",
        "description":      "15% off as a thank-you to loyal customers.",
        "discount_percent": 15.0,
        "expires_at":       datetime(2026, 3, 31, 23, 59, 59, tzinfo=timezone.utc),
    },
}


# ─────────────────────────────────────────────────────────────────────────────
# MongoDB
# ─────────────────────────────────────────────────────────────────────────────

MONGO_URI = os.getenv("MONGO_URI", "mongodb://user:pass1@localhost:27017")
MONGO_DB  = os.getenv("MONGO_DB",  "retailben")

_mongo_client: AsyncIOMotorClient | None = None


def _col() -> AsyncIOMotorCollection:
    """Return the user_subscriptions collection. Called inside request handlers."""
    return _mongo_client[MONGO_DB]["user_subscriptions"]


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _mongo_client
    _mongo_client = AsyncIOMotorClient(MONGO_URI)
    await _mongo_client.admin.command("ping")   # fail fast if Mongo is unreachable

    # Compound unique index — prevents duplicate (user_id, promo_code) at DB level
    await _col().create_index(
        [("user_id", 1), ("promo_code", 1)], unique=True
    )
    await _col().create_index("user_id")        # fast lookups by user
    yield
    _mongo_client.close()


# ─────────────────────────────────────────────────────────────────────────────
# Schemas
# ─────────────────────────────────────────────────────────────────────────────

class PromoEntry(BaseModel):
    """One row in the catalogue response."""
    promo_code:        str
    title:             str
    description:       str
    discount_percent:  float
    expires_at:        datetime
    is_subscribable:   bool    # False once expired


class BuySubscriptionRequest(BaseModel):
    user_id:    str      = Field(..., description="Unique user/customer identifier")
    email:      EmailStr
    promo_code: str      = Field(..., description="Promotion code from the catalogue")


class SubscriptionRecord(BaseModel):
    subscription_id:   str
    user_id:           str
    email:             str
    promo_code:        str
    promo_title:       str
    promo_description: str
    discount_percent:  float
    subscribed_at:     datetime
    expires_at:        datetime
    is_active:         bool     # True only when current time < expires_at


class SubscriptionRecords(BaseModel):
    user_id: str
    subscriptions: list[SubscriptionRecord]

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _is_active(record: dict) -> bool:
    """A record is active if its expiry is still in the future."""
    exp = record["expires_at"]
    # Motor returns naive UTC datetimes from MongoDB; normalise if needed
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    return _now() < exp


def _doc_to_record(doc: dict) -> SubscriptionRecord:
    doc.pop("_id", None)
    return SubscriptionRecord(**{**doc, "is_active": _is_active(doc)})


# ─────────────────────────────────────────────────────────────────────────────
# App
# ─────────────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Subscription Service",
    description="Manages promo-code subscriptions for RetailBen customers.",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "subscription-service", "port": 8010}


# ─────────────────────────────────────────────────────────────────────────────
# GET /catalogue
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/catalogue", response_model=list[PromoEntry])
async def get_catalogue():
    """
    Return the full promotion catalogue.

    Each entry includes an `is_subscribable` flag so the frontend can
    grey-out expired codes without making a separate call.
    Sorted by discount (highest first).
    """
    now = _now()
    entries = [
        PromoEntry(
            promo_code=code,
            is_subscribable=now < promo["expires_at"],
            **promo,
        )
        for code, promo in PROMO_CATALOGUE.items()
    ]
    entries.sort(key=lambda e: e.discount_percent, reverse=True)
    return entries


# ─────────────────────────────────────────────────────────────────────────────
# POST /subscriptions  —  buy a subscription
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/subscriptions", response_model=SubscriptionRecord, status_code=201)
async def buy_subscription(req: BuySubscriptionRequest):
    """
    Subscribe a user to a promotion code.

    Validation rules (in order):
      1. The promo code must exist in the catalogue.
      2. The promo code must not have expired.
      3. The same user cannot subscribe to the same promo code twice (409).

    The record is persisted in MongoDB → `user_subscriptions`.
    The Order Service can call GET /subscriptions/{user_id} at checkout
    to retrieve the best active discount for that customer.
    """
    code  = req.promo_code.upper().strip()
    promo = PROMO_CATALOGUE.get(code)

    # 1. Code must exist
    if not promo:
        logger.warning(f"Subscription attempt with invalid promo code: '{code}'")
        raise HTTPException(
            status_code=404,
            detail=f"Promo code '{code}' not found. "
                   f"Valid codes: {', '.join(PROMO_CATALOGUE)}",
        )

    # 2. Code must not be expired
    if _now() >= promo["expires_at"]:
        logger.info(f"Subscription attempt with expired promo code: '{code}'")
        raise HTTPException(
            status_code=410,
            detail=f"Promo code '{code}' expired on "
                   f"{promo['expires_at'].strftime('%Y-%m-%d')}.",
        )

    # 3. No duplicate — also enforced by the DB unique index as a safety net
    existing = await _col().find_one({"user_id": req.user_id, "promo_code": code})
    if existing:
        logger.info(f"Duplicate subscription attempt: user '{req.user_id}' already subscribed to '{code}'")
        raise HTTPException(
            status_code=409,
            detail=f"User '{req.user_id}' is already subscribed to '{code}'.",
        )

    doc = {
        "subscription_id":   str(uuid.uuid4()),
        "user_id":           req.user_id,
        "email":             str(req.email),
        "promo_code":        code,
        "promo_title":       promo["title"],
        "promo_description": promo["description"],
        "discount_percent":  promo["discount_percent"],
        "subscribed_at":     _now(),
        "expires_at":        promo["expires_at"],
    }

    await _col().insert_one(doc)
    logger.info(f"Subscription created for user '{req.user_id}' with promo code '{code}'")
    return _doc_to_record(doc)


# ─────────────────────────────────────────────────────────────────────────────
# GET /subscriptions/{user_id}  —  fetch active subscriptions for a user
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/subscriptions/{user_id}", response_model=SubscriptionRecords)
async def get_subscriptions(
    user_id: Annotated[str, Path(description="The user/customer ID to query")]
):
    """
    Return all non-expired subscriptions for the given user.

    Expiry is evaluated at read time against the current UTC clock —
    no scheduled cleanup job is needed.
    Returns an empty list (not 404) when the user has no active subscriptions.
    Results are sorted highest-discount-first.

    Typical call from Order Service at checkout:

        import httpx
        subs = httpx.get(
            f"http://subscription-service:8010/subscriptions/{customer_id}"
        ).json()
        best_discount = max((s["discount_percent"] for s in subs), default=0)
    """
    cursor = _col().find({"user_id": user_id})
    docs   = await cursor.to_list(length=500)

    active = [_doc_to_record(d) for d in docs if _is_active(d)]
    active.sort(key=lambda s: s.discount_percent, reverse=True)
    logger.info(f"Fetched and sorted {len(active)} active subscriptions for user '{user_id}'")
    return SubscriptionRecords(user_id=user_id, subscriptions=active)
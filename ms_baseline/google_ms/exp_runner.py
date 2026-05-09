"""
simulation/run_trial.py

End-to-end microservices simulation script.

Workflow per trial (mirrors a real user session):
  1. SearchProducts          → ProductCatalogService  (find item by keyword)
  2. GetProduct              → ProductCatalogService  (fetch full details)
  3. ListRecommendations     → RecommendationService  (similar products)
  4. GetAds                  → AdService              (contextual ads)
  5. AddItem                 → CartService            (add to cart)
  6. PlaceOrder              → CheckoutService        (full checkout)

Each stage records:
  • latency_s        – wall-clock seconds for that single gRPC call
  • llm_metrics      – { total_input_tokens, total_output_tokens, total_llm_calls }
                       (zero for non-AI services; aggregated across all downstream
                        services for CheckoutService)

MongoDB cleanup:
  Before every run the script truncates the orders, payments, and shipments
  collections so each run starts from a clean state.

Usage:
  python -m simulation.run_trial
  DELAY=0.5 DROP_RATE=10 N_TRIALS=20 python -m simulation.run_trial
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import statistics
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

import grpc
from pymongo import MongoClient

# ── path setup ────────────────────────────────────────────────────────────────
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__)))

from .shared import demo_pb2
from .shared import demo_pb2_grpc

# ════════════════════════════════════════════════════════════════════════════
# Configuration (all overridable via env vars)
# ════════════════════════════════════════════════════════════════════════════

SEARCH_KEYWORD  = os.environ.get("SEARCH_KEYWORD",   "sunglass")
ITEM_QTY        = int(os.environ.get("ITEM_QTY",     "2"))
N_TRIALS        = int(os.environ.get("N_TRIALS",     "100"))
MAX_WORKERS     = int(os.environ.get("MAX_WORKERS",  str(max(1, N_TRIALS // 10))))
TOTAL_RUNS      = int(os.environ.get("TOTAL_RUNS",   "1"))
DELAY           = float(os.environ.get("DELAY",      "0"))
DROP_RATE       = int(os.environ.get("DROP_RATE",    "0"))

# ── gRPC service addresses ────────────────────────────────────────────────────
PRODUCT_CATALOG_ADDR   = os.environ.get("PRODUCT_CATALOG_SERVICE_ADDR",  "localhost:5055")
RECOMMENDATION_ADDR    = os.environ.get("RECOMMENDATION_SERVICE_ADDR",   "localhost:5058")
AD_SERVICE_ADDR        = os.environ.get("AD_SERVICE_ADDR",               "localhost:5057")
CART_SERVICE_ADDR      = os.environ.get("CART_SERVICE_ADDR",             "localhost:5054")
CHECKOUT_SERVICE_ADDR  = os.environ.get("CHECKOUT_SERVICE_ADDR",         "localhost:5050")
SHIPPING_SERVICE_ADDR  = os.environ.get("SHIPPING_SERVICE_ADDR",         "localhost:5051")

# ── MongoDB ───────────────────────────────────────────────────────────────────
MONGO_URL = os.environ.get("MONGO_URL",  "mongodb://localhost:27017/")
DB_NAME   = os.environ.get("DB_NAME",   "google_ms")

# ── Log files ─────────────────────────────────────────────────────────────────
LOG_FILES = [
    os.path.dirname(__file__) + "/logs/adservice.log",
    os.path.dirname(__file__) + "/logs/cartservice.log",
    os.path.dirname(__file__) + "/logs/checkoutservice.log",
    os.path.dirname(__file__) + "/logs/currencyservice.log",
    os.path.dirname(__file__) + "/logs/emailservice.log",
    os.path.dirname(__file__) + "/logs/paymentservice.log",
    os.path.dirname(__file__) + "/logs/productcatalogservice.log",
    os.path.dirname(__file__) + "/logs/recommendationservice.log",
    os.path.dirname(__file__) + "/logs/shippingservice.log",
]

# ── Results output ────────────────────────────────────────────────────────────
os.makedirs(os.path.dirname(__file__) + "/results", exist_ok=True)
os.makedirs(os.path.dirname(__file__) + "/logs",    exist_ok=True)

# ── Test user / checkout data ─────────────────────────────────────────────────
TEST_ADDRESS = demo_pb2.Address(
    street_address="1600 Amphitheatre Pkwy",
    city="Mountain View",
    state="CA",
    country="US",
    zip_code=94043,
)
TEST_CREDIT_CARD = demo_pb2.CreditCardInfo(
    credit_card_number="4111111111111111",   # Luhn-valid Visa test number
    credit_card_cvv=123,
    credit_card_expiration_year=2030,
    credit_card_expiration_month=1,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("simulation")


# ════════════════════════════════════════════════════════════════════════════
# Data types
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class LLMMetrics:
    total_input_tokens:  int = 0
    total_output_tokens: int = 0
    total_llm_calls:     int = 0

    @classmethod
    def from_proto(cls, m) -> "LLMMetrics":
        """Build from a proto LLMMetrics message (or None)."""
        if m is None:
            return cls()
        return cls(
            total_input_tokens=getattr(m, "total_input_tokens",  0),
            total_output_tokens=getattr(m, "total_output_tokens", 0),
            total_llm_calls=getattr(m, "total_llm_calls",     0),
        )

    def __add__(self, other: "LLMMetrics") -> "LLMMetrics":
        return LLMMetrics(
            total_input_tokens=self.total_input_tokens  + other.total_input_tokens if other.total_input_tokens > 0 else self.total_input_tokens,
            total_output_tokens=self.total_output_tokens + other.total_output_tokens if other.total_output_tokens > 0 else self.total_output_tokens,
            total_llm_calls=self.total_llm_calls     + other.total_llm_calls if other.total_llm_calls > 0 else self.total_llm_calls,
        )


@dataclass
class StageResult:
    stage:       str
    status:      str            # "ok" | "error"
    latency_s:   float = 0.0
    llm_metrics: LLMMetrics = field(default_factory=LLMMetrics)
    detail:      Dict[str, Any] = field(default_factory=dict)
    error:       Optional[str] = None


@dataclass
class TrialResult:
    trial:           int
    status:          str           # "ok" | "error"
    elapsed_s:       float = 0.0
    stages:          List[StageResult] = field(default_factory=list)
    total_llm:       LLMMetrics = field(default_factory=LLMMetrics)
    error:           Optional[str] = None

    # convenience computed latencies
    @property
    def search_latency(self) -> float:
        for s in self.stages:
            if s.stage == "search_products":
                return s.latency_s
        return 0.0

    @property
    def checkout_latency(self) -> float:
        for s in self.stages:
            if s.stage == "place_order":
                return s.latency_s
        return 0.0


# ════════════════════════════════════════════════════════════════════════════
# gRPC channel helpers
# ════════════════════════════════════════════════════════════════════════════

def _channel(addr: str) -> grpc.Channel:
    """Return an insecure synchronous gRPC channel (used inside threads)."""
    return grpc.insecure_channel(addr)


# ════════════════════════════════════════════════════════════════════════════
# MongoDB helpers
# ════════════════════════════════════════════════════════════════════════════

def real_db():
    client = MongoClient(MONGO_URL)
    db     = client[DB_NAME]
    return client, db


def clean_db_for_run():
    """
    Truncate all transactional collections before a run.
    This ensures each run starts from a clean state.
    """
    client, db = real_db()
    try:
        for collection_name in ("orders", "payment_transactions", "shipments"):
            result = db[collection_name].delete_many({})
            logger.info(
                "[DB cleanup] %s: deleted %d documents",
                collection_name,
                result.deleted_count,
            )
    finally:
        client.close()


def get_final_state(db) -> Dict[str, Any]:
    """
    Read the final DB state after all trials in a run have completed.
    Returns a dict of counts and a pass/fail verdict.
    """
    total_completed   = db.orders.count_documents({"status": "completed"})
    total_pending     = db.orders.count_documents({"status": "pending"})
    total_paid        = db.orders.count_documents({"status": "paid"})
    total_shipped     = db.orders.count_documents({"status": "shipped"})
    total_pay_success = db.payment_transactions.count_documents({"status": "success"})
    total_shipments   = db.shipments.count_documents({"status": "shipped"})

    final_state  = "SUCCESS"
    qa_failures  = 0.0

    if total_pending > 0:
        qa_failures += total_pending
        final_state  = "FAIL"
    if total_pay_success != total_completed:
        qa_failures += abs(total_completed - total_pay_success)
        final_state  = "FAIL"
    if total_shipments != total_completed:
        qa_failures += math.fabs(total_completed - total_shipments)
        final_state  = "FAIL"

    return {
        "total_completed_orders":  total_completed,
        "total_pending_orders":    total_pending,
        "total_paid_orders":       total_paid,
        "total_shipped_orders":    total_shipped,
        "total_success_payments":  total_pay_success,
        "total_shipment_bookings": total_shipments,
        "final_ec_state":          final_state,
        "qa_failure_count":        qa_failures,
    }


# ════════════════════════════════════════════════════════════════════════════
# Individual stage helpers  (all synchronous — run inside ThreadPoolExecutor)
# ════════════════════════════════════════════════════════════════════════════

def _timed(fn) -> tuple[Any, float]:
    """
    Call fn() and return (result, elapsed_seconds).
    Re-raises any exception after recording elapsed time.
    """
    t0 = time.perf_counter()
    result = fn()
    return result, time.perf_counter() - t0


# ── Stage 1: Search products ──────────────────────────────────────────────────

def stage_search_products(user_id: str) -> StageResult:
    """
    SearchProducts(query=SEARCH_KEYWORD) → ProductCatalogService.
    Returns first matching product.
    """
    with _channel(PRODUCT_CATALOG_ADDR) as ch:
        stub = demo_pb2_grpc.ProductCatalogServiceStub(ch)

        def _call():
            return stub.SearchProducts(
                demo_pb2.SearchProductsRequest(query=SEARCH_KEYWORD)
            )

        try:
            resp, latency = _timed(_call)
            products = list(resp.results)
            if not products:
                return StageResult(
                    stage="search_products", status="error",
                    latency_s=latency,
                    error=f"No products found for query={SEARCH_KEYWORD!r}",
                )
            first = products[0]
            logger.info(
                "[trial] search_products | user=%s found=%d first=%s latency=%.3fs",
                user_id, len(products), first.id, latency,
            )
            return StageResult(
                stage="search_products", status="ok",
                latency_s=latency,
                llm_metrics=LLMMetrics.from_proto(getattr(resp, "llm_metrics", None)),
                detail={
                    "product_id":   first.id,
                    "product_name": first.name,
                    "result_count": len(products),
                    "categories":   list(first.categories),
                },
            )
        except grpc.RpcError as exc:
            return StageResult(
                stage="search_products", status="error",
                error=f"{exc.code()}: {exc.details()}",
            )


# ── Stage 2: Get product details ──────────────────────────────────────────────

def stage_get_product(user_id: str, product_id: str) -> StageResult:
    """
    GetProduct(id=product_id) → ProductCatalogService.
    Fetches full product detail (price, description, categories).
    """
    with _channel(PRODUCT_CATALOG_ADDR) as ch:
        stub = demo_pb2_grpc.ProductCatalogServiceStub(ch)

        def _call():
            return stub.GetProduct(demo_pb2.GetProductRequest(id=product_id))

        try:
            resp, latency = _timed(_call)

            # GetProductResponse { Product product; LLMMetrics llm_metrics }
            product = resp.product if hasattr(resp, "product") else resp

            logger.info(
                "[trial] get_product | user=%s product=%s latency=%.3fs",
                user_id, product.id, latency,
            )
            return StageResult(
                stage="get_product", status="ok",
                latency_s=latency,
                llm_metrics=LLMMetrics.from_proto(getattr(resp, "llm_metrics", None)),
                detail={
                    "product_id":   product.id,
                    "product_name": product.name,
                    "price_units":  product.price_usd.units,
                    "price_nanos":  product.price_usd.nanos,
                    "currency":     product.price_usd.currency_code,
                    "categories":   list(product.categories),
                },
            )
        except grpc.RpcError as exc:
            return StageResult(
                stage="get_product", status="error",
                error=f"{exc.code()}: {exc.details()}",
            )


# ── Stage 3: Get recommendations ─────────────────────────────────────────────

def stage_get_recommendations(user_id: str, product_id: str) -> StageResult:
    """
    ListRecommendations(user_id, [product_id]) → RecommendationService.
    Returns up to 5 complementary product IDs (LLM-ranked or random fallback).
    """
    with _channel(RECOMMENDATION_ADDR) as ch:
        stub = demo_pb2_grpc.RecommendationServiceStub(ch)

        def _call():
            return stub.ListRecommendations(
                demo_pb2.ListRecommendationsRequest(
                    user_id=user_id,
                    product_ids=[product_id],
                )
            )

        try:
            resp, latency = _timed(_call)

            # ListRecommendationsResponse { repeated string product_ids; LLMMetrics llm_metrics }
            recommended = list(resp.product_ids)
            logger.info(
                "[trial] recommendations | user=%s count=%d latency=%.3fs",
                user_id, len(recommended), latency,
            )
            return StageResult(
                stage="get_recommendations", status="ok",
                latency_s=latency,
                llm_metrics=LLMMetrics.from_proto(getattr(resp, "llm_metrics", None)),
                detail={
                    "recommended_product_ids": recommended,
                    "count":                   len(recommended),
                },
            )
        except grpc.RpcError as exc:
            return StageResult(
                stage="get_recommendations", status="error",
                error=f"{exc.code()}: {exc.details()}",
            )


# ── Stage 4: Get ads ──────────────────────────────────────────────────────────

def stage_get_ads(user_id: str, categories: list[str]) -> StageResult:
    """
    GetAds(context_keys=categories) → AdService.
    Returns contextual text ads based on product categories.
    """
    with _channel(AD_SERVICE_ADDR) as ch:
        stub = demo_pb2_grpc.AdServiceStub(ch)

        def _call():
            return stub.GetAds(
                demo_pb2.AdRequest(context_keys=categories)
            )

        try:
            resp, latency = _timed(_call)

            # AdResponse { repeated Ad ads; LLMMetrics llm_metrics }
            ads = [{"redirect_url": a.redirect_url, "text": a.text} for a in resp.ads]
            logger.info(
                "[trial] get_ads | user=%s ads=%d latency=%.3fs",
                user_id, len(ads), latency,
            )
            return StageResult(
                stage="get_ads", status="ok",
                latency_s=latency,
                llm_metrics=LLMMetrics.from_proto(getattr(resp, "llm_metrics", None)),
                detail={
                    "context_keys": categories,
                    "ads":          ads,
                    "count":        len(ads),
                },
            )
        except grpc.RpcError as exc:
            return StageResult(
                stage="get_ads", status="error",
                error=f"{exc.code()}: {exc.details()}",
            )


# ── Stage 5: Add to cart ──────────────────────────────────────────────────────

def stage_add_to_cart(user_id: str, product_id: str, quantity: int) -> StageResult:
    """
    AddItem(user_id, product_id, quantity) → CartService.
    AddItemResponse { LLMMetrics llm_metrics }  (always 0 for CartService).
    """
    with _channel(CART_SERVICE_ADDR) as ch:
        stub = demo_pb2_grpc.CartServiceStub(ch)

        def _call():
            return stub.AddItem(
                demo_pb2.AddItemRequest(
                    user_id=user_id,
                    item=demo_pb2.CartItem(product_id=product_id, quantity=quantity),
                )
            )

        try:
            resp, latency = _timed(_call)
            # AddItemResponse { LLMMetrics llm_metrics }
            logger.info(
                "[trial] add_to_cart | user=%s product=%s qty=%d latency=%.3fs",
                user_id, product_id, quantity, latency,
            )
            return StageResult(
                stage="add_to_cart", status="ok",
                latency_s=latency,
                llm_metrics=LLMMetrics.from_proto(getattr(resp, "llm_metrics", None)),
                detail={
                    "product_id": product_id,
                    "quantity":   quantity,
                },
            )
        except grpc.RpcError as exc:
            return StageResult(
                stage="add_to_cart", status="error",
                error=f"{exc.code()}: {exc.details()}",
            )


# ── Stage 6: Verify GET cart ──────────────────────────────────────────────────────

def stage_get_cart(user_id: str) -> StageResult:
    """
    GetCart(user_id) → CartService.
    GetCartResponse { Cart cart; LLMMetrics llm_metrics }
    Unwraps resp.cart before reading items.
    """
    with _channel(CART_SERVICE_ADDR) as ch:
        stub = demo_pb2_grpc.CartServiceStub(ch)

        def _call():
            return stub.GetCart(demo_pb2.GetCartRequest(user_id=user_id))

        try:
            resp, latency = _timed(_call)
            # ✓ Unwrap GetCartResponse.cart
            cart  = resp.cart
            items = [
                {"product_id": i.product_id, "quantity": i.quantity}
                for i in cart.items
            ]
            logger.info(
                "[trial] get_cart | user=%s items=%d latency=%.3fs",
                user_id, len(items), latency,
            )
            return StageResult(
                stage="get_cart", status="ok",
                latency_s=latency,
                llm_metrics=LLMMetrics.from_proto(getattr(resp, "llm_metrics", None)),
                detail={"items": items, "item_count": len(items)},
            )
        except grpc.RpcError as exc:
            return StageResult(
                stage="get_cart", status="error",
                error=f"{exc.code()}: {exc.details()}",
            )

# ── Stage 6.1: GET Shipping Quote ──────────────────────────────────────────────────────

def stage_get_shipping_quote(user_id: str, product_id: str, quantity: int) -> StageResult:
    """
    GetShippingQuote(user_id, product_id, quantity) → ShippingService.
    GetShippingQuoteResponse { ShippingCost shipping_cost; LLMMetrics llm_metrics }
    """
    with _channel(SHIPPING_SERVICE_ADDR) as ch:
        stub = demo_pb2_grpc.ShippingServiceStub(ch)

        def _call():
            return stub.GetQuote(
                demo_pb2.GetQuoteRequest(
                    address=TEST_ADDRESS,
                    items=[demo_pb2.CartItem(product_id=product_id, quantity=quantity)],
                )
            )

        try:
            resp, latency = _timed(_call)
            # ✓ Unwrap GetShippingQuoteResponse.shipping_cost
            shipping_cost = resp.cost_usd

            logger.info(
                "[trial] get_shipping_quote | user=%s product=%s qty=%d latency=%.3fs",
                user_id, product_id, quantity, latency,
            )
            return StageResult(
                stage="get_shipping_quote", status="ok",
                latency_s=latency,
                llm_metrics=LLMMetrics.from_proto(getattr(resp, "llm_metrics", None)),
                detail={"shipping_cost": {
                        "currency": shipping_cost.currency_code,
                        "units":    shipping_cost.units,
                        "nanos":    shipping_cost.nanos,
                        "formatted": f"{shipping_cost.currency_code} "
                                     f"{shipping_cost.units}.{(shipping_cost.nanos // 10_000_000):02d}",
                    }},
            )
        except grpc.RpcError as exc:
            return StageResult(
                stage="get_shipping_quote", status="error",
                error=f"{exc.code()}: {exc.details()}",
            )


# ── Stage 7: Checkout ─────────────────────────────────────────────────────────

def stage_place_order(user_id: str, user_currency: str, email: str) -> StageResult:
    """
    PlaceOrder → CheckoutService.
    PlaceOrderResponse { OrderResult order; LLMMetrics llm_metrics }
    The llm_metrics here is the SUM of all downstream service metrics
    (PaymentService agent + EmailService agent + CheckoutService ReAct agent).
    """
    with _channel(CHECKOUT_SERVICE_ADDR) as ch:
        stub = demo_pb2_grpc.CheckoutServiceStub(ch)

        def _call():
            return stub.PlaceOrder(
                demo_pb2.PlaceOrderRequest(
                    user_id=user_id,
                    user_currency=user_currency,
                    address=TEST_ADDRESS,
                    email=email,
                    credit_card=TEST_CREDIT_CARD,
                )
            )

        try:
            resp, latency = _timed(_call)
            order = resp.order
            cents = order.shipping_cost.nanos // 10_000_000

            logger.info(
                "[trial] place_order | user=%s order_id=%s tracking=%s latency=%.3fs",
                user_id, order.order_id, order.shipping_tracking_id, latency,
            )
            return StageResult(
                stage="place_order", status="ok",
                latency_s=latency,
                llm_metrics=LLMMetrics.from_proto(getattr(resp, "llm_metrics", None)),
                detail={
                    "order_id":             order.order_id,
                    "shipping_tracking_id": order.shipping_tracking_id,
                    "shipping_cost": {
                        "currency": order.shipping_cost.currency_code,
                        "units":    order.shipping_cost.units,
                        "nanos":    order.shipping_cost.nanos,
                        "formatted": f"{order.shipping_cost.currency_code} "
                                     f"{order.shipping_cost.units}.{cents:02d}",
                    },
                    "item_count": len(order.items),
                },
            )
        except grpc.RpcError as exc:
            return StageResult(
                stage="place_order", status="error",
                error=f"{exc.code()}: {exc.details()}",
            )


# ════════════════════════════════════════════════════════════════════════════
# Full trial
# ════════════════════════════════════════════════════════════════════════════

def run_trial(trial_id: int, delay: float, drop_rate: int) -> TrialResult:
    """
    Execute the complete user-session workflow for one trial.

    Stages (in order):
      1. search_products    → find the item
      2. get_product        → fetch details of first result
      3. get_recommendations → LLM-ranked complementary products
      4. get_ads            → contextual ads for the product's categories
      5. add_to_cart        → add selected product × ITEM_QTY
      6. get_cart           → verify cart contents
      6.1. get_shipping_quote → fetch shipping cost
      7. place_order        → full checkout with aggregated LLM metrics

    Short-circuits on any critical failure (stages 1, 2, 5, 7 are critical;
    stages 3, 4, 6 are best-effort — failures are recorded but don't stop
    the workflow).
    """
    if delay > 0:
        time.sleep(delay)

    trial_start = time.perf_counter()
    user_id     = f"sim_user_{trial_id}_{uuid.uuid4().hex[:8]}"
    user_email  = f"user_{trial_id}@simulation.test"
    stages:     list[StageResult] = []

    # ── Stage 1: Search products ──────────────────────────────────────────────
    s1 = stage_search_products(user_id)
    stages.append(s1)
    if s1.status == "error":
        return TrialResult(
            trial=trial_id, status="error",
            elapsed_s=time.perf_counter() - trial_start,
            stages=stages,
            error=f"search_products failed: {s1.error}",
        )

    product_id = s1.detail["product_id"]
    categories = s1.detail.get("categories", [])

    # ── Stage 2: Get product details ──────────────────────────────────────────
    s2 = stage_get_product(user_id, product_id)
    stages.append(s2)
    if s2.status == "error":
        return TrialResult(
            trial=trial_id, status="error",
            elapsed_s=time.perf_counter() - trial_start,
            stages=stages,
            error=f"get_product failed: {s2.error}",
        )

    # Use categories from GetProduct (richer than SearchProducts)
    categories = s2.detail.get("categories", categories)

    # ── Stage 3: Recommendations (best-effort) ────────────────────────────────
    s3 = stage_get_recommendations(user_id, product_id)
    stages.append(s3)
    if s3.status == "error":
        logger.warning("[trial %d] recommendations failed (non-fatal): %s", trial_id, s3.error)

    # ── Stage 4: Ads (best-effort) ────────────────────────────────────────────
    s4 = stage_get_ads(user_id, categories or [SEARCH_KEYWORD])
    stages.append(s4)
    if s4.status == "error":
        logger.warning("[trial %d] get_ads failed (non-fatal): %s", trial_id, s4.error)

    # ── Stage 5: Add to cart ──────────────────────────────────────────────────
    s5 = stage_add_to_cart(user_id, product_id, ITEM_QTY)
    stages.append(s5)
    if s5.status == "error":
        return TrialResult(
            trial=trial_id, status="error",
            elapsed_s=time.perf_counter() - trial_start,
            stages=stages,
            error=f"add_to_cart failed: {s5.error}",
        )

    # ── Stage 6: Verify cart (best-effort) ────────────────────────────────────
    s6 = stage_get_cart(user_id)
    stages.append(s6)
    if s6.status == "error":
        logger.warning("[trial %d] get_cart verification failed (non-fatal): %s", trial_id, s6.error)

    # ── Stage 6_1: Get shipping quote (best-effort) ────────────────────────────────────
    s6_1 = stage_get_shipping_quote(user_id, product_id, ITEM_QTY)
    stages.append(s6_1)
    if s6_1.status == "error":
        logger.warning("[trial %d] get_shipping_quote failed (non-fatal): %s", trial_id, s6_1.error)
        
    # ── Stage 7: Checkout ─────────────────────────────────────────────────────
    s7 = stage_place_order(user_id, "USD", user_email)
    stages.append(s7)
    if s7.status == "error":
        return TrialResult(
            trial=trial_id, status="error",
            elapsed_s=time.perf_counter() - trial_start,
            stages=stages,
            error=f"place_order failed: {s7.error}",
        )

    # ── Aggregate total LLM metrics across all stages ─────────────────────────
    total_llm = LLMMetrics()
    for s in stages:
        total_llm = total_llm + s.llm_metrics

    elapsed = time.perf_counter() - trial_start
    logger.info(
        "[trial %d] completed | elapsed=%.3fs total_llm_calls=%d "
        "in_tokens=%d out_tokens=%d",
        trial_id, elapsed,
        total_llm.total_llm_calls,
        total_llm.total_input_tokens,
        total_llm.total_output_tokens,
    )

    return TrialResult(
        trial=trial_id,
        status="ok",
        elapsed_s=round(elapsed, 3),
        stages=stages,
        total_llm=total_llm,
    )


# ════════════════════════════════════════════════════════════════════════════
# Statistics helpers
# ════════════════════════════════════════════════════════════════════════════

def _safe_stat(fn, values: list, fallback=0.0):
    try:
        return round(fn(values), 4) if values else fallback
    except Exception:
        return fallback


def _p95(values: list) -> float:
    try:
        if len(values) < 2:
            return values[0] if values else 0.0
        qs = statistics.quantiles(values, n=100)
        return round(qs[94], 4)   # index 94 = 95th percentile
    except Exception:
        return 0.0


def compute_stage_stats(results: list[TrialResult], stage_name: str) -> Dict[str, float]:
    latencies = [
        s.latency_s
        for r in results if r.status == "ok"
        for s in r.stages if s.stage == stage_name and s.status == "ok"
    ]
    return {
        "avg":  _safe_stat(statistics.mean,   latencies),
        "std":  _safe_stat(statistics.stdev,  latencies) if len(latencies) > 1 else 0.0,
        "med":  _safe_stat(statistics.median, latencies),
        "p95":  _p95(latencies),
        "min":  _safe_stat(min, latencies),
        "max":  _safe_stat(max, latencies),
        "n":    len(latencies),
    }


def compute_llm_stats(results: list[TrialResult], stage_name: str | None = None) -> Dict[str, Any]:
    """
    If stage_name is given: aggregate LLM metrics for that specific stage.
    If stage_name is None:  aggregate the trial-level total_llm.
    """
    if stage_name:
        metrics = [
            s.llm_metrics
            for r in results if r.status == "ok"
            for s in r.stages if s.stage == stage_name and s.status == "ok"
        ]
    else:
        metrics = [r.total_llm for r in results if r.status == "ok"]

    if not metrics:
        return {"total_input_tokens": 0, "total_output_tokens": 0, "total_llm_calls": 0}

    return {
        "total_input_tokens":    sum(m.total_input_tokens  for m in metrics),
        "total_output_tokens":   sum(m.total_output_tokens for m in metrics),
        "total_llm_calls":       sum(m.total_llm_calls     for m in metrics),
        "avg_input_per_trial":   round(statistics.mean([m.total_input_tokens  for m in metrics]), 1),
        "avg_output_per_trial":  round(statistics.mean([m.total_output_tokens for m in metrics]), 1),
        "avg_calls_per_trial":   round(statistics.mean([m.total_llm_calls     for m in metrics]), 2),
    }


# ════════════════════════════════════════════════════════════════════════════
# Main
# ════════════════════════════════════════════════════════════════════════════

def serialize_trial(t: TrialResult) -> dict:
    """Convert TrialResult to a JSON-serialisable dict."""
    return {
        "trial":     t.trial,
        "status":    t.status,
        "elapsed_s": t.elapsed_s,
        "error":     t.error,
        "total_llm": asdict(t.total_llm),
        "stages": [
            {
                "stage":       s.stage,
                "status":      s.status,
                "latency_s":   round(s.latency_s, 4),
                "llm_metrics": asdict(s.llm_metrics),
                "detail":      s.detail,
                "error":       s.error,
            }
            for s in t.stages
        ],
    }


def main():
    # ── Clear log files ───────────────────────────────────────────────────────
    for log_path in LOG_FILES:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "w") as f:
            f.write("")

    output_file = (
        os.path.dirname(__file__) + f"/results/ms_baseline_results_delay_{DELAY}_drop_{DROP_RATE}.json"
    )
    with open(output_file, "w") as f:
        f.write("")

    run_results = []

    for run_idx in range(TOTAL_RUNS):
        print(f"\n{'='*70}")
        print(f"RUN {run_idx + 1} / {TOTAL_RUNS}")
        print(f"  N_TRIALS={N_TRIALS}  MAX_WORKERS={MAX_WORKERS}  "
              f"DELAY={DELAY}s  DROP_RATE={DROP_RATE}%")
        print(f"{'='*70}")

        # ── Clean DB before every run ─────────────────────────────────────────
        print("\nCleaning MongoDB collections (orders, payments, shipments)...")
        clean_db_for_run()
        print("DB clean. Starting trials...\n")

        results: list[TrialResult] = []

        # ── Parallel trial execution ──────────────────────────────────────────
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {
                executor.submit(run_trial, trial_id, DELAY, DROP_RATE): trial_id
                for trial_id in range(1, N_TRIALS + 1)
            }
            for future in as_completed(futures):
                trial_result = future.result()
                results.append(trial_result)
                status_icon = "✓" if trial_result.status == "ok" else "✗"
                print(
                    f"  {status_icon} Trial {trial_result.trial:>3} | "
                    f"{trial_result.elapsed_s:.3f}s | "
                    f"llm_calls={trial_result.total_llm.total_llm_calls:>3} | "
                    f"in_tok={trial_result.total_llm.total_input_tokens:>5} | "
                    f"out_tok={trial_result.total_llm.total_output_tokens:>5}"
                    + (f" | ERROR: {trial_result.error}" if trial_result.error else "")
                )

        results.sort(key=lambda r: r.trial)

        # ── DB final state ────────────────────────────────────────────────────
        _, db = real_db()
        db_state = get_final_state(db)

        # ── Compute statistics ────────────────────────────────────────────────
        ok_results      = [r for r in results if r.status == "ok"]
        error_count     = len(results) - len(ok_results)
        elapsed_values  = [r.elapsed_s for r in ok_results]

        stage_names = [
            "search_products", "get_product", "get_recommendations",
            "get_ads", "add_to_cart", "get_cart", "get_shipping_quote", "place_order",
        ]
        stage_stats = {
            name: compute_stage_stats(ok_results, name)
            for name in stage_names
        }
        llm_by_stage = {
            name: compute_llm_stats(ok_results, name)
            for name in stage_names
        }
        total_llm_stats = compute_llm_stats(ok_results, stage_name=None)

        summary = {
            # run config
            "run":          run_idx + 1,
            "n_trials":     N_TRIALS,
            "n_workers":    MAX_WORKERS,
            "delay_s":      DELAY,
            "drop_rate":    DROP_RATE,
            "search_keyword": SEARCH_KEYWORD,
            "item_qty":     ITEM_QTY,

            # outcomes
            "successful_trials": len(ok_results),
            "failed_trials":     error_count,
            "success_rate_pct":  round(len(ok_results) / N_TRIALS * 100, 1),
            **db_state,

            # end-to-end latency
            "latency": {
                "avg_s": _safe_stat(statistics.mean,   elapsed_values),
                "std_s": _safe_stat(statistics.stdev,  elapsed_values) if len(elapsed_values) > 1 else 0.0,
                "med_s": _safe_stat(statistics.median, elapsed_values),
                "p95_s": _p95(elapsed_values),
                "min_s": _safe_stat(min, elapsed_values),
                "max_s": _safe_stat(max, elapsed_values),
            },

            # per-stage latency breakdowns
            "stage_latency": stage_stats,

            # LLM metrics aggregated across all ok trials
            "llm_totals":    total_llm_stats,
            "llm_by_stage":  llm_by_stage,
        }

        print(f"\n{'─'*70}")
        print("SUMMARY")
        print(f"{'─'*70}")
        print(f"  Successful trials : {summary['successful_trials']} / {N_TRIALS}")
        print(f"  Success rate      : {summary['success_rate_pct']}%")
        print(f"  Completed orders  : {db_state['total_completed_orders']}")
        print(f"  Pending orders    : {db_state['total_pending_orders']}")
        print(f"  Payments success  : {db_state['total_success_payments']}")
        print(f"  Shipments booked  : {db_state['total_shipment_bookings']}")
        print(f"  DB verdict        : {db_state['final_ec_state']}")
        print(f"  E2E avg latency   : {summary['latency']['avg_s']:.3f}s")
        print(f"  E2E p95 latency   : {summary['latency']['p95_s']:.3f}s")
        print(f"  Total LLM calls   : {total_llm_stats['total_llm_calls']}")
        print(f"  Total in tokens   : {total_llm_stats['total_input_tokens']}")
        print(f"  Total out tokens  : {total_llm_stats['total_output_tokens']}")
        print(f"  Avg calls/trial   : {total_llm_stats.get('avg_calls_per_trial', 0):.1f}")
        print()
        print("Per-stage latency (avg / p95 seconds):")
        for name, stats in stage_stats.items():
            llm = llm_by_stage[name]
            print(
                f"    {name:<25} avg={stats['avg']:.3f}s  "
                f"p95={stats['p95']:.3f}s  "
                f"llm_calls={llm.get('total_llm_calls', 0)}"
            )
        print(f"{'─'*70}\n")

        run_results.append({
            "run_number":   run_idx + 1,
            "summary":      summary,
            "trial_results": [serialize_trial(r) for r in results],
        })
        print(f"Run {run_idx + 1} done.\n{'─'*70}")

    # ── Write final JSON output ───────────────────────────────────────────────
    with open(output_file, "w") as f:
        json.dump(run_results, f, indent=2)
    print(f"\nResults saved to: {output_file}")


if __name__ == "__main__":
    main()
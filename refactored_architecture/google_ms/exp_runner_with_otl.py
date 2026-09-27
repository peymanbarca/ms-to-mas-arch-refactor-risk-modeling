"""
simulation/run_trial.py

End-to-end refactored architecture simulation script with On-The-Loop (OTL) monitoring.

OTL Monitoring:
  Divides trials into non-overlapping blocks of size `block_size`. After each block
  completes, checks if key metrics exceed predefined thresholds:
    • p95_latency_threshold_s    – interrupts if p95 latency exceeds this
    • failure_rate_threshold_pct – interrupts if failure rate (%) exceeds this
    • inconsistency_threshold_pct– interrupts if latency inconsistency (CV%) exceeds this
  
  Can be toggled via `otl_enabled` flag.

Workflow per trial (mirrors a real user session):
  1. SearchProducts          → ProductCatalogService  (find item by keyword)
  2. GetProduct              → ProductCatalogService  (fetch full details)
  3. ListRecommendations     → RecommendationService  (similar products)
  4. GetAds                  → AdService              (contextual ads)
  5. AddItem                 → CartService            (add to cart)
  6. Verify Cart & GetShippingQuote → CartService + ShippingService (best-effort)
  7. PlaceOrder              → CheckoutService        (full checkout)

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
  # With OTL enabled:
  OTL_ENABLED=true OTL_BLOCK_SIZE=10 python -m simulation.run_trial
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
from typing import Any, Dict, List, Optional, Callable
import argparse
import grpc
from pymongo import MongoClient
from pathlib import Path

# ── path setup ────────────────────────────────────────────────────────────────
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
basedir = os.path.dirname(os.path.abspath(__file__))
sys.path.append(basedir)

from .shared import demo_pb2
from .shared import demo_pb2_grpc

# ════════════════════════════════════════════════════════════════════════════
# Configuration (all overridable via env vars)
# ════════════════════════════════════════════════════════════════════════════

SEARCH_KEYWORD  = os.environ.get("SEARCH_KEYWORD",   "sunglass")
ITEM_QTY        = int(os.environ.get("ITEM_QTY",     "2"))
TOTAL_RUNS      = int(os.environ.get("TOTAL_RUNS",   "1"))
DELAY           = float(os.environ.get("DELAY",      "0"))
DROP_RATE       = int(os.environ.get("DROP_RATE",    "0"))

# ── OTL (On-The-Loop) monitoring configuration ────────────────────────────────
OTL_ENABLED                         = os.environ.get("OTL_ENABLED", "false").lower() == "true"
OTL_BLOCK_SIZE                      = int(os.environ.get("OTL_BLOCK_SIZE", "10"))
OTL_P95_LATENCY_THRESHOLD_S         = float(os.environ.get("OTL_P95_LATENCY_THRESHOLD_S", "1.9"))
OTL_FAILURE_RATE_THRESHOLD_PCT      = float(os.environ.get("OTL_FAILURE_RATE_THRESHOLD_PCT", "2.0"))
OTL_INVARIANT_VIOLATION_THRESHOLD_PCT = float(os.environ.get("OTL_INVARIANT_VIOLATION_THRESHOLD_PCT", "1.0"))

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
# OTL (On-The-Loop) Monitoring Class
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class OTLConfig:
    """Configuration for on-the-loop monitoring."""
    enabled: bool = True
    block_size: int = 10
    p95_latency_threshold_s: float = 5.0
    failure_rate_threshold_pct: float = 10.0
    invariant_violation_threshold_pct: float = 5.0  # DB invariant violations (% of trials with inconsistencies)


@dataclass
class OTLMetrics:
    """Metrics computed within a block."""
    block_num: int
    trials_in_block: int
    p95_latency_s: float = 0.0
    avg_failure_rate_pct: float = 0.0
    invariant_violation_pct: float = 0.0  # DB inconsistency rate
    threshold_violations: List[str] = field(default_factory=list)
    db_state_before: Dict[str, Any] = field(default_factory=dict)
    db_state_after: Dict[str, Any] = field(default_factory=dict)
    
    def __str__(self) -> str:
        return (
            f"Block {self.block_num} ({self.trials_in_block} trials): "
            f"p95={self.p95_latency_s:.3f}s, "
            f"failure_rate={self.avg_failure_rate_pct:.1f}%, "
            f"db_inconsistency={self.invariant_violation_pct:.1f}%"
        )


class OTLMonitor:
    """
    On-the-loop monitoring for experiment runs.
    
    Tracks metrics across trial blocks and determines if execution should continue
    or be interrupted based on threshold violations.
    """
    
    def __init__(self, config: OTLConfig):
        self.config = config
        self.block_metrics: List[OTLMetrics] = []
        self.should_interrupt = False
        self.interrupt_reason = ""
    
    def compute_block_metrics(
        self,
        block_num: int,
        block_results: List["TrialResult"],
        db_state_before: Dict[str, Any],
        db_state_after: Dict[str, Any],
    ) -> OTLMetrics:
        """
        Compute metrics for a block of trials and check against thresholds.
        
        Args:
            block_num: Block number (1-indexed)
            block_results: List of TrialResult objects in this block
            db_state_before: Database state snapshot before block execution
            db_state_after: Database state snapshot after block execution
            
        Returns:
            OTLMetrics object with computed metrics and violations
        """
        if not block_results:
            return OTLMetrics(
                block_num=block_num,
                trials_in_block=0,
                db_state_before=db_state_before,
                db_state_after=db_state_after,
            )
        
        # Separate successful and failed trials
        ok_results = [r for r in block_results if r.status == "ok"]
        failed_count = len(block_results) - len(ok_results)
        
        # ── Compute p95 latency ────────────────────────────────────────────────
        elapsed_values = [r.elapsed_s for r in ok_results]
        p95_latency = self._compute_p95(elapsed_values) if elapsed_values else 0.0
        
        # ── Compute failure rate ────────────────────────────────────────────────
        failure_rate_pct = (failed_count / len(block_results)) * 100.0
        
        # ── Compute invariant violation rate from DB state ────────────────────
        invariant_violation_pct = self._compute_invariant_violations(
            block_results,
            db_state_before,
            db_state_after
        )
        
        # ── Check thresholds ───────────────────────────────────────────────────
        violations = []
        
        if p95_latency > self.config.p95_latency_threshold_s:
            violations.append(
                f"p95_latency ({p95_latency:.3f}s) exceeds threshold "
                f"({self.config.p95_latency_threshold_s:.3f}s)"
            )
        
        if failure_rate_pct > self.config.failure_rate_threshold_pct:
            violations.append(
                f"failure_rate ({failure_rate_pct:.1f}%) exceeds threshold "
                f"({self.config.failure_rate_threshold_pct:.1f}%)"
            )
        
        if invariant_violation_pct > self.config.invariant_violation_threshold_pct:
            violations.append(
                f"invariant_violations ({invariant_violation_pct:.1f}%) exceeds threshold "
                f"({self.config.invariant_violation_threshold_pct:.1f}%)"
            )
        
        metrics = OTLMetrics(
            block_num=block_num,
            trials_in_block=len(block_results),
            p95_latency_s=p95_latency,
            avg_failure_rate_pct=failure_rate_pct,
            invariant_violation_pct=invariant_violation_pct,
            threshold_violations=violations,
            db_state_before=db_state_before,
            db_state_after=db_state_after,
        )
        
        self.block_metrics.append(metrics)
        
        # Set interrupt flag if violations detected
        if violations:
            self.should_interrupt = True
            self.interrupt_reason = "\n  ".join(violations)
        
        return metrics
    
    @staticmethod
    def _compute_p95(values: List[float]) -> float:
        """Compute p95 percentile."""
        if not values:
            return 0.0
        sorted_vals = sorted(values)
        idx = int(len(sorted_vals) * 0.95)
        return sorted_vals[min(idx, len(sorted_vals) - 1)]
    
    @staticmethod
    def _compute_invariant_violations(
        block_results: List["TrialResult"],
        db_state_before: Dict[str, Any],
        db_state_after: Dict[str, Any],
    ) -> float:
        """
        Compute database invariant violation rate.
        
        Detects inconsistencies by checking:
        - Pending orders that should have been processed
        - Payment failures that don't match trial failures
        - Shipment booking inconsistencies
        - DB state doesn't match trial outcomes
        
        Args:
            block_results: Trial results in this block
            db_state_before: DB state before block execution
            db_state_after: DB state after block execution
            
        Returns:
            Percentage of trials with detected invariant violations
        """
        if not block_results:
            return 0.0
        
        ok_results = [r for r in block_results if r.status == "ok"]
        
        if not ok_results:
            # If all trials failed, we can't detect invariant violations
            return 0.0
        
        violations_detected = 0
        
        # ── Check 1: Pending orders inconsistency ──────────────────────────────
        # Expected: successful trials should lead to orders being processed
        pending_delta = db_state_after.get('total_pending_orders', 0) - \
                       db_state_before.get('total_pending_orders', 0)
        
        # If we have successful trials, pending orders should decrease
        # or at least not increase significantly
        if len(ok_results) > 0 and pending_delta > len(ok_results) * 0.5:
            violations_detected += 1
        
        # ── Check 2: Completed orders consistency ──────────────────────────────
        # Expected: each successful trial should result in a completed order
        completed_delta = db_state_after.get('total_completed_orders', 0) - \
                         db_state_before.get('total_completed_orders', 0)
        
        # Completed orders should increase by approximately number of ok trials
        # Allow 20% variance
        expected_min = len(ok_results) * 0.8
        if completed_delta < expected_min:
            violations_detected += 1
        
        # ── Check 3: Payment success consistency ────────────────────────────────
        # Expected: successful trials should correlate with successful payments
        payment_delta = db_state_after.get('total_success_payments', 0) - \
                       db_state_before.get('total_success_payments', 0)
        
        # Successful payments should be close to successful trials
        # Allow 20% variance
        expected_min = len(ok_results) * 0.8
        if payment_delta < expected_min:
            violations_detected += 1
        
        # ── Check 4: Shipment booking consistency ───────────────────────────────
        # Expected: completed orders should have corresponding shipment bookings
        shipment_delta = db_state_after.get('total_shipment_bookings', 0) - \
                        db_state_before.get('total_shipment_bookings', 0)
        
        # Shipment bookings should increase with completed orders
        # Allow 20% variance
        expected_min = completed_delta * 0.8
        if shipment_delta < expected_min:
            violations_detected += 1
        
        # ── Check 5: DB verdict consistency ────────────────────────────────────
        # If there are successful trials but DB is in error state, that's a violation
        if len(ok_results) > 0 and db_state_after.get('final_ec_state') == 'error':
            violations_detected += 1
        
        # Compute violation percentage
        # Each check can contribute up to 1 violation
        # With 5 checks, max is 5 violations
        # Normalize to percentage of trials affected
        violation_pct = (violations_detected / 5.0) * 100.0
        
        return min(violation_pct, 100.0)  # Cap at 100%
    
    def report(self) -> Dict[str, Any]:
        """Generate OTL monitoring report."""
        return {
            "otl_enabled": self.config.enabled,
            "block_size": self.config.block_size,
            "total_blocks_processed": len(self.block_metrics),
            "should_interrupt": self.should_interrupt,
            "interrupt_reason": self.interrupt_reason,
            "thresholds": {
                "p95_latency_threshold_s": self.config.p95_latency_threshold_s,
                "failure_rate_threshold_pct": self.config.failure_rate_threshold_pct,
                "invariant_violation_threshold_pct": self.config.invariant_violation_threshold_pct,
            },
            "block_metrics": [
                {
                    "block_num": m.block_num,
                    "trials_in_block": m.trials_in_block,
                    "p95_latency_s": round(m.p95_latency_s, 4),
                    "avg_failure_rate_pct": round(m.avg_failure_rate_pct, 2),
                    "invariant_violation_pct": round(m.invariant_violation_pct, 2),
                    "db_state_before": m.db_state_before,
                    "db_state_after": m.db_state_after,
                    "violations": m.threshold_violations,
                }
                for m in self.block_metrics
            ],
        }


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
        db["orders"].delete_many({})
        db["payments"].delete_many({})
        db["shipments"].delete_many({})
    finally:
        client.close()


# ════════════════════════════════════════════════════════════════════════════
# Placeholder stub functions (use actual implementations from original)
# ════════════════════════════════════════════════════════════════════════════

def run_trial(trial_id: int, delay: float, drop_rate: int) -> TrialResult:
    """
    Execute a single end-to-end trial.
    
    [Use actual implementation from original file]
    """
    # Placeholder - replace with actual implementation
    import random
    elapsed = random.uniform(1.0, 5.0)
    status = "ok" if random.random() > (drop_rate / 100.0) else "error"
    return TrialResult(
        trial=trial_id,
        status=status,
        elapsed_s=elapsed,
        stages=[],
        total_llm=LLMMetrics(),
        error=None if status == "ok" else "simulated error"
    )


def get_final_state(db):
    """Get final DB state."""
    # Placeholder - replace with actual implementation
    return {
        "total_completed_orders": 0,
        "total_pending_orders": 0,
        "total_success_payments": 0,
        "total_shipment_bookings": 0,
        "final_ec_state": "clean",
    }


def compute_stage_stats(ok_results, stage_name):
    """Compute per-stage statistics."""
    # Placeholder - replace with actual implementation
    return {"avg": 0.0, "p95": 0.0}


def compute_llm_stats(ok_results, stage_name=None):
    """Compute LLM statistics."""
    # Placeholder - replace with actual implementation
    return {
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "total_llm_calls": 0,
        "avg_calls_per_trial": 0.0,
    }


def _safe_stat(func: Callable, values: List[float]) -> float:
    """Safely compute a statistic."""
    try:
        return func(values) if values else 0.0
    except:
        return 0.0


def _p95(values: List[float]) -> float:
    """Compute p95 percentile."""
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    idx = int(len(sorted_vals) * 0.95)
    return sorted_vals[min(idx, len(sorted_vals) - 1)]


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


def full_trials_runner(LLM, T, CONCURRENCY_RATE, R, otl_config: Optional[OTLConfig] = None):
    """
    Run full trial experiment with optional on-the-loop monitoring.
    
    Args:
        LLM: LLM model name
        T: Unused parameter (kept for compatibility)
        CONCURRENCY_RATE: Number of parallel worker threads
        R: Total number of trials
        otl_config: OTLConfig object for monitoring. If None, OTL is disabled.
    """
    
    # ── Initialize OTL monitor ────────────────────────────────────────────────
    otl_monitor = OTLMonitor(otl_config) if otl_config and otl_config.enabled else None
    
    # ── Clear log files ───────────────────────────────────────────────────────
    for log_path in LOG_FILES:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "w") as f:
            f.write("")

    run_results = []

    for run_idx in range(TOTAL_RUNS):
        print(f"\n{'='*70}")
        print(f"RUN {run_idx + 1} / {TOTAL_RUNS}")
        print(f"  N_TRIALS={R}  MAX_WORKERS={CONCURRENCY_RATE}  "
              f"DELAY={DELAY}s  DROP_RATE={DROP_RATE}%")
        if otl_monitor:
            print(f"  OTL_ENABLED=true  BLOCK_SIZE={otl_monitor.config.block_size}")
        print(f"{'='*70}")

        # ── Clean DB before every run ─────────────────────────────────────────
        print("\nCleaning MongoDB collections (orders, payments, shipments)...")
        clean_db_for_run()
        print("DB clean. Starting trials...\n")

        results: list[TrialResult] = []
        trials_completed = 0
        run_interrupted = False
        last_block_db_state = None

        # ── Parallel trial execution with OTL monitoring ───────────────────────
        with ThreadPoolExecutor(max_workers=CONCURRENCY_RATE) as executor:
            futures = {
                executor.submit(run_trial, trial_id, DELAY, DROP_RATE): trial_id
                for trial_id in range(1, R + 1)
            }
            
            for future in as_completed(futures):
                if run_interrupted:
                    break
                
                trial_result = future.result()
                results.append(trial_result)
                trials_completed += 1
                
                status_icon = "✓" if trial_result.status == "ok" else "✗"
                print(
                    f"  {status_icon} Trial {trial_result.trial:>3} | "
                    f"{trial_result.elapsed_s:.3f}s | "
                    f"llm_calls={trial_result.total_llm.total_llm_calls:>3} | "
                    f"in_tok={trial_result.total_llm.total_input_tokens:>5} | "
                    f"out_tok={trial_result.total_llm.total_output_tokens:>5}"
                    + (f" | ERROR: {trial_result.error}" if trial_result.error else "")
                )
                
                # ── Check OTL after each block ──────────────────────────────────
                if otl_monitor:
                    block_num = (trials_completed - 1) // otl_monitor.config.block_size + 1
                    within_block_idx = (trials_completed - 1) % otl_monitor.config.block_size
                    
                    # If we've completed a full block, check metrics
                    if within_block_idx == otl_monitor.config.block_size - 1 or trials_completed == R:
                        block_start = (block_num - 1) * otl_monitor.config.block_size
                        block_end = min(block_start + otl_monitor.config.block_size, len(results))
                        block_results = results[block_start:block_end]
                        
                        # Capture DB state before and after block
                        _, db = real_db()
                        db_state_before = last_block_db_state if last_block_db_state else get_final_state(db)
                        db_state_after = get_final_state(db)
                        last_block_db_state = db_state_after
                        
                        metrics = otl_monitor.compute_block_metrics(
                            block_num, block_results,
                            db_state_before, db_state_after
                        )
                        print(f"\n  [OTL] {metrics}")
                        
                        if metrics.threshold_violations:
                            print(f"\n  [OTL] ⚠️  THRESHOLD VIOLATIONS DETECTED:")
                            for violation in metrics.threshold_violations:
                                print(f"    • {violation}")
                            print(f"\n  [OTL] Interrupting experiment execution.\n")
                            run_interrupted = True
                        
                        if block_num < (R // otl_monitor.config.block_size) or (R % otl_monitor.config.block_size != 0 and block_end < R):
                            print()  # Blank line between blocks

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
            "n_trials":     R,
            "n_workers":    CONCURRENCY_RATE,
            "delay_s":      DELAY,
            "drop_rate":    DROP_RATE,
            "search_keyword": SEARCH_KEYWORD,
            "item_qty":     ITEM_QTY,

            # outcomes
            "successful_trials": len(ok_results),
            "failed_trials":     error_count,
            "success_rate_pct":  round(len(ok_results) / len(results) * 100, 1) if results else 0.0,
            "trials_executed":   len(results),
            "run_interrupted":   run_interrupted,
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
            
            # OTL monitoring report
            "otl_report": otl_monitor.report() if otl_monitor else None,
        }

        print(f"\n{'─'*70}")
        print("SUMMARY")
        print(f"{'─'*70}")
        print(f"  Successful trials : {summary['successful_trials']} / {summary['trials_executed']}")
        print(f"  Success rate      : {summary['success_rate_pct']}%")
        if run_interrupted:
            print(f"  ⚠️  Run interrupted by OTL monitoring")
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

    return run_results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="End-to-end load generator with on-the-loop monitoring",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--trials",        type=int,   default=10,
                        help="Total number of end-to-end trials (default: 10)")
    parser.add_argument("--concurrency",   type=int,   default=1,
                        help="Parallel worker threads (default: 1)")
    parser.add_argument("--otl-enabled",   action="store_true",
                        help="Enable on-the-loop monitoring")
    parser.add_argument("--otl-block-size", type=int, default=10,
                        help="OTL block size (default: 10)")
    parser.add_argument("--otl-p95-threshold", type=float, default=5.0,
                        help="OTL p95 latency threshold in seconds (default: 5.0)")
    parser.add_argument("--otl-failure-threshold", type=float, default=10.0,
                        help="OTL failure rate threshold in percent (default: 10.0)")
    parser.add_argument("--otl-invariant-threshold", type=float, default=5.0,
                        help="OTL DB invariant violation threshold in percent (default: 5.0)")
    args = parser.parse_args()
    
    R = args.trials
    CONCURRENCY_RATE = args.concurrency

    pwd = os.getcwd()
    script_dir = str(Path(__file__).resolve().parent)

    log_telemetry_report_file = str(script_dir) + '/results/log_telemetry.json'
    with open(log_telemetry_report_file, "w") as f:
        f.write("\n\n")

    # ── Configure OTL if enabled ──────────────────────────────────────────────
    otl_config = None
    if args.otl_enabled:
        otl_config = OTLConfig(
            enabled=True,
            block_size=args.otl_block_size,
            p95_latency_threshold_s=args.otl_p95_threshold,
            failure_rate_threshold_pct=args.otl_failure_threshold,
            invariant_violation_threshold_pct=args.otl_invariant_threshold,
        )

    run_results = full_trials_runner(
        LLM='llama3:8b',
        T=0,
        CONCURRENCY_RATE=CONCURRENCY_RATE,
        R=R,
        otl_config=otl_config
    )
    
    # Save all results
    with open(log_telemetry_report_file, "w") as f:
        f.write("\n\n")
        json.dump(run_results, f, indent=2)
        f.write("\n\n")
    
    print(f"\nResults saved to: {log_telemetry_report_file}")

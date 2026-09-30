"""
RetailBen Experiment Runner with On-The-Loop (OTL) Monitoring

End-to-end RetailBen microservice experiment with OTL health checks.

OTL Monitoring:
  Divides trials into non-overlapping blocks of size `block_size`. After each block
  completes, checks if key metrics exceed predefined thresholds:
    • p95_latency_threshold_s    – interrupts if p95 latency exceeds this
    • failure_rate_threshold_pct – interrupts if failure rate (%) exceeds this
    • invariant_violation_threshold_pct – interrupts if DB inconsistencies exceed this
  
  Checks DB state consistency across:
    • Stock availability (inventory correctness)
    • Order completion (order processing)
    • Payment success (payment pipeline)
    • Shipment bookings (fulfillment pipeline)

  Can be toggled via `otl_enabled` flag.

Trial Workflow:
  1. Search for product (search_latency)
  2. Add item to cart (cart_latency)
  3. Checkout / Place order (order_latency)
  Each trial records: elapsed time, LLM calls, API calls, and failures

Usage:
  python retailben_exp_runner_with_otl.py --trials 100 --concurrency 8
  # With OTL enabled:
  python retailben_exp_runner_with_otl.py --trials 100 --concurrency 8 \
    --otl-enabled --otl-block-size 20 --otl-p95-threshold 3.5
"""

import requests
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pymongo import MongoClient
import os
import statistics
import sys
import argparse
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


# ════════════════════════════════════════════════════════════════════════════
# RetailBen Configuration
# ════════════════════════════════════════════════════════════════════════════

SEARCH_SERVICE_URL = "http://127.0.0.1:8008/search"
CART_SERVICE_URL = "http://127.0.0.1:8003/cart/cart_id/items"
ORDER_SERVICE_URL = "http://127.0.0.1:8000/cart/cart_id/checkout"

ITEM = "headphone"
SKU = "b2926dc2-cc6d-4c3e-ae40-7a127c173b16"
INIT_STOCK = 10
QTY = 2

total_full_trials_runs = 1

DELAY = float(os.environ.get("DELAY", "0"))             # seconds to sleep inside inventory agent
DROP_RATE = int(os.environ.get("DROP_RATE", "0"))       # percent 0-100

MONGO_URL = os.environ.get("MONGO_URL", "mongodb://localhost:27017/")
DB_NAME = os.environ.get("DB_NAME", "retailben")

# ── OTL (On-The-Loop) monitoring configuration ────────────────────────────────
OTL_ENABLED                         = os.environ.get("OTL_ENABLED", "false").lower() == "true"
OTL_BLOCK_SIZE                      = int(os.environ.get("OTL_BLOCK_SIZE", "20"))
OTL_P95_LATENCY_THRESHOLD_S         = float(os.environ.get("OTL_P95_LATENCY_THRESHOLD_S", "1.9"))
OTL_FAILURE_RATE_THRESHOLD_PCT      = float(os.environ.get("OTL_FAILURE_RATE_THRESHOLD_PCT", "2.0"))
OTL_INVARIANT_VIOLATION_THRESHOLD_PCT = float(os.environ.get("OTL_INVARIANT_VIOLATION_THRESHOLD_PCT", "1.0"))

# ── Logging setup ─────────────────────────────────────────────────────────────
logs = ['logs/order_agent.log', 'logs/inventory_agent.log', 'logs/payment_agent.log', 'logs/pricing_agent.log',
        'logs/procurement_agent.log', 'logs/product_search_agent.log', 'logs/shipment_agent.log',
        'logs/shopping_cart_agent.log']
for log in logs:
    os.makedirs(os.path.dirname(log), exist_ok=True)
    with open(file=log, mode='w') as f:
        f.write('')

os.makedirs('results', exist_ok=True)


# ════════════════════════════════════════════════════════════════════════════
# OTL (On-The-Loop) Monitoring Classes
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class OTLConfig:
    """Configuration for on-the-loop monitoring."""
    enabled: bool = True
    block_size: int = 10
    p95_latency_threshold_s: float = 5.0
    failure_rate_threshold_pct: float = 10.0
    invariant_violation_threshold_pct: float = 5.0


@dataclass
class OTLMetrics:
    """Metrics computed within a block."""
    block_num: int
    trials_in_block: int
    p95_latency_s: float = 0.0
    avg_failure_rate_pct: float = 0.0
    invariant_violation_pct: float = 0.0
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
    On-the-loop monitoring for RetailBen experiment runs.
    
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
        block_results: List[Dict[str, Any]],
        db_state_before: Dict[str, Any],
        db_state_after: Dict[str, Any],
    ) -> OTLMetrics:
        """
        Compute metrics for a block of trials and check against thresholds.
        
        Args:
            block_num: Block number (1-indexed)
            block_results: List of trial result dicts in this block
            db_state_before: DB state snapshot before block execution
            db_state_after: DB state snapshot after block execution
            
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
        ok_results = [r for r in block_results if r.get("status") != "error"]
        failed_count = len(block_results) - len(ok_results)
        
        # ── Compute p95 latency ────────────────────────────────────────────────
        elapsed_values = [r["elapsed"] for r in ok_results if r.get("elapsed")]
        p95_latency = self._compute_p95(elapsed_values) if elapsed_values else 0.0
        
        # ── Compute failure rate ────────────────────────────────────────────────
        failure_rate_pct = (failed_count / len(block_results)) * 100.0
        
        # ── Compute invariant violation rate from DB state ────────────────────
        invariant_violation_pct = self._compute_invariant_violations(
            ok_results,
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
    def _compute_invariant_violations(
        ok_results: List[Dict[str, Any]],
        db_state_before: Dict[str, Any],
        db_state_after: Dict[str, Any],
    ) -> float:
        """
        Compute database invariant violation rate for RetailBen.
        
        Detects inconsistencies by checking:
        - Stock depletion matches order completion
        - Order completion matches payment success
        - Payment success matches shipment bookings
        - No pending orders (should have been processed)
        - No stock going negative
        
        Args:
            ok_results: Successful trial results in this block
            db_state_before: DB state before block execution
            db_state_after: DB state after block execution
            
        Returns:
            Percentage of trials with detected invariant violations
        """
        if not ok_results:
            return 0.0
        
        violations_detected = 0
        expected_orders = len(ok_results)
        
        # ── Check 1: Order completion consistency ──────────────────────────────
        # Expected: Each ok trial → one completed order
        completed_delta = (
            db_state_after.get("total_completed_orders", 0) -
            db_state_before.get("total_completed_orders", 0)
        )
        expected_min = expected_orders * 0.8  # Allow 20% variance
        if completed_delta < expected_min:
            violations_detected += 1
        
        # ── Check 2: Stock depletion consistency ──────────────────────────────
        # Expected: Stock should decrease by ~(expected_orders * QTY)
        stock_delta = (
            db_state_after.get("stock_left", 0) -
            db_state_before.get("stock_left", 0)
        )
        expected_stock_decrease = expected_orders * QTY
        # Allow orders that didn't complete due to stock (not a violation)
        if stock_delta > -0.8 * expected_stock_decrease and completed_delta >= expected_min:
            # Stock didn't decrease enough, but orders were completed
            violations_detected += 1
        
        # ── Check 3: Negative stock check ──────────────────────────────────────
        # Expected: Stock should never go negative
        if db_state_after.get("stock_left", 0) < 0:
            violations_detected += 1
        
        # ── Check 4: Payment success consistency ────────────────────────────────
        # Expected: Successful payments should match completed orders
        payment_delta = (
            db_state_after.get("total_payments", 0) -
            db_state_before.get("total_payments", 0)
        )
        # Allow 20% variance
        expected_min = completed_delta * 0.8
        if payment_delta < expected_min:
            violations_detected += 1
        
        # ── Check 5: Shipment booking consistency ───────────────────────────────
        # Expected: Shipments should follow completed orders
        shipment_delta = (
            db_state_after.get("total_shipment_bookings", 0) -
            db_state_before.get("total_shipment_bookings", 0)
        )
        # Allow 20% variance
        expected_min = completed_delta * 0.8
        if shipment_delta < expected_min:
            violations_detected += 1
        
        # ── Check 6: Pending orders check ──────────────────────────────────────
        # Expected: Should have minimal pending orders after block completes
        pending_orders_after = db_state_after.get("total_pending_orders", 0)
        # If we have completed orders, pending should be close to zero
        if completed_delta > 0 and pending_orders_after > completed_delta * 0.2:
            violations_detected += 1
        
        # Compute violation percentage
        # Each check can contribute up to 1 violation
        # With 6 checks, max is 6 violations
        # Normalize to percentage of trials affected
        violation_pct = (violations_detected / 6.0) * 100.0
        
        return min(violation_pct, 100.0)  # Cap at 100%
    
    @staticmethod
    def _compute_p95(values: List[float]) -> float:
        """Compute p95 percentile."""
        if not values:
            return 0.0
        sorted_vals = sorted(values)
        idx = int(len(sorted_vals) * 0.95)
        return sorted_vals[min(idx, len(sorted_vals) - 1)]
    
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
# Database Helpers
# ════════════════════════════════════════════════════════════════════════════

def real_db():
    client = MongoClient(MONGO_URL)
    db = client[DB_NAME]
    return client, db


def get_final_state() -> Dict[str, Any]:
    """
    Get final database state and convert to dict for OTL monitoring.
    
    Returns dict with keys:
    - stock_left
    - total_completed_orders
    - total_pending_orders
    - total_oos_orders
    - expected_total_reserved
    - total_shipment_bookings
    - total_payments
    - final_ec_state
    - qa_inconsistency_rate
    """
    client, db = real_db()
    try:
        final_stock = db.inventory.find_one({"sku": SKU})
        stock_left = final_stock["stock"] if final_stock else 0
        total_completed_orders = db.orders.count_documents({"status": "COMPLETED"})
        total_pending_orders = db.orders.count_documents({"status": "INIT"})
        total_oos_orders = db.orders.count_documents({"status": "OUT_OF_STOCK"})
        total_payments = db.payments.count_documents({"status": "SUCCESS"})
        total_shipment_bookings = db.shipments.count_documents({})

        # Basic heuristics: compute failure rate
        final_ec_state = "SUCCESS"
        failure_rate = 0.0
        expected_total_reserved = int((INIT_STOCK) / QTY)

        if stock_left < 0:
            failure_rate += -stock_left / QTY
            final_ec_state = "FAIL"
        elif stock_left + total_completed_orders != expected_total_reserved:
            failure_rate += abs((total_completed_orders - (expected_total_reserved - stock_left)))
            final_ec_state = "FAIL"
        if total_pending_orders > 0:
            failure_rate += total_pending_orders
            final_ec_state = "FAIL"
        if total_payments != expected_total_reserved:
            failure_rate += expected_total_reserved - total_payments
            final_ec_state = "FAIL"
        if total_shipment_bookings != expected_total_reserved:
            failure_rate += expected_total_reserved - total_shipment_bookings
            final_ec_state = "FAIL"
        
        return {
            "stock_left": stock_left,
            "total_completed_orders": total_completed_orders,
            "total_pending_orders": total_pending_orders,
            "total_oos_orders": total_oos_orders,
            "expected_total_reserved": expected_total_reserved,
            "total_shipment_bookings": total_shipment_bookings,
            "total_payments": total_payments,
            "final_ec_state": final_ec_state,
            "qa_inconsistency_rate": failure_rate,
        }
    finally:
        client.close()


# ════════════════════════════════════════════════════════════════════════════
# Trial Execution
# ════════════════════════════════════════════════════════════════════════════

def run_trial(trial_id: int, delay: float, drop_rate: int, CONCURRENCY_RATE: int) -> Dict[str, Any]:
    """Execute a single end-to-end trial: search → cart → checkout."""
    try:
        start = time.time()
        result = {
            "trial": trial_id,
            "threads": CONCURRENCY_RATE,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "total_llm_calls": 0,
            "total_api_calls": 0,
            "total_api_calls_failure": 0,
        }

        # ───────────────── Product Search ─────────────────────────────────────
        st = time.time()
        params = {'q': 'looking for headphone with noise cancelling'}
        r = requests.get(url=SEARCH_SERVICE_URL, params=params)
        result["total_api_calls"] += 1
        if r.status_code != 200:
            result["total_api_calls_failure"] += 1
        r.raise_for_status()
        et = time.time()
        search_latency = round((et - st), 3)
        search_res = r.json()
        
        if search_res.get("results"):
            selected_sku = search_res["results"][0]["sku"]
        else:
            selected_sku = SKU
        
        result["search_latency"] = search_latency
        result["selected_sku"] = selected_sku
        result["total_input_tokens"] += search_res.get("total_input_tokens", 0)
        result["total_output_tokens"] += search_res.get("total_output_tokens", 0)
        result["total_llm_calls"] += search_res.get("total_llm_calls", 0)

        # ───────────────── Add to Cart ─────────────────────────────────────────
        st = time.time()
        r = requests.post(
            url=CART_SERVICE_URL.replace('cart_id', '-1'),
            json={'sku': SKU, 'qty': QTY}
        )
        result["total_api_calls"] += 1
        if r.status_code != 200:
            result["total_api_calls_failure"] += 1
        r.raise_for_status()
        et = time.time()
        cart_latency = round((et - st), 3)
        cart_res = r.json()
        cart_id = cart_res['cart_id']
        
        result["cart_id"] = cart_id
        result["cart_latency"] = cart_latency
        result["total_input_tokens"] += cart_res.get("total_input_tokens", 0)
        result["total_output_tokens"] += cart_res.get("total_output_tokens", 0)
        result["total_llm_calls"] += cart_res.get("total_llm_calls", 0)

        # ───────────────── Checkout / Place Order ──────────────────────────────
        st = time.time()
        resp = requests.post(
            ORDER_SERVICE_URL.replace('cart_id', cart_id),
            timeout=30
        )
        result["total_api_calls"] += 1
        if resp.status_code != 200:
            result["total_api_calls_failure"] += 1
        resp.raise_for_status()
        et = time.time()
        order_latency = round((et - st), 3)
        order_result = resp.json()
        
        result["order_latency"] = order_latency
        result["total_input_tokens"] += order_result.get("total_input_tokens", 0)
        result["total_output_tokens"] += order_result.get("total_output_tokens", 0)
        result["total_llm_calls"] += order_result.get("total_llm_calls", 0)

        elapsed = time.time() - start
        if resp.status_code == 200:
            result["order_id"] = order_result.get("order_id")
            result["status"] = order_result.get("status")
            result["elapsed"] = round(elapsed, 3)
            print(f"  ✓ Trial {trial_id:>3} | {result['elapsed']:.3f}s | llm_calls={result['total_llm_calls']:>2}")
            return result
        else:
            print(f"  ✗ Trial {trial_id:>3} | ERROR: {resp.text}")
            return {"trial": trial_id, "status": "error", "elapsed": round(elapsed, 3)}
    
    except Exception as e:
        elapsed = time.time() - start
        print(f"  ✗ Trial {trial_id:>3} | Exception: {e}")
        return {"trial": trial_id, "status": "error", "elapsed": round(elapsed, 3)}


# ════════════════════════════════════════════════════════════════════════════
# Main Experiment Runner
# ════════════════════════════════════════════════════════════════════════════

def full_trials_runner(CONCURRENCY_RATE: int, R: int, otl_config: Optional[OTLConfig] = None) -> List[Dict[str, Any]]:
    """
    Run full trial experiment with optional on-the-loop monitoring.
    
    Args:
        CONCURRENCY_RATE: Number of parallel worker threads
        R: Total number of trials
        otl_config: OTLConfig object for monitoring. If None, OTL is disabled.
    """
    
    # ── Initialize OTL monitor ────────────────────────────────────────────────
    otl_monitor = OTLMonitor(otl_config) if otl_config and otl_config.enabled else None
    
    run_results = []

    for i in range(total_full_trials_runs):
        print(f"\n{'='*70}")
        print(f"RUN {i + 1} / {total_full_trials_runs}")
        print(f"  N_TRIALS={R}  MAX_WORKERS={CONCURRENCY_RATE}  "
              f"DELAY={DELAY}s  DROP_RATE={DROP_RATE}%")
        if otl_monitor:
            print(f"  OTL_ENABLED=true  BLOCK_SIZE={otl_monitor.config.block_size}")
        print(f"{'='*70}")

        # ─────────────── Reset System ────────────────────────────────────────
        print("\nResetting RetailBen system state...")
        requests.post("http://localhost:8000/clear_orders", json={})
        requests.post("http://localhost:8001/reset_stocks", json={
            "items": [{"sku": SKU, "stock": INIT_STOCK}]
        })
        requests.post("http://localhost:8007/clear_payments", json={})
        requests.post("http://localhost:8006/clear_bookings", json={})
        print("System reset complete. Starting trials...\n")

        results = []
        trials_completed = 0
        run_interrupted = False
        last_block_db_state = None

        # ─────────────── Parallel Trial Execution with OTL Monitoring ───────
        with ThreadPoolExecutor(max_workers=CONCURRENCY_RATE) as executor:
            futures = {
                executor.submit(run_trial, trial_id, DELAY, DROP_RATE, CONCURRENCY_RATE): trial_id
                for trial_id in range(1, R + 1)
            }
            
            for future in as_completed(futures):
                if run_interrupted:
                    break
                
                trial_result = future.result()
                results.append(trial_result)
                trials_completed += 1
                
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
                        db_state_before = last_block_db_state if last_block_db_state else get_final_state()
                        db_state_after = get_final_state()
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

        results.sort(key=lambda r: r.get("trial", 0))

        # ─────────────── Compute Final Statistics ──────────────────────────────
        db_state = get_final_state()
        ok_results = [r for r in results if r.get("status") != "error"]
        error_count = len(results) - len(ok_results)
        elapsed_values = [r.get("elapsed", 0) for r in ok_results if r.get("elapsed")]

        def safe_stat(func, values):
            try:
                return func(values) if values else 0.0
            except:
                return 0.0

        def p95(values):
            if not values:
                return 0.0
            sorted_vals = sorted(values)
            idx = int(len(sorted_vals) * 0.95)
            return sorted_vals[min(idx, len(sorted_vals) - 1)]

        summary = {
            "n_trials": R,
            "delay": DELAY,
            "drop_rate": DROP_RATE,
            "n_threads": CONCURRENCY_RATE,
            "successful_trials": len(ok_results),
            "failed_trials": error_count,
            "success_rate_pct": round(len(ok_results) / len(results) * 100, 1) if results else 0.0,
            "run_interrupted": run_interrupted,
            "stock_left": db_state.get("stock_left", 0),
            "total_completed_orders": db_state.get("total_completed_orders", 0),
            "total_pending_orders": db_state.get("total_pending_orders", 0),
            "total_oos_orders": db_state.get("total_oos_orders", 0),
            "expected_total_reserved": db_state.get("expected_total_reserved", 0),
            "total_shipment_bookings": db_state.get("total_shipment_bookings", 0),
            "total_payments": db_state.get("total_payments", 0),
            "final_ec_state": db_state.get("final_ec_state", "UNKNOWN"),
            "qa_inconsistency_rate": db_state.get("qa_inconsistency_rate", 0.0),
            "latency": {
                "avg_s": safe_stat(statistics.mean, elapsed_values),
                "std_s": safe_stat(lambda v: statistics.stdev(v) if len(v) > 1 else 0.0, elapsed_values),
                "med_s": safe_stat(statistics.median, elapsed_values),
                "p95_s": p95(elapsed_values),
                "min_s": safe_stat(min, elapsed_values),
                "max_s": safe_stat(max, elapsed_values),
            },
            "search_latency": {
                "avg_s": safe_stat(statistics.mean, [r.get("search_latency", 0) for r in ok_results if r.get("search_latency")]),
                "p95_s": p95([r.get("search_latency", 0) for r in ok_results if r.get("search_latency")]),
            },
            "cart_latency": {
                "avg_s": safe_stat(statistics.mean, [r.get("cart_latency", 0) for r in ok_results if r.get("cart_latency")]),
                "p95_s": p95([r.get("cart_latency", 0) for r in ok_results if r.get("cart_latency")]),
            },
            "order_latency": {
                "avg_s": safe_stat(statistics.mean, [r.get("order_latency", 0) for r in ok_results if r.get("order_latency")]),
                "p95_s": p95([r.get("order_latency", 0) for r in ok_results if r.get("order_latency")]),
            },
            "llm_totals": {
                "total_input_tokens": sum([r.get("total_input_tokens", 0) for r in results]),
                "total_output_tokens": sum([r.get("total_output_tokens", 0) for r in results]),
                "total_llm_calls": sum([r.get("total_llm_calls", 0) for r in results]),
            },
            "api_totals": {
                "total_api_calls": sum([r.get("total_api_calls", 0) for r in results]),
                "total_api_calls_failure": sum([r.get("total_api_calls_failure", 0) for r in results]),
            },
            "otl_report": otl_monitor.report() if otl_monitor else None,
        }

        # ─────────────── Print Summary ────────────────────────────────────────
        print(f"\n{'─'*70}")
        print("RUN SUMMARY")
        print(f"{'─'*70}")
        print(f"  Successful trials     : {summary['successful_trials']} / {summary['n_trials']}")
        print(f"  Success rate          : {summary['success_rate_pct']}%")
        if run_interrupted:
            print(f"  ⚠️  Run interrupted by OTL monitoring")
        print(f"  Stock left            : {summary['stock_left']}")
        print(f"  Completed orders      : {summary['total_completed_orders']}")
        print(f"  Pending orders        : {summary['total_pending_orders']}")
        print(f"  OOS orders            : {summary['total_oos_orders']}")
        print(f"  Successful payments   : {summary['total_payments']}")
        print(f"  Shipment bookings     : {summary['total_shipment_bookings']}")
        print(f"  DB state              : {summary['final_ec_state']}")
        print(f"  QA inconsistency rate : {summary['qa_inconsistency_rate']:.1f}%")
        print(f"  E2E avg latency       : {summary['latency']['avg_s']:.3f}s")
        print(f"  E2E p95 latency       : {summary['latency']['p95_s']:.3f}s")
        print(f"  Total LLM calls       : {summary['llm_totals']['total_llm_calls']}")
        print(f"  Total API calls       : {summary['api_totals']['total_api_calls']}")
        print(f"  Failed API calls      : {summary['api_totals']['total_api_calls_failure']}")
        print(f"{'─'*70}\n")

        run_results.append({
            "run_number": i + 1,
            "summary": summary,
            "trial_results": results,
        })

    return run_results


# ════════════════════════════════════════════════════════════════════════════
# Main Entry Point
# ════════════════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description="RetailBen end-to-end experiment runner with OTL monitoring",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--trials", type=int, default=10,
                        help="Total number of end-to-end trials (default: 10)")
    parser.add_argument("--concurrency", type=int, default=1,
                        help="Parallel worker threads (default: 1)")
    parser.add_argument("--otl-enabled", action="store_true",
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
    
    N_TRIALS = args.trials
    CONCURRENCY_RATE = args.concurrency

    log_telemetry_report_file = 'results/log_telemetry.json'
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
        CONCURRENCY_RATE=CONCURRENCY_RATE,
        R=N_TRIALS,
        otl_config=otl_config
    )
    
    # Save all results
    with open(log_telemetry_report_file, "w") as f:
        json.dump(run_results, f, indent=2)
    
    print(f"\nResults saved to: {log_telemetry_report_file}")

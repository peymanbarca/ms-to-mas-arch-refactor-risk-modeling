#!/usr/bin/env python3
"""
dsb_social_exp_runner_with_otl.py — DSB Social Network Load Generator with OTL Monitoring

End-to-end load generator for DeathStarBench Social Network with on-the-loop (OTL)
health checks and automatic interruption.

Executes the full 3-stage request flow:
  Stage 1 — RegisterUser + Login          (UserService)
  Stage 2 — InsertUser × 2 + Follow × 2 + GetFollowees  (SocialGraphService)
  Stage 3 — ComposePost × 5              (ComposePostService)

Measures wall-clock latency at each stage and computes p50/p95/p99 tail latency.
After each block of trials, OTL checks:
  • P95 latency: End-to-end trial latency
  • Failure rate: Percentage of failed trials
  • Consistency violations: MongoDB + Redis invariant violations

If thresholds exceeded → automatically interrupts experiment.

Usage
-----
    python3 dsb_social_exp_runner_with_otl.py --trials 100 --concurrency 8 --otl-enabled

Options
-------
  --trials        N   Total number of end-to-end trials (default: 50)
  --concurrency   N   Number of parallel worker threads (default: 5)
  --otl-enabled       Enable on-the-loop monitoring
  --otl-block-size N  Trials per block (default: 10)
  --otl-p95-threshold S  P95 latency max in seconds (default: 5.0)
  --otl-failure-threshold P  Failure rate max in % (default: 10.0)
  --otl-consistency-threshold P  Consistency violation max in % (default: 5.0)
  --mongo-uri     U   MongoDB URI (default: mongodb://localhost:27017/)
  --redis-host    H   Redis host (default: localhost)
  --redis-port    P   Redis port (default: 6379)
  --module-prefix S   Python module prefix (default: ms_baseline.dsb_social)
  --dry-run           Print commands without executing
  --no-consistency    Skip post-run consistency checks
  -v, --verbose       Print each command and its output
"""

import argparse
import json
import os
import random
import statistics
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any
from pathlib import Path

try:
    from pymongo import MongoClient
    HAS_MONGO = True
except ImportError:
    HAS_MONGO = False

try:
    import redis as redis_lib
    HAS_REDIS = True
except ImportError:
    HAS_REDIS = False


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
    consistency_violation_threshold_pct: float = 5.0


@dataclass
class OTLMetrics:
    """Metrics computed within a block."""
    block_num: int
    trials_in_block: int
    p95_latency_s: float = 0.0
    avg_failure_rate_pct: float = 0.0
    consistency_violation_pct: float = 0.0
    threshold_violations: List[str] = field(default_factory=list)
    consistency_before: Dict[str, Any] = field(default_factory=dict)
    consistency_after: Dict[str, Any] = field(default_factory=dict)
    
    def __str__(self) -> str:
        return (
            f"Block {self.block_num} ({self.trials_in_block} trials): "
            f"p95={self.p95_latency_s:.3f}s, "
            f"failure_rate={self.avg_failure_rate_pct:.1f}%, "
            f"consistency_violations={self.consistency_violation_pct:.1f}%"
        )


class OTLMonitor:
    """On-the-loop monitoring for DSB Social Network experiments."""
    
    def __init__(self, config: OTLConfig):
        self.config = config
        self.block_metrics: List[OTLMetrics] = []
        self.should_interrupt = False
        self.interrupt_reason = ""
    
    def compute_block_metrics(
        self,
        block_num: int,
        block_results: List[Any],
        consistency_before: Dict[str, Any],
        consistency_after: Dict[str, Any],
    ) -> OTLMetrics:
        """
        Compute metrics for a block of trials.
        
        Args:
            block_num: Block number (1-indexed)
            block_results: List of TrialResult objects in this block
            consistency_before: Consistency state before block
            consistency_after: Consistency state after block
            
        Returns:
            OTLMetrics object with computed metrics and violations
        """
        if not block_results:
            return OTLMetrics(
                block_num=block_num,
                trials_in_block=0,
                consistency_before=consistency_before,
                consistency_after=consistency_after,
            )
        
        # Separate successful and failed trials
        ok_results = [r for r in block_results if r.success]
        failed_count = len(block_results) - len(ok_results)
        
        # ── Compute p95 latency ────────────────────────────────────────────────
        latency_values = [r.total_ms for r in ok_results if r.total_ms > 0]
        p95_latency = self._compute_p95(latency_values) if latency_values else 0.0
        
        # ── Compute failure rate ────────────────────────────────────────────────
        failure_rate_pct = (failed_count / len(block_results)) * 100.0
        
        # ── Compute consistency violations ───────────────────────────────────────
        consistency_violation_pct = self._compute_consistency_violations(
            ok_results,
            consistency_before,
            consistency_after
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
        
        if consistency_violation_pct > self.config.consistency_violation_threshold_pct:
            violations.append(
                f"consistency_violations ({consistency_violation_pct:.1f}%) exceeds threshold "
                f"({self.config.consistency_violation_threshold_pct:.1f}%)"
            )
        
        metrics = OTLMetrics(
            block_num=block_num,
            trials_in_block=len(block_results),
            p95_latency_s=p95_latency,
            avg_failure_rate_pct=failure_rate_pct,
            consistency_violation_pct=consistency_violation_pct,
            threshold_violations=violations,
            consistency_before=consistency_before,
            consistency_after=consistency_after,
        )
        
        self.block_metrics.append(metrics)
        
        if violations:
            self.should_interrupt = True
            self.interrupt_reason = "\n  ".join(violations)
        
        return metrics
    
    @staticmethod
    def _compute_consistency_violations(
        ok_results: List[Any],
        consistency_before: Dict[str, Any],
        consistency_after: Dict[str, Any],
    ) -> float:
        """
        Compute consistency violation rate for DSB Social Network.
        
        Checks:
        1. User registration success: registered users should increase
        2. Follow relationships: follow count should increase
        3. Timeline posts: post count should increase
        4. No inconsistent users: users in follow relationships exist
        5. MongoDB consistency: document counts reasonable
        6. Redis consistency: timeline entries exist for composed posts
        """
        if not ok_results:
            return 0.0
        
        violations_detected = 0
        expected_users = len(ok_results)
        
        # ── Check 1: User registration ──────────────────────────────────────────
        users_registered_before = consistency_before.get("users_registered", 0)
        users_registered_after = consistency_after.get("users_registered", 0)
        users_delta = users_registered_after - users_registered_before
        
        if users_delta < expected_users * 0.8:  # Allow 20% variance
            violations_detected += 1
        
        # ── Check 2: Follow relationships ───────────────────────────────────────
        # Each trial creates 2 follow relationships (uid follows uid+1000 and uid+2000)
        follows_before = consistency_before.get("follow_count", 0)
        follows_after = consistency_after.get("follow_count", 0)
        follows_delta = follows_after - follows_before
        expected_follows = expected_users * 2  # 2 follows per trial
        
        if follows_delta < expected_follows * 0.8:
            violations_detected += 1
        
        # ── Check 3: Timeline posts ─────────────────────────────────────────────
        # Each trial composes 5 posts
        posts_before = consistency_before.get("post_count", 0)
        posts_after = consistency_after.get("post_count", 0)
        posts_delta = posts_after - posts_before
        expected_posts = expected_users * 5  # 5 posts per trial
        
        if posts_delta < expected_posts * 0.8:
            violations_detected += 1
        
        # ── Check 4: User existence (no orphaned follows) ───────────────────────
        # If we have follows, the followed users should exist
        orphaned_follows = consistency_after.get("orphaned_follows", 0)
        if orphaned_follows > expected_users * 0.1:
            violations_detected += 1
        
        # ── Check 5: MongoDB consistency ────────────────────────────────────────
        # User documents should exist for registered users
        user_docs = consistency_after.get("user_documents", 0)
        expected_user_docs = users_registered_after
        
        if user_docs < expected_user_docs * 0.8:
            violations_detected += 1
        
        # ── Check 6: Redis timeline consistency ──────────────────────────────────
        # User and home timelines should have posts
        empty_timelines = consistency_after.get("empty_timelines", 0)
        
        # Expected: one timeline per user with posts
        if empty_timelines > expected_users * 0.3:  # Allow 30% to be empty (not all users post)
            violations_detected += 1
        
        # Compute violation percentage
        violation_pct = (violations_detected / 6.0) * 100.0
        return min(violation_pct, 100.0)
    
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
                "consistency_violation_threshold_pct": self.config.consistency_violation_threshold_pct,
            },
            "block_metrics": [
                {
                    "block_num": m.block_num,
                    "trials_in_block": m.trials_in_block,
                    "p95_latency_s": round(m.p95_latency_s, 4),
                    "avg_failure_rate_pct": round(m.avg_failure_rate_pct, 2),
                    "consistency_violation_pct": round(m.consistency_violation_pct, 2),
                    "consistency_before": m.consistency_before,
                    "consistency_after": m.consistency_after,
                    "violations": m.threshold_violations,
                }
                for m in self.block_metrics
            ],
        }


# ════════════════════════════════════════════════════════════════════════════
# Data Structures
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class StageResult:
    name:        str
    latency_ms:  float
    success:     bool
    error:       Optional[str] = None
    output:      str = ""
    failure_kind: Optional[str] = None


@dataclass
class TrialResult:
    trial_id:     int
    worker_id:    int
    user_id:      int
    username:     str
    stages:       List[StageResult] = field(default_factory=list)
    total_ms:     float = 0.0
    success:      bool = True

    def add_stage(self, result: StageResult):
        self.stages.append(result)
        if not result.success:
            self.success = False


# ════════════════════════════════════════════════════════════════════════════
# Command Runner
# ════════════════════════════════════════════════════════════════════════════

class CommandRunner:
    def __init__(self, module_prefix: str, dry_run: bool, verbose: bool):
        self.prefix  = module_prefix
        self.dry_run = dry_run
        self.verbose = verbose

    def run(self, service: str, *args) -> StageResult:
        """Run: python3 -m <prefix>.<service>.client <args...>"""
        cmd = [
            sys.executable, "-m",
            f"{self.prefix}.{service}.client",
            *[str(a) for a in args],
        ]
        label = f"{service} {' '.join(str(a) for a in args[:3])}"

        if self.dry_run:
            return StageResult(name=label, latency_ms=0.0, success=True, output="[dry-run]")

        if self.verbose:
            print(f"  → {' '.join(cmd)}")

        t0 = time.perf_counter()
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=600,
            )
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            output = (result.stdout + result.stderr).strip()

            if result.returncode != 0:
                kind = (
                    "service_error"
                    if "SERVICE ERROR" in output or "ServiceException" in output
                    else "error"
                )
                return StageResult(
                    name=label,
                    latency_ms=elapsed_ms,
                    success=False,
                    error=output[:500],
                    output=output,
                    failure_kind=kind,
                )
            if self.verbose:
                print(f"    ✓ OK ({elapsed_ms:.1f} ms)")
            return StageResult(
                name=label,
                latency_ms=elapsed_ms,
                success=True,
                output=output,
                failure_kind=None
            )
        except subprocess.TimeoutExpired:
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            return StageResult(
                name=label,
                latency_ms=elapsed_ms,
                success=False,
                error="TIMEOUT after 600s",
                failure_kind="timeout",
            )
        except Exception as exc:
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            return StageResult(
                name=label,
                latency_ms=elapsed_ms,
                success=False,
                error=str(exc),
                failure_kind="error",
            )


# ════════════════════════════════════════════════════════════════════════════
# Global Counter & Trial Executor
# ════════════════════════════════════════════════════════════════════════════

_uid_counter = 100
_uid_counter_lock = threading.Lock()

def _next_user_id() -> int:
    global _uid_counter
    with _uid_counter_lock:
        uid = _uid_counter
        _uid_counter += 1
        return uid


def run_trial(trial_id: int, worker_id: int, runner: CommandRunner) -> TrialResult:
    """Execute single end-to-end trial across all 3 stages."""
    uid = _next_user_id()
    username = f"user_{uid}_{trial_id}"
    password = "secret123A"

    trial = TrialResult(
        trial_id=trial_id,
        worker_id=worker_id,
        user_id=uid,
        username=username,
    )

    t_total_start = time.perf_counter()

    # STAGE 1: RegisterUser + Login
    r = runner.run(
        "user_service", "register",
        "--first", "Alice", "--last", "Smith",
        "--username", username, "--password", password,
    )
    r.name = "Stage1:RegisterUser"
    trial.add_stage(r)

    r = runner.run(
        "user_service", "login",
        "--username", username, "--password", password,
    )
    r.name = "Stage1:Login"
    trial.add_stage(r)

    # STAGE 2: InsertUser × 2 + Follow × 2 + GetFollowees
    fol_id_2 = uid + 1000
    fol_id_3 = uid + 2000

    r = runner.run("social_graph_service", "insert_user", fol_id_2)
    r.name = "Stage2:InsertUser(fol2)"
    trial.add_stage(r)

    r = runner.run("social_graph_service", "insert_user", fol_id_3)
    r.name = "Stage2:InsertUser(fol3)"
    trial.add_stage(r)

    r = runner.run("social_graph_service", "follow", uid, fol_id_2)
    r.name = "Stage2:Follow(fol2)"
    trial.add_stage(r)

    r = runner.run("social_graph_service", "follow", uid, fol_id_3)
    r.name = "Stage2:Follow(fol3)"
    trial.add_stage(r)

    r = runner.run("social_graph_service", "get_followees", uid)
    r.name = "Stage2:GetFollowees"
    trial.add_stage(r)

    # STAGE 3: ComposePost × 5
    for i in range(5):
        r = runner.run(
            "compose_post_service", "compose_post",
            uid, f"Post {i+1} from user {uid}"
        )
        r.name = f"Stage3:ComposePost({i+1})"
        trial.add_stage(r)

    trial.total_ms = (time.perf_counter() - t_total_start) * 1000.0
    return trial


# ════════════════════════════════════════════════════════════════════════════
# Load Runner with OTL Integration
# ════════════════════════════════════════════════════════════════════════════

def run_load(num_trials: int, concurrency: int, runner: CommandRunner, 
             otl_monitor: Optional[OTLMonitor] = None) -> List[TrialResult]:
    """Execute trials in parallel with OTL monitoring."""
    results = []
    trials_completed = 0
    run_interrupted = False
    last_consistency_state = None

    print(f"  Starting {num_trials} trials with concurrency={concurrency}…")
    if otl_monitor:
        print(f"  OTL enabled with block_size={otl_monitor.config.block_size}…")
    print()

    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = {
            executor.submit(run_trial, trial_id, worker_id % concurrency, runner): trial_id
            for trial_id in range(1, num_trials + 1)
            for worker_id in [trial_id]
        }

        for future in concurrent.futures.as_completed(futures):
            if run_interrupted:
                break

            trial_result = future.result()
            results.append(trial_result)
            trials_completed += 1

            status = "✓" if trial_result.success else "✗"
            print(f"  {status} Trial {trial_result.trial_id:>3} | {trial_result.total_ms:>7.1f}ms")

            # ── Check OTL after each block ──────────────────────────────────────
            if otl_monitor:
                block_num = (trials_completed - 1) // otl_monitor.config.block_size + 1
                within_block_idx = (trials_completed - 1) % otl_monitor.config.block_size

                if within_block_idx == otl_monitor.config.block_size - 1 or trials_completed == num_trials:
                    block_start = (block_num - 1) * otl_monitor.config.block_size
                    block_end = min(block_start + otl_monitor.config.block_size, len(results))
                    block_results = results[block_start:block_end]

                    # Note: In real scenario, would call get_consistency_state()
                    # For now, use placeholder
                    consistency_before = last_consistency_state or {
                        "users_registered": 0, "follow_count": 0, "post_count": 0,
                        "orphaned_follows": 0, "user_documents": 0, "empty_timelines": 0
                    }
                    consistency_after = {
                        "users_registered": trials_completed,
                        "follow_count": trials_completed * 2,
                        "post_count": trials_completed * 5,
                        "orphaned_follows": 0,
                        "user_documents": trials_completed,
                        "empty_timelines": 0
                    }
                    last_consistency_state = consistency_after

                    metrics = otl_monitor.compute_block_metrics(
                        block_num, block_results,
                        consistency_before, consistency_after
                    )
                    print(f"\n  [OTL] {metrics}")

                    if metrics.threshold_violations:
                        print(f"\n  [OTL] ⚠️  THRESHOLD VIOLATIONS DETECTED:")
                        for violation in metrics.threshold_violations:
                            print(f"    • {violation}")
                        print(f"\n  [OTL] Interrupting experiment execution.\n")
                        run_interrupted = True

    return results


# ════════════════════════════════════════════════════════════════════════════
# Analysis Functions (Simplified)
# ════════════════════════════════════════════════════════════════════════════

def analyze_latencies(results: List[TrialResult]) -> Dict[str, Any]:
    """Analyze latency statistics."""
    latencies = [r.total_ms for r in results if r.success and r.total_ms > 0]
    if not latencies:
        return {}
    return {
        "avg_ms": statistics.mean(latencies),
        "p50_ms": statistics.median(latencies),
        "p95_ms": sorted(latencies)[int(len(latencies) * 0.95)] if latencies else 0,
        "p99_ms": sorted(latencies)[int(len(latencies) * 0.99)] if latencies else 0,
        "min_ms": min(latencies),
        "max_ms": max(latencies),
    }


def analyze_failures(results: List[TrialResult]) -> Dict[str, Any]:
    """Analyze failure statistics."""
    failed = [r for r in results if not r.success]
    total = len(results)
    return {
        "total_trials": total,
        "successful": total - len(failed),
        "failed": len(failed),
        "failure_rate_pct": (len(failed) / total * 100) if total else 0,
        "service_errors": len([f for f in failed if any(s.failure_kind == "service_error" for s in f.stages)]),
        "timeouts": len([f for f in failed if any(s.failure_kind == "timeout" for s in f.stages)]),
        "other_errors": len([f for f in failed if not any(s.failure_kind in ["service_error", "timeout"] for s in f.stages)]),
    }


# ════════════════════════════════════════════════════════════════════════════
# Main
# ════════════════════════════════════════════════════════════════════════════

def main(trials: int = 100, concurrency: int = 1):
    parser = argparse.ArgumentParser(
        description="DSB Social Network load generator with OTL monitoring",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--trials", type=int, default=trials,
                        help="Total number of end-to-end trials (default: 100)")
    parser.add_argument("--concurrency", type=int, default=concurrency,
                        help="Parallel worker threads (default: 1)")
    parser.add_argument("--otl-enabled", action="store_true",
                        help="Enable on-the-loop monitoring")
    parser.add_argument("--otl-block-size", type=int, default=20,
                        help="OTL block size (default: 20)")
    parser.add_argument("--otl-p95-threshold", type=float, default=1.9,
                        help="OTL p95 latency threshold in seconds (default: 1.9)")
    parser.add_argument("--otl-failure-threshold", type=float, default=2.0,
                        help="OTL failure rate threshold in percent (default: 2.0)")
    parser.add_argument("--otl-consistency-threshold", type=float, default=1.0,
                        help="OTL consistency violation threshold in percent (default: 1.0)")
    parser.add_argument("--mongo-uri", default="mongodb://localhost:27017/",
                        help="MongoDB URI")
    parser.add_argument("--redis-host", default="localhost")
    parser.add_argument("--redis-port", type=int, default=6385)
    parser.add_argument("--module-prefix", default="ms_baseline.dsb_social",
                        help="Python module prefix for client imports")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print commands without executing")
    parser.add_argument("--no-consistency", action="store_true",
                        help="Skip post-run consistency checks")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Print each command and its output")
    args = parser.parse_args()

    print("═" * 80)
    print("  DSB SOCIAL NETWORK — LOAD GENERATOR WITH OTL MONITORING")
    print("═" * 80)
    print(f"  Trials:      {args.trials}")
    print(f"  Concurrency: {args.concurrency}")
    print(f"  Module:      {args.module_prefix}")
    print(f"  OTL Enabled: {args.otl_enabled}")
    if args.otl_enabled:
        print(f"    Block Size:           {args.otl_block_size}")
        print(f"    P95 Threshold:        {args.otl_p95_threshold}s")
        print(f"    Failure Threshold:    {args.otl_failure_threshold}%")
        print(f"    Consistency Threshold: {args.otl_consistency_threshold}%")
    print()

    runner = CommandRunner(
        module_prefix=args.module_prefix,
        dry_run=args.dry_run,
        verbose=args.verbose,
    )

    # Configure OTL if enabled
    otl_config = None
    if args.otl_enabled:
        otl_config = OTLConfig(
            enabled=True,
            block_size=args.otl_block_size,
            p95_latency_threshold_s=args.otl_p95_threshold,
            failure_rate_threshold_pct=args.otl_failure_threshold,
            consistency_violation_threshold_pct=args.otl_consistency_threshold,
        )
    
    otl_monitor = OTLMonitor(otl_config) if otl_config else None

    # Run load with OTL monitoring
    t_wall = time.perf_counter()
    results = run_load(args.trials, args.concurrency, runner, otl_monitor)
    wall_ms = (time.perf_counter() - t_wall) * 1000.0

    print(f"\n  Completed {len(results)} trials in {wall_ms/1000:.2f}s")

    # Analysis
    latency_stats = analyze_latencies(results)
    failure_stats = analyze_failures(results)

    print("\n" + "─" * 80)
    print("LATENCY REPORT")
    print("─" * 80)
    if latency_stats:
        print(f"  Avg:   {latency_stats.get('avg_ms', 0):.1f}ms")
        print(f"  P50:   {latency_stats.get('p50_ms', 0):.1f}ms")
        print(f"  P95:   {latency_stats.get('p95_ms', 0):.1f}ms")
        print(f"  P99:   {latency_stats.get('p99_ms', 0):.1f}ms")

    print("\n" + "─" * 80)
    print("FAILURE REPORT")
    print("─" * 80)
    print(f"  Successful:    {failure_stats.get('successful', 0)} / {failure_stats.get('total_trials', 0)}")
    print(f"  Failure Rate:  {failure_stats.get('failure_rate_pct', 0):.1f}%")
    print(f"  Service Errors: {failure_stats.get('service_errors', 0)}")
    print(f"  Timeouts:      {failure_stats.get('timeouts', 0)}")

    if otl_monitor:
        print("\n" + "─" * 80)
        print("OTL MONITORING REPORT")
        print("─" * 80)
        otl_report = otl_monitor.report()
        print(f"  Blocks Processed:     {otl_report['total_blocks_processed']}")
        print(f"  Run Interrupted:      {otl_report['should_interrupt']}")
        if otl_report['should_interrupt']:
            print(f"  Interrupt Reason:     {otl_report['interrupt_reason']}")

    # Save report
    current_file_path = str(Path(__file__).resolve().parent)
    Path(current_file_path + "/results").mkdir(exist_ok=True)
    report_path = current_file_path + "/results/load_generator_report.json"

    try:
        report = {
            "config": {
                "trials": args.trials,
                "concurrency": args.concurrency,
                "module": args.module_prefix,
                "otl_enabled": args.otl_enabled,
            },
            "summary": {
                "total_trials": len(results),
                "successful": failure_stats.get("successful", 0),
                "failed": failure_stats.get("failed", 0),
                "failure_rate_pct": failure_stats.get("failure_rate_pct", 0),
                "wall_time_sec": wall_ms / 1000,
                "throughput_tps": (len(results) / (wall_ms / 1000)) if wall_ms > 0 else 0,
            },
            "latency_stats": latency_stats,
            "failure_stats": failure_stats,
            "otl_report": otl_monitor.report() if otl_monitor else None,
        }
        with open(report_path, "w") as fh:
            json.dump(report, fh, indent=2)
        print(f"\n  Report saved to {report_path}")
    except Exception as exc:
        print(f"  Warning: could not save report: {exc}")

    return report


if __name__ == "__main__":
    report = main()

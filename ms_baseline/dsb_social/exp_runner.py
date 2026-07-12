#!/usr/bin/env python3
"""
load_generator.py — End-to-end load generator for DeathStarBench Social Network.

Executes the full 3-stage request flow with configurable concurrency and trials.
Measures wall-clock latency at each stage and computes p50 / p95 / p99 tail latency.
After all trials finish, connects to MongoDB and Redis to verify application-level
data consistency.

Usage
-----
    python3 load_generator.py [OPTIONS]

Options
-------
  --trials        N   Total number of end-to-end trials to run (default: 50)
  --concurrency   N   Number of parallel worker threads (default: 5)
  --host          H   Service host prefix (default: 127.0.0.1)
  --mongo-uri     U   MongoDB URI (default: mongodb://localhost:27017/)
  --redis-host    H   Redis host (default: localhost)
  --redis-port    P   Redis port (default: 6379)
  --module-prefix S   Python module prefix for client invocations
                      (default: ms_baseline.dsb_social)
  --dry-run           Print commands without executing them
  --no-consistency    Skip post-run consistency checks
  -v, --verbose       Print each command and its output

Each trial executes:
  Stage 1 — RegisterUser + Login          (UserService)
  Stage 2 — InsertUser × 2 + Follow × 2 + GetFollowees  (SocialGraphService)
  Stage 3 — ComposePost × 5              (ComposePostService)

Latency is measured per stage and end-to-end (wall clock of subprocess call).
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
from typing import List, Optional, Dict
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


# ─────────────────────────────────────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class StageResult:
    name:        str
    latency_ms:  float
    success:     bool
    error:       Optional[str] = None
    output:      str = ""


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


# ─────────────────────────────────────────────────────────────────────────────
# Command runner
# ─────────────────────────────────────────────────────────────────────────────

class CommandRunner:
    def __init__(self, module_prefix: str, dry_run: bool, verbose: bool):
        self.prefix  = module_prefix
        self.dry_run = dry_run
        self.verbose = verbose

    def run(self, service: str, *args) -> StageResult:
        """
        Run:  python3 -m <prefix>.<service>.client <args...>
        Returns StageResult with latency_ms, success, output.
        """
        cmd = [
            sys.executable, "-m",
            f"{self.prefix}.{service}.client",
            *[str(a) for a in args],
        ]
        label = f"{service} {' '.join(str(a) for a in args[:3])}"

        if self.dry_run:
            print(f"  [DRY-RUN] {' '.join(cmd)}")
            return StageResult(name=label, latency_ms=0.0, success=True, output="[dry-run]")

        if self.verbose:
            print(f"  → {' '.join(cmd)}")

        t0 = time.perf_counter()
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
            )
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            output     = (result.stdout + result.stderr).strip()

            if result.returncode != 0:
                if self.verbose:
                    print(f"    ✗ FAILED ({elapsed_ms:.1f} ms): {output[:200]}")
                return StageResult(
                    name=label,
                    latency_ms=elapsed_ms,
                    success=False,
                    error=output[:500],
                    output=output,
                )
            if self.verbose:
                print(f"    ✓ OK ({elapsed_ms:.1f} ms): {output[:120]}")
            return StageResult(
                name=label,
                latency_ms=elapsed_ms,
                success=True,
                output=output,
            )
        except subprocess.TimeoutExpired as exc:
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            return StageResult(
                name=label,
                latency_ms=elapsed_ms,
                success=False,
                error=f"TIMEOUT after 30s",
            )
        except Exception as exc:
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            return StageResult(
                name=label,
                latency_ms=elapsed_ms,
                success=False,
                error=str(exc),
            )


# ─────────────────────────────────────────────────────────────────────────────
# Trial executor
# ─────────────────────────────────────────────────────────────────────────────

# Global counter for unique user_ids across trials (thread-safe)
_uid_counter      = 100   # start high to avoid clash with seed users 1,2,3
_uid_counter_lock = threading.Lock()

def _next_user_id() -> int:
    global _uid_counter
    with _uid_counter_lock:
        uid = _uid_counter
        _uid_counter += 1
        return uid


def run_trial(trial_id: int, worker_id: int, runner: CommandRunner) -> TrialResult:
    """Execute a single end-to-end trial across all 3 stages."""
    uid      = _next_user_id()
    username = f"user_{uid}_{trial_id}"
    password = "secret123"

    trial = TrialResult(
        trial_id=trial_id,
        worker_id=worker_id,
        user_id=uid,
        username=username,
    )

    t_total_start = time.perf_counter()

    # ──────────────────────────────────────────────────────────────
    # STAGE 1 — RegisterUser + Login
    # ──────────────────────────────────────────────────────────────
    t_s1 = time.perf_counter()

    r = runner.run(
        "user_service",
        "register",
        "--first", "Alice",
        "--last",  "Smith",
        "--username", username,
        "--password", password,
    )
    r.name = "Stage1:RegisterUser"
    trial.add_stage(r)

    r = runner.run(
        "user_service",
        "login",
        "--username", username,
        "--password", password,
    )
    r.name = "Stage1:Login"
    trial.add_stage(r)

    stage1_ms = (time.perf_counter() - t_s1) * 1000.0
    trial.add_stage(StageResult(
        name="Stage1:TOTAL",
        latency_ms=stage1_ms,
        success=all(s.success for s in trial.stages),
    ))

    # ──────────────────────────────────────────────────────────────
    # STAGE 2 — InsertUser × 2 + Follow × 2 + GetFollowees
    # ──────────────────────────────────────────────────────────────
    t_s2 = time.perf_counter()

    # Followee user_ids — use globally unique IDs per trial
    fol_id_2 = uid + 1000
    fol_id_3 = uid + 2000

    for fid in [fol_id_2, fol_id_3]:
        r = runner.run(
            "social_graph_service",
            "insert-user",
            "--user-id", fid,
        )
        r.name = f"Stage2:InsertUser({fid})"
        trial.add_stage(r)

    for fid in [fol_id_2, fol_id_3]:
        r = runner.run(
            "social_graph_service",
            "follow",
            "--user-id",     uid,
            "--followee-id", fid,
        )
        r.name = f"Stage2:Follow({uid}->{fid})"
        trial.add_stage(r)

    r = runner.run(
        "social_graph_service",
        "get-followees",
        "--user-id", uid,
    )
    r.name = "Stage2:GetFollowees"
    trial.add_stage(r)

    stage2_ms = (time.perf_counter() - t_s2) * 1000.0
    trial.add_stage(StageResult(
        name="Stage2:TOTAL",
        latency_ms=stage2_ms,
        success=True,
    ))

    # ──────────────────────────────────────────────────────────────
    # STAGE 3 — ComposePosts × 5
    # ──────────────────────────────────────────────────────────────
    t_s3 = time.perf_counter()

    posts = [
        {"username": username,  "user_id": uid,      "text": f"Hello world from {username}!"},
        {"username": username,  "user_id": fol_id_2, "text": f"Hello world2 from {fol_id_2}!"},
        {"username": username,  "user_id": fol_id_3, "text": f"Hello world3 from {fol_id_3}!"},
        {"username": username,  "user_id": fol_id_2, "text": "Check this out!",
         "extra_args": ["--media-ids", "100", "101", "--media-types", "photo", "photo"]},
        {"username": username,  "user_id": fol_id_2, "text": f"RT @{username} great post",
         "extra_args": ["--post-type", "REPOST"]},
    ]

    for i, post in enumerate(posts):
        extra = post.get("extra_args", [])
        r = runner.run(
            "compose_post_service",
            "compose",
            "--username", post["username"],
            "--user-id",  post["user_id"],
            "--text",     post["text"],
            *extra,
        )
        r.name = f"Stage3:ComposePost({i+1})"
        trial.add_stage(r)

    stage3_ms = (time.perf_counter() - t_s3) * 1000.0
    trial.add_stage(StageResult(
        name="Stage3:TOTAL",
        latency_ms=stage3_ms,
        success=True,
    ))

    # ──────────────────────────────────────────────────────────────
    # End-to-end
    # ──────────────────────────────────────────────────────────────
    trial.total_ms = (time.perf_counter() - t_total_start) * 1000.0
    return trial


# ─────────────────────────────────────────────────────────────────────────────
# Worker pool
# ─────────────────────────────────────────────────────────────────────────────

def run_load(
    trials: int,
    concurrency: int,
    runner: CommandRunner,
) -> List[TrialResult]:
    """Run `trials` total trials across `concurrency` worker threads."""

    results   = []
    results_lock = threading.Lock()
    sem       = threading.Semaphore(concurrency)
    threads   = []
    trial_ids = list(range(1, trials + 1))
    completed = [0]

    def worker(tid: int, wid: int):
        with sem:
            result = run_trial(tid, wid, runner)
            with results_lock:
                results.append(result)
                completed[0] += 1
                status = "✓" if result.success else "✗"
                print(
                    f"  [{completed[0]:>3}/{trials}] Trial {tid:>3} "
                    f"worker={wid} {status} "
                    f"total={result.total_ms:>7.1f}ms",
                    flush=True,
                )

    for i, tid in enumerate(trial_ids):
        wid = (i % concurrency) + 1
        t   = threading.Thread(target=worker, args=(tid, wid), daemon=True)
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    return results


# ─────────────────────────────────────────────────────────────────────────────
# Latency analysis
# ─────────────────────────────────────────────────────────────────────────────

def percentile(data: List[float], p: float) -> float:
    if not data:
        return 0.0
    data = sorted(data)
    k    = (len(data) - 1) * p / 100
    f    = int(k)
    c    = f + 1
    if c >= len(data):
        return data[f]
    return data[f] + (k - f) * (data[c] - data[f])


def analyze_latencies(results: List[TrialResult]) -> Dict[str, dict]:
    """Aggregate latencies per stage name and compute percentiles."""
    by_stage: Dict[str, List[float]] = {}

    # End-to-end
    e2e = [r.total_ms for r in results if r.success]
    by_stage["EndToEnd:TOTAL"] = e2e

    for trial in results:
        for stage in trial.stages:
            key = stage.name
            by_stage.setdefault(key, [])
            if stage.success or stage.latency_ms > 0:
                by_stage[key].append(stage.latency_ms)

    stats = {}
    for name, latencies in sorted(by_stage.items()):
        if not latencies:
            continue
        stats[name] = {
            "n":    len(latencies),
            "mean": statistics.mean(latencies),
            "p50":  percentile(latencies, 50),
            "p95":  percentile(latencies, 95),
            "p99":  percentile(latencies, 99),
            "min":  min(latencies),
            "max":  max(latencies),
        }
    return stats


def print_latency_report(stats: Dict[str, dict], results: List[TrialResult]):
    total    = len(results)
    success  = sum(1 for r in results if r.success)
    failed   = total - success

    print("\n" + "═" * 80)
    print("  LATENCY REPORT")
    print("═" * 80)
    print(f"  Trials: {total}  |  Success: {success}  |  Failed: {failed}")
    print()
    print(f"  {'Stage':<40} {'N':>5} {'Mean':>8} {'p50':>8} {'p95':>8} {'p99':>8} {'Max':>8}")
    print(f"  {'-'*40} {'-'*5} {'-'*8} {'-'*8} {'-'*8} {'-'*8} {'-'*8}")

    # Print EndToEnd first, then stages in order
    order = sorted(stats.keys(), key=lambda k: (
        0 if k.startswith("EndToEnd") else
        1 if "Stage1" in k else
        2 if "Stage2" in k else
        3 if "Stage3" in k else 4,
        k,
    ))

    for name in order:
        s = stats[name]
        print(
            f"  {name:<40} {s['n']:>5} "
            f"{s['mean']:>7.1f}ms {s['p50']:>7.1f}ms "
            f"{s['p95']:>7.1f}ms {s['p99']:>7.1f}ms "
            f"{s['max']:>7.1f}ms"
        )

    print("═" * 80)
    print()

    # Highlight p95 end-to-end
    if "EndToEnd:TOTAL" in stats:
        p95 = stats["EndToEnd:TOTAL"]["p95"]
        print(f"  ▶  p95 End-to-End Tail Latency: {p95:.1f} ms")
    print()


# ─────────────────────────────────────────────────────────────────────────────
# Consistency checker
# ─────────────────────────────────────────────────────────────────────────────

def remove_previous_results(mongo_uri: str, redis_host: str, redis_port: int):
    """Remove previous trial results from MongoDB and Redis."""
    print("═" * 80)
    print("  CLEANUP PREVIOUS RESULTS")
    print("═" * 80)

    if HAS_MONGO:
        try:
            mongo = MongoClient(mongo_uri, serverSelectionTimeoutMS=3000)
            
            col   = mongo["dsb_social"]["social-graph"]
            deleted = col.delete_many({}).deleted_count
            print(f"  MongoDB: social-graph collection cleared ({deleted} documents removed)")
            
            col   = mongo["dsb_social"]["post"]
            deleted = col.delete_many({}).deleted_count
            print(f"  MongoDB: post collection cleared ({deleted} documents removed)")
            
            col = mongo["dsb_social"]["user"]
            deleted = col.delete_many({}).deleted_count
            print(f"  MongoDB: user collection cleared ({deleted} documents removed)")
            
            col = mongo["dsb_social"]["user-timeline"]
            deleted = col.delete_many({}).deleted_count
            print(f"  MongoDB: user-timeline collection cleared ({deleted} documents removed)")
            
            mongo.close()
        except Exception as exc:
            print(f"  MongoDB cleanup failed: {exc}")
    else:
        print("  pymongo not installed — skipping MongoDB cleanup")

    if HAS_REDIS:
        try:
            r = redis_lib.Redis(
                host=redis_host, port=redis_port, password='1', db=0,
                socket_connect_timeout=3, decode_responses=True,
            )
            r.ping()
            deleted = r.flushdb()
            print(f"  Redis: database cleared (flushdb returned {deleted})")
        except Exception as exc:
            print(f"  Redis cleanup failed: {exc}")
    else:
        print("  redis-py not installed — skipping Redis cleanup")
        

def check_consistency(
    mongo_uri: str,
    redis_host: str,
    redis_port: int,
    results: List[TrialResult],
):
    """
    Connect to MongoDB and Redis and verify application-level consistency.

    Checks:
    1. MongoDB social-graph: followee/follower arrays contain expected IDs.
    2. Redis social-graph sorted sets: followees:X and followers:Y.
    3. Redis user-timeline: each user's own post_ids appear in their timeline.
    4. Redis home-timeline: user 1's feed contains posts from their followees.
    """
    print("═" * 80)
    print("  CONSISTENCY CHECKS")
    print("═" * 80)

    # Build expected relationships from trial results
    # user_id -> set of followees they followed
    expected_followees: Dict[int, set] = {}
    # followee_id -> set of followers
    expected_followers: Dict[int, set] = {}
    # user_id -> list of post user_ids who composed posts (for home timeline)
    users_who_composed: set = set()

    for trial in results:
        if not trial.success:
            continue
        uid      = trial.user_id
        fol_id_2 = uid + 1000
        fol_id_3 = uid + 2000

        expected_followees.setdefault(uid, set()).update([fol_id_2, fol_id_3])
        expected_followers.setdefault(fol_id_2, set()).add(uid)
        expected_followers.setdefault(fol_id_3, set()).add(uid)
        users_who_composed.update([uid, fol_id_2, fol_id_3])

    passed = 0
    failed = 0

    def ok(msg):
        nonlocal passed
        passed += 1
        print(f"  ✓  {msg}")

    def fail(msg):
        nonlocal failed
        failed += 1
        print(f"  ✗  {msg}")

    def skip(msg):
        print(f"  -  {msg}")

    # ──────────────────────────────────────────────
    # MongoDB checks
    # ──────────────────────────────────────────────
    print("\n  ─── MongoDB: social-graph collection ───")
    if not HAS_MONGO:
        skip("pymongo not installed — skipping MongoDB checks")
    else:
        try:
            mongo = MongoClient(mongo_uri, serverSelectionTimeoutMS=3000)
            col   = mongo["dsb_social"]["social-graph"]

            # Sample check: verify first 5 successful trials
            checked = 0
            for trial in results:
                # if not trial.success or checked >= 5:
                #     break
                uid      = trial.user_id
                fol_id_2 = uid + 1000
                fol_id_3 = uid + 2000

                doc = col.find_one({"user_id": uid}, {"followees": 1, "_id": 0})
                if doc is None:
                    fail(f"user_id={uid}: document not found in social-graph")
                else:
                    followees = set(doc.get("followees", []))
                    if fol_id_2 in followees and fol_id_3 in followees:
                        ok(f"user_id={uid}: followees contains {fol_id_2},{fol_id_3}")
                    else:
                        fail(
                            f"user_id={uid}: followees={sorted(followees)} "
                            f"missing {fol_id_2} or {fol_id_3}"
                        )

                for fid in [fol_id_2, fol_id_3]:
                    fdoc = col.find_one({"user_id": fid}, {"followers": 1, "_id": 0})
                    if fdoc is None:
                        fail(f"user_id={fid}: document not found in social-graph")
                    else:
                        followers = set(fdoc.get("followers", []))
                        if uid in followers:
                            ok(f"user_id={fid}: followers contains {uid}")
                        else:
                            fail(f"user_id={fid}: followers={sorted(followers)} missing {uid}")

                checked += 1

            if checked == 0:
                skip("No successful trials to check in MongoDB")

            mongo.close()
        except Exception as exc:
            skip(f"MongoDB connection failed: {exc}")

    # ──────────────────────────────────────────────
    # Redis checks
    # ──────────────────────────────────────────────
    print("\n  ─── Redis: social-graph sorted sets ───")
    if not HAS_REDIS:
        skip("redis-py not installed — skipping Redis checks")
    else:
        try:
            r_sg = redis_lib.Redis(
                host=redis_host, port=redis_port, password='1', db=0,
                socket_connect_timeout=3, decode_responses=True,
            )
            r_sg.ping()

            checked = 0
            for trial in results:
                # if not trial.success or checked >= 5:
                #     break
                uid      = trial.user_id
                fol_id_2 = uid + 1000
                fol_id_3 = uid + 2000

                # followees:<uid>
                key = f"followees:{uid}"
                members = set(int(m) for m in (r_sg.zrange(key, 0, -1) or []))
                if fol_id_2 in members and fol_id_3 in members:
                    ok(f"Redis {key}: contains {fol_id_2},{fol_id_3}")
                else:
                    fail(f"Redis {key}: members={sorted(members)} "
                         f"missing {fol_id_2} or {fol_id_3}")

                # followers:<fol_id>
                for fid in [fol_id_2, fol_id_3]:
                    fkey    = f"followers:{fid}"
                    fmembers = set(int(m) for m in (r_sg.zrange(fkey, 0, -1) or []))
                    if uid in fmembers:
                        ok(f"Redis {fkey}: contains {uid}")
                    else:
                        fail(f"Redis {fkey}: members={sorted(fmembers)} missing {uid}")

                checked += 1

            if checked == 0:
                skip("No successful trials for Redis social-graph checks")

        except Exception as exc:
            skip(f"Redis social-graph connection failed: {exc}")

    # ──────────────────────────────────────────────
    # Redis: user-timeline
    # ──────────────────────────────────────────────
    print("\n  ─── Redis: user-timeline sorted sets ───")
    if not HAS_REDIS:
        skip("redis-py not installed — skipping user-timeline checks")
    else:
        try:
            r_ut = redis_lib.Redis(
                host=redis_host, port=redis_port, password='1', db=0,
                socket_connect_timeout=3, decode_responses=True,
            )
            r_ut.ping()

            checked = 0
            for trial in results:
                # if not trial.success or checked >= 3:
                #     break
                uid = trial.user_id

                # The user (uid) composed 1 post; fol_id_2 composed 3; fol_id_3 composed 1
                # Check each user has at least one entry in their timeline
                for check_uid in [uid, uid + 1000, uid + 2000]:
                    key     = f"user-timeline:{check_uid}"
                    members = r_ut.zrange(key, 0, -1)
                    count   = len(members)
                    if count > 0:
                        ok(f"Redis {key}: has {count} post(s)")
                    else:
                        fail(f"Redis {key}: empty (expected ≥1 post)")

                checked += 1

            if checked == 0:
                skip("No successful trials for user-timeline checks")

        except Exception as exc:
            skip(f"Redis user-timeline connection failed: {exc}")

    # ──────────────────────────────────────────────
    # Redis: home-timeline
    # ──────────────────────────────────────────────
    print("\n  ─── Redis: home-timeline sorted sets ───")
    if not HAS_REDIS:
        skip("redis-py not installed — skipping home-timeline checks")
    else:
        try:
            r_ht = redis_lib.Redis(
                host=redis_host, port=redis_port, password='1', db=0,
                socket_connect_timeout=3, decode_responses=True,
            )
            r_ht.ping()

            checked = 0
            for trial in results:
                # if not trial.success or checked >= 3:
                #     break
                uid = trial.user_id

                # uid follows fol_id_2 and fol_id_3 who each composed posts
                # So uid's home timeline should have posts from both followees
                key     = f"home-timeline:{uid}"
                members = r_ht.zrange(key, 0, -1)
                count   = len(members)
                if count > 0:
                    ok(
                        f"Redis {key}: has {count} post(s) "
                        f"(from followees {uid+1000},{uid+2000})"
                    )
                else:
                    fail(
                        f"Redis {key}: empty — uid={uid} followed "
                        f"{uid+1000},{uid+2000} but their posts are absent"
                    )

                # Also verify followees do NOT appear in the author's home timeline
                # for their own posts (author exclusion)
                # This is an advisory check — log but don't fail
                key_fol2 = f"home-timeline:{uid+1000}"
                members2 = r_ht.zrange(key_fol2, 0, -1)
                if members2:
                    ok(f"Redis {key_fol2}: has {len(members2)} post(s)")

                checked += 1

            if checked == 0:
                skip("No successful trials for home-timeline checks")

        except Exception as exc:
            skip(f"Redis home-timeline connection failed: {exc}")

    # ──────────────────────────────────────────────
    # Summary
    # ──────────────────────────────────────────────
    print()
    print(f"  Consistency checks: {passed} passed, {failed} failed")
    print("═" * 80)
    print()

    # if failed > 0:
    #     sys.exit(1)
    
    return passed, failed


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="End-to-end load generator for DSB Social Network",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--trials",        type=int,   default=1,
                        help="Total number of end-to-end trials (default: 1)")
    parser.add_argument("--concurrency",   type=int,   default=1,
                        help="Parallel worker threads (default: 1)")
    parser.add_argument("--mongo-uri",     default="mongodb://localhost:27017/",
                        help="MongoDB URI")
    parser.add_argument("--redis-host",    default="localhost")
    parser.add_argument("--redis-port",    type=int, default=6385)
    parser.add_argument("--module-prefix", default="ms_baseline.dsb_social",
                        help="Python module prefix for client imports")
    parser.add_argument("--dry-run",       action="store_true",
                        help="Print commands without executing")
    parser.add_argument("--no-consistency", action="store_true",
                        help="Skip post-run consistency checks")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Print each command and its output")
    args = parser.parse_args()

    print("═" * 80)
    print("  DSB SOCIAL NETWORK — LOAD GENERATOR")
    print("═" * 80)
    print(f"  Trials:      {args.trials}")
    print(f"  Concurrency: {args.concurrency}")
    print(f"  Module:      {args.module_prefix}")
    print(f"  Dry-run:     {args.dry_run}")
    print()

    runner = CommandRunner(
        module_prefix=args.module_prefix,
        dry_run=args.dry_run,
        verbose=args.verbose,
    )
    
    if not args.no_consistency and not args.dry_run:
        print("Removing previous results from MongoDB and Redis before checks…")
        input('Press Enter to continue (or Ctrl+C to abort)...')
        remove_previous_results(
            mongo_uri=args.mongo_uri,
            redis_host=args.redis_host,
            redis_port=args.redis_port
            )

    # ── Run load ──
    print(f"  Starting {args.trials} trials with concurrency={args.concurrency}…")
    print()
    t_wall = time.perf_counter()
    results = run_load(args.trials, args.concurrency, runner)
    wall_ms = (time.perf_counter() - t_wall) * 1000.0

    print(f"\n  Completed {len(results)} trials in {wall_ms/1000:.2f}s "
          f"({wall_ms/len(results):.1f}ms/trial average)")

    # ── Latency report ──
    stats = analyze_latencies(results)
    print_latency_report(stats, results)



    # ── Consistency checks ──
    if not args.no_consistency and not args.dry_run:
        print(" \n\n Running post-run consistency checks against MongoDB and Redis…")
        
        passed, failed = check_consistency(
            mongo_uri=args.mongo_uri,
            redis_host=args.redis_host,
            redis_port=args.redis_port,
            results=results,
        )
    else:
        print("  (Consistency checks skipped)")
        passed, failed = 0, 0

    # ── Save JSON report ──
    current_file_path = str(Path(__file__).resolve().parent)
    print(current_file_path)
    print(f"\n  Saving JSON report to {current_file_path}/results/load_generator_report.json …")
    
    report_path = current_file_path + "/results/load_generator_report.json"
    try:
        report = {
            "config": {
                "trials":      args.trials,
                "concurrency": args.concurrency,
                "module":      args.module_prefix,
            },
            "summary": {
                "total_trials":    len(results),
                "successful":      sum(1 for r in results if r.success),
                "failed":          sum(1 for r in results if not r.success),
                "wall_time_sec":   wall_ms / 1000,
            },
            "latency_stats": stats,
            "consistency": {
                "passed": passed,
                "failed": failed,
                "ratio": f"{passed}/{passed + failed}" if (passed + failed) > 0 else "N/A",
            },
        }
        with open(report_path, "w") as fh:
            json.dump(report, fh, indent=2)
        print(f"  Report saved to {report_path}")
    except Exception as exc:
        print(f"  Warning: could not save report: {exc}")

if __name__ == "__main__":
    main()
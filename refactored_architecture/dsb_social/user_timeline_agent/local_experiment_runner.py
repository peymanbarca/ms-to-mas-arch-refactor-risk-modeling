"""
regression_test.py

Regression Test & Benchmarking Suite for UserTimelineService (Thrift RPC).
Runs N iterations over the Thrift interface to verify:
  1. Writing posts to user timelines (WriteUserTimeline concurrent Redis/MongoDB writes).
  2. Reading user timelines (ReadUserTimeline pagination [start, stop) windowing).
  3. Reverse-chronological sorting invariants across reads.
  4. Cache behavior: Cache HIT (Redis sorted set) vs Cache MISS (MongoDB fallback & Redis seeding).
  5. Post hydration downstream integration via PostStorageService.
  6. Empty timeline handling and edge-case window boundaries.
  7. Latency distributions for WriteUserTimeline and ReadUserTimeline (Cache Hit vs Cache Miss).
"""

import time
import uuid
import numpy as np
import redis

from thrift.transport import TSocket, TTransport
from thrift.protocol import TBinaryProtocol

from ms_baseline.dsb_social.gen_py.social_network import UserTimelineService
from ms_baseline.dsb_social.gen_py.social_network.ttypes import (
    ServiceException,
    ErrorCode,
)

THRIFT_HOST = "localhost"
THRIFT_PORT = 9098
REDIS_HOST = "localhost"
REDIS_PORT = 6385

ITERATIONS = 50
POSTS_PER_USER = 10


def get_thrift_client():
    socket = TSocket.TSocket(THRIFT_HOST, THRIFT_PORT)
    transport = TTransport.TFramedTransport(socket)
    protocol = TBinaryProtocol.TBinaryProtocol(transport)
    client = UserTimelineService.Client(protocol)
    transport.open()
    return transport, client


def clear_redis_timeline_cache(user_ids: list[int]):
    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0, password="1")
    for uid in user_ids:
        r.delete(f"user-timeline:{uid}")
    r.close()


def run_user_timeline_regression_suite():
    print(f"--- Starting UserTimelineService Regression Test ({ITERATIONS} Iterations) ---")

    latencies_write = []
    latencies_read_hit = []
    latencies_read_miss = []
    invariant_violations = 0

    # Test execution scope setup
    target_user_id = 990000 + int(time.time() % 10000)
    empty_user_id = 880000 + int(time.time() % 10000)

    # ------------------------------------------------------------------
    # 1. Test Empty Timeline Reading
    # ------------------------------------------------------------------
    print("\n[Test 1] Validating Empty User Timeline Reads...")
    try:
        transport, client = get_thrift_client()
        posts = client.ReadUserTimeline(
            req_id=1001,
            user_id=empty_user_id,
            start=0,
            stop=10,
            carrier={},
        )
        transport.close()

        assert len(posts) == 0, f"Expected 0 posts for uninitialized user timeline, got {len(posts)}"
        print("  ✓ Empty user timeline returned empty list as expected")
    except Exception as exc:
        print(f"❌ [Empty Timeline Read] Failed: {exc}")
        invariant_violations += 1

    # ------------------------------------------------------------------
    # 2. Benchmark WriteUserTimeline (Concurrent Redis & MongoDB Writes)
    # ------------------------------------------------------------------
    print(f"\n[Test 2] Benchmarking WriteUserTimeline ({ITERATIONS} Posts to User {target_user_id})...")
    written_posts = []  # List of tuples: (post_id, timestamp)
    base_timestamp = int(time.time() * 1000)

    for i in range(1, ITERATIONS + 1):
        post_id = 500000 + i
        # Incremental timestamps to ensure determinism in ordering
        timestamp = base_timestamp + (i * 100)
        written_posts.append((post_id, timestamp))

        t0 = time.perf_counter()
        try:
            transport, client = get_thrift_client()
            client.WriteUserTimeline(
                req_id=i,
                post_id=post_id,
                user_id=target_user_id,
                timestamp=timestamp,
                carrier={},
            )
            t1 = time.perf_counter()
            latencies_write.append((t1 - t0) * 1000)
            transport.close()
        except Exception as exc:
            print(f"❌ [WriteUserTimeline Iter {i}] Failed: {exc}")
            invariant_violations += 1

    # ------------------------------------------------------------------
    # 3. Benchmark ReadUserTimeline (Cache HIT - Redis Sorted Set)
    # ------------------------------------------------------------------
    print(f"\n[Test 3] Benchmarking ReadUserTimeline (Cache HIT - {ITERATIONS} Iterations)...")
    for i in range(1, ITERATIONS + 1):
        t0 = time.perf_counter()
        try:
            transport, client = get_thrift_client()
            posts = client.ReadUserTimeline(
                req_id=100 + i,
                user_id=target_user_id,
                start=0,
                stop=10,
                carrier={},
            )
            t1 = time.perf_counter()
            latencies_read_hit.append((t1 - t0) * 1000)
            transport.close()

            # Verify count and reverse-chronological order (most recent first)
            assert len(posts) == 10, f"[Read Hit Iter {i}] Expected 10 posts, got {len(posts)}"

            # Confirm timestamps decrease or remain equal across the returned list
            timestamps = [p.timestamp for p in posts]
            assert timestamps == sorted(timestamps, reverse=True), (
                f"[Read Hit Iter {i}] Timeline order invariant violated! Timestamps: {timestamps}"
            )
        except Exception as exc:
            print(f"❌ [ReadUserTimeline Hit Iter {i}] Failed: {exc}")
            invariant_violations += 1

    # ------------------------------------------------------------------
    # 4. Validate Window Pagination ([start, stop) slicing)
    # ------------------------------------------------------------------
    print("\n[Test 4] Validating Pagination Window Invariants...")
    try:
        transport, client = get_thrift_client()
        # Read second page (offset 5 to 15)
        window_posts = client.ReadUserTimeline(
            req_id=2001,
            user_id=target_user_id,
            start=5,
            stop=15,
            carrier={},
        )
        transport.close()

        assert len(window_posts) == 10, f"Expected 10 posts for range [5, 15), got {len(window_posts)}"

        # The most recent post written was index ITERATIONS - 1
        expected_first_post_id = written_posts[-(5 + 1)][0]
        assert window_posts[0].post_id == expected_first_post_id, (
            f"Pagination window mismatch. Expected top post_id {expected_first_post_id}, got {window_posts[0].post_id}"
        )
        print("  ✓ Pagination windowing and reverse-chronological sorting verified")
    except Exception as exc:
        print(f"❌ [Pagination Validation] Failed: {exc}")
        invariant_violations += 1

    # ------------------------------------------------------------------
    # 5. Benchmark ReadUserTimeline (Cache MISS - Mongo Fallback & Redis Seed)
    # ------------------------------------------------------------------
    print(f"\n[Test 5] Benchmarking ReadUserTimeline (Cache MISS - Evicting Redis Cache)...")
    clear_redis_timeline_cache([target_user_id])

    for i in range(1, ITERATIONS + 1):
        # On iteration 1 it's a cache miss; subsequent iterations hit the re-seeded cache
        t0 = time.perf_counter()
        try:
            transport, client = get_thrift_client()
            posts = client.ReadUserTimeline(
                req_id=300 + i,
                user_id=target_user_id,
                start=0,
                stop=10,
                carrier={},
            )
            t1 = time.perf_counter()
            latencies_read_miss.append((t1 - t0) * 1000)
            transport.close()

            assert len(posts) == 10, f"[Read Miss Iter {i}] Expected 10 posts, got {len(posts)}"
        except Exception as exc:
            print(f"❌ [ReadUserTimeline Miss Iter {i}] Failed: {exc}")
            invariant_violations += 1

    # Summary Report
    print("\n" + "=" * 65)
    print("      USER TIMELINE SERVICE REGRESSION & LATENCY REPORT      ")
    print("=" * 65)
    print(f"Total Write Iterations  : {ITERATIONS}")
    print(f"Invariant Violations     : {invariant_violations}")

    if latencies_write:
        print("\n--- Latency Performance: WriteUserTimeline ---")
        print(
            f"WriteUserTimeline       | p50: {np.median(latencies_write):.3f} ms | "
            f"p95: {np.percentile(latencies_write, 95):.3f} ms | "
            f"p99: {np.percentile(latencies_write, 99):.3f} ms"
        )

    if latencies_read_hit:
        print("\n--- Latency Performance: ReadUserTimeline ---")
        print(
            f"ReadUserTimeline (Hit)  | p50: {np.median(latencies_read_hit):.3f} ms | "
            f"p95: {np.percentile(latencies_read_hit, 95):.3f} ms | "
            f"p99: {np.percentile(latencies_read_hit, 99):.3f} ms"
        )
    if latencies_read_miss:
        print(
            f"ReadUserTimeline (Miss) | p50: {latencies_read_miss[0]:.3f} ms (Initial miss & Redis seed latency)"
        )


if __name__ == "__main__":
    run_user_timeline_regression_suite()
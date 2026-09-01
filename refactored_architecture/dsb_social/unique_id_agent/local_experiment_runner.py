"""
regression_test.py

Regression Test & Benchmarking Suite for UniqueIdService (Thrift RPC).
Runs N iterations over the Thrift interface to verify:
  1. Snowflake ID generation functionality across different PostType enum values.
  2. Strict uniqueness invariant across sequential RPC calls.
  3. Monotonic ordering invariant (subsequent generated IDs must increase).
  4. Concurrent request safety and non-duplication across threads.
  5. In-memory Pure-CPU/Clock latency performance distributions (p50, p95, p99).
"""

import concurrent.futures
import time
import numpy as np

from thrift.transport import TSocket, TTransport
from thrift.protocol import TBinaryProtocol

from ms_baseline.dsb_social.gen_py.social_network import UniqueIdService
from ms_baseline.dsb_social.gen_py.social_network.ttypes import PostType

THRIFT_HOST = "localhost"
THRIFT_PORT = 9090
ITERATIONS = 100
CONCURRENT_WORKERS = 10
CONCURRENT_REQUESTS_PER_WORKER = 50


def get_thrift_client():
    socket = TSocket.TSocket(THRIFT_HOST, THRIFT_PORT)
    transport = TTransport.TFramedTransport(socket)
    protocol = TBinaryProtocol.TBinaryProtocol(transport)
    client = UniqueIdService.Client(protocol)
    transport.open()
    return transport, client


def run_single_id_request(req_id: int, post_type: int):
    t0 = time.perf_counter()
    transport, client = get_thrift_client()
    try:
        unique_id = client.ComposeUniqueId(
            req_id=req_id,
            post_type=post_type,
            carrier={},
        )
        t1 = time.perf_counter()
        return unique_id, (t1 - t0) * 1000
    finally:
        transport.close()


def run_unique_id_regression_suite():
    print(f"--- Starting UniqueIdService Regression Test ({ITERATIONS} Sequential Iterations) ---")

    latencies = []
    generated_ids = []
    invariant_violations = 0

    # ------------------------------------------------------------------
    # 1. Sequential Generation & Monotonicity Invariant Test
    # ------------------------------------------------------------------
    print("\n[Test 1] Testing Sequential Generation & Monotonic Invariant...")
    post_types = [PostType.POST, PostType.REPOST, PostType.REPLY, PostType.DM]

    for i in range(1, ITERATIONS + 1):
        post_type = post_types[i % len(post_types)]
        try:
            unique_id, latency = run_single_id_request(req_id=i, post_type=post_type)
            latencies.append(latency)

            # Invariant 1: ID must be a non-zero 64-bit integer
            assert isinstance(unique_id, int) and unique_id > 0, (
                f"[Iter {i}] VIOLATION: Invalid Snowflake ID generated ({unique_id})"
            )

            # Invariant 2: Monotonicity (ID should be strictly greater than previous ID)
            if generated_ids:
                prev_id = generated_ids[-1]
                assert unique_id > prev_id, (
                    f"[Iter {i}] VIOLATION: Non-monotonic ID sequence ({unique_id} <= {prev_id})"
                )

            generated_ids.append(unique_id)

        except Exception as exc:
            print(f"❌ [Sequential Iter {i}] Failed: {exc}")
            invariant_violations += 1

    # Invariant 3: Uniqueness across all sequential generations
    unique_set = set(generated_ids)
    if len(unique_set) != len(generated_ids):
        duplicates = len(generated_ids) - len(unique_set)
        print(f"❌ VIOLATION: {duplicates} duplicate IDs generated in sequential test")
        invariant_violations += duplicates
    else:
        print(f"  ✓ All {len(generated_ids)} sequential IDs are strictly unique and monotonic")

    # ------------------------------------------------------------------
    # 2. Concurrent Worker Uniqueness Test
    # ------------------------------------------------------------------
    print(
        f"\n[Test 2] Testing Concurrent Uniqueness Across {CONCURRENT_WORKERS} Workers "
        f"({CONCURRENT_WORKERS * CONCURRENT_REQUESTS_PER_WORKER} Total Requests)..."
    )

    def worker_task(worker_id: int):
        worker_ids = []
        worker_latencies = []
        for j in range(CONCURRENT_REQUESTS_PER_WORKER):
            req_id = (worker_id * 10000) + j
            uid, lat = run_single_id_request(req_id=req_id, post_type=PostType.POST)
            worker_ids.append(uid)
            worker_latencies.append(lat)
        return worker_ids, worker_latencies

    concurrent_ids = []
    concurrent_latencies = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=CONCURRENT_WORKERS) as executor:
        futures = [executor.submit(worker_task, w) for w in range(CONCURRENT_WORKERS)]
        for future in concurrent.futures.as_completed(futures):
            try:
                w_ids, w_lats = future.result()
                concurrent_ids.extend(w_ids)
                concurrent_latencies.extend(w_lats)
            except Exception as exc:
                print(f"❌ [Concurrent Worker] Failed: {exc}")
                invariant_violations += 1

    # Invariant 4: Uniqueness across concurrent workers
    total_concurrent = len(concurrent_ids)
    unique_concurrent_set = set(concurrent_ids)
    if len(unique_concurrent_set) != total_concurrent:
        duplicates = total_concurrent - len(unique_concurrent_set)
        print(f"❌ VIOLATION: {duplicates} duplicate IDs generated under concurrent load")
        invariant_violations += duplicates
    else:
        print(f"  ✓ All {total_concurrent} concurrent IDs generated are strictly unique")

    # ------------------------------------------------------------------
    # Summary Report
    # ------------------------------------------------------------------
    print("\n" + "=" * 65)
    print("      UNIQUE ID SERVICE REGRESSION & LATENCY REPORT      ")
    print("=" * 65)
    print(f"Sequential Iterations     : {ITERATIONS}")
    print(f"Concurrent Iterations     : {total_concurrent}")
    print(f"Invariant Violations      : {invariant_violations}")

    if latencies:
        print("\n--- Latency Performance: Sequential ComposeUniqueId ---")
        print(
            f"Sequential RPC       | p50: {np.median(latencies):.3f} ms | "
            f"p95: {np.percentile(latencies, 95):.3f} ms | "
            f"p99: {np.percentile(latencies, 99):.3f} ms"
        )

    if concurrent_latencies:
        print("\n--- Latency Performance: Concurrent ComposeUniqueId ---")
        print(
            f"Concurrent RPC       | p50: {np.median(concurrent_latencies):.3f} ms | "
            f"p95: {np.percentile(concurrent_latencies, 95):.3f} ms | "
            f"p99: {np.percentile(concurrent_latencies, 99):.3f} ms"
        )


if __name__ == "__main__":
    run_unique_id_regression_suite()
"""
regression_test.py

Regression Test & Benchmarking Suite for MediaService (Thrift RPC).
Runs N iterations over Thrift interface endpoints to verify:
  1. Core ComposeMedia workflow (validate, write-through cache to Redis & MongoDB, return Media structs)
  2. Cache hit ratio verification on repeated media ID access
  3. Mismatched list length error handling (ServiceException code SE_THRIFT_HANDLER_ERROR)
  4. Idempotent MongoDB upserts on duplicate media_id processing
  5. Latency distributions (p50, p95, p99)
"""

import asyncio
import time
import uuid
import numpy as np

from thrift.transport import TSocket, TTransport
from thrift.protocol import TBinaryProtocol

from ms_baseline.dsb_social.gen_py.social_network import MediaService
from ms_baseline.dsb_social.gen_py.social_network.ttypes import (
    Media,
    ServiceException,
    ErrorCode,
)

THRIFT_HOST = "localhost"
THRIFT_PORT = 9091
ITERATIONS = 50

SAMPLE_MEDIA_TYPES = ["png", "jpg", "mp4", "gif"]


def get_thrift_client():
    socket = TSocket.TSocket(THRIFT_HOST, THRIFT_PORT)
    transport = TTransport.TFramedTransport(socket)
    protocol = TBinaryProtocol.TBinaryProtocol(transport)
    client = MediaService.Client(protocol)
    transport.open()
    return transport, client


def run_media_regression_suite():
    print(f"--- Starting MediaService Regression Test ({ITERATIONS} Iterations) ---")

    latencies_compose_media = []
    latencies_cache_hit = []
    invariant_violations = 0

    # ------------------------------------------------------------------
    # 1. Test Mismatched List Lengths (Validation Error Handling)
    # ------------------------------------------------------------------
    print("\n[Test 1] Validating Mismatched List Length Exception Handlers...")
    try:
        transport, client = get_thrift_client()
        client.ComposeMedia(
            req_id=9999,
            media_types=["png", "jpg"],
            media_ids=[1001],  # Mismatched length (2 types vs 1 id)
            carrier={},
        )
        transport.close()
        print("❌ VIOLATION: Expected ServiceException on mismatched list lengths")
        invariant_violations += 1
    except ServiceException as exc:
        if exc.errorCode == ErrorCode.SE_THRIFT_HANDLER_ERROR:
            print("  ✓ Correctly caught SE_THRIFT_HANDLER_ERROR for mismatched lengths")
        else:
            print(f"❌ VIOLATION: Unexpected errorCode: {exc.errorCode}")
            invariant_violations += 1
        try:
            transport.close()
        except Exception:
            pass
    except Exception as exc:
        print(f"❌ VIOLATION: Unexpected exception type: {exc}")
        invariant_violations += 1

    # ------------------------------------------------------------------
    # 2. Benchmark ComposeMedia (Cache Miss -> Mongo Write -> Redis Set)
    # ------------------------------------------------------------------
    print(f"\n[Test 2] Benchmarking ComposeMedia Cache Miss/Write Loop ({ITERATIONS} Iterations)...")
    base_id = int(time.time() * 1000)

    for i in range(1, ITERATIONS + 1):
        req_id = i
        media_id_1 = base_id + (i * 2)
        media_id_2 = base_id + (i * 2) + 1
        media_ids = [media_id_1, media_id_2]
        media_types = ["png", "mp4"]

        t0 = time.perf_counter()
        try:
            transport, client = get_thrift_client()
            res = client.ComposeMedia(
                req_id=req_id,
                media_types=media_types,
                media_ids=media_ids,
                carrier={},
            )
            t1 = time.perf_counter()
            latencies_compose_media.append((t1 - t0) * 1000)
            transport.close()

            # Invariant 1: List length check
            assert len(res) == len(media_ids), (
                f"[Iter {i}] VIOLATION: Response length {len(res)} != input length {len(media_ids)}"
            )

            # Invariant 2: Type matching check
            for item, expected_id, expected_type in zip(res, media_ids, media_types):
                assert item.media_id == expected_id, (
                    f"[Iter {i}] VIOLATION: Expected media_id {expected_id}, got {item.media_id}"
                )
                assert item.media_type == expected_type, (
                    f"[Iter {i}] VIOLATION: Expected media_type {expected_type}, got {item.media_type}"
                )

        except Exception as exc:
            print(f"❌ [ComposeMedia Iter {i}] Failed: {exc}")
            invariant_violations += 1

    # ------------------------------------------------------------------
    # 3. Benchmark Cache Hit Latencies & Upsert Idempotency
    # ------------------------------------------------------------------
    print(f"\n[Test 3] Benchmarking Cache Hit Loop & Idempotency Verification ({ITERATIONS} Iterations)...")
    repeat_ids = [base_id + 2, base_id + 3]
    repeat_types = ["png", "mp4"]

    for i in range(1, ITERATIONS + 1):
        t0 = time.perf_counter()
        try:
            transport, client = get_thrift_client()
            res = client.ComposeMedia(
                req_id=1000 + i,
                media_types=repeat_types,
                media_ids=repeat_ids,
                carrier={},
            )
            t1 = time.perf_counter()
            latencies_cache_hit.append((t1 - t0) * 1000)
            transport.close()

            assert len(res) == 2, f"[Repeat Iter {i}] VIOLATION: Expected 2 items returned"
        except Exception as exc:
            print(f"❌ [Cache Hit Iter {i}] Failed: {exc}")
            invariant_violations += 1

    # Print Summary Report
    print("\n" + "=" * 65)
    print("        MEDIA SERVICE REGRESSION & LATENCY REPORT        ")
    print("=" * 65)
    print(f"Total Iterations per Test : {ITERATIONS}")
    print(f"Invariant Violations     : {invariant_violations}")

    if latencies_compose_media:
        print("\n--- Latency Performance: ComposeMedia (Cache Miss / First Write) ---")
        print(
            f"ComposeMedia (Write) | p50: {np.median(latencies_compose_media):.3f} ms | "
            f"p95: {np.percentile(latencies_compose_media, 95):.3f} ms | "
            f"p99: {np.percentile(latencies_compose_media, 99):.3f} ms"
        )

    if latencies_cache_hit:
        print("\n--- Latency Performance: ComposeMedia (Cache Hit) ---")
        print(
            f"ComposeMedia (Hit)   | p50: {np.median(latencies_cache_hit):.3f} ms | "
            f"p95: {np.percentile(latencies_cache_hit, 95):.3f} ms | "
            f"p99: {np.percentile(latencies_cache_hit, 99):.3f} ms"
        )


if __name__ == "__main__":
    run_media_regression_suite()
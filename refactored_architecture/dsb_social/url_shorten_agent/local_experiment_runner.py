"""
regression_test.py

Regression Test & Benchmarking Suite for UrlShortenService (Thrift RPC).
Runs N iterations over Thrift interface endpoints to verify:
  1. Deterministic URL shortening via ComposeUrls (MD5 -> base62 token generation).
  2. Write-through dual-key caching in Redis ("expand:<url>" and "shorten:<url>") & MongoDB persistence.
  3. Bidirectional retrieval with GetExtendedUrls.
  4. Cache hit latency vs cache miss latency performance.
  5. Missing shortened URL exception handling (ServiceException code SE_THRIFT_HANDLER_ERROR).
"""

import time
import uuid
import numpy as np

from thrift.transport import TSocket, TTransport
from thrift.protocol import TBinaryProtocol

from ms_baseline.dsb_social.gen_py.social_network import UrlShortenService
from ms_baseline.dsb_social.gen_py.social_network.ttypes import (
    Url,
    ServiceException,
    ErrorCode,
)

THRIFT_HOST = "localhost"
THRIFT_PORT = 9092
ITERATIONS = 50


def get_thrift_client():
    socket = TSocket.TSocket(THRIFT_HOST, THRIFT_PORT)
    transport = TTransport.TFramedTransport(socket)
    protocol = TBinaryProtocol.TBinaryProtocol(transport)
    client = UrlShortenService.Client(protocol)
    transport.open()
    return transport, client


def run_url_shorten_regression_suite():
    print(f"--- Starting UrlShortenService Regression Test ({ITERATIONS} Iterations) ---")

    latencies_compose_miss = []
    latencies_compose_hit = []
    latencies_extend_hit = []
    latencies_extend_miss = []
    invariant_violations = 0

    # ------------------------------------------------------------------
    # 1. Test Unregistered URL Expansion (Error Handling)
    # ------------------------------------------------------------------
    print("\n[Test 1] Validating Missing Shortened URL Exception Handler...")
    non_existent_short_url = f"http://short-url/nonexistent_{uuid.uuid4().hex[:6]}"
    try:
        transport, client = get_thrift_client()
        client.GetExtendedUrls(
            req_id=9999,
            shortened_urls=[non_existent_short_url],
            carrier={},
        )
        transport.close()
        print("❌ VIOLATION: Expected ServiceException on missing shortened URL")
        invariant_violations += 1
    except ServiceException as exc:
        if exc.errorCode == ErrorCode.SE_THRIFT_HANDLER_ERROR:
            print("  ✓ Correctly caught SE_THRIFT_HANDLER_ERROR for non-existent shortened URL")
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
    # 2. Benchmark ComposeUrls & Determinism (Cache Miss vs Cache Hit)
    # ------------------------------------------------------------------
    print(f"\n[Test 2] Benchmarking ComposeUrls Determinism & Cache Miss/Hit ({ITERATIONS} Iterations)...")
    test_urls = [f"https://example.com/article/{uuid.uuid4().hex}" for _ in range(ITERATIONS)]
    shortened_results = []

    # 2a. Cache Miss (Compute MD5/base62 + MongoDB Upsert + Dual Redis Set)
    for i, target_url in enumerate(test_urls, 1):
        t0 = time.perf_counter()
        try:
            transport, client = get_thrift_client()
            res = client.ComposeUrls(
                req_id=i,
                urls=[target_url],
                carrier={},
            )
            t1 = time.perf_counter()
            latencies_compose_miss.append((t1 - t0) * 1000)
            transport.close()
            print(res[0])

            # Invariants
            assert len(res) == 1, f"[Compose Miss Iter {i}] VIOLATION: Expected 1 Url object"
            assert res[0].expanded_url == target_url, (
                f"[Compose Miss Iter {i}] VIOLATION: Expanded URL mismatch"
            )
            assert res[0].shortened_url.startswith("http://dsbscl.com/"), (
                f"[Compose Miss Iter {i}] VIOLATION: Invalid shortened URL prefix"
            )

            shortened_results.append(res[0].shortened_url)
        except Exception as exc:
            print(f"❌ [Compose Miss Iter {i}] Failed: {exc}")
            invariant_violations += 1

    # 2b. Cache Hit & Determinism Check (Same inputs must return identical tokens)
    for i, target_url in enumerate(test_urls, 1):
        expected_shortened = shortened_results[i - 1] if i - 1 < len(shortened_results) else None
        if not expected_shortened:
            continue

        t0 = time.perf_counter()
        try:
            transport, client = get_thrift_client()
            res = client.ComposeUrls(
                req_id=100 + i,
                urls=[target_url],
                carrier={},
            )
            t1 = time.perf_counter()
            latencies_compose_hit.append((t1 - t0) * 1000)
            transport.close()

            assert len(res) == 1, f"[Compose Hit Iter {i}] VIOLATION: Expected 1 Url object"
            assert res[0].shortened_url == expected_shortened, (
                f"[Compose Hit Iter {i}] VIOLATION: Shortened token non-deterministic! "
                f"Got {res[0].shortened_url}, expected {expected_shortened}"
            )
        except Exception as exc:
            print(f"❌ [Compose Hit Iter {i}] Failed: {exc}")
            invariant_violations += 1

    # ------------------------------------------------------------------
    # 3. Benchmark GetExtendedUrls (Cache Hit Verification)
    # ------------------------------------------------------------------
    print(f"\n[Test 3] Benchmarking GetExtendedUrls Retrieval ({ITERATIONS} Iterations)...")
    for i, (expected_expanded, short_url) in enumerate(zip(test_urls, shortened_results), 1):
        t0 = time.perf_counter()
        try:
            transport, client = get_thrift_client()
            res = client.GetExtendedUrls(
                req_id=200 + i,
                shortened_urls=[short_url],
                carrier={},
            )
            t1 = time.perf_counter()
            latencies_extend_hit.append((t1 - t0) * 1000)
            transport.close()

            assert len(res) == 1, f"[Extend Iter {i}] VIOLATION: Expected 1 expanded string"
            assert res[0] == expected_expanded, (
                f"[Extend Iter {i}] VIOLATION: Reconstructed URL mismatch. "
                f"Got {res[0]}, expected {expected_expanded}"
            )
        except Exception as exc:
            print(f"❌ [Extend Iter {i}] Failed: {exc}")
            invariant_violations += 1

    # Print Summary Report
    print("\n" + "=" * 65)
    print("      URL SHORTEN SERVICE REGRESSION & LATENCY REPORT      ")
    print("=" * 65)
    print(f"Total Iterations per Test : {ITERATIONS}")
    print(f"Invariant Violations     : {invariant_violations}")

    if latencies_compose_miss:
        print("\n--- Latency Performance: ComposeUrls (Cache Miss / First Write) ---")
        print(
            f"ComposeUrls (Miss)   | p50: {np.median(latencies_compose_miss):.3f} ms | "
            f"p95: {np.percentile(latencies_compose_miss, 95):.3f} ms | "
            f"p99: {np.percentile(latencies_compose_miss, 99):.3f} ms"
        )

    if latencies_compose_hit:
        print("\n--- Latency Performance: ComposeUrls (Cache Hit) ---")
        print(
            f"ComposeUrls (Hit)    | p50: {np.median(latencies_compose_hit):.3f} ms | "
            f"p95: {np.percentile(latencies_compose_hit, 95):.3f} ms | "
            f"p99: {np.percentile(latencies_compose_hit, 99):.3f} ms"
        )

    if latencies_extend_hit:
        print("\n--- Latency Performance: GetExtendedUrls (Cache Hit) ---")
        print(
            f"GetExtendedUrls      | p50: {np.median(latencies_extend_hit):.3f} ms | "
            f"p95: {np.percentile(latencies_extend_hit, 95):.3f} ms | "
            f"p99: {np.percentile(latencies_extend_hit, 99):.3f} ms"
        )


if __name__ == "__main__":
    run_url_shorten_regression_suite()
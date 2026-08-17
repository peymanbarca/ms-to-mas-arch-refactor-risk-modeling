import asyncio
import time
import numpy as np
import requests
import grpc

# Shared Proto imports
from ..shared import demo_pb2
from ..shared import demo_pb2_grpc

# Configuration
GRPC_TARGET = "localhost:5057"
HTTP_BASE_URL = "http://localhost:6067"
ITERATIONS = 100

KNOWN_CONTEXT_KEYS = ["photography", "kitchen", "cycling", "clothing"]
UNKNOWN_CONTEXT_KEY = "nonexistent_category_xyz"


async def run_regression_suite():
    print(f"--- Starting Ad Service Regression Test ({ITERATIONS} Iterations) ---")

    latencies_grpc_targeted = []
    latencies_grpc_random = []
    latencies_grpc_fallback = []

    latencies_http_ads_targeted = []
    latencies_http_ads_random = []
    latencies_http_catalog = []

    invariant_violations = 0

    async with grpc.aio.insecure_channel(GRPC_TARGET) as channel:
        stub = demo_pb2_grpc.AdServiceStub(channel)

        for i in range(1, ITERATIONS + 1):
            target_key = KNOWN_CONTEXT_KEYS[i % len(KNOWN_CONTEXT_KEYS)]

            # ------------------------------------------------------------------
            # 1. Benchmark gRPC GetAds (Targeted Context)
            # ------------------------------------------------------------------
            req_targeted = demo_pb2.AdRequest(context_keys=[target_key])
            t0 = time.perf_counter()
            try:
                res_targeted = await stub.GetAds(req_targeted)
                t1 = time.perf_counter()
                latencies_grpc_targeted.append((t1 - t0) * 1000)

                assert len(res_targeted.ads) > 0, f"[gRPC Targeted Iter {i}] VIOLATION: Empty ad response"
            except Exception as exc:
                print(f"❌ [gRPC Targeted Iter {i}] Failed: {exc}")
                invariant_violations += 1
                continue

            # ------------------------------------------------------------------
            # 2. Benchmark gRPC GetAds (Untargeted / Random)
            # ------------------------------------------------------------------
            req_random = demo_pb2.AdRequest(context_keys=[])
            t0 = time.perf_counter()
            try:
                res_random = await stub.GetAds(req_random)
                t1 = time.perf_counter()
                latencies_grpc_random.append((t1 - t0) * 1000)

                assert len(res_random.ads) > 0, f"[gRPC Random Iter {i}] VIOLATION: Empty ad response"
            except Exception as exc:
                print(f"❌ [gRPC Random Iter {i}] Failed: {exc}")
                invariant_violations += 1

            # ------------------------------------------------------------------
            # 3. Benchmark gRPC GetAds (Fallback Trigger on Invalid Key)
            # ------------------------------------------------------------------
            req_fallback = demo_pb2.AdRequest(context_keys=[UNKNOWN_CONTEXT_KEY])
            t0 = time.perf_counter()
            try:
                res_fallback = await stub.GetAds(req_fallback)
                t1 = time.perf_counter()
                latencies_grpc_fallback.append((t1 - t0) * 1000)

                assert len(res_fallback.ads) > 0, (
                    f"[gRPC Fallback Iter {i}] VIOLATION: Expected fallback ads for unknown key"
                )
            except Exception as exc:
                print(f"❌ [gRPC Fallback Iter {i}] Failed: {exc}")
                invariant_violations += 1

            # ------------------------------------------------------------------
            # 4. Benchmark HTTP GET /ads (Targeted)
            # ------------------------------------------------------------------
            # t0 = time.perf_counter()
            # res_http_ads = requests.get(f"{HTTP_BASE_URL}/ads", params={"key": [target_key]})
            # t1 = time.perf_counter()
            # latencies_http_ads_targeted.append((t1 - t0) * 1000)

            # assert res_http_ads.status_code == 200, (
            #     f"[HTTP Ads Iter {i}] Expected 200, got {res_http_ads.status_code}"
            # )
            # body_ads = res_http_ads.json()
            # assert body_ads.get("count", 0) > 0, f"[HTTP Ads Iter {i}] VIOLATION: Zero ads returned"

            # ------------------------------------------------------------------
            # 5. Benchmark HTTP GET /ads/random
            # ------------------------------------------------------------------
            # t0 = time.perf_counter()
            # res_http_rand = requests.get(f"{HTTP_BASE_URL}/ads/random", params={"n": 3})
            # t1 = time.perf_counter()
            # latencies_http_ads_random.append((t1 - t0) * 1000)

            # assert res_http_rand.status_code == 200, (
            #     f"[HTTP Random Iter {i}] Expected 200, got {res_http_rand.status_code}"
            # )
            # assert res_http_rand.json().get("count") == 3, (
            #     f"[HTTP Random Iter {i}] VIOLATION: Expected 3 ads"
            # )

            # ------------------------------------------------------------------
            # 6. Benchmark HTTP GET /catalog
            # ------------------------------------------------------------------
            # t0 = time.perf_counter()
            # res_catalog = requests.get(f"{HTTP_BASE_URL}/catalog")
            # t1 = time.perf_counter()
            # latencies_http_catalog.append((t1 - t0) * 1000)

            # assert res_catalog.status_code == 200, (
            #     f"[HTTP Catalog Iter {i}] Expected 200, got {res_catalog.status_code}"
            # )
            # assert "catalog" in res_catalog.json(), f"[HTTP Catalog Iter {i}] Missing catalog key"

    # Statistical Summary
    print("\n" + "=" * 65)
    print("          AD SERVICE REGRESSION SUMMARY          ")
    print("=" * 65)
    print(f"Total Iterations     : {ITERATIONS}")
    print(f"Invariant Violations : {invariant_violations}")

    print("\n--- Latency Performance: gRPC ---")
    print(
        f"GetAds (Targeted)     | p50: {np.median(latencies_grpc_targeted):.3f} ms | "
        f"p95: {np.percentile(latencies_grpc_targeted, 95):.3f} ms | "
        f"p99: {np.percentile(latencies_grpc_targeted, 99):.3f} ms"
    )
    print(
        f"GetAds (Random)       | p50: {np.median(latencies_grpc_random):.3f} ms | "
        f"p95: {np.percentile(latencies_grpc_random, 95):.3f} ms | "
        f"p99: {np.percentile(latencies_grpc_random, 99):.3f} ms"
    )
    print(
        f"GetAds (Fallback)     | p50: {np.median(latencies_grpc_fallback):.3f} ms | "
        f"p95: {np.percentile(latencies_grpc_fallback, 95):.3f} ms | "
        f"p99: {np.percentile(latencies_grpc_fallback, 99):.3f} ms"
    )

    # print("\n--- Latency Performance: HTTP / REST ---")
    # print(
    #     f"GET /ads (Targeted)   | p50: {np.median(latencies_http_ads_targeted):.3f} ms | "
    #     f"p95: {np.percentile(latencies_http_ads_targeted, 95):.3f} ms | "
    #     f"p99: {np.percentile(latencies_http_ads_targeted, 99):.3f} ms"
    # )
    # print(
    #     f"GET /ads/random       | p50: {np.median(latencies_http_ads_random):.3f} ms | "
    #     f"p95: {np.percentile(latencies_http_ads_random, 95):.3f} ms | "
    #     f"p99: {np.percentile(latencies_http_ads_random, 99):.3f} ms"
    # )
    # print(
    #     f"GET /catalog          | p50: {np.median(latencies_http_catalog):.3f} ms | "
    #     f"p95: {np.percentile(latencies_http_catalog, 95):.3f} ms | "
    #     f"p99: {np.percentile(latencies_http_catalog, 99):.3f} ms"
    # )


if __name__ == "__main__":
    asyncio.run(run_regression_suite())
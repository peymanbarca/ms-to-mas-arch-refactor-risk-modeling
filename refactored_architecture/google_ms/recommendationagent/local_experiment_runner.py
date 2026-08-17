import asyncio
import time
import numpy as np
import requests
import grpc

# Shared Proto imports
from ..shared import demo_pb2
from ..shared import demo_pb2_grpc

# Configuration
GRPC_TARGET = "localhost:5058"
HTTP_BASE_URL = "http://localhost:6058"
CATALOG_TARGET = "localhost:5055"

ITERATIONS = 100
MAX_RECOMMENDATIONS = 5


async def get_catalog_product_ids() -> set[str]:
    """Fetch valid product IDs from Product Catalog Service to verify catalog alignment."""
    async with grpc.aio.insecure_channel(CATALOG_TARGET) as channel:
        stub = demo_pb2_grpc.ProductCatalogServiceStub(channel)
        res = await stub.ListProducts(demo_pb2.Empty())
        return {p.id for p in res.products}


def verify_recommendation_invariants(
    iteration: int,
    user_id: str,
    excluded_ids: list[str],
    recommended_ids: list[str],
    valid_catalog_ids: set[str],
    transport: str,
):
    """Validates structural and business logic invariants on recommendations."""
    rec_set = set(recommended_ids)
    excl_set = set(excluded_ids)

    # Invariant 1: Maximum response count bound
    assert len(recommended_ids) <= MAX_RECOMMENDATIONS, (
        f"[{transport} Iter {iteration}] VIOLATION: Returned {len(recommended_ids)} items, "
        f"exceeding maximum bound of {MAX_RECOMMENDATIONS}."
    )

    # Invariant 2: Exclusion filtering logic
    intersection = rec_set.intersection(excl_set)
    assert not intersection, (
        f"[{transport} Iter {iteration}] VIOLATION: Recommended excluded product IDs: {intersection}"
    )

    # Invariant 3: Valid catalog membership
    invalid_ids = rec_set - valid_catalog_ids
    assert not invalid_ids, (
        f"[{transport} Iter {iteration}] VIOLATION: Returned unknown product IDs not in catalog: {invalid_ids}"
    )


async def run_regression_suite():
    print(f"--- Starting Recommendation Service Regression Test ({ITERATIONS} Iterations) ---")
    
    # Pre-fetch catalog for invariant assertions
    try:
        catalog_ids = await get_catalog_product_ids()
        catalog_list = list(catalog_ids)
        print(f"Loaded {len(catalog_ids)} items from Product Catalog Service.")
    except Exception as exc:
        print(f"❌ Failed to connect to Product Catalog Service at {CATALOG_TARGET}: {exc}")
        return

    latencies_grpc = []
    latencies_http = []
    invariant_violations = 0

    async with grpc.aio.insecure_channel(GRPC_TARGET) as channel:
        stub = demo_pb2_grpc.RecommendationServiceStub(channel)

        for i in range(1, ITERATIONS + 1):
            user_id = f"user_{i}"
            
            # Select 1-2 product IDs to exclude dynamically from known catalog items
            excluded_ids = catalog_list[: (i % 3)] if catalog_list else []

            # ------------------------------------------------------------------
            # 1. Benchmark gRPC ListRecommendations
            # ------------------------------------------------------------------
            grpc_req = demo_pb2.ListRecommendationsRequest(
                user_id=user_id,
                product_ids=excluded_ids,
            )

            t0 = time.perf_counter()
            try:
                grpc_res = await stub.ListRecommendations(grpc_req)
                t1 = time.perf_counter()
                latencies_grpc.append((t1 - t0) * 1000)

                recommended_ids = list(grpc_res.product_ids)
                verify_recommendation_invariants(
                    iteration=i,
                    user_id=user_id,
                    excluded_ids=excluded_ids,
                    recommended_ids=recommended_ids,
                    valid_catalog_ids=catalog_ids,
                    transport="gRPC",
                )
            except AssertionError as err:
                print(f"❌ {err}")
                invariant_violations += 1
            except Exception as exc:
                print(f"❌ [gRPC Iter {i}] RPC Failed: {exc}")
                invariant_violations += 1

            # ------------------------------------------------------------------
            # 2. Benchmark HTTP REST Proxy (GET /recommendations)
            # ------------------------------------------------------------------
            # params = [("user_id", user_id)] + [("product_id", pid) for pid in excluded_ids]

            # t0 = time.perf_counter()
            # http_res = requests.get(f"{HTTP_BASE_URL}/recommendations", params=params)
            # t1 = time.perf_counter()
            # latencies_http.append((t1 - t0) * 1000)

            # if http_res.status_code == 200:
            #     body = http_res.json()
            #     http_recs = body.get("recommended_product_ids", [])
            #     try:
            #         verify_recommendation_invariants(
            #             iteration=i,
            #             user_id=user_id,
            #             excluded_ids=excluded_ids,
            #             recommended_ids=http_recs,
            #             valid_catalog_ids=catalog_ids,
            #             transport="HTTP",
            #         )
            #     except AssertionError as err:
            #         print(f"❌ {err}")
            #         invariant_violations += 1
            # else:
            #     print(f"❌ [HTTP Iter {i}] Request failed ({http_res.status_code}): {http_res.text}")
            #     invariant_violations += 1

    # Statistical Summary
    print("\n" + "=" * 60)
    print("      RECOMMENDATION SERVICE REGRESSION SUMMARY       ")
    print("=" * 60)
    print(f"Total Iterations     : {ITERATIONS}")
    print(f"Invariant Violations : {invariant_violations}")

    print("\n--- Latency Performance: gRPC (Native) ---")
    print(f"ListRecommendations | p50: {np.median(latencies_grpc):.3f} ms | p95: {np.percentile(latencies_grpc, 95):.3f} ms | p99: {np.percentile(latencies_grpc, 99):.3f} ms")

    # print("\n--- Latency Performance: HTTP / FastAPI (REST Proxy) ---")
    # print(f"GET /recommendations| p50: {np.median(latencies_http):.3f} ms | p95: {np.percentile(latencies_http, 95):.3f} ms | p99: {np.percentile(latencies_http, 99):.3f} ms")


if __name__ == "__main__":
    asyncio.run(run_regression_suite())
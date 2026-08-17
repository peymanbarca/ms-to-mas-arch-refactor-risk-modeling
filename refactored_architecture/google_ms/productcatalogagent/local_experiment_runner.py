import asyncio
import time
import numpy as np
import requests
import grpc

# Shared Proto imports
from ..shared import demo_pb2
from ..shared import demo_pb2_grpc

# Configuration
GRPC_TARGET = "localhost:5055"
HTTP_BASE_URL = "http://localhost:6055"
ITERATIONS = 100
INVALID_PRODUCT_ID = "NON_EXISTENT_ID_9999"


def verify_product_fields(product, source: str, iteration: int):
    """Validates structural invariants for individual product entries."""
    if isinstance(product, demo_pb2.Product):
        pid = product.id
        units = product.price_usd.units
    else:
        pid = product.get("id")
        units = product.get("price_usd", {}).get("units", 0)

    assert pid and isinstance(pid, str), f"[{source} Iter {iteration}] VIOLATION: Missing product ID"
    assert units >= 0, f"[{source} Iter {iteration}] VIOLATION: Invalid price units ({units}) for product {pid}"


async def run_regression_suite():
    print(f"--- Starting Product Catalog Service Regression Test ({ITERATIONS} Iterations) ---")

    latencies_grpc_list = []
    latencies_grpc_get = []
    latencies_grpc_search = []

    latencies_http_list = []
    latencies_http_get = []
    latencies_http_search = []

    invariant_violations = 0

    async with grpc.aio.insecure_channel(GRPC_TARGET) as channel:
        stub = demo_pb2_grpc.ProductCatalogServiceStub(channel)

        for i in range(1, ITERATIONS + 1):
            # ------------------------------------------------------------------
            # 1. Benchmark gRPC ListProducts
            # ------------------------------------------------------------------
            t0 = time.perf_counter()
            try:
                list_res = await stub.ListProducts(demo_pb2.Empty())
                t1 = time.perf_counter()
                latencies_grpc_list.append((t1 - t0) * 1000)

                products = list(list_res.products)
                assert len(products) > 0, f"[gRPC Iter {i}] VIOLATION: Catalog returned 0 products"
                for p in products:
                    verify_product_fields(p, "gRPC List", i)
                
                # Pick target product ID for Get/Search
                sample_prod = products[i % len(products)]
                target_id = sample_prod.id
                search_query = sample_prod.name.split()[0].lower() if sample_prod.name else "a"
            except Exception as exc:
                print(f"❌ [gRPC List Iter {i}] Failed: {exc}")
                invariant_violations += 1
                continue

            # ------------------------------------------------------------------
            # 2. Benchmark gRPC GetProduct (Valid & Invalid IDs)
            # ------------------------------------------------------------------
            t0 = time.perf_counter()
            get_res = await stub.GetProduct(demo_pb2.GetProductRequest(id=target_id))
            t1 = time.perf_counter()
            latencies_grpc_get.append((t1 - t0) * 1000)

            assert get_res.product.id == target_id, (
                f"[gRPC Get Iter {i}] VIOLATION: Requested {target_id}, got {get_res.product.id}"
            )

            # Verification of NOT_FOUND abort
            try:
                await stub.GetProduct(demo_pb2.GetProductRequest(id=INVALID_PRODUCT_ID))
                print(f"❌ [gRPC Get Iter {i}] VIOLATION: Expected NOT_FOUND for invalid ID")
                invariant_violations += 1
            except grpc.aio.AioRpcError as err:
                assert err.code() == grpc.StatusCode.NOT_FOUND, (
                    f"[gRPC Get Iter {i}] Unexpected error status: {err.code()}"
                )

            # ------------------------------------------------------------------
            # 3. Benchmark gRPC SearchProducts
            # ------------------------------------------------------------------
            t0 = time.perf_counter()
            search_res = await stub.SearchProducts(demo_pb2.SearchProductsRequest(query=search_query))
            t1 = time.perf_counter()
            latencies_grpc_search.append((t1 - t0) * 1000)

            for sp in search_res.results:
                name_match = search_query in sp.name.lower()
                desc_match = search_query in sp.description.lower()
                assert name_match or desc_match, (
                    f"[gRPC Search Iter {i}] VIOLATION: Query '{search_query}' not found in product {sp.id}"
                )

            # ------------------------------------------------------------------
            # 4. Benchmark HTTP GET /products
            # ------------------------------------------------------------------
            # t0 = time.perf_counter()
            # res_http_list = requests.get(f"{HTTP_BASE_URL}/products")
            # t1 = time.perf_counter()
            # latencies_http_list.append((t1 - t0) * 1000)

            # if res_http_list.status_code == 200:
            #     http_products = res_http_list.json().get("products", [])
            #     assert len(http_products) > 0, f"[HTTP List Iter {i}] VIOLATION: Catalog empty"
            # else:
            #     print(f"❌ [HTTP List Iter {i}] Failed status: {res_http_list.status_code}")
            #     invariant_violations += 1

            # ------------------------------------------------------------------
            # 5. Benchmark HTTP GET /products/{product_id}
            # ------------------------------------------------------------------
            # t0 = time.perf_counter()
            # res_http_get = requests.get(f"{HTTP_BASE_URL}/products/{target_id}")
            # t1 = time.perf_counter()
            # latencies_http_get.append((t1 - t0) * 1000)

            # assert res_http_get.status_code == 200, f"[HTTP Get Iter {i}] Failed: {res_http_get.status_code}"
            # assert res_http_get.json().get("id") == target_id

            # # Verify 404 behavior
            # res_http_404 = requests.get(f"{HTTP_BASE_URL}/products/{INVALID_PRODUCT_ID}")
            # assert res_http_404.status_code == 404, (
            #     f"[HTTP Get Iter {i}] Expected 404 for invalid ID, got {res_http_404.status_code}"
            # )

            # ------------------------------------------------------------------
            # 6. Benchmark HTTP GET /products/search?query=...
            # ------------------------------------------------------------------
            # t0 = time.perf_counter()
            # res_http_search = requests.get(f"{HTTP_BASE_URL}/products/search", params={"query": search_query})
            # t1 = time.perf_counter()
            # latencies_http_search.append((t1 - t0) * 1000)

            # assert res_http_search.status_code == 200, f"[HTTP Search Iter {i}] Failed: {res_http_search.status_code}"

    # Statistical Summary
    print("\n" + "=" * 65)
    print("      PRODUCT CATALOG SERVICE REGRESSION SUMMARY       ")
    print("=" * 65)
    print(f"Total Iterations     : {ITERATIONS}")
    print(f"Invariant Violations : {invariant_violations}")

    print("\n--- Latency Performance: gRPC (Native) ---")
    print(f"ListProducts   | p50: {np.median(latencies_grpc_list):.3f} ms | p95: {np.percentile(latencies_grpc_list, 95):.3f} ms | p99: {np.percentile(latencies_grpc_list, 99):.3f} ms")
    print(f"GetProduct    | p50: {np.median(latencies_grpc_get):.3f} ms | p95: {np.percentile(latencies_grpc_get, 95):.3f} ms | p99: {np.percentile(latencies_grpc_get, 99):.3f} ms")
    print(f"SearchProducts | p50: {np.median(latencies_grpc_search):.3f} ms | p95: {np.percentile(latencies_grpc_search, 95):.3f} ms | p99: {np.percentile(latencies_grpc_search, 99):.3f} ms")

    # print("\n--- Latency Performance: HTTP / FastAPI (REST) ---")
    # print(f"GET /products        | p50: {np.median(latencies_http_list):.3f} ms | p95: {np.percentile(latencies_http_list, 95):.3f} ms | p99: {np.percentile(latencies_http_list, 99):.3f} ms")
    # print(f"GET /products/{{id}}   | p50: {np.median(latencies_http_get):.3f} ms | p95: {np.percentile(latencies_http_get, 95):.3f} ms | p99: {np.percentile(latencies_http_get, 99):.3f} ms")
    # print(f"GET /products/search | p50: {np.median(latencies_http_search):.3f} ms | p95: {np.percentile(latencies_http_search, 95):.3f} ms | p99: {np.percentile(latencies_http_search, 99):.3f} ms")


if __name__ == "__main__":
    asyncio.run(run_regression_suite())
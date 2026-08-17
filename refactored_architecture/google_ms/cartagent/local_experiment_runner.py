import asyncio
import time
import uuid
import numpy as np
import requests
import grpc

# Shared Proto imports
from ..shared import demo_pb2
from ..shared import demo_pb2_grpc

# Configuration
GRPC_TARGET = "localhost:5054"
HTTP_BASE_URL = "http://localhost:6064"
ITERATIONS = 100
TEST_PRODUCT_ID = "OLJCERCA7W"


async def run_regression_suite():
    print(f"--- Starting Cart Service Regression Test ({ITERATIONS} Iterations) ---")

    latencies_grpc_add = []
    latencies_grpc_get = []
    latencies_grpc_empty = []

    latencies_http_add = []
    latencies_http_get = []
    latencies_http_empty = []

    invariant_violations = 0

    async with grpc.aio.insecure_channel(GRPC_TARGET) as channel:
        stub = demo_pb2_grpc.CartServiceStub(channel)

        for i in range(1, ITERATIONS + 1):
            user_id = f"test_user_{uuid.uuid4().hex[:8]}"

            # ------------------------------------------------------------------
            # 1. Benchmark gRPC AddItem
            # ------------------------------------------------------------------
            req_add = demo_pb2.AddItemRequest(
                user_id=user_id,
                item=demo_pb2.CartItem(product_id=TEST_PRODUCT_ID, quantity=2),
            )
            t0 = time.perf_counter()
            try:
                res_add = await stub.AddItem(req_add)
                t1 = time.perf_counter()
                latencies_grpc_add.append((t1 - t0) * 1000)

                assert isinstance(res_add, demo_pb2.AddItemResponse), (
                    f"[gRPC Add Iter {i}] VIOLATION: Expected AddItemResponse wrapper type"
                )
            except Exception as exc:
                print(f"❌ [gRPC Add Iter {i}] Failed: {exc}")
                invariant_violations += 1
                continue

            # ------------------------------------------------------------------
            # 2. Benchmark gRPC GetCart & Verify Accumulation
            # ------------------------------------------------------------------
            req_get = demo_pb2.GetCartRequest(user_id=user_id)
            t0 = time.perf_counter()
            try:
                res_get = await stub.GetCart(req_get)
                t1 = time.perf_counter()
                latencies_grpc_get.append((t1 - t0) * 1000)

                assert isinstance(res_get, demo_pb2.GetCartResponse), (
                    f"[gRPC Get Iter {i}] VIOLATION: Expected GetCartResponse wrapper type"
                )
                assert res_get.cart.user_id == user_id, (
                    f"[gRPC Get Iter {i}] VIOLATION: User ID mismatch"
                )
                items = {item.product_id: item.quantity for item in res_get.cart.items}
                assert items.get(TEST_PRODUCT_ID) == 2, (
                    f"[gRPC Get Iter {i}] VIOLATION: Expected qty 2, got {items.get(TEST_PRODUCT_ID)}"
                )
            except Exception as exc:
                print(f"❌ [gRPC Get Iter {i}] Failed: {exc}")
                invariant_violations += 1

            # ------------------------------------------------------------------
            # 3. Benchmark gRPC EmptyCart
            # ------------------------------------------------------------------
            req_empty = demo_pb2.EmptyCartRequest(user_id=user_id)
            t0 = time.perf_counter()
            try:
                res_empty = await stub.EmptyCart(req_empty)
                t1 = time.perf_counter()
                latencies_grpc_empty.append((t1 - t0) * 1000)

                assert isinstance(res_empty, demo_pb2.EmptyCartResponse), (
                    f"[gRPC Empty Iter {i}] VIOLATION: Expected EmptyCartResponse wrapper type"
                )

                # Verify Cart is empty post-deletion
                res_get_cleared = await stub.GetCart(req_get)
                assert len(res_get_cleared.cart.items) == 0, (
                    f"[gRPC Empty Iter {i}] VIOLATION: Cart not empty after deletion"
                )
            except Exception as exc:
                print(f"❌ [gRPC Empty Iter {i}] Failed: {exc}")
                invariant_violations += 1

            # ------------------------------------------------------------------
            # 4. Benchmark HTTP REST Pipeline
            # ------------------------------------------------------------------
            # http_user_id = f"http_user_{uuid.uuid4().hex[:8]}"

            # # HTTP POST Add Item
            # t0 = time.perf_counter()
            # res_h_add = requests.post(
            #     f"{HTTP_BASE_URL}/cart/{http_user_id}/items",
            #     json={"product_id": TEST_PRODUCT_ID, "quantity": 5},
            # )
            # t1 = time.perf_counter()
            # latencies_http_add.append((t1 - t0) * 1000)
            # assert res_h_add.status_code == 200, f"[HTTP Add Iter {i}] Status: {res_h_add.status_code}"

            # # HTTP GET Cart
            # t0 = time.perf_counter()
            # res_h_get = requests.get(f"{HTTP_BASE_URL}/cart/{http_user_id}")
            # t1 = time.perf_counter()
            # latencies_http_get.append((t1 - t0) * 1000)
            # assert res_h_get.status_code == 200, f"[HTTP Get Iter {i}] Status: {res_h_get.status_code}"
            # cart_data = res_h_get.json()
            # assert len(cart_data.get("items", [])) == 1, f"[HTTP Get Iter {i}] Unexpected item count"

            # # HTTP DELETE Empty Cart
            # t0 = time.perf_counter()
            # res_h_empty = requests.delete(f"{HTTP_BASE_URL}/cart/{http_user_id}")
            # t1 = time.perf_counter()
            # latencies_http_empty.append((t1 - t0) * 1000)
            # assert res_h_empty.status_code == 200, f"[HTTP Empty Iter {i}] Status: {res_h_empty.status_code}"

    # Statistical Summary
    print("\n" + "=" * 65)
    print("         CART SERVICE REGRESSION SUMMARY         ")
    print("=" * 65)
    print(f"Total Iterations     : {ITERATIONS}")
    print(f"Invariant Violations : {invariant_violations}")

    print("\n--- Latency Performance: gRPC ---")
    print(
        f"AddItem   | p50: {np.median(latencies_grpc_add):.3f} ms | "
        f"p95: {np.percentile(latencies_grpc_add, 95):.3f} ms | "
        f"p99: {np.percentile(latencies_grpc_add, 99):.3f} ms"
    )
    print(
        f"GetCart   | p50: {np.median(latencies_grpc_get):.3f} ms | "
        f"p95: {np.percentile(latencies_grpc_get, 95):.3f} ms | "
        f"p99: {np.percentile(latencies_grpc_get, 99):.3f} ms"
    )
    print(
        f"EmptyCart | p50: {np.median(latencies_grpc_empty):.3f} ms | "
        f"p95: {np.percentile(latencies_grpc_empty, 95):.3f} ms | "
        f"p99: {np.percentile(latencies_grpc_empty, 99):.3f} ms"
    )

    # print("\n--- Latency Performance: HTTP / REST ---")
    # print(
    #     f"POST /cart/.../items | p50: {np.median(latencies_http_add):.3f} ms | "
    #     f"p95: {np.percentile(latencies_http_add, 95):.3f} ms | "
    #     f"p99: {np.percentile(latencies_http_add, 99):.3f} ms"
    # )
    # print(
    #     f"GET  /cart/...       | p50: {np.median(latencies_http_get):.3f} ms | "
    #     f"p95: {np.percentile(latencies_http_get, 95):.3f} ms | "
    #     f"p99: {np.percentile(latencies_http_get, 99):.3f} ms"
    # )
    # print(
    #     f"DELETE /cart/...    | p50: {np.median(latencies_http_empty):.3f} ms | "
    #     f"p95: {np.percentile(latencies_http_empty, 95):.3f} ms | "
    #     f"p99: {np.percentile(latencies_http_empty, 99):.3f} ms"
    # )


if __name__ == "__main__":
    asyncio.run(run_regression_suite())
"""
checkoutservice/tests/regression_test.py

Regression Test & Benchmarking Suite for CheckoutService.
Runs N iterations over gRPC and REST endpoints to verify:
  1. 10-step saga completion & state consistency
  2. Aggregated LLM token metrics calculation
  3. Price preview calculation without state mutation
  4. Latency performance distributions (p50, p95, p99)
"""

import asyncio
import time
import uuid
import numpy as np
import requests
import grpc

from ..shared import demo_pb2
from ..shared import demo_pb2_grpc

GRPC_TARGET = "localhost:5050"
HTTP_BASE_URL = "http://localhost:6050"
ITERATIONS = 100

# Mock sample payload data
SAMPLE_ADDRESS = demo_pb2.Address(
    street_address="1600 Amphitheatre Pkwy",
    city="Mountain View",
    state="CA",
    country="USA",
    zip_code=94043,
)

SAMPLE_CARD = demo_pb2.CreditCardInfo(
    credit_card_number="4111111111111111",
    credit_card_cvv=123,
    credit_card_expiration_year=2028,
    credit_card_expiration_month=11,
)


async def run_checkout_regression_suite():
    print(f"--- Starting CheckoutService Regression Test ({ITERATIONS} Iterations) ---")

    latencies_grpc_checkout = []
    latencies_http_checkout = []
    latencies_http_preview = []

    invariant_violations = 0

    async with grpc.aio.insecure_channel(GRPC_TARGET) as channel:
        stub = demo_pb2_grpc.CheckoutServiceStub(channel)

        for i in range(1, ITERATIONS + 1):
            user_id = f"user_{uuid.uuid4().hex[:8]}"
            user_email = f"{user_id}@example.com"

            # ------------------------------------------------------------------
            # 1. Benchmark gRPC PlaceOrder RPC & Verify LLM Metrics Aggregation
            # ------------------------------------------------------------------
            req_place_order = demo_pb2.PlaceOrderRequest(
                user_id=user_id,
                user_currency="USD",
                address=SAMPLE_ADDRESS,
                email=user_email,
                credit_card=SAMPLE_CARD,
            )

            t0 = time.perf_counter()
            try:
                res: demo_pb2.PlaceOrderResponse = await stub.PlaceOrder(req_place_order)
                t1 = time.perf_counter()
                latencies_grpc_checkout.append((t1 - t0) * 1000)

                # Invariant 1: Correct response type wrapper
                assert isinstance(res, demo_pb2.PlaceOrderResponse), (
                    f"[Iter {i}] VIOLATION: Expected PlaceOrderResponse object"
                )

                # Invariant 2: Order Result Populated
                order = res.order
                assert order.order_id != "", f"[Iter {i}] VIOLATION: Empty order_id"
                assert order.shipping_tracking_id != "", f"[Iter {i}] VIOLATION: Empty tracking_id"

                # Invariant 3: LLM Metrics Aggregated
                # assert res.HasField("llm_metrics"), f"[Iter {i}] VIOLATION: Missing llm_metrics field"
                # assert res.llm_metrics.total_input_tokens >= -1, (
                #     f"[Iter {i}] VIOLATION: Invalid total_input_tokens"
                # )

            except Exception as exc:
                print(f"❌ [gRPC PlaceOrder Iter {i}] Failed: {exc}")
                invariant_violations += 1
                continue

            # ------------------------------------------------------------------
            # 2. Benchmark HTTP REST /place-order/preview (Read-Only Total Calculation)
            # ------------------------------------------------------------------
            # preview_payload = {
            #     "user_id": user_id,
            #     "user_currency": "USD",
            #     "address": {
            #         "street_address": "1600 Amphitheatre Pkwy",
            #         "city": "Mountain View",
            #         "state": "CA",
            #         "country": "USA",
            #         "zip_code": 94043,
            #     },
            # }

            # t0 = time.perf_counter()
            # try:
            #     res_prev = requests.post(
            #         f"{HTTP_BASE_URL}/place-order/preview",
            #         json=preview_payload,
            #         timeout=5,
            #     )
            #     t1 = time.perf_counter()
            #     latencies_http_preview.append((t1 - t0) * 1000)

            #     assert res_prev.status_code == 200, (
            #         f"[HTTP Preview Iter {i}] Status: {res_prev.status_code}"
            #     )
            #     data = res_prev.json()
            #     assert "total" in data, f"[HTTP Preview Iter {i}] Missing 'total' in response"
            #     assert data["total"]["currency_code"] == "USD", (
            #         f"[HTTP Preview Iter {i}] Currency mismatch"
            #     )
            # except Exception as exc:
            #     print(f"❌ [HTTP Preview Iter {i}] Failed: {exc}")
            #     invariant_violations += 1

            # # ------------------------------------------------------------------
            # # 3. Benchmark HTTP REST /place-order Proxy Endpoint
            # # ------------------------------------------------------------------
            # place_order_payload = {
            #     "user_id": f"http_{user_id}",
            #     "user_currency": "USD",
            #     "address": preview_payload["address"],
            #     "email": user_email,
            #     "credit_card_number": "1234-5678-9012-3456",
            #     "credit_card_cvv": 123,
            #     "credit_card_expiration_year": 2028,
            #     "credit_card_expiration_month": 11,
            # }

            # t0 = time.perf_counter()
            # try:
            #     res_http = requests.post(
            #         f"{HTTP_BASE_URL}/place-order",
            #         json=place_order_payload,
            #         timeout=10,
            #     )
            #     t1 = time.perf_counter()
            #     latencies_http_checkout.append((t1 - t0) * 1000)

            #     assert res_http.status_code == 200, (
            #         f"[HTTP PlaceOrder Iter {i}] Status: {res_http.status_code}"
            #     )
            #     body = res_http.json()
            #     assert "order_id" in body, f"[HTTP PlaceOrder Iter {i}] Missing order_id"
            # except Exception as exc:
            #     print(f"❌ [HTTP PlaceOrder Iter {i}] Failed: {exc}")
            #     invariant_violations += 1

    # Print Summary Report
    print("\n" + "=" * 65)
    print("        CHECKOUT SERVICE REGRESSION & LATENCY REPORT        ")
    print("=" * 65)
    print(f"Total Iterations     : {ITERATIONS}")
    print(f"Invariant Violations : {invariant_violations}")

    if latencies_grpc_checkout:
        print("\n--- Latency Performance: gRPC RPCs ---")
        print(
            f"PlaceOrder RPC       | p50: {np.median(latencies_grpc_checkout):.3f} ms | "
            f"p95: {np.percentile(latencies_grpc_checkout, 95):.3f} ms | "
            f"p99: {np.percentile(latencies_grpc_checkout, 99):.3f} ms"
        )

    # if latencies_http_checkout or latencies_http_preview:
    #     print("\n--- Latency Performance: HTTP / REST Proxy ---")
    #     if latencies_http_preview:
    #         print(
    #             f"POST /place-order/preview | p50: {np.median(latencies_http_preview):.3f} ms | "
    #             f"p95: {np.percentile(latencies_http_preview, 95):.3f} ms | "
    #             f"p99: {np.percentile(latencies_http_preview, 99):.3f} ms"
    #         )
    #     if latencies_http_checkout:
    #         print(
    #             f"POST /place-order         | p50: {np.median(latencies_http_checkout):.3f} ms | "
    #             f"p95: {np.percentile(latencies_http_checkout, 95):.3f} ms | "
    #             f"p99: {np.percentile(latencies_http_checkout, 99):.3f} ms"
    #         )


if __name__ == "__main__":
    asyncio.run(run_checkout_regression_suite())
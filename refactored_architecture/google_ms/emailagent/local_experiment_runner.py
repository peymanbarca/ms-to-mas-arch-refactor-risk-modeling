import asyncio
import time
import numpy as np
import requests
import grpc

# Shared Proto imports
from ms_baseline.google_ms.shared import demo_pb2
from ms_baseline.google_ms.shared import demo_pb2_grpc

# Configuration
GRPC_TARGET = "localhost:5056"
HTTP_BASE_URL = "http://localhost:6056"
ITERATIONS = 100


def build_sample_order_proto(iteration: int) -> demo_pb2.OrderResult:
    """Constructs a valid OrderResult protobuf message for testing."""
    return demo_pb2.OrderResult(
        order_id=f"ORD-REG-{iteration:04d}",
        shipping_tracking_id=f"TRACK-{iteration:04d}-XYZ",
        shipping_cost=demo_pb2.Money(currency_code="USD", units=5, nanos=990000000),
        shipping_address=demo_pb2.Address(
            street_address="1600 Amphitheatre Pkwy",
            city="Mountain View",
            state="CA",
            country="USA",
            zip_code=94043,
        ),
        items=[
            demo_pb2.OrderItem(
                item=demo_pb2.CartItem(product_id="OLJCERCA7W", quantity=2),
                cost=demo_pb2.Money(currency_code="USD", units=29, nanos=990000000),
            )
        ],
    )


async def run_regression_suite():
    print(f"--- Starting Email Service Regression Test ({ITERATIONS} Iterations) ---")

    latencies_grpc_send = []
    latencies_http_send = []
    latencies_http_templates = []

    invariant_violations = 0

    async with grpc.aio.insecure_channel(GRPC_TARGET) as channel:
        stub = demo_pb2_grpc.EmailServiceStub(channel)

        for i in range(1, ITERATIONS + 1):
            recipient_email = f"user_{i}@example.com"
            order_proto = build_sample_order_proto(i)

            # ------------------------------------------------------------------
            # 1. Benchmark gRPC SendOrderConfirmation
            # ------------------------------------------------------------------
            req_grpc = demo_pb2.SendOrderConfirmationRequest(
                email=recipient_email,
                order=order_proto,
            )

            t0 = time.perf_counter()
            try:
                res_grpc = await stub.SendOrderConfirmation(req_grpc)
                t1 = time.perf_counter()
                latencies_grpc_send.append((t1 - t0) * 1000)

                assert isinstance(res_grpc, demo_pb2.Empty), (
                    f"[gRPC Send Iter {i}] VIOLATION: Response type mismatch"
                )
            except Exception as exc:
                print(f"❌ [gRPC Send Iter {i}] Failed: {exc}")
                invariant_violations += 1
                continue

            # ------------------------------------------------------------------
            # 2. Benchmark HTTP POST /send-confirmation
            # ------------------------------------------------------------------
            # payload_http = {
            #     "email": recipient_email,
            #     "order": {
            #         "order_id": f"ORD-REG-{i:04d}",
            #         "shipping_tracking_id": f"TRACK-{i:04d}-XYZ",
            #         "shipping_cost_units": 5,
            #         "shipping_cost_nanos": 990000000,
            #         "shipping_address": {
            #             "street_address": "1600 Amphitheatre Pkwy",
            #             "city": "Mountain View",
            #             "state": "CA",
            #             "country": "USA",
            #             "zip_code": 94043,
            #         },
            #         "items": [
            #             {
            #                 "product_id": "OLJCERCA7W",
            #                 "quantity": 2,
            #                 "cost_units": 29,
            #                 "cost_nanos": 990000000,
            #             }
            #         ],
            #     },
            # }

            # t0 = time.perf_counter()
            # res_http = requests.post(f"{HTTP_BASE_URL}/send-confirmation", json=payload_http)
            # t1 = time.perf_counter()
            # latencies_http_send.append((t1 - t0) * 1000)

            # if res_http.status_code == 200:
            #     body = res_http.json()
            #     assert body.get("status") == "ok", (
            #         f"[HTTP Send Iter {i}] VIOLATION: Unexpected status in response"
            #     )
            # else:
            #     print(f"❌ [HTTP Send Iter {i}] Failed status: {res_http.status_code}")
            #     invariant_violations += 1

            # ------------------------------------------------------------------
            # 3. Benchmark HTTP GET /templates
            # ------------------------------------------------------------------
            # t0 = time.perf_counter()
            # res_templates = requests.get(f"{HTTP_BASE_URL}/templates")
            # t1 = time.perf_counter()
            # latencies_http_templates.append((t1 - t0) * 1000)

            # assert res_templates.status_code == 200, (
            #     f"[HTTP Templates Iter {i}] Expected 200, got {res_templates.status_code}"
            # )
            # data_templates = res_templates.json()
            # assert "default" in data_templates, f"[HTTP Templates Iter {i}] Missing default template key"

    # Statistical Summary
    print("\n" + "=" * 65)
    print("         EMAIL SERVICE REGRESSION SUMMARY         ")
    print("=" * 65)
    print(f"Total Iterations     : {ITERATIONS}")
    print(f"Invariant Violations : {invariant_violations}")

    print("\n--- Latency Performance: gRPC ---")
    print(
        f"SendOrderConfirmation | p50: {np.median(latencies_grpc_send):.3f} ms | "
        f"p95: {np.percentile(latencies_grpc_send, 95):.3f} ms | "
        f"p99: {np.percentile(latencies_grpc_send, 99):.3f} ms"
    )

    # print("\n--- Latency Performance: HTTP / REST ---")
    # print(
    #     f"POST /send-confirmation | p50: {np.median(latencies_http_send):.3f} ms | "
    #     f"p95: {np.percentile(latencies_http_send, 95):.3f} ms | "
    #     f"p99: {np.percentile(latencies_http_send, 99):.3f} ms"
    # )
    # print(
    #     f"GET /templates          | p50: {np.median(latencies_http_templates):.3f} ms | "
    #     f"p95: {np.percentile(latencies_http_templates, 95):.3f} ms | "
    #     f"p99: {np.percentile(latencies_http_templates, 99):.3f} ms"
    # )


if __name__ == "__main__":
    asyncio.run(run_regression_suite())
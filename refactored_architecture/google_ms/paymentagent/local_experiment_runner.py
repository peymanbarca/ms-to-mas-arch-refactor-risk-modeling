import asyncio
import time
import numpy as np
import requests
import grpc

# Shared Proto imports
from ..shared import demo_pb2
from ..shared import demo_pb2_grpc

# Configuration
GRPC_TARGET = "localhost:5052"
HTTP_BASE_URL = "http://localhost:6052"
ITERATIONS = 100

# Test Data
VALID_CARD = {
    "number": "4532015112830366",  # Visa passes Luhn
    "cvv": 123,
    "exp_year": 2028,
    "exp_month": 12,
}

INVALID_CARD_LUHN = {
    "number": "4532015112830367",  # Fails Luhn
    "cvv": 123,
    "exp_year": 2028,
    "exp_month": 12,
}

EXPIRED_CARD = {
    "number": "4532015112830366",
    "cvv": 123,
    "exp_year": 2020,
    "exp_month": 1,
}


async def run_regression_suite():
    print(f"--- Starting Payment Service Regression Test ({ITERATIONS} Iterations) ---")

    latencies_grpc_charge_valid = []
    latencies_grpc_charge_invalid = []

    latencies_http_charge = []
    latencies_http_validate = []

    invariant_violations = 0

    async with grpc.aio.insecure_channel(GRPC_TARGET) as channel:
        stub = demo_pb2_grpc.PaymentServiceStub(channel)

        for i in range(1, ITERATIONS + 1):
            # ------------------------------------------------------------------
            # 1. Benchmark gRPC Charge (Valid Card)
            # ------------------------------------------------------------------
            req_valid = demo_pb2.ChargeRequest(
                amount=demo_pb2.Money(currency_code="USD", units=50, nanos=0),
                credit_card=demo_pb2.CreditCardInfo(
                    credit_card_number=VALID_CARD["number"],
                    credit_card_cvv=VALID_CARD["cvv"],
                    credit_card_expiration_year=VALID_CARD["exp_year"],
                    credit_card_expiration_month=VALID_CARD["exp_month"],
                ),
            )

            t0 = time.perf_counter()
            try:
                res_grpc = await stub.Charge(req_valid)
                t1 = time.perf_counter()
                latencies_grpc_charge_valid.append((t1 - t0) * 1000)

                assert res_grpc.transaction_id, f"[gRPC Charge Iter {i}] VIOLATION: Empty transaction_id"
            except Exception as exc:
                print(f"❌ [gRPC Charge Iter {i}] Unexpected error: {exc}")
                invariant_violations += 1
                continue

            # ------------------------------------------------------------------
            # 2. Benchmark gRPC Charge Error Handling (Invalid Card)
            # ------------------------------------------------------------------
            req_invalid = demo_pb2.ChargeRequest(
                amount=demo_pb2.Money(currency_code="USD", units=10, nanos=0),
                credit_card=demo_pb2.CreditCardInfo(
                    credit_card_number=INVALID_CARD_LUHN["number"],
                    credit_card_cvv=INVALID_CARD_LUHN["cvv"],
                    credit_card_expiration_year=INVALID_CARD_LUHN["exp_year"],
                    credit_card_expiration_month=INVALID_CARD_LUHN["exp_month"],
                ),
            )

            t0 = time.perf_counter()
            try:
                await stub.Charge(req_invalid)
                print(f"❌ [gRPC Charge Iter {i}] VIOLATION: Expected INVALID_ARGUMENT but succeeded")
                invariant_violations += 1
            except grpc.aio.AioRpcError as err:
                t1 = time.perf_counter()
                latencies_grpc_charge_invalid.append((t1 - t0) * 1000)
                assert err.code() == grpc.StatusCode.INVALID_ARGUMENT, (
                    f"[gRPC Charge Iter {i}] Expected INVALID_ARGUMENT, got {err.code()}"
                )

            # ------------------------------------------------------------------
            # 3. Benchmark HTTP POST /charge
            # ------------------------------------------------------------------
            # payload_charge = {
            #     "amount": {"currency_code": "USD", "units": 25, "nanos": 500000000},
            #     "credit_card_number": VALID_CARD["number"],
            #     "credit_card_cvv": VALID_CARD["cvv"],
            #     "credit_card_expiration_year": VALID_CARD["exp_year"],
            #     "credit_card_expiration_month": VALID_CARD["exp_month"],
            # }

            # t0 = time.perf_counter()
            # res_http_charge = requests.post(f"{HTTP_BASE_URL}/charge", json=payload_charge)
            # t1 = time.perf_counter()
            # latencies_http_charge.append((t1 - t0) * 1000)

            # assert res_http_charge.status_code == 200, (
            #     f"[HTTP Charge Iter {i}] Expected 200, got {res_http_charge.status_code}"
            # )
            # data_charge = res_http_charge.json()
            # assert "transaction_id" in data_charge, f"[HTTP Charge Iter {i}] Missing transaction_id"
            # assert data_charge.get("card_type") == "Visa"

            # # Verify HTTP 422 for expired card
            # payload_expired = dict(payload_charge)
            # payload_expired["credit_card_expiration_year"] = EXPIRED_CARD["exp_year"]
            # res_http_422 = requests.post(f"{HTTP_BASE_URL}/charge", json=payload_expired)
            # assert res_http_422.status_code == 422, (
            #     f"[HTTP Charge Iter {i}] Expected 422 for expired card, got {res_http_422.status_code}"
            # )

            # ------------------------------------------------------------------
            # 4. Benchmark HTTP POST /charge/validate
            # ------------------------------------------------------------------
            # payload_val = {
            #     "credit_card_number": VALID_CARD["number"],
            #     "credit_card_cvv": VALID_CARD["cvv"],
            #     "credit_card_expiration_year": VALID_CARD["exp_year"],
            #     "credit_card_expiration_month": VALID_CARD["exp_month"],
            # }

            # t0 = time.perf_counter()
            # res_http_val = requests.post(f"{HTTP_BASE_URL}/charge/validate", json=payload_val)
            # t1 = time.perf_counter()
            # latencies_http_validate.append((t1 - t0) * 1000)

            # assert res_http_val.status_code == 200, (
            #     f"[HTTP Validate Iter {i}] Expected 200, got {res_http_val.status_code}"
            # )
            # data_val = res_http_val.json()
            # assert data_val.get("valid") is True, f"[HTTP Validate Iter {i}] Expected valid card"

    # Statistical Summary
    print("\n" + "=" * 65)
    print("        PAYMENT SERVICE REGRESSION SUMMARY        ")
    print("=" * 65)
    print(f"Total Iterations     : {ITERATIONS}")
    print(f"Invariant Violations : {invariant_violations}")

    print("\n--- Latency Performance: gRPC ---")
    print(
        f"Charge (Valid)    | p50: {np.median(latencies_grpc_charge_valid):.3f} ms | "
        f"p95: {np.percentile(latencies_grpc_charge_valid, 95):.3f} ms | "
        f"p99: {np.percentile(latencies_grpc_charge_valid, 99):.3f} ms"
    )
    print(
        f"Charge (Rejected) | p50: {np.median(latencies_grpc_charge_invalid):.3f} ms | "
        f"p95: {np.percentile(latencies_grpc_charge_invalid, 95):.3f} ms | "
        f"p99: {np.percentile(latencies_grpc_charge_invalid, 99):.3f} ms"
    )

    # print("\n--- Latency Performance: HTTP / REST ---")
    # print(
    #     f"POST /charge      | p50: {np.median(latencies_http_charge):.3f} ms | "
    #     f"p95: {np.percentile(latencies_http_charge, 95):.3f} ms | "
    #     f"p99: {np.percentile(latencies_http_charge, 99):.3f} ms"
    # )
    # print(
    #     f"POST /validate    | p50: {np.median(latencies_http_validate):.3f} ms | "
    #     f"p95: {np.percentile(latencies_http_validate, 95):.3f} ms | "
    #     f"p99: {np.percentile(latencies_http_validate, 99):.3f} ms"
    # )


if __name__ == "__main__":
    asyncio.run(run_regression_suite())
import asyncio
import time
import numpy as np
import requests
import grpc

# Shared Proto imports
from ..shared import demo_pb2
from ..shared import demo_pb2_grpc

# Configuration
GRPC_TARGET = "localhost:5053"
HTTP_BASE_URL = "http://localhost:6063"
ITERATIONS = 100
INVALID_CURRENCY_CODE = "INVALID_XYZ"


async def run_regression_suite():
    print(f"--- Starting Currency Service Regression Test ({ITERATIONS} Iterations) ---")

    latencies_grpc_get_supported = []
    latencies_grpc_convert_valid = []
    latencies_grpc_convert_invalid = []

    latencies_http_get_supported = []
    latencies_http_convert_valid = []

    invariant_violations = 0

    async with grpc.aio.insecure_channel(GRPC_TARGET) as channel:
        stub = demo_pb2_grpc.CurrencyServiceStub(channel)

        for i in range(1, ITERATIONS + 1):
            # ------------------------------------------------------------------
            # 1. Benchmark gRPC GetSupportedCurrencies
            # ------------------------------------------------------------------
            t0 = time.perf_counter()
            try:
                res_supported = await stub.GetSupportedCurrencies(demo_pb2.Empty())
                t1 = time.perf_counter()
                latencies_grpc_get_supported.append((t1 - t0) * 1000)

                codes = list(res_supported.currency_codes)
                assert len(codes) > 0, f"[gRPC Iter {i}] VIOLATION: Empty currency list"
                assert "EUR" in codes and "USD" in codes, f"[gRPC Iter {i}] VIOLATION: Missing core currency codes"
            except Exception as exc:
                print(f"❌ [gRPC GetSupported Iter {i}] Failed: {exc}")
                invariant_violations += 1
                continue

            # Pick test currency pairs
            from_code = codes[i % len(codes)]
            to_code = codes[(i + 1) % len(codes)]

            # ------------------------------------------------------------------
            # 2. Benchmark gRPC Convert (Valid Pair)
            # ------------------------------------------------------------------
            req_convert = demo_pb2.CurrencyConversionRequest(
                from_=demo_pb2.Money(currency_code=from_code, units=100, nanos=500000000),
                to_code=to_code,
            )

            t0 = time.perf_counter()
            try:
                res_convert = await stub.Convert(req_convert)
                t1 = time.perf_counter()
                latencies_grpc_convert_valid.append((t1 - t0) * 1000)

                conv_money = res_convert.money
                assert conv_money.currency_code == to_code, (
                    f"[gRPC Convert Iter {i}] VIOLATION: Expected {to_code}, got {conv_money.currency_code}"
                )
                assert conv_money.units >= 0 and conv_money.nanos >= 0, (
                    f"[gRPC Convert Iter {i}] VIOLATION: Negative monetary value returned"
                )
            except Exception as exc:
                print(f"❌ [gRPC Convert Iter {i}] Failed: {exc}")
                invariant_violations += 1

            # ------------------------------------------------------------------
            # 3. Benchmark gRPC Convert Error Handling (Invalid Code)
            # ------------------------------------------------------------------
            req_invalid = demo_pb2.CurrencyConversionRequest(
                from_=demo_pb2.Money(currency_code=INVALID_CURRENCY_CODE, units=10, nanos=0),
                to_code="USD",
            )

            t0 = time.perf_counter()
            try:
                await stub.Convert(req_invalid)
                print(f"❌ [gRPC Convert Iter {i}] VIOLATION: Expected INVALID_ARGUMENT but succeeded")
                invariant_violations += 1
            except grpc.aio.AioRpcError as err:
                t1 = time.perf_counter()
                latencies_grpc_convert_invalid.append((t1 - t0) * 1000)
                assert err.code() == grpc.StatusCode.INVALID_ARGUMENT, (
                    f"[gRPC Convert Iter {i}] Expected INVALID_ARGUMENT, got {err.code()}"
                )

            # ------------------------------------------------------------------
            # 4. Benchmark HTTP GET /currencies
            # ------------------------------------------------------------------
            # t0 = time.perf_counter()
            # res_http_currencies = requests.get(f"{HTTP_BASE_URL}/currencies")
            # t1 = time.perf_counter()
            # latencies_http_get_supported.append((t1 - t0) * 1000)

            # assert res_http_currencies.status_code == 200, (
            #     f"[HTTP Currencies Iter {i}] Expected 200, got {res_http_currencies.status_code}"
            # )
            # http_codes = res_http_currencies.json().get("currency_codes", [])
            # assert len(http_codes) > 0, f"[HTTP Currencies Iter {i}] VIOLATION: Empty list returned"

            # ------------------------------------------------------------------
            # 5. Benchmark HTTP POST /convert
            # ------------------------------------------------------------------
            # payload_convert = {
            #     "from": {"currency_code": from_code, "units": 100, "nanos": 500000000},
            #     "to_code": to_code,
            # }

            # t0 = time.perf_counter()
            # res_http_convert = requests.post(f"{HTTP_BASE_URL}/convert", json=payload_convert)
            # t1 = time.perf_counter()
            # latencies_http_convert_valid.append((t1 - t0) * 1000)

            # assert res_http_convert.status_code == 200, (
            #     f"[HTTP Convert Iter {i}] Expected 200, got {res_http_convert.status_code}"
            # )
            # data_conv = res_http_convert.json()
            # assert data_conv.get("currency_code") == to_code, (
            #     f"[HTTP Convert Iter {i}] VIOLATION: Target currency mismatch"
            # )

    # Statistical Summary
    print("\n" + "=" * 65)
    print("        CURRENCY SERVICE REGRESSION SUMMARY        ")
    print("=" * 65)
    print(f"Total Iterations     : {ITERATIONS}")
    print(f"Invariant Violations : {invariant_violations}")

    print("\n--- Latency Performance: gRPC ---")
    # print(
    #     f"GetSupportedCurrencies | p50: {np.median(latencies_grpc_get_supported):.3f} ms | "
    #     f"p95: {np.percentile(latencies_grpc_get_supported, 95):.3f} ms | "
    #     f"p99: {np.percentile(latencies_grpc_get_supported, 99):.3f} ms"
    # )
    print(
        f"Convert (Valid)        | p50: {np.median(latencies_grpc_convert_valid):.3f} ms | "
        f"p95: {np.percentile(latencies_grpc_convert_valid, 95):.3f} ms | "
        f"p99: {np.percentile(latencies_grpc_convert_valid, 99):.3f} ms"
    )
    print(
        f"Convert (Rejected)     | p50: {np.median(latencies_grpc_convert_invalid):.3f} ms | "
        f"p95: {np.percentile(latencies_grpc_convert_invalid, 95):.3f} ms | "
        f"p99: {np.percentile(latencies_grpc_convert_invalid, 99):.3f} ms"
    )

    # print("\n--- Latency Performance: HTTP / REST ---")
    # print(
    #     f"GET /currencies        | p50: {np.median(latencies_http_get_supported):.3f} ms | "
    #     f"p95: {np.percentile(latencies_http_get_supported, 95):.3f} ms | "
    #     f"p99: {np.percentile(latencies_http_get_supported, 99):.3f} ms"
    # )
    # print(
    #     f"POST /convert          | p50: {np.median(latencies_http_convert_valid):.3f} ms | "
    #     f"p95: {np.percentile(latencies_http_convert_valid, 95):.3f} ms | "
    #     f"p99: {np.percentile(latencies_http_convert_valid, 99):.3f} ms"
    # )


if __name__ == "__main__":
    asyncio.run(run_regression_suite())
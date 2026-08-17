import asyncio
import time
import re
import numpy as np
import requests
import grpc
from motor.motor_asyncio import AsyncIOMotorClient

# Shared Proto imports
from ..shared import demo_pb2
from ..shared import demo_pb2_grpc

# Configuration
GRPC_TARGET = "localhost:5051"
HTTP_BASE_URL = "http://localhost:6051"
MONGO_URI = "mongodb://localhost:27017"
DB_NAME = "google_ms"
COLLECTION_NAME = "shipments"

ITERATIONS = 100
TRACKING_ID_REGEX = re.compile(r"^[A-Z]{2}-\d{8}-[A-Z]{2}$")

# Database setup
mongo_client = AsyncIOMotorClient(MONGO_URI)
db = mongo_client[DB_NAME]
shipments_col = db[COLLECTION_NAME]


def calculate_expected_quote(item_count: int) -> float:
    """Calculates expected shipping quote based on service pricing tiers."""
    if item_count == 0:
        return 0.0
    elif item_count in (1, 2):
        return 8.99
    elif item_count in (3, 4):
        return 15.99
    elif item_count in (5, 6, 7):
        return 23.99
    elif item_count in (8, 9):
        return 31.99
    else:
        return 39.99


async def clear_shipments():
    """Reset MongoDB shipments collection before testing."""
    await shipments_col.delete_many({})


async def verify_mongo_invariant(iteration: int, tracking_id: str):
    """Verify that the shipment was persisted accurately to MongoDB."""
    # Allow brief window for async write execution
    await asyncio.sleep(0.05)
    doc = await shipments_col.find_one({"tracking_id": tracking_id})
    
    assert doc is not None, (
        f"[Iter {iteration}] VIOLATION: Tracking ID '{tracking_id}' not found in MongoDB."
    )
    assert doc.get("status") == "shipped", (
        f"[Iter {iteration}] VIOLATION: Unexpected shipment status '{doc.get('status')}'."
    )
    assert "quote" in doc and "request" in doc, (
        f"[Iter {iteration}] VIOLATION: Shipment payload missing quote or request metadata."
    )


async def run_regression_suite():
    print(f"--- Starting Shipping Service Regression Test ({ITERATIONS} Iterations) ---")
    await clear_shipments()

    latencies_grpc_quote = []
    latencies_grpc_ship = []
    latencies_http_quote = []
    latencies_http_ship = []
    
    invariant_violations = 0

    async with grpc.aio.insecure_channel(GRPC_TARGET) as channel:
        stub = demo_pb2_grpc.ShippingServiceStub(channel)

        for i in range(1, ITERATIONS + 1):
            test_address = demo_pb2.Address(
                street_address=f"{100 + i} Main St",
                city="Seattle",
                state="WA",
                country="USA",
                zip_code=98101,
            )
            item_count = (i % 12)  # Cycle through item counts 0 to 11
            test_items = [demo_pb2.CartItem(product_id="prod_123", quantity=item_count)] if item_count > 0 else []

            # ------------------------------------------------------------------
            # 1. Benchmark gRPC GetQuote
            # ------------------------------------------------------------------
            quote_req = demo_pb2.GetQuoteRequest(address=test_address, items=test_items)
            
            t0 = time.perf_counter()
            quote_res = await stub.GetQuote(quote_req)
            t1 = time.perf_counter()
            latencies_grpc_quote.append((t1 - t0) * 1000)

            # Invariant: Tiered price check
            expected_usd = calculate_expected_quote(item_count)
            actual_usd = quote_res.cost_usd.units + (quote_res.cost_usd.nanos / 1e9)
            if round(actual_usd, 2) != expected_usd:
                print(f"❌ [Iter {i}] gRPC Quote Mismatch! Expected: ${expected_usd}, Got: ${actual_usd:.2f}")
                invariant_violations += 1

            # ------------------------------------------------------------------
            # 2. Benchmark gRPC ShipOrder
            # ------------------------------------------------------------------
            ship_req = demo_pb2.ShipOrderRequest(address=test_address, items=test_items)
            
            t0 = time.perf_counter()
            ship_res = await stub.ShipOrder(ship_req)
            t1 = time.perf_counter()
            latencies_grpc_ship.append((t1 - t0) * 1000)

            # Invariant: Tracking ID format check & MongoDB Persistence
            if not TRACKING_ID_REGEX.match(ship_res.tracking_id):
                print(f"❌ [Iter {i}] Invalid gRPC Tracking ID format: {ship_res.tracking_id}")
                invariant_violations += 1
            else:
                try:
                    await verify_mongo_invariant(i, ship_res.tracking_id)
                except AssertionError as err:
                    print(f"❌ {err}")
                    invariant_violations += 1

            # ------------------------------------------------------------------
            # 3. Benchmark HTTP REST Proxy (POST /quote)
            # ------------------------------------------------------------------
            # http_quote_payload = {
            #     "address": {
            #         "street_address": f"{100 + i} Main St",
            #         "city": "Seattle",
            #         "state": "WA",
            #         "country": "USA",
            #         "zip_code": 98101,
            #     },
            #     "items": [{"product_id": "prod_123", "quantity": item_count}] if item_count > 0 else []
            # }

            # t0 = time.perf_counter()
            # http_quote_res = requests.post(f"{HTTP_BASE_URL}/quote", json=http_quote_payload)
            # t1 = time.perf_counter()
            # latencies_http_quote.append((t1 - t0) * 1000)

            # if http_quote_res.status_code != 200:
            #     print(f"❌ [Iter {i}] HTTP /quote request failed: {http_quote_res.status_code}")
            #     invariant_violations += 1

            # ------------------------------------------------------------------
            # 4. Benchmark HTTP REST Proxy (POST /ship)
            # ------------------------------------------------------------------
            # t0 = time.perf_counter()
            # http_ship_res = requests.post(f"{HTTP_BASE_URL}/ship", json=http_quote_payload)
            # t1 = time.perf_counter()
            # latencies_http_ship.append((t1 - t0) * 1000)

            # if http_ship_res.status_code == 200:
            #     http_tracking_id = http_ship_res.json().get("tracking_id", "")
            #     if not TRACKING_ID_REGEX.match(http_tracking_id):
            #         print(f"❌ [Iter {i}] Invalid HTTP Tracking ID format: {http_tracking_id}")
            #         invariant_violations += 1
            #     else:
            #         try:
            #             await verify_mongo_invariant(i, http_tracking_id)
            #         except AssertionError as err:
            #             print(f"❌ {err}")
            #             invariant_violations += 1
            # else:
            #     print(f"❌ [Iter {i}] HTTP /ship request failed: {http_ship_res.status_code}")
            #     invariant_violations += 1

    # Statistical Summary
    print("\n" + "=" * 60)
    print("         SHIPPING SERVICE REGRESSION SUMMARY          ")
    print("=" * 60)
    print(f"Total Iterations     : {ITERATIONS}")
    print(f"Invariant Violations : {invariant_violations}")

    print("\n--- Latency Performance: gRPC (Native) ---")
    print(f"GetQuote  | p50: {np.median(latencies_grpc_quote):.3f} ms | p95: {np.percentile(latencies_grpc_quote, 95):.3f} ms | p99: {np.percentile(latencies_grpc_quote, 99):.3f} ms")
    print(f"ShipOrder | p50: {np.median(latencies_grpc_ship):.3f} ms | p95: {np.percentile(latencies_grpc_ship, 95):.3f} ms | p99: {np.percentile(latencies_grpc_ship, 99):.3f} ms")

    # print("\n--- Latency Performance: HTTP / FastAPI (REST Proxy) ---")
    # print(f"POST /quote | p50: {np.median(latencies_http_quote):.3f} ms | p95: {np.percentile(latencies_http_quote, 95):.3f} ms | p99: {np.percentile(latencies_http_quote, 99):.3f} ms")
    # print(f"POST /ship  | p50: {np.median(latencies_http_ship):.3f} ms | p95: {np.percentile(latencies_http_ship, 95):.3f} ms | p99: {np.percentile(latencies_http_ship, 99):.3f} ms")


if __name__ == "__main__":
    asyncio.run(run_regression_suite())
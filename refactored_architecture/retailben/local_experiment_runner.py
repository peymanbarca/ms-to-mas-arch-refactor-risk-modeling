import time
import numpy as np
import requests
from pymongo import MongoClient


def inventory_agent_experiment_runner(N_TRIALS=100):

    # Configuration
    BASE_URL = "http://127.0.0.1:8001"
    MONGO_URI = "mongodb://localhost:27017/"
    DB_NAME = "retailben"
    COL_NAME = "inventory"
    ITERATIONS = N_TRIALS

    SKU_1 = "4cc0770f-91bc-4c0d-a26f-7b872f02ca94"
    SKU_2 = "b2926dc2-cc6d-4c3e-ae40-7a127c173b16"

    INITIAL_STOCK = 500  # Set high enough so reserve calls succeed during the 100 loops
    QTY_PER_RESERVE = 2

    # Database Connection
    mongo_client = MongoClient(MONGO_URI)
    inventory_col = mongo_client[DB_NAME][COL_NAME]


    def reset_environment():
        """Reset DB stock state before starting regression test."""
        payload = {
            "items": [
                {"sku": SKU_1, "stock": INITIAL_STOCK},
                {"sku": SKU_2, "stock": INITIAL_STOCK},
            ]
        }
        resp = requests.post(f"{BASE_URL}/reset_stocks", json=payload)
        resp.raise_for_status()


    def verify_invariants(iteration: int, successful_reservations: int):
        """Checks invariants directly against MongoDB."""
        docs = list(inventory_col.find({"sku": {"$in": [SKU_1, SKU_2]}}))
        doc_map = {d["sku"]: d["stock"] for d in docs}

        # Invariant 1: Non-negative Stock Constraint
        for sku, stock in doc_map.items():
            assert stock >= 0, f"[Iter {iteration}] VIOLATION: Negative stock detected for SKU {sku}: {stock}"

        # Invariant 2: Inventory Conservation Law
        expected_stock_sku1 = INITIAL_STOCK - (successful_reservations * QTY_PER_RESERVE)
        actual_stock_sku1 = doc_map.get(SKU_1, 0)
        assert actual_stock_sku1 == expected_stock_sku1, (
            f"[Iter {iteration}] VIOLATION: Conservation law failed for {SKU_1}! "
            f"Expected {expected_stock_sku1}, Got {actual_stock_sku1}"
        )


    def run_regression_test(N_TRIALS: int):
        print(f"--- Starting Regression Test ({N_TRIALS} Iterations) ---")
        reset_environment()

        latencies_reset = []
        latencies_reserve = []
        successful_reservations = 0
        invariant_violations = 0

        for i in range(1, N_TRIALS + 1):
            # 1. Reset benchmark (measuring latency)
            t0 = time.perf_counter()
            resp_reset = requests.post(
                f"{BASE_URL}/reset_stocks",
                json={"items": [{"sku": SKU_1, "stock": INITIAL_STOCK}]},
            )
            t1 = time.perf_counter()
            latencies_reset.append((t1 - t0) * 1000)  # ms

            # Correct for reset in calculation
            successful_reservations = 0

            # 2. Reserve Benchmark
            reserve_payload = {
                "order_id": f"order-{i}",
                "items": [{"sku": SKU_1, "qty": QTY_PER_RESERVE}],
                "atomic_update": True  # Change to False to test non-atomic behavior
            }

            t0 = time.perf_counter()
            resp_reserve = requests.post(f"{BASE_URL}/reserve", json=reserve_payload)
            t1 = time.perf_counter()
            latencies_reserve.append((t1 - t0) * 1000)  # ms

            if resp_reserve.status_code == 200 and resp_reserve.json().get("status") == "RESERVED":
                successful_reservations += 1

            # 3. Check DB Invariants
            try:
                verify_invariants(i, successful_reservations)
            except AssertionError as e:
                print(f"❌ {e}")
                invariant_violations += 1

        # Statistical Computations
        print("\n" + "=" * 50)
        print("           REGRESSION TEST RESULTS SUMMARY          ")
        print("=" * 50)
        print(f"Total Runs: {ITERATIONS}")
        print(f"Invariant Violations: {invariant_violations}")

        print("\n--- Latency Performance (POST /reserve) ---")
        print(f"Median (p50): {np.median(latencies_reserve):.3f} ms")
        print(f"p95 Latency : {np.percentile(latencies_reserve, 95):.3f} ms")
        print(f"p99 Latency : {np.percentile(latencies_reserve, 99):.3f} ms")
        print(f"Min / Max   : {np.min(latencies_reserve):.3f} ms / {np.max(latencies_reserve):.3f} ms")

        print("\n--- Latency Performance (POST /reset_stocks) ---")
        print(f"Median (p50): {np.median(latencies_reset):.3f} ms")
        print(f"p95 Latency : {np.percentile(latencies_reset, 95):.3f} ms")

    run_regression_test(N_TRIALS=ITERATIONS)



def order_agent_experiment_runner(N_TRIALS=100):
    # Configuration
    BASE_URL = "http://127.0.0.1:8000"
    CART_SERVICE_URL = "http://127.0.0.1:8003"
    SUBSCRIPTION_SERVICE_URL = "http://127.0.0.1:8010"

    MONGO_URI = "mongodb://localhost:27017/"
    DB_NAME = "retailben"
    COL_NAME = "orders"
    ITERATIONS = N_TRIALS

    SKU = "4cc0770f-91bc-4c0d-a26f-7b872f02ca94"

    # Database Connection (Synchronous for invariant checks)
    mongo_client = MongoClient(MONGO_URI)
    orders_col = mongo_client[DB_NAME][COL_NAME]


    def clear_orders():
        """Reset orders collection before running regression suite."""
        orders_col.delete_many({})


    def setup_test_cart() -> str:
        """Helper to create a shopping cart with an item for testing checkout."""
        resp = requests.post(f"{CART_SERVICE_URL}/cart/-1/items", json={"sku": SKU, "qty": 2})
        assert resp.status_code == 200, f"Failed to setup cart: {resp.text}"
        return resp.json()["cart_id"]


    def setup_test_subscription(user_id: str):
        """Helper to register a promo subscription for discount validation."""
        payload = {"user_id": user_id, "email": f"{user_id}@example.com", "promo_code": "SUMMER20"}
        requests.post(f"{SUBSCRIPTION_SERVICE_URL}/subscriptions", json=payload)


    def verify_invariants(iteration: int, order_id: str, expected_cart_id: str, expected_user_id: str = None):
        """Validates MongoDB order records and orchestration invariants."""
        doc = orders_col.find_one({"_id": order_id})

        # Invariant 1: Document persistence
        assert doc is not None, (
            f"[Iter {iteration}] VIOLATION: Order ID {order_id} not found in MongoDB"
        )

        # Invariant 2: Cart association
        assert doc.get("cart_id") == expected_cart_id, (
            f"[Iter {iteration}] VIOLATION: Cart ID mismatch in DB. Expected {expected_cart_id}, got {doc.get('cart_id')}"
        )

        # Invariant 3: Terminal order status validation
        valid_statuses = {"COMPLETED", "OUT_OF_STOCK", "PAYMENT_FAILED", "SHIPMENT_FAILED"}
        assert doc.get("status") in valid_statuses, (
            f"[Iter {iteration}] VIOLATION: Unexpected terminal status '{doc.get('status')}' for order {order_id}"
        )

        # Invariant 4: Subscription discount persistence if user_id was provided
        if expected_user_id:
            assert doc.get("applied_promo_code") == "SUMMER20", (
                f"[Iter {iteration}] VIOLATION: Applied promo code missing or invalid for user {expected_user_id}"
            )
            assert doc.get("discount_percent") == 20.0, (
                f"[Iter {iteration}] VIOLATION: Discount percent mismatch for order {order_id}"
            )


    def run_regression_test(N_TRIALS):
        print(f"--- Starting Order Service Regression Test ({N_TRIALS} Iterations) ---")
        clear_orders()

        latencies_checkout_standard = []
        latencies_checkout_with_user = []
        invariant_violations = 0

        for i in range(1, N_TRIALS + 1):
            # 1. Setup prerequisite shopping cart
            cart_id = setup_test_cart()

            # 2. Benchmark standard checkout (without user_id)
            t0 = time.perf_counter()
            resp_std = requests.post(f"{BASE_URL}/cart/{cart_id}/checkout")
            t1 = time.perf_counter()
            latencies_checkout_standard.append((t1 - t0) * 1000)

            if resp_std.status_code == 200:
                std_data = resp_std.json()
                order_id = std_data.get("order_id")
                try:
                    verify_invariants(iteration=i, order_id=order_id, expected_cart_id=cart_id)
                except AssertionError as e:
                    print(f"❌ {e}")
                    invariant_violations += 1
            else:
                print(f"❌ [Iter {i}] Standard Checkout Failed ({resp_std.status_code}): {resp_std.text}")
                invariant_violations += 1

            # 3. Benchmark checkout with active user subscription discount
            user_id = f"order_user_{i}"
            setup_test_subscription(user_id)
            cart_id_discount = setup_test_cart()

            t0 = time.perf_counter()
            resp_user = requests.post(f"{BASE_URL}/cart/{cart_id_discount}/checkout?user_id={user_id}")
            t1 = time.perf_counter()
            latencies_checkout_with_user.append((t1 - t0) * 1000)

            if resp_user.status_code == 200:
                user_data = resp_user.json()
                order_id_user = user_data.get("order_id")
                try:
                    verify_invariants(
                        iteration=i,
                        order_id=order_id_user,
                        expected_cart_id=cart_id_discount,
                        expected_user_id=user_id
                    )
                except AssertionError as e:
                    print(f"❌ {e}")
                    invariant_violations += 1
            else:
                print(f"❌ [Iter {i}] User Checkout Failed ({resp_user.status_code}): {resp_user.text}")
                invariant_violations += 1

        # Statistical Summary
        print("\n" + "=" * 55)
        print("         ORDER SERVICE REGRESSION SUMMARY          ")
        print("=" * 55)
        print(f"Total Runs           : {ITERATIONS * 2} (Standard + Discount Flows)")
        print(f"Invariant Violations : {invariant_violations}")

        print("\n--- Latency Performance (Standard Checkout) ---")
        print(f"Median (p50) Latency : {np.median(latencies_checkout_standard):.3f} ms")
        print(f"p95 Latency          : {np.percentile(latencies_checkout_standard, 95):.3f} ms")
        print(f"p99 Latency          : {np.percentile(latencies_checkout_standard, 99):.3f} ms")
        print(f"Min / Max            : {np.min(latencies_checkout_standard):.3f} ms / {np.max(latencies_checkout_standard):.3f} ms")

        print("\n--- Latency Performance (Checkout with Subscription Discount) ---")
        print(f"Median (p50) Latency : {np.median(latencies_checkout_with_user):.3f} ms")
        print(f"p95 Latency          : {np.percentile(latencies_checkout_with_user, 95):.3f} ms")
        print(f"p99 Latency          : {np.percentile(latencies_checkout_with_user, 99):.3f} ms")
        print(f"Min / Max            : {np.min(latencies_checkout_with_user):.3f} ms / {np.max(latencies_checkout_with_user):.3f} ms")

    run_regression_test(N_TRIALS=ITERATIONS)

def payment_agent_experiment_runner(N_TRIALS=100):

    # Configuration
    BASE_URL = "http://127.0.0.1:8007"
    MONGO_URI = "mongodb://localhost:27017/"
    DB_NAME = "retailben"
    COL_NAME = "payments"
    ITERATIONS = N_TRIALS

    # Database Connection
    mongo_client = MongoClient(MONGO_URI)
    payment_col = mongo_client[DB_NAME][COL_NAME]


    def reset_environment():
        """Clear payments DB collection before starting regression test."""
        resp = requests.post(f"{BASE_URL}/clear_payments")
        resp.raise_for_status()


    def verify_invariants(iteration: int, expected_count: int):
        """Checks database state and invariants against MongoDB."""
        docs = list(payment_col.find({}))
        
        # Invariant 1: Document Count Integrity
        assert len(docs) == expected_count, (
            f"[Iter {iteration}] VIOLATION: Expected {expected_count} records in DB, found {len(docs)}"
        )

        order_ids = set()
        for doc in docs:
            # Invariant 2: Positive Final Price
            assert doc.get("final_price", 0) > 0, (
                f"[Iter {iteration}] VIOLATION: Invalid final_price in DB doc: {doc}"
            )

            # Invariant 3: Status Schema Integrity
            assert doc.get("status") in ["SUCCESS", "FAILED"], (
                f"[Iter {iteration}] VIOLATION: Invalid status '{doc.get('status')}' for order_id {doc.get('order_id')}"
            )

            # Invariant 4: No Unintended Duplicates per Order ID
            oid = doc.get("order_id")
            assert oid not in order_ids, (
                f"[Iter {iteration}] VIOLATION: Duplicate payment record found for order_id {oid}"
            )
            order_ids.add(oid)


    def run_regression_test(N_TRIALS: int):
        print(f"--- Starting Payment Service Regression Test ({N_TRIALS} iterations) ---")
        reset_environment()

        latencies_pay = []
        invariant_violations = 0

        for i in range(1, N_TRIALS + 1):
            order_id = f"order-{i}"
            final_price = 2000.0 + (i * 10)  # Dynamic price for test variety

            payload = {
                "order_id": order_id,
                "final_price": final_price
            }

            # Measure latency for POST /pay-order
            t0 = time.perf_counter()
            resp = requests.post(f"{BASE_URL}/pay-order", json=payload)
            t1 = time.perf_counter()
            
            latency_ms = (t1 - t0) * 1000
            latencies_pay.append(latency_ms)

            # Check request success
            if resp.status_code != 200:
                print(f"❌ [Iter {i}] HTTP Error {resp.status_code}: {resp.text}")

            # Verify DB Invariants after execution
            try:
                verify_invariants(iteration=i, expected_count=i)
            except AssertionError as e:
                print(f"❌ {e}")
                invariant_violations += 1

        # Statistical Computations
        print("\n" + "=" * 50)
        print("        PAYMENT SERVICE REGRESSION SUMMARY        ")
        print("=" * 50)
        print(f"Total Runs           : {ITERATIONS}")
        print(f"Invariant Violations : {invariant_violations}")

        print("\n--- Latency Performance (POST /pay-order) ---")
        print(f"Median (p50) Latency : {np.median(latencies_pay):.3f} ms")
        print(f"p95 Latency          : {np.percentile(latencies_pay, 95):.3f} ms")
        print(f"p99 Latency          : {np.percentile(latencies_pay, 99):.3f} ms")
        print(f"Min / Max            : {np.min(latencies_pay):.3f} ms / {np.max(latencies_pay):.3f} ms")


    run_regression_test(N_TRIALS=ITERATIONS)
    
    

def pricing_agent_experiment_runner(N_TRIALS=100):
    # Configuration
    BASE_URL = "http://127.0.0.1:8002"
    MONGO_URI = "mongodb://localhost:27017/"
    DB_NAME = "retailben"
    COL_NAME = "prices"
    ITERATIONS = N_TRIALS

    SKU_1 = "4cc0770f-91bc-4c0d-a26f-7b872f02ca94"
    SKU_2 = "b2926dc2-cc6d-4c3e-ae40-7a127c173b16"

    # Database Connection (Synchronous for invariant checks)
    mongo_client = MongoClient(MONGO_URI)
    prices_col = mongo_client[DB_NAME][COL_NAME]


    def setup_initial_prices():
        """Seed initial prices using the admin API."""
        requests.post(f"{BASE_URL}/price/put", json={"product_id": SKU_1, "price": 2000.0}).raise_for_status()
        requests.post(f"{BASE_URL}/price/put", json={"product_id": SKU_2, "price": 200.0}).raise_for_status()


    def verify_db_invariant(product_id: str, expected_price: float, iteration: int):
        """Verifies that the database accurately reflects upserted product prices."""
        doc = prices_col.find_one({"product_id": product_id})
        assert doc is not None, f"[Iter {iteration}] VIOLATION: Product {product_id} not found in DB"
        assert doc["price"] == expected_price, (
            f"[Iter {iteration}] VIOLATION: Price mismatch in DB for {product_id}! "
            f"Expected {expected_price}, got {doc['price']}"
        )


    def verify_calculation_invariants(resp_data: dict, iteration: int):
        """Validates math invariants on the /price response."""
        subtotal = resp_data.get("subtotal", 0.0)
        total_discount = resp_data.get("total_discount", 0.0)
        total = resp_data.get("total", 0.0)
        items = resp_data.get("items", [])

        calc_subtotal = 0.0
        calc_discount = 0.0

        for item in items:
            unit_price = item["unit_price"]
            qty = item["qty"]
            line_total = item["line_total"]
            discounts = item["discounts"]

            # Invariant 1: Line total math
            expected_line_total = round((unit_price * qty) - discounts, 2)
            assert line_total == expected_line_total, (
                f"[Iter {iteration}] VIOLATION: Line total mismatch for {item['product_id']}. "
                f"Expected {expected_line_total}, got {line_total}"
            )

            calc_subtotal += unit_price * qty
            calc_discount += discounts

        # Invariant 2: Subtotal and Discount sum consistency
        assert round(subtotal, 2) == round(calc_subtotal, 2), (
            f"[Iter {iteration}] VIOLATION: Subtotal sum mismatch. Expected {calc_subtotal}, got {subtotal}"
        )
        assert round(total_discount, 2) == round(calc_discount, 2), (
            f"[Iter {iteration}] VIOLATION: Total discount sum mismatch. Expected {calc_discount}, got {total_discount}"
        )

        # Invariant 3: Total calculation invariant (Total = max(0, Subtotal - Total Discount))
        expected_total = round(max(0.0, subtotal - total_discount), 2)
        assert total == expected_total, (
            f"[Iter {iteration}] VIOLATION: Grand total mismatch. Expected {expected_total}, got {total}"
        )


    def run_regression_test(N_TRIALS: int):
        print(f"--- Starting Pricing Service Regression Test ({N_TRIALS} Iterations) ---")
        setup_initial_prices()

        latencies_put = []
        latencies_compute = []
        invariant_violations = 0

        for i in range(1, N_TRIALS + 1):
            # 1. Benchmark POST /price/put (Update Price)
            new_price_sku1 = 2000.0 + (i % 10)  # Fluctuate price slightly
            put_payload = {"product_id": SKU_1, "price": new_price_sku1}

            t0 = time.perf_counter()
            resp_put = requests.post(f"{BASE_URL}/price/put", json=put_payload)
            t1 = time.perf_counter()
            latencies_put.append((t1 - t0) * 1000)

            if resp_put.status_code != 200:
                print(f"❌ [Iter {i}] PUT Failed: {resp_put.text}")

            # Check DB invariant for PUT
            try:
                verify_db_invariant(SKU_1, new_price_sku1, i)
            except AssertionError as e:
                print(f"❌ {e}")
                invariant_violations += 1

            # 2. Benchmark POST /price (Compute Price)
            compute_payload = {
                "items": [
                    {"product_id": SKU_1, "qty": 2},
                    {"product_id": SKU_2, "qty": 3}
                ],
                "promo_codes": ["PROMO10"],
                "currency": "USD"
            }

            t0 = time.perf_counter()
            resp_compute = requests.post(f"{BASE_URL}/price", json=compute_payload)
            t1 = time.perf_counter()
            latencies_compute.append((t1 - t0) * 1000)

            if resp_compute.status_code == 200:
                # Check response calculation invariants
                try:
                    verify_calculation_invariants(resp_compute.json(), i)
                except AssertionError as e:
                    print(f"❌ {e}")
                    invariant_violations += 1
            else:
                print(f"❌ [Iter {i}] Compute Failed: {resp_compute.text}")

        # Statistical Analysis
        print("\n" + "=" * 50)
        print("        PRICING SERVICE REGRESSION SUMMARY        ")
        print("=" * 50)
        print(f"Total Runs           : {ITERATIONS}")
        print(f"Invariant Violations : {invariant_violations}")

        print("\n--- Latency Performance (POST /price/put) ---")
        print(f"Median (p50) Latency : {np.median(latencies_put):.3f} ms")
        print(f"p95 Latency          : {np.percentile(latencies_put, 95):.3f} ms")
        print(f"p99 Latency          : {np.percentile(latencies_put, 99):.3f} ms")

        print("\n--- Latency Performance (POST /price) ---")
        print(f"Median (p50) Latency : {np.median(latencies_compute):.3f} ms")
        print(f"p95 Latency          : {np.percentile(latencies_compute, 95):.3f} ms")
        print(f"p99 Latency          : {np.percentile(latencies_compute, 99):.3f} ms")


    run_regression_test(N_TRIALS=ITERATIONS)

def procurement_agent_experiment_runner(N_TRIALS=100):
    # Configuration
    BASE_URL = "http://127.0.0.1:8009"
    MONGO_URI = "mongodb://localhost:27017/"
    DB_NAME = "retailben"
    COL_NAME = "procurement_orders"
    ITERATIONS = N_TRIALS

    SKU = "4cc0770f-91bc-4c0d-a26f-7b872f02ca94"
    PREFERRED_SUPPLIER = "IGI"

    # Database Connection (Synchronous for invariant checks)
    mongo_client = MongoClient(MONGO_URI)
    procurement_col = mongo_client[DB_NAME][COL_NAME]


    def clear_procurement_orders():
        """Reset MongoDB collection state before running test suite."""
        procurement_col.delete_many({})


    def verify_invariants(iteration: int, expected_count: int, last_order_id: str, requested_qty: int):
        """Validates MongoDB record counts and schema/field invariants."""
        docs = list(procurement_col.find({}))

        # Invariant 1: Document count matches successful requests
        assert len(docs) == expected_count, (
            f"[Iter {iteration}] VIOLATION: Expected {expected_count} records in DB, found {len(docs)}"
        )

        order_ids = set()
        last_doc_found = False

        for doc in docs:
            order_id = doc.get("supplier_order_id")

            # Invariant 2: Unique supplier_order_id
            assert order_id not in order_ids, (
                f"[Iter {iteration}] VIOLATION: Duplicate supplier_order_id found: {order_id}"
            )
            order_ids.add(order_id)

            # Track if the last returned order ID is present
            if order_id == last_order_id:
                last_doc_found = True

            # Invariant 3: Field constraints
            assert doc.get("status") in ["PLACED", "FAILED"], (
                f"[Iter {iteration}] VIOLATION: Unexpected order status: {doc.get('status')}"
            )
            assert doc.get("qty", 0) > 0, (
                f"[Iter {iteration}] VIOLATION: Non-positive quantity: {doc.get('qty')}"
            )
            assert doc.get("eta_days", 0) >= 0, (
                f"[Iter {iteration}] VIOLATION: Invalid eta_days: {doc.get('eta_days')}"
            )

        assert last_doc_found, (
            f"[Iter {iteration}] VIOLATION: Order ID {last_order_id} not persisted in MongoDB"
        )


    def run_regression_test(N_TRIALS: int):
        print(f"--- Starting Procurement Service Regression Test ({N_TRIALS} Iterations) ---")
        clear_procurement_orders()

        latencies = []
        invariant_violations = 0

        for i in range(1, N_TRIALS + 1):
            qty = 100 + i
            payload = {
                "sku": SKU,
                "qty": qty,
                "preferred_supplier": PREFERRED_SUPPLIER
            }

            # Measure latency for POST /order_supplier
            t0 = time.perf_counter()
            resp = requests.post(f"{BASE_URL}/order_supplier", json=payload)
            t1 = time.perf_counter()

            latency_ms = (t1 - t0) * 1000
            latencies.append(latency_ms)

            if resp.status_code == 200:
                res_json = resp.json()
                order_id = res_json.get("supplier_order_id")

                # Check DB Invariants
                try:
                    verify_invariants(iteration=i, expected_count=i, last_order_id=order_id, requested_qty=qty)
                except AssertionError as e:
                    print(f"❌ {e}")
                    invariant_violations += 1
            else:
                print(f"❌ [Iter {i}] HTTP Error {resp.status_code}: {resp.text}")

        # Statistical Computations
        print("\n" + "=" * 50)
        print("      PROCUREMENT SERVICE REGRESSION SUMMARY      ")
        print("=" * 50)
        print(f"Total Runs           : {ITERATIONS}")
        print(f"Invariant Violations : {invariant_violations}")

        print("\n--- Latency Performance (POST /order_supplier) ---")
        print(f"Median (p50) Latency : {np.median(latencies):.3f} ms")
        print(f"p95 Latency          : {np.percentile(latencies, 95):.3f} ms")
        print(f"p99 Latency          : {np.percentile(latencies, 99):.3f} ms")
        print(f"Min / Max            : {np.min(latencies):.3f} ms / {np.max(latencies):.3f} ms")


    run_regression_test(N_TRIALS=ITERATIONS)

def product_search_agent_experiment_runner(N_TRIALS=100):
    # Configuration
    BASE_URL = "http://127.0.0.1:8008"
    INVENTORY_URL = "http://127.0.0.1:8001"
    PRICING_URL = "http://127.0.0.1:8002"
    MONGO_URI = "mongodb://localhost:27017/"
    DB_NAME = "retailben"
    ITERATIONS = N_TRIALS

    SKU_1 = "4cc0770f-91bc-4c0d-a26f-7b872f02ca94"
    SKU_2 = "b2926dc2-cc6d-4c3e-ae40-7a127c173b16"

    # Database Connection (Synchronous for invariant verification)
    mongo_client = MongoClient(MONGO_URI)
    db = mongo_client[DB_NAME]
    products_col = db["products"]
    inventory_col = db["inventory"]
    prices_col = db["prices"]


    def setup_environment():
        """Seed test data in Product, Inventory, and Pricing databases."""
        # Reset products
        products_col.delete_many({})
        
        # Create text index on products collection for search
        # products_col.create_index([("name", "text"), ("description", "text")], name="product_text_index")

        # Insert test products
        products_col.insert_many([
            {
                "sku": SKU_1,
                "name": "Noise Cancelling Headphones",
                "description": "Premium wireless headphones with active noise cancelling under 300$"
            },
            {
                "sku": SKU_2,
                "name": "Wireless Earbuds",
                "description": "In-ear noise cancelling wireless earbuds"
            }
        ])

        # Seed Inventory Service
        requests.post(f"{INVENTORY_URL}/reset_stocks", json={
            "items": [
                {"sku": SKU_1, "stock": 10},
                {"sku": SKU_2, "stock": 0}  # Out of stock to test stock filtering
            ]
        })

        # Seed Pricing Service
        requests.post(f"{PRICING_URL}/price/put", json={"product_id": SKU_1, "price": 250.0})
        requests.post(f"{PRICING_URL}/price/put", json={"product_id": SKU_2, "price": 150.0})


    def verify_search_invariants(search_resp: dict, iteration: int):
        """Verifies inventory filtering and schema constraints on search results."""
        results = search_resp.get("results", [])

        for item in results:
            sku = item["sku"]
            price = item["price"]
            score = item["score"]

            # Invariant 1: Stock Availability (Returned items must have stock > 0 in MongoDB)
            inv_doc = inventory_col.find_one({"sku": sku})
            stock = inv_doc["stock"] if inv_doc else 0
            assert stock > 0, (
                f"[Iter {iteration}] VIOLATION: Product {sku} returned in search results but has stock = {stock}"
            )

            # Invariant 2: Valid Price Constraint
            assert price >= 0.0, (
                f"[Iter {iteration}] VIOLATION: Invalid price ({price}) returned for SKU {sku}"
            )

            # Invariant 3: Score Non-Zero
            assert score > 0.0, (
                f"[Iter {iteration}] VIOLATION: Invalid text relevance score ({score}) for SKU {sku}"
            )


    def run_regression_test(N_TRIALS):
        print(f"--- Starting Product Search Service Regression Test ({N_TRIALS} Iterations) ---")
        setup_environment()

        latencies_search = []
        latencies_create = []
        invariant_violations = 0

        query_str = "looking for headphone with noise cancelling under 300$"

        for i in range(1, N_TRIALS + 1):
            # 1. Benchmark GET /search
            t0 = time.perf_counter()
            resp_search = requests.get(f"{BASE_URL}/search", params={"q": query_str})
            t1 = time.perf_counter()

            latency_ms = (t1 - t0) * 1000
            latencies_search.append(latency_ms)

            if resp_search.status_code == 200:
                try:
                    verify_search_invariants(resp_search.json(), i)
                except AssertionError as e:
                    print(f"❌ {e}")
                    invariant_violations += 1
            else:
                print(f"❌ [Iter {i}] GET /search HTTP Error {resp_search.status_code}: {resp_search.text}")

            # 2. Benchmark POST /products (Product Creation)
            new_sku = f"sku-dynamic-{i}"
            create_payload = {
                "sku": new_sku,
                "name": f"Dynamic Test Product {i}",
                "description": "Benchmarking product creation performance"
            }

            t0 = time.perf_counter()
            resp_create = requests.post(f"{BASE_URL}/products", json=create_payload)
            t1 = time.perf_counter()

            latencies_create.append((t1 - t0) * 1000)

            # Verify DB insertion for POST /products
            doc = products_col.find_one({"sku": new_sku})
            if not doc:
                print(f"❌ [Iter {i}] VIOLATION: Product {new_sku} not persisted in MongoDB")
                invariant_violations += 1

        # Statistical Analysis Summary
        print("\n" + "=" * 50)
        print("     PRODUCT SEARCH SERVICE REGRESSION SUMMARY     ")
        print("=" * 50)
        print(f"Total Runs           : {ITERATIONS}")
        print(f"Invariant Violations : {invariant_violations}")

        print("\n--- Latency Performance (GET /search) ---")
        print(f"Median (p50) Latency : {np.median(latencies_search):.3f} ms")
        print(f"p95 Latency          : {np.percentile(latencies_search, 95):.3f} ms")
        print(f"p99 Latency          : {np.percentile(latencies_search, 99):.3f} ms")
        print(f"Min / Max            : {np.min(latencies_search):.3f} ms / {np.max(latencies_search):.3f} ms")

        print("\n--- Latency Performance (POST /products) ---")
        print(f"Median (p50) Latency : {np.median(latencies_create):.3f} ms")
        print(f"p95 Latency          : {np.percentile(latencies_create, 95):.3f} ms")
        print(f"p99 Latency          : {np.percentile(latencies_create, 99):.3f} ms")


    run_regression_test(N_TRIALS=ITERATIONS)

def shipment_agent_experiment_runner(N_TRIALS=100):
    # Configuration
    BASE_URL = "http://127.0.0.1:8006"
    MONGO_URI = "mongodb://localhost:27017/"
    DB_NAME = "retailben"
    COL_NAME = "shipments"
    ITERATIONS = N_TRIALS

    # Database Connection (Synchronous for invariant checks)
    mongo_client = MongoClient(MONGO_URI)
    shipments_col = mongo_client[DB_NAME][COL_NAME]


    def clear_bookings():
        """Reset MongoDB collection state before starting regression test."""
        shipments_col.delete_many({})


    def verify_invariants(iteration: int, expected_count: int, last_shipment_id: str, last_order_id: str):
        """Validates MongoDB record counts and schema invariants."""
        docs = list(shipments_col.find({}))

        # Invariant 1: Document count matches successful requests
        assert len(docs) == expected_count, (
            f"[Iter {iteration}] VIOLATION: Expected {expected_count} records in DB, found {len(docs)}"
        )

        shipment_ids = set()
        last_doc_found = False

        for doc in docs:
            sid = doc.get("shipment_id")

            # Invariant 2: Unique shipment_id
            assert sid not in shipment_ids, (
                f"[Iter {iteration}] VIOLATION: Duplicate shipment_id found in DB: {sid}"
            )
            shipment_ids.add(sid)

            if sid == last_shipment_id:
                last_doc_found = True
                # Invariant 3: Order ID matching
                assert doc.get("order_id") == last_order_id, (
                    f"[Iter {iteration}] VIOLATION: Order ID mismatch in DB for shipment {sid}"
                )
                # Invariant 4: Tracking ID presence
                assert doc.get("tracking_id") is not None, (
                    f"[Iter {iteration}] VIOLATION: Missing tracking_id in DB for shipment {sid}"
                )

        assert last_doc_found, (
            f"[Iter {iteration}] VIOLATION: Shipment ID {last_shipment_id} was not persisted in MongoDB"
        )


    def test_business_validation_rules():
        """Verifies that invalid address payloads trigger expected HTTP error responses."""
        # Test 1: Short address (<5 chars) -> 400 Bad Request
        resp_short = requests.post(f"{BASE_URL}/book", json={"order_id": "val-1", "address": "abc"})
        assert resp_short.status_code == 400, (
            f"Validation Failure: Expected 400 for short address, got {resp_short.status_code}"
        )

        # Test 2: PO Box address -> 422 Unprocessable Entity
        resp_pobox = requests.post(f"{BASE_URL}/book", json={"order_id": "val-2", "address": "123 Main St PO Box 5"})
        assert resp_pobox.status_code == 422, (
            f"Validation Failure: Expected 422 for PO Box address, got {resp_pobox.status_code}"
        )


    def run_regression_test(N_TRIALS: int):
        print(f"--- Starting Shipment Service Regression Test ({N_TRIALS} Iterations) ---")
        clear_bookings()

        # Pre-test business validation assertions
        test_business_validation_rules()

        latencies = []
        invariant_violations = 0

        for i in range(1, N_TRIALS + 1):
            order_id = f"order-shipment-{i}"
            valid_address = f"{100 + i} Commercial Way, Suite 200"

            payload = {
                "order_id": order_id,
                "address": valid_address
            }

            # Measure latency for POST /book
            t0 = time.perf_counter()
            resp = requests.post(f"{BASE_URL}/book", json=payload)
            t1 = time.perf_counter()

            latency_ms = (t1 - t0) * 1000
            latencies.append(latency_ms)

            if resp.status_code == 200:
                res_json = resp.json()
                shipment_id = res_json.get("shipment_id")

                # Verify DB Invariants
                try:
                    verify_invariants(
                        iteration=i,
                        expected_count=i,
                        last_shipment_id=shipment_id,
                        last_order_id=order_id
                    )
                except AssertionError as e:
                    print(f"❌ {e}")
                    invariant_violations += 1
            else:
                print(f"❌ [Iter {i}] HTTP Error {resp.status_code}: {resp.text}")

        # Statistical Computations
        print("\n" + "=" * 50)
        print("       SHIPMENT SERVICE REGRESSION SUMMARY        ")
        print("=" * 50)
        print(f"Total Runs           : {ITERATIONS}")
        print(f"Invariant Violations : {invariant_violations}")

        print("\n--- Latency Performance (POST /book) ---")
        print(f"Median (p50) Latency : {np.median(latencies):.3f} ms")
        print(f"p95 Latency          : {np.percentile(latencies, 95):.3f} ms")
        print(f"p99 Latency          : {np.percentile(latencies, 99):.3f} ms")
        print(f"Min / Max            : {np.min(latencies):.3f} ms / {np.max(latencies):.3f} ms")

    run_regression_test(N_TRIALS=ITERATIONS)

def shopping_cart_agent_experiment_runner(N_TRIALS=100):
    # Configuration
    BASE_URL = "http://127.0.0.1:8003"
    MONGO_URI = "mongodb://localhost:27017/"
    DB_NAME = "retailben"
    COL_NAME = "carts"
    ITERATIONS = N_TRIALS

    SKU = "4cc0770f-91bc-4c0d-a26f-7b872f02ca94"

    # Database Connection (Synchronous for invariant checks)
    mongo_client = MongoClient(MONGO_URI)
    carts_col = mongo_client[DB_NAME][COL_NAME]


    def clear_carts():
        """Reset MongoDB carts collection before running regression test."""
        carts_col.delete_many({})


    def verify_invariants(iteration: int, cart_id: str, expected_sku: str, expected_qty: int):
        """Validates MongoDB document state and business invariants."""
        doc = carts_col.find_one({"cart_id": cart_id})

        # Invariant 1: Cart existence in DB
        assert doc is not None, (
            f"[Iter {iteration}] VIOLATION: Cart ID {cart_id} not found in MongoDB"
        )

        items = doc.get("items", [])
        found_item = False
        for item in items:
            # Invariant 2: Positive quantity constraint
            assert item.get("qty", 0) > 0, (
                f"[Iter {iteration}] VIOLATION: Non-positive quantity for SKU {item.get('sku')}"
            )

            if item.get("sku") == expected_sku:
                found_item = True
                # Invariant 3: Quantity accumulation correctness
                assert item.get("qty") == expected_qty, (
                    f"[Iter {iteration}] VIOLATION: Quantity mismatch for SKU {expected_sku}. "
                    f"Expected {expected_qty}, got {item.get('qty')}"
                )

        assert found_item, (
            f"[Iter {iteration}] VIOLATION: Expected SKU {expected_sku} missing from cart {cart_id}"
        )


    def run_regression_test(N_TRIALS):
        print(f"--- Starting Shopping Cart Service Regression Test ({N_TRIALS} Iterations) ---")
        clear_carts()

        latencies_add_item = []
        latencies_get_cart = []
        invariant_violations = 0

        for i in range(1, N_TRIALS + 1):
            # 1. Benchmark POST /cart/-1/items (Create cart via item addition shortcut)
            add_payload = {"sku": SKU, "qty": 1}

            t0 = time.perf_counter()
            resp_add = requests.post(f"{BASE_URL}/cart/-1/items", json=add_payload)
            t1 = time.perf_counter()
            latencies_add_item.append((t1 - t0) * 1000)

            if resp_add.status_code != 200:
                print(f"❌ [Iter {i}] Add Item Failed: {resp_add.text}")
                invariant_violations += 1
                continue

            resp_data = resp_add.json()
            cart_id = resp_data.get("cart_id")

            # 2. Benchmark GET /cart/{cart_id} (Retrieve Cart)
            t0 = time.perf_counter()
            resp_get = requests.get(f"{BASE_URL}/cart/{cart_id}")
            t1 = time.perf_counter()
            latencies_get_cart.append((t1 - t0) * 1000)

            if resp_get.status_code != 200:
                print(f"❌ [Iter {i}] Get Cart Failed: {resp_get.text}")
                invariant_violations += 1
                continue

            # Verify DB Invariants
            try:
                verify_invariants(iteration=i, cart_id=cart_id, expected_sku=SKU, expected_qty=1)
            except AssertionError as e:
                print(f"❌ {e}")
                invariant_violations += 1

        # Statistical Analysis Summary
        print("\n" + "=" * 50)
        print("      SHOPPING CART SERVICE REGRESSION SUMMARY    ")
        print("=" * 50)
        print(f"Total Runs           : {ITERATIONS}")
        print(f"Invariant Violations : {invariant_violations}")

        print("\n--- Latency Performance (POST /cart/-1/items) ---")
        print(f"Median (p50) Latency : {np.median(latencies_add_item):.3f} ms")
        print(f"p95 Latency          : {np.percentile(latencies_add_item, 95):.3f} ms")
        print(f"p99 Latency          : {np.percentile(latencies_add_item, 99):.3f} ms")
        print(f"Min / Max            : {np.min(latencies_add_item):.3f} ms / {np.max(latencies_add_item):.3f} ms")

        print("\n--- Latency Performance (GET /cart/{cart_id}) ---")
        print(f"Median (p50) Latency : {np.median(latencies_get_cart):.3f} ms")
        print(f"p95 Latency          : {np.percentile(latencies_get_cart, 95):.3f} ms")
        print(f"p99 Latency          : {np.percentile(latencies_get_cart, 99):.3f} ms")
        print(f"Min / Max            : {np.min(latencies_get_cart):.3f} ms / {np.max(latencies_get_cart):.3f} ms")


    run_regression_test(N_TRIALS=ITERATIONS)

def subscription_agent_experiment_runner(N_TRIALS=100):
    # Configuration
    BASE_URL = "http://127.0.0.1:8010"
    MONGO_URI = "mongodb://localhost:27017/"
    DB_NAME = "retailben"
    COL_NAME = "user_subscriptions"
    ITERATIONS = N_TRIALS

    # Database Connection (Synchronous for invariant checks)
    mongo_client = MongoClient(MONGO_URI)
    subscriptions_col = mongo_client[DB_NAME][COL_NAME]


    def clear_subscriptions():
        """Reset user_subscriptions collection before starting regression test."""
        subscriptions_col.delete_many({})


    def verify_invariants(user_id: str, promo_code: str, expected_discount: float):
        """Validates MongoDB record creation and schema integrity."""
        doc = subscriptions_col.find_one({"user_id": user_id, "promo_code": promo_code})

        # Invariant 1: Document existence in DB
        assert doc is not None, (
            f"VIOLATION: Subscription for user '{user_id}' with code '{promo_code}' not found in MongoDB"
        )

        # Invariant 2: Field integrity
        assert doc.get("discount_percent") == expected_discount, (
            f"VIOLATION: Discount mismatch for promo '{promo_code}'. Expected {expected_discount}, got {doc.get('discount_percent')}"
        )
        assert doc.get("subscription_id") is not None, (
            f"VIOLATION: Missing subscription_id for user '{user_id}'"
        )


    def test_business_validation_rules():
        """Validates edge cases and HTTP error statuses."""
        # Test 1: Non-existent promo code -> 404 Not Found
        resp_invalid = requests.post(
            f"{BASE_URL}/subscriptions",
            json={"user_id": "test_user_val", "email": "val@test.com", "promo_code": "INVALID_CODE"}
        )
        assert resp_invalid.status_code == 404, (
            f"Validation Failure: Expected 404 for invalid code, got {resp_invalid.status_code}"
        )

        # Test 2: Expired promo code (FLASH50) -> 410 Gone
        resp_expired = requests.post(
            f"{BASE_URL}/subscriptions",
            json={"user_id": "test_user_val", "email": "val@test.com", "promo_code": "FLASH50"}
        )
        assert resp_expired.status_code == 410, (
            f"Validation Failure: Expected 410 for expired code, got {resp_expired.status_code}"
        )

        # Test 3: Duplicate subscription -> 409 Conflict
        requests.post(
            f"{BASE_URL}/subscriptions",
            json={"user_id": "test_user_dup", "email": "dup@test.com", "promo_code": "SUMMER20"}
        )
        resp_dup = requests.post(
            f"{BASE_URL}/subscriptions",
            json={"user_id": "test_user_dup", "email": "dup@test.com", "promo_code": "SUMMER20"}
        )
        assert resp_dup.status_code == 409, (
            f"Validation Failure: Expected 409 for duplicate subscription, got {resp_dup.status_code}"
        )


    def run_regression_test(N_TRIALS):
        print(f"--- Starting Subscription Service Regression Test ({N_TRIALS} Iterations) ---")
        clear_subscriptions()

        # Verify edge cases before running performance loops
        test_business_validation_rules()

        latencies_catalogue = []
        latencies_post_sub = []
        latencies_get_sub = []
        invariant_violations = 0

        for i in range(1, N_TRIALS + 1):
            user_id = f"user_sub_{i}"
            email = f"user_{i}@example.com"

            # 1. Benchmark GET /catalogue
            t0 = time.perf_counter()
            resp_cat = requests.get(f"{BASE_URL}/catalogue")
            t1 = time.perf_counter()
            latencies_catalogue.append((t1 - t0) * 1000)

            if resp_cat.status_code != 200:
                print(f"❌ [Iter {i}] GET /catalogue failed: {resp_cat.status_code}")
                invariant_violations += 1
                continue

            # 2. Benchmark POST /subscriptions (SUMMER20: 20% discount)
            sub_payload = {
                "user_id": user_id,
                "email": email,
                "promo_code": "SUMMER20"
            }

            t0 = time.perf_counter()
            resp_post = requests.post(f"{BASE_URL}/subscriptions", json=sub_payload)
            t1 = time.perf_counter()
            latencies_post_sub.append((t1 - t0) * 1000)

            if resp_post.status_code != 201:
                print(f"❌ [Iter {i}] POST /subscriptions failed: {resp_post.status_code}")
                invariant_violations += 1
                continue

            # 3. Benchmark GET /subscriptions/{user_id}
            t0 = time.perf_counter()
            resp_get = requests.get(f"{BASE_URL}/subscriptions/{user_id}")
            t1 = time.perf_counter()
            latencies_get_sub.append((t1 - t0) * 1000)

            if resp_get.status_code != 200:
                print(f"❌ [Iter {i}] GET /subscriptions/{user_id} failed: {resp_get.status_code}")
                invariant_violations += 1
                continue

            # 4. Verify Active Subscriptions Invariant (Descending Order Check)
            subs = resp_get.json().get("subscriptions", [])
            if len(subs) > 1:
                discounts = [s["discount_percent"] for s in subs]
                assert discounts == sorted(discounts, reverse=True), (
                    f"[Iter {i}] VIOLATION: Subscriptions for '{user_id}' are not sorted by discount descending"
                )

            # 5. Verify Database State
            try:
                verify_invariants(user_id=user_id, promo_code="SUMMER20", expected_discount=20.0)
            except AssertionError as e:
                print(f"❌ {e}")
                invariant_violations += 1

        # Statistical Summary
        print("\n" + "=" * 55)
        print("      SUBSCRIPTION SERVICE REGRESSION SUMMARY     ")
        print("=" * 55)
        print(f"Total Runs           : {ITERATIONS}")
        print(f"Invariant Violations : {invariant_violations}")

        print("\n--- Latency Performance (GET /catalogue) ---")
        print(f"Median (p50) Latency : {np.median(latencies_catalogue):.3f} ms")
        print(f"p95 Latency          : {np.percentile(latencies_catalogue, 95):.3f} ms")
        print(f"p99 Latency          : {np.percentile(latencies_catalogue, 99):.3f} ms")
        print(f"Min / Max            : {np.min(latencies_catalogue):.3f} ms / {np.max(latencies_catalogue):.3f} ms")

        print("\n--- Latency Performance (POST /subscriptions) ---")
        print(f"Median (p50) Latency : {np.median(latencies_post_sub):.3f} ms")
        print(f"p95 Latency          : {np.percentile(latencies_post_sub, 95):.3f} ms")
        print(f"p99 Latency          : {np.percentile(latencies_post_sub, 99):.3f} ms")
        print(f"Min / Max            : {np.min(latencies_post_sub):.3f} ms / {np.max(latencies_post_sub):.3f} ms")

        print("\n--- Latency Performance (GET /subscriptions/{{user_id}}) ---")
        print(f"Median (p50) Latency : {np.median(latencies_get_sub):.3f} ms")
        print(f"p95 Latency          : {np.percentile(latencies_get_sub, 95):.3f} ms")
        print(f"p99 Latency          : {np.percentile(latencies_get_sub, 99):.3f} ms")
        print(f"Min / Max            : {np.min(latencies_get_sub):.3f} ms / {np.max(latencies_get_sub):.3f} ms")


    run_regression_test(N_TRIALS=ITERATIONS)


if __name__ == "__main__":
    # inventory_agent_experiment_runner(N_TRIALS=100)
    # payment_agent_experiment_runner(N_TRIALS=10)
    # pricing_agent_experiment_runner(N_TRIALS=100)
    # procurement_agent_experiment_runner(N_TRIALS=100)
    # product_search_agent_experiment_runner(N_TRIALS=100)
    # shipment_agent_experiment_runner(N_TRIALS=100)
    # shopping_cart_agent_experiment_runner(N_TRIALS=100)
    # subscription_agent_experiment_runner(N_TRIALS=100)
    order_agent_experiment_runner(N_TRIALS=100)
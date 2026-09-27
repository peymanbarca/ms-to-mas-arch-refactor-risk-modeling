import requests
import json
import time
import random
from concurrent.futures import ThreadPoolExecutor, as_completed
from pymongo import MongoClient
import os
import statistics
import sys
import argparse


# ---------------- CONFIG ----------------
# Strangler Pattern Endpoints (V1 (microservice) and V2 (agent))
SEARCH_SERVICE_URL_V1 = os.environ.get("SEARCH_SERVICE_URL_V1", "http://127.0.0.1:8008/search")
SEARCH_SERVICE_URL_V2 = os.environ.get("SEARCH_SERVICE_URL_V2", "http://127.0.0.1:9008/search")

CART_SERVICE_URL_V1 = os.environ.get("CART_SERVICE_URL_V1", "http://127.0.0.1:8003/cart/cart_id/items")
CART_SERVICE_URL_V2 = os.environ.get("CART_SERVICE_URL_V2", "http://127.0.0.1:9003/cart/cart_id/items")

ORDER_SERVICE_URL_V1 = os.environ.get("ORDER_SERVICE_URL_V1", "http://127.0.0.1:8000/cart/cart_id/checkout")
ORDER_SERVICE_URL_V2 = os.environ.get("ORDER_SERVICE_URL_V2", "http://127.0.0.1:9000/cart/cart_id/checkout")

# Strangler traffic split: percentage (0 to 100) of requests directed to V2
STRANGLER_V2_RATE = int(os.environ.get("STRANGLER_V2_RATE", "90"))

ITEM = "headphone"
SKU = "b2926dc2-cc6d-4c3e-ae40-7a127c173b16"
INIT_STOCK = 10
QTY = 2

total_full_trials_runs = 1

DELAY = float(os.environ.get("DELAY", "0"))             # seconds to sleep inside inventory agent
DROP_RATE = int(os.environ.get("DROP_RATE", "0"))       # percent 0-100
atomic_update = False

MONGO_URL = os.environ.get("MONGO_URL", "mongodb://localhost:27017/")
DB_NAME = os.environ.get("DB_NAME", "retailben")


logs = ['logs/order_agent.log', 'logs/inventory_agent.log', 'logs/payment_agent.log', 'logs/pricing_agent.log',
        'logs/procurement_agent.log', 'logs/product_search_agent.log', 'logs/shipment_agent.log',
        'logs/shopping_cart_agent.log']
for log in logs:
    with open(file=log, mode='w') as f:
        f.write('')


def real_db():
    client = MongoClient(MONGO_URL)
    db = client[DB_NAME]
    return client, db


def get_service_url(v1_url: str, v2_url: str, v2_rate: int) -> tuple[str, str]:
    """
    Determines whether to route to V1 or V2 based on the strangler V2 rate percentage.
    Returns the chosen URL and the version tag ('V1' or 'V2').
    """
    if random.randint(1, 100) <= v2_rate:
        return v2_url, "V2"
    return v1_url, "V1"


def run_trial(trial_id: int, delay: float, drop_rate: int, CONCURRENCY_RATE: int):
    try:
        start = time.time()
        result = {"trial": trial_id, "threads": CONCURRENCY_RATE,
                    "total_input_tokens": 0,
                    "total_output_tokens": 0,
                    "total_llm_calls": 0,
                    "total_api_calls": 0,
                    "total_api_calls_failure": 0}

        # ------------------- product search ---------------------------------
        st = time.time()
        search_url, search_version = get_service_url(SEARCH_SERVICE_URL_V1, SEARCH_SERVICE_URL_V2, STRANGLER_V2_RATE)
        params = {'q': 'looking for headphone with noise cancelling'}
        r = requests.get(url=search_url, params=params)
        result["total_api_calls"] += 1
        result["search_version"] = search_version
        if r.status_code != 200:
            result["total_api_calls_failure"] += 1
        r.raise_for_status()
        et = time.time()
        search_latency = round((et - st), 3)
        search_res = r.json()
        
        if search_res.get("results"):
            selected_sku = search_res["results"][0]["sku"]
            result["search_latency"] = search_latency
            result["selected_sku"] = selected_sku
        else:
            selected_sku = SKU 
            result["search_latency"] = search_latency
            result["selected_sku"] = selected_sku
        result["total_input_tokens"] += search_res.get("total_input_tokens", 0)
        result["total_output_tokens"] += search_res.get("total_output_tokens", 0)
        result["total_llm_calls"] += search_res.get("total_llm_calls", 0)

        # ---------------- add cart -----------------------------
        st = time.time()
        cart_url, cart_version = get_service_url(CART_SERVICE_URL_V1, CART_SERVICE_URL_V2, STRANGLER_V2_RATE)
        r = requests.post(url=cart_url.replace('cart_id', '-1'), json={'sku': SKU, 'qty': QTY})
        result["total_api_calls"] += 1
        result["cart_version"] = cart_version
        if r.status_code != 200:
            result["total_api_calls_failure"] += 1
        r.raise_for_status()
        et = time.time()
        cart_latency = round((et - st), 3)
        cart_res = r.json()
        cart_id = cart_res['cart_id']
        result["cart_id"] = cart_id
        result["cart_latency"] = cart_latency
        result["total_input_tokens"] += cart_res.get("total_input_tokens", 0)
        result["total_output_tokens"] += cart_res.get("total_output_tokens", 0)
        result["total_llm_calls"] += cart_res.get("total_llm_calls", 0)

        # ----------------------- main workflow for purchase cart with order -------------------
        st = time.time()
        order_url, order_version = get_service_url(ORDER_SERVICE_URL_V1, ORDER_SERVICE_URL_V2, STRANGLER_V2_RATE)
        resp = requests.post(order_url.replace('cart_id', cart_id), timeout=30)
        result["total_api_calls"] += 1
        result["order_version"] = order_version
        if resp.status_code != 200:
            result["total_api_calls_failure"] += 1
        resp.raise_for_status()
        et = time.time()
        order_latency = round((et - st), 3)
        order_result = resp.json()
        result["order_latency"] = order_latency
        result["total_input_tokens"] += order_result.get("total_input_tokens", 0)
        result["total_output_tokens"] += order_result.get("total_output_tokens", 0)
        result["total_llm_calls"] += order_result.get("total_llm_calls", 0)

        elapsed = time.time() - start
        if resp.status_code == 200:
            result["order_id"] = order_result.get("order_id")
            result["status"] = order_result.get("status")
            result["elapsed"] = round(elapsed, 3)
            print(f"Trial {trial_id} [Strangler Rate: {STRANGLER_V2_RATE}% V2]: {result}")
            return result
        else:
            print(f"Trial {trial_id}: ERROR: {resp.text}")
            return {"trial": trial_id, "status": "error", "elapsed": round(elapsed, 3)}
    except Exception as e:
        elapsed = time.time() - start
        print(f"Trial {trial_id}: Exception {e}")
        exc_type, exc_obj, tb = sys.exc_info()
        line_number = tb.tb_lineno
        print(f"An error occurred on line: {line_number}, exc_type: {exc_type}")
        print(f"Error details: {e}")        
        return {"trial": trial_id, "status": "error", "elapsed": round(elapsed, 3)}


def get_final_state():
    client, db = real_db()
    final_stock = db.inventory.find_one({"sku": SKU})
    stock_left = final_stock["stock"] if final_stock else 0
    total_completed_orders = db.orders.count_documents({"status": "COMPLETED"})
    total_pending_orders = db.orders.count_documents({"status": "INIT"})
    total_oos_orders = db.orders.count_documents({"status": "OUT_OF_STOCK"})
    total_payments = db.payments.count_documents({"status": "SUCCESS"})
    total_shipment_bookings = db.shipments.count_documents({})

    final_ec_state = "SUCCESS"
    failure_rate = 0.0
    expected_total_reserved = int((INIT_STOCK) / QTY)

    if stock_left < 0:
        failure_rate += -stock_left / QTY
        final_ec_state = "FAIL"
    elif stock_left + total_completed_orders != expected_total_reserved:
        failure_rate += abs((total_completed_orders - (expected_total_reserved - stock_left)))
        final_ec_state = "FAIL"
    if total_pending_orders > 0:
        failure_rate += total_pending_orders
        final_ec_state = "FAIL"
    if total_payments != expected_total_reserved:
        failure_rate += expected_total_reserved - total_payments
        final_ec_state = "FAIL"
    if total_shipment_bookings != expected_total_reserved:
        failure_rate += expected_total_reserved - total_shipment_bookings
        final_ec_state = "FAIL"
    return stock_left, total_completed_orders, total_pending_orders, total_oos_orders, expected_total_reserved, \
           total_shipment_bookings, total_payments, \
           final_ec_state, failure_rate


def full_trials_runner(CONCURRENCY_RATE, R):
    run_results = []

    for i in range(total_full_trials_runs):
        # ----------------- reset system ------------------
        requests.post("http://localhost:8000/clear_orders", json={})
        requests.post("http://localhost:8001/reset_stocks", json={
            "items": [{"sku": SKU, "stock": INIT_STOCK}]})
        requests.post("http://localhost:8007/clear_payments", json={})
        requests.post("http://localhost:8006/clear_bookings", json={})

        print('Check DB state is clean ...')

        results = []

        # ---------------- PARALLEL EXECUTION of TRIALS ----------------
        with ThreadPoolExecutor(max_workers=CONCURRENCY_RATE) as executor:
            futures = [executor.submit(run_trial, tid, DELAY, DROP_RATE, CONCURRENCY_RATE) for tid in range(1, R + 1)]
            for future in as_completed(futures):
                results.append(future.result())

        stock_left, total_completed_orders, total_pending_orders, total_oos_orders, expected_total_reserved, \
            total_shipment_bookings, total_payments, \
            final_ec_state, qa_inconsistency_rate = get_final_state()

        summary = {
            "n_trials": R,
            "delay": DELAY,
            "drop_rate": DROP_RATE,
            "strangler_v2_rate": STRANGLER_V2_RATE,
            "n_threads": CONCURRENCY_RATE,
            "stock_left": stock_left,
            "total_completed_orders": total_completed_orders,
            "total_pending_orders": total_pending_orders,
            "total_oos_orders": total_oos_orders,
            "expected_total_reserved": expected_total_reserved,
            "total_shipment_bookings": total_shipment_bookings,
            "total_payments": total_payments,
            "final_ec_state": final_ec_state,
            "qa_inconsistency_rate": (qa_inconsistency_rate / R) * 100,
            "avg_search_latency": statistics.mean([x['search_latency'] for x in results if x.get('search_latency')]),
            "std_search_latency": statistics.stdev([x['search_latency'] for x in results if x.get('search_latency')]) if len([x['search_latency'] for x in results if x.get('search_latency')]) > 1 else 0,
            "p95_search_latency": statistics.quantiles(data=[x['search_latency'] for x in results if x.get('search_latency')], n=100)[95] if len([x['search_latency'] for x in results if x.get('search_latency')]) >= 2 else 0,
            "med_search_latency": statistics.median([x['search_latency'] for x in results if x.get('search_latency')]),
            "avg_latency": statistics.mean([x['elapsed'] for x in results if x.get('elapsed')]),
            "std_latency": statistics.stdev([x['elapsed'] for x in results if x.get('elapsed')]) if len([x['elapsed'] for x in results if x.get('elapsed')]) > 1 else 0,
            "p95_latency": statistics.quantiles(data=[x['elapsed'] for x in results if x.get('elapsed')], n=100)[95] if len([x['elapsed'] for x in results if x.get('elapsed')]) >= 2 else 0,
            "med_latency": statistics.median([x['elapsed'] for x in results if x.get('elapsed')]),
            "v2_search_calls": sum(1 for x in results if x.get('search_version') == 'V2'),
            "v2_cart_calls": sum(1 for x in results if x.get('cart_version') == 'V2'),
            "v2_order_calls": sum(1 for x in results if x.get('order_version') == 'V2'),
            "total_input_tokens": sum([x.get('total_input_tokens', 0) for x in results]),
            "total_output_tokens": sum([x.get('total_output_tokens', 0) for x in results]),
            "total_llm_calls": sum([x.get('total_llm_calls', 0) for x in results]),
            "total_api_calls": sum([x.get('total_api_calls', 0) for x in results]),
            "total_api_calls_failure": sum([x.get('total_api_calls_failure', 0) for x in results]),
        }
        print("Final summary:", summary)
        run_results.append({"run_number": i + 1, "trial_results": results, "final_summary": summary})
        print(f"Full Trials Run {i + 1} Done,\n-----------------------------------------")

    return run_results

if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description="End-to-end load generator with Strangler Pattern support",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--trials",        type=int,   default=10,
                        help="Total number of end-to-end trials (default: 10)")
    parser.add_argument("--concurrency",   type=int,   default=1,
                        help="Parallel worker threads (default: 1)")
    args = parser.parse_args()
    
    N_TRIALS = args.trials
    CONCURRENCY_RATE = args.concurrency

    os.makedirs('results', exist_ok=True)
    log_telemetry_report_file = 'results/log_telemetry.json'

    run_results = full_trials_runner(CONCURRENCY_RATE=CONCURRENCY_RATE, R=N_TRIALS)
    
    with open(log_telemetry_report_file, "w") as f:
        json.dump(run_results, f, indent=4)
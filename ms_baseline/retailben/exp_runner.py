import requests
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pymongo import MongoClient
import os
import statistics

# ---------------- CONFIG ----------------
SEARCH_SERVICE_URL = "http://127.0.0.1:8008/search"
CART_SERVICE_URL = "http://127.0.0.1:8003/cart/cart_id/items"
ORDER_SERVICE_URL = "http://127.0.0.1:8000/cart/cart_id/checkout"

ITEM = "headphone"
SKU = "b2926dc2-cc6d-4c3e-ae40-7a127c173b16"
INIT_STOCK = 10
QTY = 2

N_TRIALS = 10
MAX_WORKERS = N_TRIALS / 2  # Number of concurrent threads
total_runs = 1

DELAY = float(os.environ.get("DELAY", "0"))             # seconds to sleep inside inventory agent
DROP_RATE = int(os.environ.get("DROP_RATE", "0"))       # percent 0-100
atomic_update = False

MONGO_URL = os.environ.get("MONGO_URL", "mongodb://user:pass1@localhost:27017/")
DB_NAME = os.environ.get("DB_NAME", "ms_baseline")


logs = ['logs/order_service.log', 'logs/inventory_service.log', 'logs/payment_service.log', 'logs/pricing_service.log',
        'logs/procurement_service.log', 'logs/product_search_service.log', 'logs/shipment_service.log',
        'logs/shopping_cart_service.log']
for log in logs:
    with open(file=log, mode='w') as f:
        f.write('')


def real_db():
    client = MongoClient(MONGO_URL)
    db = client[DB_NAME]
    return client, db


def run_trial(trial_id: int, delay: float, drop_rate: int):
    try:
        start = time.time()
        result = {"trial": trial_id, "threads": MAX_WORKERS}

        # ------------------- product search ---------------------------------
        st = time.time()
        params = {'q': 'looking for headphone with noise cancelling'}
        r = requests.get(url=SEARCH_SERVICE_URL, params=params)
        r.raise_for_status()
        et = time.time()
        search_latency = round((et - st), 3)
        search_res = r.json()
        # print(f"Result of product search: {search_res}, latency: {search_latency}")
        selected_sku = search_res["results"][0]["sku"]
        result["search_latency"] = search_latency
        result["selected_sku"] = selected_sku

        # ---------------- add cart -----------------------------
        st = time.time()
        r = requests.post(url=CART_SERVICE_URL.replace('cart_id', '-1'), json={'sku': SKU, 'qty': QTY})
        et = time.time()
        cart_latency = round((et - st), 3)
        cart_res = r.json()
        cart_id = cart_res['cart_id']
        result["cart_id"] = cart_id
        result["cart_latency"] = cart_latency


        # ----------------------- main workflow for purchase cart with order -------------------
        st = time.time()
        resp = requests.post(ORDER_SERVICE_URL.replace('cart_id', cart_id), timeout=30)
        et = time.time()
        order_latency = round((et - st), 3)
        result["order_latency"] = order_latency

        elapsed = time.time() - start
        if resp.status_code == 200:
            order_result = resp.json()
            result["order_id"] = order_result["order_id"]
            result["status"] = order_result["status"]
            result["elapsed"] = round(elapsed, 3)
            print(f"Trial {trial_id}: {result}")
            return result
        else:
            print(f"Trial {trial_id}: ERROR: {resp.json()}")
            return {"trial": trial_id, "status": "error", "elapsed": round(elapsed,3)}
    except Exception as e:
        elapsed = time.time() - start
        print(f"Trial {trial_id}: Exception {e}")
        return {"trial": trial_id, "status": "error", "elapsed": round(elapsed,3)}


def get_final_state():

    client, db = real_db()
    final_stock = db.inventory.find_one({"sku": SKU})
    stock_left = final_stock["stock"] if final_stock else 0
    total_completed_orders = db.orders.count_documents({"status": "COMPLETED"})
    total_pending_orders = db.orders.count_documents({"status": "INIT"})
    total_oos_orders = db.orders.count_documents({"status": "OUT_OF_STOCK"})
    total_payments = db.payments.count_documents({"status": "SUCCESS"})
    total_shipment_bookings = db.shipments.count_documents({})

    # basic heuristics used previously: compute failure rate loosely
    final_ec_state = "SUCCESS"
    failure_rate = 0.0
    expected_total_reserved = int((INIT_STOCK) / QTY)  # approximate expectation from your earlier code

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


if __name__ == '__main__':

    with open(f"results/ms_baseline_results_delay_{DELAY}_drop_{DROP_RATE}.json", "w") as f:
        f.write("")

    run_results = []

    for i in range(total_runs):

        requests.post("http://localhost:8000/clear_orders", json={})
        requests.post("http://localhost:8001/reset_stocks", json={
          "items": [
            {
              "sku": SKU,
              "stock": INIT_STOCK
            }
          ]
        })
        requests.post("http://localhost:8007/clear_payments", json={})
        requests.post("http://localhost:8006/clear_bookings", json={})

        print('Check DB state is clean ...')

        results = []

        # ---------------- PARALLEL EXECUTION ----------------
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = [executor.submit(run_trial, i, DELAY, DROP_RATE) for i in range(1, N_TRIALS + 1)]
            for future in as_completed(futures):
                results.append(future.result())

        stock_left, total_completed_orders, total_pending_orders, total_oos_orders, expected_total_reserved, \
        total_shipment_bookings, total_payments,        \
        final_ec_state, failure_rate = get_final_state()

        summary = {
            "n_trials": N_TRIALS,
            "delay": DELAY,
            "drop_rate": DROP_RATE,
            "n_threads": MAX_WORKERS,
            "stock_left": stock_left,
            "total_completed_orders": total_completed_orders,
            "total_pending_orders": total_pending_orders,
            "total_oos_orders": total_oos_orders,
            "expected_total_reserved": expected_total_reserved,
            "total_shipment_bookings": total_shipment_bookings,
            "total_payments": total_payments,
            "final_ec_state": final_ec_state,
            "failure_rate": (failure_rate / N_TRIALS) * 100,
            "avg_search_latency": statistics.mean([x['search_latency'] for x in results if x.get('search_latency')]),
            "std_search_latency": statistics.stdev([x['search_latency'] for x in results if x.get('search_latency')]),
            "p95_search_latency": statistics.quantiles(data=[x['search_latency'] for x in results if x.get('search_latency')], n=100)[95],
            "med_search_latency": statistics.median([x['search_latency'] for x in results if x.get('search_latency')]),
            "avg_latency": statistics.mean([x['elapsed'] for x in results if x.get('elapsed')]),
            "std_latency": statistics.stdev([x['elapsed'] for x in results if x.get('elapsed')]),
            "p95_latency": statistics.quantiles(data=[x['elapsed'] for x in results if x.get('elapsed')], n=100)[95],
            "med_latency": statistics.median([x['elapsed'] for x in results if x.get('elapsed')]),
        }
        print("Final summary:", summary)
        run_results.append({"run_number": i + 1, "trial_results": results, "final_summary": summary})
        print(f"Run {i + 1} Done,\n-----------------------------------------")

    # Save all results
    with open(f"results/ms_baseline_results_delay_{DELAY}_drop_{DROP_RATE}.json", "w") as f:
        f.write("\n\n")
        json.dump(run_results, f)
        f.write("\n\n")

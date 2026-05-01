import sys

import requests
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from networkx.algorithms.approximation.clique import large_clique_size
from pymongo import MongoClient
import os
import statistics
import random

# ----------------- RUNTIME Configuration ----------------
LLM = "llama3" # "llama3.2:3b" or "llama3:8b"
T = 0 # 0 or 0.8

# ----------------- Concurrency Configuration (low / high) ----------------

N_TRIALS = 10
CONCURRENCY_RATE = int (N_TRIALS / 10)  # Number of concurrent threads
total_full_trials_runs = 1



# ---------------- CONFIG ----------------
SEARCH_SERVICE_URL = "http://127.0.0.1:8008/search"
CART_SERVICE_URL = "http://127.0.0.1:8003/cart/cart_id/items"
ORDER_SERVICE_URL = "http://127.0.0.1:8000/cart/cart_id/checkout"

ITEM = "headphone"
SKU = "b2926dc2-cc6d-4c3e-ae40-7a127c173b16"
INIT_STOCK = 10
QTY = 2

DELAY = float(os.environ.get("DELAY", "0"))             # seconds to sleep inside inventory agent
DROP_RATE = int(os.environ.get("DROP_RATE", "0"))       # percent 0-100
atomic_update = False

MONGO_URL = os.environ.get("MONGO_URL", "mongodb://localhost:27017/")
DB_NAME = os.environ.get("DB_NAME", "ms_baseline")


logs = ['logs/order_agent.log', 'logs/inventory_agent.log', 'logs/payment_agent.log', 'logs/pricing_agent.log',
        'logs/procurement_agent.log', 'logs/product_search_agent.log', 'logs/shipment_agent.log',
        'logs/shopping_cart_agent.log']
for log in logs:
    try:
        with open(file=log, mode='w') as f:
            f.write('')
    except Exception as e:
        pass


def real_db():
    client = MongoClient(MONGO_URL)
    db = client[DB_NAME]
    return client, db


def run_trial(trial_id: int, delay: float, drop_rate: int):
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
        params = {'q': 'looking for headphone with noise cancelling'}
        r = requests.get(url=SEARCH_SERVICE_URL, params=params)
        result["total_api_calls"] += 1
        if r.status_code != 200:
            result["total_api_calls_failure"] += 1
        r.raise_for_status()
        et = time.time()
        search_latency = round((et - st), 3)
        search_res = r.json()
        # print(f"Result of product search: {search_res}, latency: {search_latency}")
        selected_sku = search_res["results"][0]["sku"]
        result["search_latency"] = search_latency
        result["selected_sku"] = selected_sku
        result["total_input_tokens"] += search_res["total_input_tokens"]
        result["total_output_tokens"] += search_res["total_output_tokens"]
        result["total_llm_calls"] += search_res["total_llm_calls"]

        # ---------------- add cart -----------------------------
        st = time.time()
        r = requests.post(url=CART_SERVICE_URL.replace('cart_id', '-1'), json={'sku': SKU, 'qty': QTY})
        result["total_api_calls"] += 1
        if r.status_code != 200:
            result["total_api_calls_failure"] += 1
        r.raise_for_status()
        et = time.time()
        cart_latency = round((et - st), 3)
        cart_res = r.json()
        cart_id = cart_res['cart_id']
        result["cart_id"] = cart_id
        result["cart_latency"] = cart_latency
        result["total_input_tokens"] += cart_res["total_input_tokens"]
        result["total_output_tokens"] += cart_res["total_output_tokens"]
        result["total_llm_calls"] += cart_res["total_llm_calls"]

        # ----------------------- main workflow for purchase cart with order -------------------
        st = time.time()
        resp = requests.post(ORDER_SERVICE_URL.replace('cart_id', cart_id), timeout=30)
        result["total_api_calls"] += 1
        if resp.status_code != 200:
            result["total_api_calls_failure"] += 1
        resp.raise_for_status()
        et = time.time()
        order_latency = round((et - st), 3)
        order_result = resp.json()
        result["order_latency"] = order_latency
        result["total_input_tokens"] += order_result["total_input_tokens"]
        result["total_output_tokens"] += order_result["total_output_tokens"]
        result["total_llm_calls"] += order_result["total_llm_calls"]

        elapsed = time.time() - start
        if resp.status_code == 200:
            result["order_id"] = order_result["order_id"]
            result["status"] = order_result["status"]
            result["elapsed"] = round(elapsed, 3)
            print(f"Trial {trial_id}: {result}")
            return result
        else:
            print(f"Trial {trial_id}: ERROR: {resp.text()}")
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


def full_trials_runner():
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
            futures = [executor.submit(run_trial, i, DELAY, DROP_RATE) for i in range(1, N_TRIALS + 1)]
            for future in as_completed(futures):
                results.append(future.result())

        stock_left, total_completed_orders, total_pending_orders, total_oos_orders, expected_total_reserved, \
            total_shipment_bookings, total_payments, \
            final_ec_state, qa_inconsistency_rate = get_final_state()

        summary = {
            "n_trials": N_TRIALS,
            "delay": DELAY,
            "drop_rate": DROP_RATE,
            "n_threads": CONCURRENCY_RATE,
            "stock_left": stock_left,
            "total_completed_orders": total_completed_orders,
            "total_pending_orders": total_pending_orders,
            "total_oos_orders": total_oos_orders,
            "expected_total_reserved": expected_total_reserved,
            "total_shipment_bookings": total_shipment_bookings,
            "total_payments": total_payments,
            "final_ec_state": final_ec_state,
            "qa_inconsistency_rate": (qa_inconsistency_rate / N_TRIALS) * 100,
            "avg_search_latency": statistics.mean([x['search_latency'] for x in results if x.get('search_latency')]),
            "std_search_latency": statistics.stdev([x['search_latency'] for x in results if x.get('search_latency')]),
            "p95_search_latency":
                statistics.quantiles(data=[x['search_latency'] for x in results if x.get('search_latency')], n=100)[95],
            "med_search_latency": statistics.median([x['search_latency'] for x in results if x.get('search_latency')]),
            "avg_latency": statistics.mean([x['elapsed'] for x in results if x.get('elapsed')]),
            "std_latency": statistics.stdev([x['elapsed'] for x in results if x.get('elapsed')]),
            "p95_latency": statistics.quantiles(data=[x['elapsed'] for x in results if x.get('elapsed')], n=100)[95],
            "med_latency": statistics.median([x['elapsed'] for x in results if x.get('elapsed')]),
            "total_input_tokens": sum([x['total_input_tokens'] for x in results if x.get('total_input_tokens')]),
            "total_output_tokens": sum([x['total_output_tokens'] for x in results if x.get('total_output_tokens')]),
            "total_llm_calls": sum([x['total_llm_calls'] for x in results if x.get('total_llm_calls')]),
            "total_api_calls": sum([x['total_api_calls'] for x in results if x.get('total_api_calls')]),
            "total_api_calls_failure": sum([x['total_api_calls_failure'] for x in results if x.get('total_api_calls_failure')]),
        }
        print("Final summary:", summary)
        run_results.append({"run_number": i + 1, "trial_results": results, "final_summary": summary})
        print(f"Full Trials Run {i + 1} Done,\n-----------------------------------------")

    return run_results


def run_experiment_of_architecture_step_full_predicate():
    with open(f"results/refactored_arch_results_llm_{LLM}_T_{T}_U_{CONCURRENCY_RATE}"
              f".json", "w") as f:
        f.write("\n\n")

    full_run_results = full_trials_runner()

    # Save all results
    with open(f"results/refactored_arch_results_llm_{LLM}_T_{T}_U_{CONCURRENCY_RATE}"
              f".json", "w") as f:
        f.write("\n\n")
        json.dump(full_run_results, f)
        f.write("\n\n")

    p95_latency = full_run_results[0]["final_summary"]["p95_latency"]
    qa_inconsistency_rate = full_run_results[0]["final_summary"]["qa_inconsistency_rate"]
    failure_rate = (full_run_results[0]["final_summary"]["total_api_calls_failure"]
                    / full_run_results[0]["final_summary"]["total_api_calls"]) \
        if full_run_results[0]["final_summary"]["total_api_calls"] > 0 else 0
    print(f"Final p95 latency: {p95_latency}, QA inconsistency rate: {qa_inconsistency_rate}%,"
          f" failure rate: {failure_rate*100}%")

    return p95_latency, qa_inconsistency_rate, failure_rate


def acceptance_of_architecture_step_predicate_based(epsilon_l, epsilon_qa, epsilon_f):
    latency_predicate_failed = None; qa_predicate_failed = None; failure_rate_predicate_failed = None
    # p95_latency, qa_inconsistency_rate, failure_rate = run_experiment_of_architecture_step_full_predicate()
    # # check with baseline w.s.t thresholds:
    # if epsilon_l and epsilon_l > -1:
    #     if p95_latency > epsilon_l:
    #         success =  False
    #         latency_predicate_failed = True
    #     else:
    #         success = True
    #         latency_predicate_failed = False
    # if epsilon_qa and epsilon_qa > -1:
    #     if qa_inconsistency_rate > epsilon_qa:
    #         success =  False
    #         qa_predicate_failed = True
    #     else:
    #         success = True
    #         qa_predicate_failed = False
    # if epsilon_f and epsilon_f > -1:
    #     if failure_rate > epsilon_f:
    #         success = False
    #         failure_rate_predicate_failed = True
    #     else:
    #         success = True
    #         failure_rate_predicate_failed = False
    # success = False

    p95_latency, qa_inconsistency_rate, failure_rate = 1.1, 0.1, 0.01  # dummy values for testing
    latency_predicate_failed = True; qa_predicate_failed = True; failure_rate_predicate_failed = True
    success = random.choices([True, False], weights=[3, 1])[0]  # 70% success


    result = {
        "epsilon_l": epsilon_l,
        "epsilon_qa": epsilon_qa,
        "epsilon_f": epsilon_f,
        "p95_latency": p95_latency,
        "qa_inconsistency_rate": qa_inconsistency_rate,
        "failure_rate": failure_rate,
        "latency_predicate_failed": latency_predicate_failed,
        "qa_predicate_failed": qa_predicate_failed,
        "failure_rate_predicate_failed": failure_rate_predicate_failed,
        "success": success
    }
    return result

if __name__ == '__main__':
    migration_order = sys.argv[1]
    acceptance_predicate_mode = sys.argv[2]
    step = sys.argv[3]
    services = sys.argv[4]
    agents = sys.argv[5]
    epsilon_l = sys.argv[6]
    epsilon_qa = sys.argv[7]
    epsilon_f = sys.argv[8]
    if len(sys.argv) < 9:
        raise ValueError("Expected: migration_order predicate-mode step services agents epsilon_l epsilon_qa epsilon_f")

    acceptance_result = acceptance_of_architecture_step_predicate_based(
                                                                        epsilon_l=epsilon_l,
                                                                        epsilon_qa=epsilon_qa,
                                                                        epsilon_f=epsilon_f)

    full_run_step_results = {"migration_order": migration_order,
                             "step": step, "services": services, "agents": agents, "acceptance_result": acceptance_result}
    step_report_file_name = f"results/refactored_arch_results_llm_{LLM}_T_{T}_U_{CONCURRENCY_RATE}" \
              f"_migration_order_{migration_order}_acceptance_predicate_mode_{acceptance_predicate_mode}_step_{step}.json"
    # print(step_report_file_name, full_run_step_results)
    with open(step_report_file_name, "w") as f:
        f.write("\n\n")
        json.dump(full_run_step_results, f, indent=2)
        f.write("\n\n")

    if acceptance_result["success"]:
        print(json.dumps({"result": "ACCEPTED", "details": acceptance_result}))
    else:
        print(json.dumps({"result": "REJECTED", "details": acceptance_result}))



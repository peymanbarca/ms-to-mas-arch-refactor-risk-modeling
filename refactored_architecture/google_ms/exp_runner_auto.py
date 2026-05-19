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
from .exp_runner import full_trials_runner


# ----------------- RUNTIME Configuration ----------------
LLM = "llama3" # "llama3.2:3b" or "llama3:8b"
T = 0 # 0 or 0.8

# ----------------- Concurrency Configuration (low / high) ----------------

N_TRIALS = 10
CONCURRENCY_RATE = 5  # Number of concurrent threads
total_full_trials_runs = 1


def run_experiment_of_architecture_step_full_predicate():
    with open(f"./results/refactored_arch_results_llm_{LLM}_T_{T}_U_{CONCURRENCY_RATE}"
              f".json", "w") as f:
        f.write("\n\n")

    full_run_results = full_trials_runner()

    # Save all results
    with open(f"./results/refactored_arch_results_llm_{LLM}_T_{T}_U_{CONCURRENCY_RATE}"
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
    
    # -------------- Real execution of the architecture step and evaluation of predicates --------------
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


    p95_latency, qa_inconsistency_rate, failure_rate = 1.1, 0.1, 0.01 
    latency_predicate_failed = True; qa_predicate_failed = True; failure_rate_predicate_failed = True
    success = random.choices([True, False], weights=[3, 1])[0]  # 70% success
    step_self_temporal_propagation = random.uniform(0.2, 0.7)  # Simulate some temporal propagation effect


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
        "success": success,
        "step_self_temporal_propagation": step_self_temporal_propagation
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
    governance_mode = sys.argv[9]
    target_service = sys.argv[10]
    previous_step_acceptance_type = sys.argv[11]
    temporal_propagation_effect_enabled = sys.argv[12]
    migration_sorting_strategy_services = sys.argv[13]
    if len(sys.argv) < 14:
        raise ValueError("Expected: migration_order predicate-mode step services agents epsilon_l epsilon_qa epsilon_f governance_mode target_service temporal_propagation_effect_enabled migration_sorting_strategy_services")

    acceptance_result = acceptance_of_architecture_step_predicate_based(
                                                                        epsilon_l=epsilon_l,
                                                                        epsilon_qa=epsilon_qa,
                                                                        epsilon_f=epsilon_f)


    full_run_step_results = {"migration_order": migration_order, "migration_sorting_strategy_services": migration_sorting_strategy_services,
                             "previous_step_acceptance_type": previous_step_acceptance_type,
                             "step": step, "services": services, "agents": agents, "acceptance_result": acceptance_result,
                             "acceptance_predicate_mode": acceptance_predicate_mode, "governance_mode": governance_mode,
                             "target_service": target_service, "temporal_propagation_effect_enabled": temporal_propagation_effect_enabled,
                             "predicate_acceptance_result": acceptance_result["success"]}
    
    step_report_file_name = f"refactored_architecture/google_ms/results/refactored_arch_results_llm_{LLM}_T_{T}_U_{CONCURRENCY_RATE}" \
              f"_migration_order_{migration_order}_acceptance_predicate_mode_{acceptance_predicate_mode}_governance_mode_{governance_mode}_tprop_enabled_{temporal_propagation_effect_enabled}.json"
    # print(step_report_file_name, full_run_step_results)
    
    if step=="1":
         # For the first step, we create a new report file (overwriting if it already exists)
        with open(step_report_file_name, "w") as f:
            f.write("")
    
    with open(step_report_file_name, "a") as f:
        f.write("\n\n")
        json.dump(full_run_step_results, f, indent=2)
        f.write("\n\n------------\n\n")

    if acceptance_result["success"]:
        print(json.dumps({"result": "ACCEPTED", "step_self_temporal_propagation": acceptance_result["step_self_temporal_propagation"], "details": acceptance_result}))
    else:
        print(json.dumps({"result": "REJECTED", "step_self_temporal_propagation": acceptance_result["step_self_temporal_propagation"], "details": acceptance_result}))
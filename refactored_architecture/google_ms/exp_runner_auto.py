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
from pathlib import Path


N_TRIALS = 5000
total_full_trials_runs = 1


def run_experiment_of_architecture_step_full_predicate(LLM, T, CONCURRENCY_RATE, log_telemetry_file):
    with open(log_telemetry_file, "w") as f:
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


def acceptance_of_architecture_step_predicate_based(epsilon_l, epsilon_qa, epsilon_f, acceptance_predicate_mode, target_service, step, T, LLM, CONCURRENCY_RATE, log_telemetry_file):
    
    log_telemetry_file = log_telemetry_file.replace("res_LLM", "telemetry_res_LLM") 
    latency_predicate_failed = None; qa_predicate_failed = None; failure_rate_predicate_failed = None
    
    # --------------  Execution of the architecture step and evaluation of predicates --------------
    p95_latency, qa_inconsistency_rate, failure_rate = run_experiment_of_architecture_step_full_predicate(T, LLM, CONCURRENCY_RATE, log_telemetry_file)
        
    
    success = False
    original_epsilon_l = epsilon_l; original_epsilon_qa = epsilon_qa; original_epsilon_f = epsilon_f
    
    # check with baseline w.s.t thresholds:
    if acceptance_predicate_mode == "QA-Only":
            epsilon_l = -1; epsilon_f = -1
    elif acceptance_predicate_mode == "Latency-Only":
            epsilon_qa = -1; epsilon_f = -1
    elif acceptance_predicate_mode == "Failure-Only":
            epsilon_l = -1; epsilon_qa = -1 
            
    if epsilon_l and epsilon_l > -1:
        if p95_latency > epsilon_l:
            success_l =  False
            latency_predicate_failed = True
        else:
            success_l = True
            latency_predicate_failed = False
    if epsilon_qa and epsilon_qa > -1:
        if qa_inconsistency_rate > epsilon_qa:
            success_qa =  False
            qa_predicate_failed = True
        else:
            success_qa = True
            qa_predicate_failed = False
    if epsilon_f and epsilon_f > -1:
        if failure_rate > epsilon_f:
            success_f = False
            failure_rate_predicate_failed = True
        else:
            success_f = True
            failure_rate_predicate_failed = False
    success = (success_l if epsilon_l and epsilon_l > -1 else True) and \
              (success_qa if epsilon_qa and epsilon_qa > -1 else True) and \
              (success_f if epsilon_f and epsilon_f > -1 else True) 


    step_self_temporal_propagation = qa_inconsistency_rate + failure_rate  + p95_latency / N_TRIALS
    
    
    pwd = os.getcwd()
    
    result = {
        "epsilon_l": original_epsilon_l,
        "epsilon_qa": original_epsilon_qa,
        "epsilon_f": original_epsilon_f,
        "log_telemetry_file": log_telemetry_file,
        "p95_latency": p95_latency,
        "qa_inconsistency_rate": qa_inconsistency_rate,
        "failure_rate": failure_rate,
        "latency_predicate_failed": latency_predicate_failed,
        "qa_predicate_failed": qa_predicate_failed,
        "failure_rate_predicate_failed": failure_rate_predicate_failed,
        "success": success,
        "step_self_temporal_propagation": step_self_temporal_propagation,
        "target_service": target_service,
        "total_trials": N_TRIALS
    }
    return result

if __name__ == '__main__':
    migration_order = sys.argv[1]
    acceptance_predicate_mode = sys.argv[2]
    step = sys.argv[3]
    services = sys.argv[4]
    agents = sys.argv[5]
    epsilon_l = float(sys.argv[6])
    epsilon_qa = float(sys.argv[7])
    epsilon_f = float(sys.argv[8])
    governance_mode = sys.argv[9]
    target_service = sys.argv[10]
    previous_step_acceptance_type = sys.argv[11]
    temporal_propagation_effect_enabled = sys.argv[12]
    migration_sorting_strategy_services = sys.argv[13]
    T_, LLM_, CONCURRENCY_RATE_ = sys.argv[14], sys.argv[15], sys.argv[16]
    if len(sys.argv) < 17:
        raise ValueError("Expected: migration_order predicate-mode step services agents epsilon_l epsilon_qa epsilon_f governance_mode target_service temporal_propagation_effect_enabled migration_sorting_strategy_services T LLM CONCURRENCY_RATE")


    
    pwd = os.getcwd()
    script_dir = str(Path(__file__).resolve().parent)
    os.makedirs(script_dir + f"/results/{migration_order}", exist_ok=True)

    step_report_file_name = script_dir + f"/results/{migration_order}/res_LLM_{LLM_}_T_{T_}_U_{CONCURRENCY_RATE_}" \
              f"_MO_{migration_order}_PRED_{acceptance_predicate_mode}_GM_{governance_mode}_TPOP_{temporal_propagation_effect_enabled}.json"
     
    # print(step_report_file_name)
    
    acceptance_result = acceptance_of_architecture_step_predicate_based(
                                                                        epsilon_l=epsilon_l,
                                                                        epsilon_qa=epsilon_qa,
                                                                        epsilon_f=epsilon_f,
                                                                        acceptance_predicate_mode=acceptance_predicate_mode,
                                                                        target_service=target_service,
                                                                        step=step,
                                                                        T=float(T_),
                                                                        LLM=LLM_,
                                                                        CONCURRENCY_RATE=int(CONCURRENCY_RATE_),
                                                                        log_telemetry_file=step_report_file_name)


    

    if acceptance_result["success"]:
        print(json.dumps({"result": "ACCEPTED", "step_self_temporal_propagation": acceptance_result["step_self_temporal_propagation"], "step_report_file_name": step_report_file_name, "details": acceptance_result}))
    else:
        print(json.dumps({"result": "REJECTED", "step_self_temporal_propagation": acceptance_result["step_self_temporal_propagation"], "step_report_file_name": step_report_file_name, "details": acceptance_result}))
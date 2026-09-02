import subprocess
import json
import time
import logging 
import sys
import numpy as np

import tqdm
from post_action_adjudication import (
    PostActionAdjudicator,
    AdjudicationMode,
    AdjudicationCriteria,
    create_execution_metrics_from_step_result
)

logger = logging.getLogger("arch-optimization-migration-experiment-runner")
logging.basicConfig(
    filename='./logs/arch-optimization-experiment.log',
    level=logging.INFO,  # Log all messages with severity DEBUG or higher
    format='%(asctime)s - %(levelname)s - %(message)s'  # Define the message format
)

with open('./logs/arch-optimization-experiment.log', 'w') as f:
    f.write('')

with open('./logs/experiment.log', 'w') as f:
    f.write('')
    
with open('./opt_res/results.txt', 'w') as f:
    f.write('')

with open('./opt_res/results.json', 'w') as f:
    f.write('')

QA_thresholds = list(np.linspace(80, 100, 10))
latency_thresholds = list(np.linspace(40, 200, 10))

configs = []
for qa_t in QA_thresholds:
    for l_t in latency_thresholds:
    
        config = {
            "benchmark": "google_ms",
            "predicates": {
                "qa": True,
                "qa_threshold": str(qa_t),
                "latency": True,
                "latency_threshold": str(int(l_t)),
                "failure": True,
                "failure_threshold": "2"
            },
            "governance_mode": "off",
            "governance_thresholds": {
                "beta": "30",
                "gmid": "20",
                "deltaL": "10",
                "deltaSLO": "0.5",
                "deltaTProp": "0.1",
                "gpost": "30"
            },
            "runtime": {
                "model": "llama3.2:3b",
                "temperature": 0,
                "R": 5000,
                "concurrency": 20
            },
            "ranking_weights": {
                "fanout": 0.2,
                "bc": 0.2,
                "ccyl": 0.2,
                "ccog": 0.2,
                "tprop": 0.2
            },
            "ranked_services": [
                {
                    "rank": "1",
                    "service": "Currency",
                    "score": "0.000"
                },
                {
                    "rank": "2",
                    "service": "Product Catalog",
                    "score": "0.023"
                },
                {
                    "rank": "3",
                    "service": "Ad",
                    "score": "0.075"
                },
                {
                    "rank": "4",
                    "service": "Cart",
                    "score": "0.088"
                },
                {
                    "rank": "5",
                    "service": "Recommendation",
                    "score": "0.096"
                },
                {
                    "rank": "6",
                    "service": "Shipping",
                    "score": "0.122"
                },
                {
                    "rank": "7",
                    "service": "Email",
                    "score": "0.177"
                },
                {
                    "rank": "8",
                    "service": "Payment",
                    "score": "0.288"
                },
                {
                    "rank": "9",
                    "service": "Checkout",
                    "score": "0.800"
                }
            ]
        }
        configs.append(config)

def get_service_port(s_name):
    if s_name == 'currency_service':
        return 5053
    elif s_name == 'product_catalog_service':
        return 5055
    elif s_name == 'ad_service':
        return 5057
    elif s_name == 'cart_service':
        return 5054
    elif s_name == 'recommendation_service':
        return 5058
    elif s_name == 'shipping_service':
        return 5051
    elif s_name == 'email_service':
        return 5056
    elif s_name == 'payment_service':
        return 5052
    elif s_name == 'checkout_service':
        return 5050
                                
ranked_services = [
    [
        str(s["service"]).lower().replace(' ','_') + '_service' + ':' + 
            str(get_service_port(str(s["service"]).lower().replace(' ','_') + '_service')),
        float(s["score"])
    ]
    for s in configs[0]["ranked_services"]
]


# mapping service -> agent
service_to_agent = {
    "checkout_service:5050": "checkout_agent:5050",
    "payment_service:5052": "payment_agent:5052",
    "email_service:5056": "email_agent:5056",
    "shipping_service:5051": "shipping_agent:5051",
    "recommendation_service:5058": "recommendation_agent:5058",
    "cart_service:5054": "cart_agent:5054",
    "ad_service:5057": "ad_agent:5057",
    "product_catalog_service:5055": "product_catalog_agent:5055",
    "currency_service:5053": "currency_agent:5053",
}

# -------------------------- Apply ranking strategy -------------------------
migration_order_strategy = "Ranked" + "_weight_" + "_".join([f"{k}{v}" for k, v in configs[0]["ranking_weights"].items()])+ "_" + "live_progressive_refactor"




# --------------------------------- Governance Mechanism  ---------------------------

mode = configs[0]["governance_mode"]

if mode == "off":

    governance_policies = ["No"]

elif mode == "auto":

    governance_policies = [
        "Post-Audit-Comprehensive"
    ]

elif mode == "human":

    governance_policies = [
        "Human-In-The-Loop"
    ]

gov = configs[0]["governance_thresholds"]

adjudication_criteria = AdjudicationCriteria(

    delta_qa=0,

    delta_latency=float(gov["deltaL"])/100,

    delta_failure=float(gov["deltaSLO"])/100,

    delta_temporal_prop=float(gov["deltaTProp"]),

    grace_window_fraction=float(gov["beta"])/100

)
post_action_adjudicator = PostActionAdjudicator(adjudication_criteria)


# ------------------------------------------- TProp ------------------

temporal_propagation_enabled = True
temporal_propagation_dependency_influence_weight = {
    "product_catalog_service->checkout_service": 1,
    "product_catalog_service->recommendation_service": 1,
    "cart_service->checkout_service": 1,
    "currency_service->checkout_service": 1,
    "payment_service->checkout_service": 1,
    "shipping_service->checkout_service": 1,
    "email_service->checkout_service": 1,
}


# ----------------- RUNTIME Configurations ----------------
# LLM = ["llama3.2:3b"]  # "llama3.2:3b" or "qwen3:14b"
# T = [0.0] # 0 or 0.8
# CONCURRENCY_RATE = [20] # [20, 100] # concurrent requests

runtime = configs[0]["runtime"]

LLM = [
    runtime["model"]
]

T = [
    runtime["temperature"]
]

CONCURRENCY_RATE = [
    runtime["concurrency"]
]

TOTAL_REQUESTS = runtime["R"]

# ---- HELPERS ----

def build_args(services, agents):
    """
    Convert lists to CLI format:
    services=svc1:8000,svc2:8001 ...
    """
    svc_pairs = []
    for s in services:
        name = s.split(":")[0]
        port = s.split(":")[1]
        svc_pairs.append(f"{name}:{port}")

    agent_pairs = []
    for a in agents:
        name = a.split(":")[0]
        port = a.split(":")[1]
        agent_pairs.append(f"{name}:{port}")

    return [
        f"services={','.join(svc_pairs)}",
        f"agents={','.join(agent_pairs)}"
    ]


def deploy(services, agents):
    DEPLOY_SCRIPT = "./deploy-local.sh"
    args = build_args(services, agents)

    logger.info("\n🚀 Deploying:")
    logger.info(f"Services: {services}" )
    logger.info(f"Agents: {agents}")

    #subprocess.run([DEPLOY_SCRIPT] + args, check=True)

def shutdown(services, agents):
    SD_SCRIPT = "./shutdown-local.sh"
    args = build_args(services, agents)

    logger.info("\nShutting Down:")
    logger.info(f"Services: {services}")
    logger.info(f"Agents: {agents}")

    #subprocess.run([SD_SCRIPT] + args, check=True)




def run_experiment_for_step(migration_order, step_num, predicate_mode, governance_policy, services, agents,
                            target_service, temporal_propagation_enabled, previous_step_acceptance_type,
                            migration_sorting_strategy_services, T, LLM, CONCURRENCY_RATE, R,
                            cumulative_QA_inconsistency_rate=0, cumulative_p95_latency_inflation=0, cumulative_failure_rate_inflation=0,
                            QA_threshold=100, latency_threshold=90, failure_threshold=2):
    logger.info(f"🧪 Running Predicate-based Acceptance Experiment for step {step_num}...")
    # time.sleep(2) 
 

    # ---------- Specify predicates thresholds based on predicate mode ----------
    baseline_latency_p95 = 1
   
    epsilon_l = baseline_latency_p95 * (100 + latency_threshold)/100
    epsilon_qa = (100 - QA_threshold)/100
    epsilon_f = failure_threshold / 100
    
   
    if predicate_mode == "QA-Only":
        epsilon_l = -1
        epsilon_f = -1
    elif predicate_mode == "Latency-Only":
        epsilon_qa = -1
        epsilon_f = -1
    elif predicate_mode == "Failure-Only":
        epsilon_l = -1
        epsilon_qa = -1

    step_result = subprocess.run(
        ["python3", "-m", "refactored_architecture.google_ms.exp_runner_auto2",
         migration_order,
         predicate_mode, str(step_num), ",".join(services), ",".join(agents),
         str(epsilon_l), str(epsilon_qa), str(epsilon_f), str(governance_policy), 
         str(target_service), str(previous_step_acceptance_type), str(temporal_propagation_enabled),
         str(migration_sorting_strategy_services), str(T), str(LLM), str(CONCURRENCY_RATE)
        ],
        cwd="../..",
        capture_output=True,
        text=True,
        check=True  # Raise exception if subprocess fails
    )

    # Debug output
    # if step_result.stdout.strip():
    #     logger.info(f"Raw experiment output for step {step_num}: {step_result.stdout.strip()}")
    if step_result.stderr.strip():
        logger.info(f"⚠️  Experiment stderr for step {step_num}: {step_result.stderr.strip()}")
    
    if not step_result.stdout.strip():
        raise RuntimeError(f"Experiment for step {step_num} produced no output. Check stderr above.")
    
    try:
        step_result_parsed = json.loads(step_result.stdout.strip())
    except json.JSONDecodeError as e:
        logger.error(f"❌ Failed to parse JSON from step {step_num} output:")
        logger.error(f"Raw output: {step_result.stdout}")
        raise ValueError(f"Invalid JSON output from experiment: {e}")
    
    # print(f"Experiment output for step {step_num}:", step_result_parsed)

    # acceptance_result = step_result_parsed["result"]

    if step_num == -1:
        logger.info(f"Final Architecture Experiment Run Results : {step_result_parsed}")
        logger.info(f"services: {services}, agents: {agents}")
        return None, None, None, None, \
            step_result_parsed['details']['qa_inconsistency_rate'], step_result_parsed['details']['p95_latency'],\
                step_result_parsed['details']['failure_rate']  # No decision for final architecture run, just final architecture metrics
    
    # ============================================================================
    # POST-ACTION ADJUDICATION: Apply governance mechanism with HITL decision logic
    # ============================================================================
    
    # Map governance policy string to AdjudicationMode enum
    governance_mode_map = {
        "No": AdjudicationMode.NO_GOVERNANCE,
        "Post-Audit-Selective-Only": AdjudicationMode.SELECTIVE,
        "Post-Audit-Comprehensive": AdjudicationMode.COMPREHENSIVE,
        "Human-In-The-Loop": AdjudicationMode.HITL
    }
    
    adjudication_mode = governance_mode_map.get(
        governance_policy,
        AdjudicationMode.NO_GOVERNANCE
    )
    
    
    # detect upstream for temporal propagation influence
    upstream_effect = False
    for dependency, weight in temporal_propagation_dependency_influence_weight.items():
        upstream = dependency.split("->")[1]
        downstream = dependency.split("->")[0]
        if target_service == downstream:
            # print(f"    {svc} is downstream of {upstream}. Adding to affecting services with weight {weight}.")
            upstream_effect = True
    
    if not upstream_effect:
        logger.info("🔄 No temporal propagation influence detected for this step.")
        step_self_temporal_propagation = 0
        step_result_parsed["step_self_temporal_propagation"] = 0
    else:
        step_self_temporal_propagation = step_result_parsed.get("step_self_temporal_propagation", 0)
    
    step_report_file_name = step_result_parsed.get("step_report_file_name", None)

        
    # Extract execution metrics from step result
    execution_metrics = create_execution_metrics_from_step_result(
        step_result=step_result_parsed,
        step_number=step_num,
        target_service=target_service,
        total_trials=5000
    )
    
    # Perform post-action adjudication
    final_decision, decision_type, evidence_summary, prediction_category = post_action_adjudicator.adjudicate_step(
        metrics=execution_metrics,
        mode=adjudication_mode,
        evidence_context={
            "previous_step_type": previous_step_acceptance_type,
            "temporal_propagation_enabled": temporal_propagation_enabled
        }
    )
    
    
    # print(f"Evidence Summary for step {step_num}:", evidence_summary)
    success_qa = '✅'  if step_result_parsed['details']['success_qa'] is True else '❌'
    success_l = '✅' if step_result_parsed['details']['success_l'] is True  else '❌'
    success_f = '✅' if step_result_parsed['details']['success_f'] is True  else '❌'
    success_predicate = '✅' if step_result_parsed['details']['success'] is True else '❌'
    success_adjudicate = '✅' if final_decision is True else '❌'
    logger.info(f"\n\n Predicate-driven automatic acceptance test results summary: \
                \n Prediction Category for step {step_num}: {prediction_category}, \
                \n qa_inconsistency_rate: {step_result_parsed['details']['qa_inconsistency_rate']:.4f},\
                    QA_threshold: {epsilon_qa}, success_QA: {success_qa} \
                \n p95_latency: {step_result_parsed['details']['p95_latency']:.4f}, \
                    Latency_threshold: {epsilon_l}, success_L: {success_l} \
                \n failure_rate: {step_result_parsed['details']['failure_rate']:.4f}, \
                    Failure_threshold: {epsilon_f}, success_F: {success_f} \
                \n final predicate automatic raw decision: {success_predicate} \
                \n temporal_propagation: {step_self_temporal_propagation:.4f}, \
                \n final adjudicated decision: {success_adjudicate} \n\n")
    
    cumulative_QA_inconsistency_rate += step_result_parsed['details']['qa_inconsistency_rate']
    cumulative_p95_latency_inflation += step_result_parsed['details']['p95_latency'] - baseline_latency_p95
    cumulative_failure_rate_inflation += step_result_parsed['details']['failure_rate']
    
    if str(step_num)=="1":
         # For the first step, we create a new report file (overwriting if it already exists)
        with open(step_report_file_name, "w") as f:
            f.write("")
    
    
    
    full_run_step_results = {"migration_order": migration_order, "migration_sorting_strategy_services": migration_sorting_strategy_services,
                        "step": step, "services": services, "agents": agents, "evidence_summary": evidence_summary,
                        "acceptance_predicate_mode": predicate_mode, "governance_policy": governance_policy,
                        "target_service": target_service, "temporal_propagation_effect_enabled": temporal_propagation_enabled,
                        "is_accepted": final_decision, "decision_type": decision_type, "prediction_category": prediction_category,
                        "step_self_temporal_propagation": step_self_temporal_propagation}
    
    with open(step_report_file_name, "a") as f:
        f.write("\n\n")
        json.dump(full_run_step_results, f, indent=2)
        f.write("\n\n------------\n\n")
    
    return final_decision, step_self_temporal_propagation, decision_type, prediction_category, \
        cumulative_QA_inconsistency_rate, cumulative_p95_latency_inflation, cumulative_failure_rate_inflation



# --------------------------------- Acceptance Predicate ---------------------------

pred = configs[0]["predicates"]

enabled = []

if pred["qa"]:
    enabled.append("QA")

if pred["latency"]:
    enabled.append("Latency")

if pred["failure"]:
    enabled.append("Failure")

if len(enabled) == 3:
    acceptance_predicate_modes = ["Full"]

elif enabled == ["QA"]:
    acceptance_predicate_modes = ["QA-Only"]

elif enabled == ["Latency"]:
    acceptance_predicate_modes = ["Latency-Only"]

elif enabled == ["Failure"]:
    acceptance_predicate_modes = ["Failure-Only"]

else:
    acceptance_predicate_modes = enabled



# ---- Main Refactoring LOOP ----
    
def init_conditions():
    subprocess.run("rm -f *.log", shell=True, cwd=".", check=True)

    migration_sorting_strategy_services = ranked_services
    current_services_with_scores = ranked_services.copy()
    previous_step_acceptance_types = ['N/A']
    temporal_propagations = []
    
    cumulative_QA_inconsistency_rate = 0
    cumulative_p95_latency_inflation = 0
    cumulative_failure_rate_inflation = 0
    
    return migration_sorting_strategy_services, current_services_with_scores, previous_step_acceptance_types, temporal_propagations, \
        cumulative_QA_inconsistency_rate, cumulative_p95_latency_inflation, cumulative_failure_rate_inflation

total = (
    len(acceptance_predicate_modes)
    * len(governance_policies)
    * len(LLM)
    * len(T)
    * len(CONCURRENCY_RATE)
    * len(configs)
)

logger.info(f"Total experiments to run: {total} (Acceptance Predicate Modes: {len(acceptance_predicate_modes)}, Governance Policies: {len(governance_policies)}, LLMs: {len(LLM)}, Temperatures: {len(T)}, Concurrency Rates: {len(CONCURRENCY_RATE)}, Configurations: {len(configs)})")


opt_res = []

with tqdm.tqdm(total=total, desc="Experiments") as pbar:
    for predicate_mode in acceptance_predicate_modes:
        for governance_policy in governance_policies:
            for LLM_ in LLM:
                for T_ in T:
                    for CONCURRENCY_RATE_ in CONCURRENCY_RATE:
                        for predicate_pair in range(len(configs)):
                            pred = configs[predicate_pair]["predicates"]
                            QA_threshold = float(pred["qa_threshold"])
                            latency_threshold = float(pred["latency_threshold"])
                            failure_threshold = float(pred["failure_threshold"])
                    
                            logger.info(f"""\n\n============================== Starting Migration Strategy: {migration_order_strategy}, 
                                        Predicate Mode: {predicate_mode}, Governance Policy: {governance_policy}, T: {T_}, LLM: {LLM_}, 
                                        CONCURRENCY_RATE: {CONCURRENCY_RATE_}, QA_threshold: {QA_threshold}, Latency_threshold: {latency_threshold}, 
                                        Failure_threshold: {failure_threshold} 
                                        ==============================\n\n""")

                            try:
                                
                                # ---------------- State Tracking for Current Architecture --------------

                                # Initialize: all services running, no agents yet
                                current_services = [s[0] for s in ranked_services]
                                current_agents = []
                                migration_sorting_strategy_services, current_services_with_scores, previous_step_acceptance_types, \
                                    temporal_propagations, cumulative_QA_inconsistency_rate, cumulative_p95_latency_inflation, \
                                    cumulative_failure_rate_inflation= init_conditions()

                                
                                for step in range(1, len(migration_sorting_strategy_services)+1):
                                    logger.info(f"\n\n============================== Starting Step {step}/{len(migration_sorting_strategy_services)} ==============================")
                                    
                                    try:
                                        svc = migration_sorting_strategy_services[step-1][0]
                                        risk_score = migration_sorting_strategy_services[step-1][1]
                                        logger.info(f"\n\n=== Step:{step}, Refactoring {svc} with risk score {risk_score} as AI agent ===\n\n")
                                        logger.info(f"Current services ranking scores: {migration_sorting_strategy_services}")
                                        logger.info(f"\n\nCurrent successfully agentified services so far: {current_agents}\n\n")

                                        agent = service_to_agent[svc]

                                        # candidate configuration: remove current service, add as agent
                                        candidate_services = [s for s in current_services if s != svc]
                                        candidate_agents = current_agents + [agent]

                                        logger.info("\n\n ============== Deployment of step candidate architecture ==============\n\n")
                                        # deploy candidate
                                        deploy(candidate_services, candidate_agents)

                                        # optional: wait for services to stabilize
                                        logger.info("... Waiting for the deployment to stabilize ...")
                                        # time.sleep(0.1)
                                        logger.info("Candidate architecture deployed successfully.\n\n")



                                        logger.info("\n\n ============== Predicate-driven acceptance of step's candidate architecture ==============\n\n")
                                        final_decision, step_self_temporal_propagation, decision_type, prediction_category, cumulative_QA_inconsistency_rate, \
                                            cumulative_p95_latency_inflation, cumulative_failure_rate_inflation = \
                                                run_experiment_for_step(migration_order_strategy, step, predicate_mode, governance_policy,
                                                                                    candidate_services, candidate_agents, svc.split(":")[0],
                                                                                    temporal_propagation_enabled, previous_step_acceptance_types[-1],
                                                                                    migration_sorting_strategy_services, T_, LLM_,
                                                                                    CONCURRENCY_RATE_, TOTAL_REQUESTS, 
                                                                                    cumulative_QA_inconsistency_rate, cumulative_p95_latency_inflation, 
                                                                                    cumulative_failure_rate_inflation,
                                                                                    QA_threshold, latency_threshold, failure_threshold)
                                        previous_step_acceptance_types.append(decision_type)

                                        if final_decision is True:
                                            logger.info(f"Step {step} final result: ✅ ACCEPTED: {svc} → {agent}, decision type: {decision_type}")
                                            current_services = candidate_services
                                            current_agents = candidate_agents
                                        else:
                                            logger.info(f"Step {step} final result: ❌ REJECTED: {svc} remains as service, decision type: {decision_type}")
                                            # current_services and current_agents remain unchanged
                                            
                                        # handle temporal propagation influence on next steps if this step is accepted and has temporal propagation influence, and if the strategy is ranked (so we can adjust ranking)
                                        if final_decision is True and temporal_propagation_enabled and \
                                                step_self_temporal_propagation > 0 and migration_order_strategy.__contains__("Ranked"):
                                                    
                                            temporal_propagations.append(step_self_temporal_propagation)
                                            step_self_temporal_propagation_normalized = step_self_temporal_propagation / max(temporal_propagations) if temporal_propagations else 0
                                            
                                            logger.info(f"\n\n ============ 🔄 Detecting Temporal Propagation Influence To Update Remaining Services Ranking Score ================= \n")
                                            # Adjust the ranking of remaining services based on temporal propagation influence
                                            affecting_services = []
                                            for dependency, weight in temporal_propagation_dependency_influence_weight.items():
                                                upstream = dependency.split("->")[1]
                                                downstream = dependency.split("->")[0]
                                                #print(f"  Checking dependency {downstream} -> {upstream} with influence weight {weight} ...")
                                                if svc.split(":")[0] == downstream:
                                                    # print(f"    {svc} is downstream of {upstream}. Adding to affecting services with weight {weight}.")
                                                    affecting_services.append((upstream, weight))
                                            
                                            if not affecting_services:
                                                logger.info("  No temporal propagation influence detected for this step.")
                                            
                                            # Update ranking for affected services
                                            if affecting_services:
                                                # logger.info(f"🔄 Temporal Propagation Influence Detected for some affected (upstream) services: {step_self_temporal_propagation_normalized}, {affecting_services}")
                                                # logger.info(f"  Affected upstream services: {affecting_services}")
                                                for affected_svc, influence_weight in affecting_services:
                                                    # Find and update the affected service's score in current_services_with_scores
                                                    for i, (service_name_with_port, score) in enumerate(current_services_with_scores):
                                                        service_name = service_name_with_port.split(":")[0]
                                                        if service_name == affected_svc:
                                                            # Increase the score based on temporal propagation influence
                                                            old_score = score
                                                            new_score = score + (step_self_temporal_propagation_normalized * influence_weight)
                                                            current_services_with_scores[i] = [service_name_with_port, new_score]
                                                            logger.info(f"    Updated {service_name_with_port}: score {old_score:.3f} → {new_score:.3f} due to temporal propagation influence from {svc} with weight {influence_weight}")
                                                            break
                                                
                                                # Re-sort services based on updated scores (lowest first)
                                                current_services_with_scores.sort(key=lambda x: x[1], reverse=False)
                                                # logger.info(f"  Updated migration ranking: {[s[0] for s in current_services_with_scores]}")
                                                
                                                # Update migration_sorting_strategy_services for next steps
                                                migration_sorting_strategy_services = current_services_with_scores.copy()
                                                # print(f"  Migration strategy updated for next steps: {[s[0] for s in migration_sorting_strategy_services]}")
                                    except Exception as e_in:
                                        logger.error(f"❌ Exception occurred during step {step}: {e_in}")
                                        # Attempt to shutdown any deployed services/agents before exiting
                                        shutdown(current_services, current_agents)
                                        continue
                                    
                                logger.info("\n\n\n\n------------------------------- 🎯 Final architecture: -------------------- \n\n")
                                logger.info(f"Services: {current_services}\n")
                                logger.info(f"Agents: {current_agents}\n\n")
                                
                                logger.info(f"Cumulative QA Inconsistency Rate: {cumulative_QA_inconsistency_rate:.4f}")
                                logger.info(f"Cumulative p95 Latency Inflation: {cumulative_p95_latency_inflation:.4f}")
                                logger.info(f"Cumulative Failure Rate Inflation: {cumulative_failure_rate_inflation:.4f}")
                            
    
                                
                                # test final architecture with a final experiment run
                                logger.info(f"\n\n------------------------------- 🎯 Final Architecture Experiment Run -------------------- \n")
                                _, _, _, _, delta_qa, delta_l, delta_f = run_experiment_for_step(migration_order_strategy, -1, predicate_mode, governance_policy,
                                                                                current_services, current_agents, svc.split(":")[0],
                                                                                temporal_propagation_enabled, previous_step_acceptance_types[-1],
                                                                                migration_sorting_strategy_services, T_, LLM_, CONCURRENCY_RATE_, R=TOTAL_REQUESTS)
                                
                                with open('./opt_res/results.txt', 'a') as f:
                                    f.write(f"\n\n------------------------------- -------------------- \n\n")
                                    f.write(f"Predicate Mode: {predicate_mode}, Governance Policy: {governance_policy}, T: {T_}, LLM: {LLM_}, CONCURRENCY_RATE: {CONCURRENCY_RATE_}, QA_threshold: {QA_threshold}, Latency_threshold: {latency_threshold}, Failure_threshold: {failure_threshold}\n")
                                    f.write(f"\n\n--- 🎯 Final architecture: --- \n\n")
                                    f.write(f"Services: {current_services}\n")
                                    f.write(f"Agents: {current_agents}\n\n")
                                    f.write(f"Migration coverage: {len(current_agents)/len(ranked_services):.4f}\n")
                                    
                                    f.write(f"Cumulative QA Inconsistency Rate: {cumulative_QA_inconsistency_rate:.4f}\n")
                                    f.write(f"Cumulative p95 Latency Inflation: {cumulative_p95_latency_inflation:.4f}\n")
                                    f.write(f"Cumulative Failure Rate Inflation: {cumulative_failure_rate_inflation:.4f}\n")
                                    f.write(f"final architecture experiment run results: delta_qa: {delta_qa:.4f}, delta_l: {delta_l:.4f}, delta_f: {delta_f:.4f}\n")
                                
                                opt_res.append({
                                    "predicate_mode": predicate_mode,
                                    "governance_policy": governance_policy,
                                    "T": T_,
                                    "LLM": LLM_,
                                    "CONCURRENCY_RATE": CONCURRENCY_RATE_,
                                    "QA_threshold": QA_threshold,
                                    "latency_threshold": latency_threshold,
                                    "failure_threshold": failure_threshold,
                                    "final_services": current_services,
                                    "final_agents": current_agents,
                                    "migration_coverage": len(current_agents) / len(ranked_services),
                                    "cumulative_QA_inconsistency_rate": cumulative_QA_inconsistency_rate,
                                    "cumulative_p95_latency_inflation": cumulative_p95_latency_inflation,
                                    "cumulative_failure_rate_inflation": cumulative_failure_rate_inflation,
                                    "final_architecture_experiment_results": {
                                        "delta_qa": delta_qa,
                                        "delta_l": delta_l,
                                        "delta_f": delta_f
                                    }
                                })                                    
                                    
                                # input("Press Enter to gracefully shutdown final configuration...")
                                shutdown(current_services, current_agents)
                                logger.info(f"Final Architecture Experiment Run Finished Successfully")

                            except Exception as e:
                                logger.error(f"❌ Exception occurred during step {step}: {e}")
                                # Attempt to shutdown any deployed services/agents before exiting
                                shutdown(current_services, current_agents)
                                continue
                            finally:
                                pbar.update(1)


with open('./opt_res/results.json', 'a') as f:
    f.write(json.dumps(opt_res, indent=2))
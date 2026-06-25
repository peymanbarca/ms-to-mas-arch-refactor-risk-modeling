import subprocess
import json
import time
from post_action_adjudication import (
    PostActionAdjudicator,
    AdjudicationMode,
    AdjudicationCriteria,
    create_execution_metrics_from_step_result
)


import random
import tqdm


# --------------------------------- Migration Strategy ---------------------------

fanout = [["currency_service:5053", 0],
           ["product_catalog_service:5055", 0],
           ["ad_service:5057", 0],
           ["cart_service:5054", 0],
           ["recommendation_service:5058", 1], 
           ["shipping_service:5051", 1],
           ["email_service:5056", 1],
           ["payment_service:5052", 1],
           ["checkout_service:5050", 6]
           ]

bc = [["currency_service:5053", 0],
           ["product_catalog_service:5055", 0],
           ["ad_service:5057", 0],
           ["cart_service:5054", 0],
           ["recommendation_service:5058", 0], 
           ["shipping_service:5051", 0],
           ["email_service:5056", 0],
           ["payment_service:5052", 0],
           ["checkout_service:5050", 0.56]
           ]

c_cyc = [["currency_service:5053", 8],
           ["product_catalog_service:5055", 12],
           ["ad_service:5057", 17],
           ["cart_service:5054", 17],
           ["recommendation_service:5058", 16], 
           ["shipping_service:5051", 21],
           ["email_service:5056", 23],
           ["payment_service:5052", 41],
           ["checkout_service:5050", 65]
           ]



c_cog = [["currency_service:5053", 4],
           ["product_catalog_service:5055", 6],
           ["ad_service:5057", 14],
           ["cart_service:5054", 17],
           ["recommendation_service:5058", 12], 
           ["shipping_service:5051", 14],
           ["email_service:5056", 25],
           ["payment_service:5052", 36],
           ["checkout_service:5050", 50]
           ]

w_fanout = 0.2
w_bc = 0.2
w_c_cyc = 0.2
w_c_cog = 0.2
w_t_prop = 0.2

service_risk_scores = []

for i in range(len(fanout)):
    risk_score = w_fanout * (fanout[i][1] / sum([f[1] for f in fanout])) + \
                 w_bc * (bc[i][1] / sum([b[1] for b in bc])) + \
                 w_c_cyc * (c_cyc[i][1] / sum([c[1] for c in c_cyc])) + \
                 w_c_cog * (c_cog[i][1] / sum([c[1] for c in c_cog])) + \
                 w_t_prop * 0
    service_risk_scores.append([fanout[i][0], risk_score])

# print("service_risk_scores: ",  service_risk_scores)

# ranked services 
ranked_services = [
    ["currency_service:5053", [x[1] for x in service_risk_scores if x[0] == "currency_service:5053"][0]],
    ["product_catalog_service:5055", [x[1] for x in service_risk_scores if x[0] == "product_catalog_service:5055"][0]],
    ["ad_service:5057", [x[1] for x in service_risk_scores if x[0] == "ad_service:5057"][0]],
    ["cart_service:5054", [x[1] for x in service_risk_scores if x[0] == "cart_service:5054"][0]],
    ["recommendation_service:5058", [x[1] for x in service_risk_scores if x[0] == "recommendation_service:5058"][0]],
    ["shipping_service:5051", [x[1] for x in service_risk_scores if x[0] == "shipping_service:5051"][0]],
    ["email_service:5056", [x[1] for x in service_risk_scores if x[0] == "email_service:5056"][0]],
    ["payment_service:5052", [x[1] for x in service_risk_scores if x[0] == "payment_service:5052"][0]],
    ["checkout_service:5050", [x[1] for x in service_risk_scores if x[0] == "checkout_service:5050"][0]],
]

# reverse-ranked services
reverse_ranked_services = [
    ["checkout_service:5050",[x[1] for x in service_risk_scores if x[0] == "checkout_service:5050"][0]],
    ["payment_service:5052", [x[1] for x in service_risk_scores if x[0] == "payment_service:5052"][0]],
    ["email_service:5056", [x[1] for x in service_risk_scores if x[0] == "email_service:5056"][0]],
    ["shipping_service:5051", [x[1] for x in service_risk_scores if x[0] == "shipping_service:5051"][0]],
    ["recommendation_service:5058", [x[1] for x in service_risk_scores if x[0] == "recommendation_service:5058"][0]],
    ["cart_service:5054", [x[1] for x in service_risk_scores if x[0] == "cart_service:5054"][0]],
    ["ad_service:5057", [x[1] for x in service_risk_scores if x[0] == "ad_service:5057"][0]],
    ["product_catalog_service:5055", [x[1] for x in service_risk_scores if x[0] == "product_catalog_service:5055"][0]],
    ["currency_service:5053", [x[1] for x in service_risk_scores if x[0] == "currency_service:5053"][0]],
]

# random-ranked services 
random_ranked_services = ranked_services.copy()
random.shuffle(random_ranked_services)
# print(f"Random-ranked services: {random_ranked_services}")


# dependency-based ranked services (based on fanout and bc only)
w2_fanout = 0.5
w2_bc = 0.5
w2_c_cyc = 0
w2_c_cog = 0
w2_t_prop = 0

service_risk_scores2 = []

for i in range(len(fanout)):
    risk_score = w2_fanout * (fanout[i][1] / sum([f[1] for f in fanout])) + \
                 w2_bc * (bc[i][1] / sum([b[1] for b in bc]))
    service_risk_scores2.append([fanout[i][0], risk_score])

# sorted by risk score based on fanout and bc only
dependency_ranked_services = [[s[0], s[1]] for s in sorted(service_risk_scores2, key=lambda x: x[1], reverse=False)]

# complexity-based ranked services (based on c_cyl and c_cog only)
w3_fanout = 0
w3_bc = 0
w3_c_cyc = 0.5
w3_c_cog = 0.5
w3_t_prop = 0

service_risk_scores3 = []

for i in range(len(c_cyc)):
    risk_score = w3_c_cyc * (c_cyc[i][1] / sum([c[1] for c in c_cyc])) + \
                 w3_c_cog * (c_cog[i][1] / sum([c[1] for c in c_cog]))
    service_risk_scores3.append([c_cyc[i][0], risk_score])

# sorted by risk score based on c_cyl and c_cog only
complexity_ranked_services = [[s[0], s[1]] for s in sorted(service_risk_scores3, key=lambda x: x[1], reverse=False)]

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
migration_order_strategy = "Complexity_Based" # ["Ranked", "Reverse_Ranked", "Random", "Dependency_Based", "Complexity_Based"]

if migration_order_strategy == "Ranked":
    current_services_with_scores = ranked_services.copy()
elif migration_order_strategy == "Reverse_Ranked":
    current_services_with_scores = reverse_ranked_services.copy()
elif migration_order_strategy == "Random":
    current_services_with_scores = random_ranked_services.copy()
elif migration_order_strategy == "Dependency_Based":
    current_services_with_scores = dependency_ranked_services.copy()
elif migration_order_strategy == "Complexity_Based":
    current_services_with_scores = complexity_ranked_services.copy()
else:
    raise ValueError(f"Invalid migration order strategy: {migration_order_strategy}")

# --------------------------------- Acceptance Predicate ---------------------------
acceptance_predicate_modes =  ["Full", "Latency-Only", "Failure-Only", "QA-Only"]

# --------------------------------- Governance Mechanism  ---------------------------
governance_policies = ["No", "Post-Audit-Selective-Only", "Post-Audit-Comprehensive"]


# Initialize the Post-Action Adjudicator with custom criteria
adjudication_criteria = AdjudicationCriteria(
    delta_qa=0,  # tolerance on QA inconsistency rate
    delta_latency=0.1,  # 0.1s tolerance on p95 latency
    delta_failure=0.005,  # tolerance on failure rate
    delta_temporal_prop=0.1,  # 0.1 tolerance on temporal propagation
    grace_window_fraction=0.3  # 30% of trials as grace window for transient violations
)
post_action_adjudicator = PostActionAdjudicator(adjudication_criteria)

temporal_propagation_enabled = True
temporal_propagation_dependency_influence_weight = {
    "product_catalog_service->checkout_service": 0.5,
    "product_catalog_service->recommendation_service": 1,
    "cart_service->checkout_service": 0.5,
    "currency_service->checkout_service": 0.5,
    "payment_service->checkout_service": 0.5,
    "shipping_service->checkout_service": 0.5,
    "email_service->checkout_service": 0.5,
}


# ----------------- RUNTIME Configurations ----------------
LLM = ["llama3.2:3b", "qwen3:14b"] # "llama3.2:3b" or "qwen3:14b"
T = [0, 0.8] # 0 or 0.8
CONCURRENCY_RATE = [5, 25] # 5 or 25 concurrent requests


# ---- HELPERS ----

def build_args(services, agents):
    """
    Convert lists to CLI format:
    services=svc1:8000,svc2:8001 ...
    """
    svc_pairs = []
    for s in services:
        name = s.split(":")[0]
        port = int(s.split(":")[1])
        svc_pairs.append(f"{name}:{port}")

    agent_pairs = []
    for a in agents:
        name = a.split(":")[0]
        port = int(a.split(":")[1])
        agent_pairs.append(f"{name}:{port}")

    return [
        f"services={','.join(svc_pairs)}",
        f"agents={','.join(agent_pairs)}"
    ]


def deploy(services, agents):
    DEPLOY_SCRIPT = "./deploy-local.sh"
    args = build_args(services, agents)

    print("\n🚀 Deploying:")
    print("Services:", services)
    print("Agents:", agents)

    subprocess.run([DEPLOY_SCRIPT] + args, check=True)

def shutdown(services, agents):
    SD_SCRIPT = "./shutdown-local.sh"
    args = build_args(services, agents)

    print("\nShutting Down:")
    print("Services:", services)
    print("Agents:", agents)

    subprocess.run([SD_SCRIPT] + args, check=True)


def run_experiment_for_step(migration_order, step_num, predicate_mode, governance_policy, services, agents,
                            target_service, temporal_propagation_enabled, previous_step_acceptance_type,
                            migration_sorting_strategy_services, T, LLM, CONCURRENCY_RATE):
    print(f"🧪 Running Predicate-based Acceptance Experiment for step {step_num}...")
    # time.sleep(2/10)  

    # ---------- Specify predicates thresholds based on predicate mode ----------
    baseline_latency_p95 = 1
    epsilon_l = baseline_latency_p95 * 1.9
    epsilon_qa = 0
    epsilon_f = 0.02
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
        ["python3", "-m", "refactored_architecture.google_ms.exp_runner_auto",
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
    #     print(f"Raw experiment output for step {step_num}:", step_result.stdout.strip())
    if step_result.stderr.strip():
        print(f"⚠️  Experiment stderr for step {step_num}:", step_result.stderr.strip())
    
    if not step_result.stdout.strip():
        raise RuntimeError(f"Experiment for step {step_num} produced no output. Check stderr above.")
    
    try:
        step_result_parsed = json.loads(step_result.stdout.strip())
    except json.JSONDecodeError as e:
        print(f"❌ Failed to parse JSON from step {step_num} output:")
        print(f"Raw output: {step_result.stdout}")
        raise ValueError(f"Invalid JSON output from experiment: {e}")
    
    # print(f"Experiment output for step {step_num}:", step_result_parsed)
    # acceptance_result = step_result_parsed["result"]
    # step_self_temporal_propagation = step_result_parsed.get("step_self_temporal_propagation", 0)


    # ============================================================================
    # POST-ACTION ADJUDICATION: Apply governance mechanism with HITL decision logic
    # ============================================================================
    
    # Map governance policy string to AdjudicationMode enum
    governance_mode_map = {
        "No": AdjudicationMode.NO_GOVERNANCE,
        "Post-Audit-Selective-Only": AdjudicationMode.SELECTIVE,
        "Post-Audit-Comprehensive": AdjudicationMode.COMPREHENSIVE
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
        print("  No temporal propagation influence detected for this step.")
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
    print(f"Prediction Category for step {step_num}:", prediction_category)
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
    
    return final_decision, step_self_temporal_propagation, decision_type, prediction_category




# ---- Main Refactoring LOOP ----

subprocess.run("rm -f *.log", shell=True, cwd=".", check=True)
    
def init_conditions():
    subprocess.run("rm -f *.log", shell=True, cwd=".", check=True)

    migration_sorting_strategy_services = complexity_ranked_services # ranked_services, reverse_ranked_services, random_ranked_services, dependency_ranked_services, complexity_ranked_services
    current_services_with_scores = complexity_ranked_services.copy() # ranked_services, reverse_ranked_services, random_ranked_services, dependency_ranked_services, complexity_ranked_services
    previous_step_acceptance_types = ['N/A']
    temporal_propagations = []
    
    return migration_sorting_strategy_services, current_services_with_scores, previous_step_acceptance_types, temporal_propagations

total = (
    len(acceptance_predicate_modes)
    * len(governance_policies)
    * len(LLM)
    * len(T)
    * len(CONCURRENCY_RATE)
)


with tqdm.tqdm(total=total, desc="Experiments") as pbar:
    for predicate_mode in acceptance_predicate_modes:
        for governance_policy in governance_policies:
            for LLM_ in LLM:
                for T_ in T:
                    for CONCURRENCY_RATE_ in CONCURRENCY_RATE:
                    
                        print(f"\n\n============================== Starting Migration Strategy: {migration_order_strategy}, Predicate Mode: {predicate_mode}, Governance Policy: {governance_policy}, T: {T_}, LLM: {LLM_}, CONCURRENCY_RATE: {CONCURRENCY_RATE_} ==============================\n\n")

                        try:
                            
                            # ---------------- State Tracking for Current Architecture --------------

                            # Initialize: all services running, no agents yet
                            current_services = [s[0] for s in ranked_services]
                            current_agents = []
                            migration_sorting_strategy_services, current_services_with_scores, previous_step_acceptance_types, temporal_propagations = init_conditions()

                            
                            for step in range(1, len(migration_sorting_strategy_services)+1):
                                print(f"\n============================== Starting Step {step}/{len(migration_sorting_strategy_services)} ==============================")
                                print("current services with scores:", migration_sorting_strategy_services)
                                
                                
                                svc = migration_sorting_strategy_services[step-1][0]
                                risk_score = migration_sorting_strategy_services[step-1][1]
                                print(f"\n=== Step:{step}, Refactoring {svc} with risk score {risk_score} as AI agent ===")

                                agent = service_to_agent[svc]

                                # candidate configuration: remove current service, add as agent
                                candidate_services = [s for s in current_services if s != svc]
                                candidate_agents = current_agents + [agent]

                                # deploy candidate
                                deploy(candidate_services, candidate_agents)

                                # optional: wait for services to stabilize
                                print("... Waiting for the deployment to stabilize ...")
                                # time.sleep(0.1)

                                # input("Press Enter to run the experiment for this configuration...")

                                final_decision, step_self_temporal_propagation, decision_type, prediction_category = run_experiment_for_step(migration_order_strategy, step, predicate_mode, governance_policy,
                                                                            candidate_services, candidate_agents, svc.split(":")[0],
                                                                            temporal_propagation_enabled, previous_step_acceptance_types[-1],
                                                                            migration_sorting_strategy_services, T_, LLM_, CONCURRENCY_RATE_)
                                previous_step_acceptance_types.append(decision_type)

                                if final_decision is True:
                                    print(f"✅ ACCEPTED: {svc} → {agent}, decision type: {decision_type}")
                                    current_services = candidate_services
                                    current_agents = candidate_agents
                                else:
                                    print(f"❌ REJECTED: {svc} remains as service, decision type: {decision_type}")
                                    # current_services and current_agents remain unchanged
                                    
                                # handle temporal propagation influence on next steps if this step is accepted and has temporal propagation influence, and if the strategy is ranked (so we can adjust ranking)
                                if final_decision is True and temporal_propagation_enabled and \
                                        step_self_temporal_propagation > 0 and migration_order_strategy in ["Ranked"]:
                                            
                                    temporal_propagations.append(step_self_temporal_propagation)
                                    step_self_temporal_propagation_normalized = step_self_temporal_propagation / max(temporal_propagations) if temporal_propagations else 0
                                    
                                    print(f"🔄 Detecting Temporal Propagation Influence ...")
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
                                        print("  No temporal propagation influence detected for this step.")
                                    
                                    # Update ranking for affected services
                                    if affecting_services:
                                        # print(f"🔄 Temporal Propagation Influence Detected for some affected (upstream) services: {step_self_temporal_propagation_normalized}, {affecting_services}")
                                        # print(f"  Affected upstream services: {affecting_services}")
                                        for affected_svc, influence_weight in affecting_services:
                                            # Find and update the affected service's score in current_services_with_scores
                                            for i, (service_name_with_port, score) in enumerate(current_services_with_scores):
                                                service_name = service_name_with_port.split(":")[0]
                                                if service_name == affected_svc:
                                                    # Increase the score based on temporal propagation influence
                                                    old_score = score
                                                    new_score = score + (step_self_temporal_propagation_normalized * influence_weight)
                                                    current_services_with_scores[i] = [service_name_with_port, new_score]
                                                    print(f"    Updated {service_name_with_port}: score {old_score:.3f} → {new_score:.3f} due to temporal propagation influence from {svc} with weight {influence_weight}")
                                                    break
                                        
                                        # Re-sort services based on updated scores (lowest first)
                                        current_services_with_scores.sort(key=lambda x: x[1], reverse=False)
                                        # print(f"  Updated migration ranking: {[s[0] for s in current_services_with_scores]}")
                                        
                                        # Update migration_sorting_strategy_services for next steps
                                        migration_sorting_strategy_services = current_services_with_scores.copy()
                                        # print(f"  Migration strategy updated for next steps: {[s[0] for s in migration_sorting_strategy_services]}")

                            print("\n🎯 Final architecture:")
                            print("Services:", current_services)
                            print("Agents:", current_agents)

                            # input("Press Enter to gracefully shutdown final configuration...")
                            shutdown(current_services, current_agents)
                        except Exception as e:
                            print(f"❌ Exception occurred during step {step}: {e}")
                            # Attempt to shutdown any deployed services/agents before exiting
                            shutdown(current_services, current_agents)
                            continue
                        finally:
                            pbar.update(1)
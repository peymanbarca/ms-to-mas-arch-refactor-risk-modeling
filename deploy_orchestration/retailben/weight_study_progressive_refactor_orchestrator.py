import random
import subprocess
import json
import time
import itertools
import numpy as np
from scipy.stats import spearmanr, kendalltau

import tqdm
from post_action_adjudication import (
    PostActionAdjudicator,
    AdjudicationMode,
    AdjudicationCriteria,
    create_execution_metrics_from_step_result
)


# --------------------------------- Migration Strategy ---------------------------

fanout = [["pricing_service:8003", 0],
           ["product_catalog_service:8008", 2],
           ["inventory_service:8001", 1],
           ["shopping_cart_service:8003", 0],
           ["payment_service:8007", 1], 
           ["order_service:8000", 6],
           ["subscription_service:8010", 1],
           ["procurement_service:8009", 1],
           ["shipment_service:8006", 1], 
        #    ["notification_service:8011", 1]
           ]

bc = [["pricing_service:8003", 0],
           ["product_catalog_service:8008", 0.05],
           ["inventory_service:8001", 0.22],
           ["shopping_cart_service:8003", 0],
           ["payment_service:8007", 0], 
           ["order_service:8000", 0.7],
           ["subscription_service:8010", 0.016],
           ["procurement_service:8009", 0],
           ["shipment_service:8006", 0], 
        #    ["notification_service:8011", 0]
           ]

c_cyc = [["pricing_service:8003", 10],
           ["product_catalog_service:8008", 22],
           ["inventory_service:8001", 10],
           ["shopping_cart_service:8003", 18],
           ["payment_service:8007", 10], 
           ["order_service:8000", 16],
           ["subscription_service:8010", 12],
           ["procurement_service:8009", 36],
           ["shipment_service:8006", 33], 
        #    ["notification_service:8011", 11]
           ]



c_cog = [["pricing_service:8003", 8],
           ["product_catalog_service:8008", 18],
           ["inventory_service:8001", 14],
           ["shopping_cart_service:8003", 14],
           ["payment_service:8007", 6], 
           ["order_service:8000", 28],
           ["subscription_service:8010", 10],
           ["procurement_service:8009", 26],
           ["shipment_service:8006", 30], 
        #    ["notification_service:8011", 4]
           ]



all_rankings_with_different_weights = []    


# --------------------------------------------------
# Build normalized metric dictionaries
# --------------------------------------------------

service_names = [x[0] for x in fanout]

fanout_norm = {
    x[0]: x[1] / sum(v[1] for v in fanout)
    for x in fanout
}

bc_norm = {
    x[0]: x[1] / sum(v[1] for v in bc)
    for x in bc
}

c_cyc_norm = {
    x[0]: x[1] / sum(v[1] for v in c_cyc)
    for x in c_cyc
}

c_cog_norm = {
    x[0]: x[1] / sum(v[1] for v in c_cog)
    for x in c_cog
}

t_prop_norm = {
    s: 0.0
    for s in service_names
}

def compute_risk_scores(
    w_fanout,
    w_bc,
    w_c_cyc,
    w_c_cog,
    w_t_prop
):

    scores = []

    for service in service_names:

        risk_score = (
            w_fanout * fanout_norm[service]
            + w_bc * bc_norm[service]
            + w_c_cyc * c_cyc_norm[service]
            + w_c_cog * c_cog_norm[service]
            + w_t_prop * t_prop_norm[service]
        )

        scores.append([service, risk_score])

    return scores


def rank_services(scores):

    ranked = sorted(
        scores,
        key=lambda x: x[1],
        reverse=False
    )

    return ranked

original_scores = compute_risk_scores(
    0.2, 0.2, 0.2, 0.2, 0.2
)

original_ranked_services = rank_services(original_scores)

original_rank_names = [
    x[0]
    for x in original_ranked_services
]

print("\nOriginal ranking with all equal weights")
for i, (svc, score) in enumerate(original_ranked_services):
    print(i + 1, svc, round(score, 5))


all_rankings_with_different_weights.append(original_rank_names)

def ranking_similarity(candidate_ranking):

    candidate_names = [
        x[0]
        for x in candidate_ranking
    ]

    original_positions = {
        svc: i
        for i, svc in enumerate(original_rank_names)
    }

    candidate_positions = {
        svc: i
        for i, svc in enumerate(candidate_names)
    }

    original_order = []
    candidate_order = []

    for svc in service_names:
        original_order.append(
            original_positions[svc]
        )

        candidate_order.append(
            candidate_positions[svc]
        )

    rho, _ = spearmanr(
        original_order,
        candidate_order
    )

    tau, _ = kendalltau(
        original_order,
        candidate_order
    )

    return rho, tau



# --------------- weights ablation (the remaining weights should be equal and sum of them = 1) -----------------------

print("\n\n===== ABLATION =====")

factors = [
    "fanout",
    "bc",
    "c_cyc",
    "c_cog",
    "t_prop"
]

ablation_sets = [
    ["fanout"],
    ["bc"],
    ["c_cyc"],
    ["c_cog"],
    ["t_prop"],

    ["fanout", "bc"],
    ["c_cyc", "c_cog"]
]

cnt_ablation = 0

for removed_features in ablation_sets:

    active_features = [
        f for f in factors
        if f not in removed_features
    ]
    
    cnt_ablation += 1

    weights = {
        f: 0.0
        for f in factors
    }

    if len(active_features) > 0:

        equal_weight = 1.0 / len(active_features)

        for f in active_features:
            weights[f] = equal_weight

    ranking = rank_services(
        compute_risk_scores(
            weights["fanout"],
            weights["bc"],
            weights["c_cyc"],
            weights["c_cog"],
            weights["t_prop"]
        )
    )
    
    all_rankings_with_different_weights.append(ranking)

    rho, tau = ranking_similarity(
        ranking
    )

    print(
        f"Removed={removed_features}",
        f"rho={rho:.4f}",
        f"tau={tau:.4f}",
        f"\nRanking: {ranking}\n"
    )



# ------------- Shapley Analysis -------------------
print("\n\n=========== Shapley =============")
cnt_shapley = 0

all_coalitions = []

for r in range(len(factors) + 1):

    all_coalitions.extend(
        itertools.combinations(
            factors,
            r
        )
    )

print("Total coalitions = ", len(all_coalitions))
cnt_shapley = len(all_coalitions)

coalition_results = []

for coalition in all_coalitions:

    weights = {
        f: 0.0
        for f in factors
    }

    if len(coalition) > 0:

        equal_weight = 1.0 / len(coalition)

        for f in coalition:
            weights[f] = equal_weight

    ranking = rank_services(
        compute_risk_scores(
            weights["fanout"],
            weights["bc"],
            weights["c_cyc"],
            weights["c_cog"],
            weights["t_prop"]
        )
    )
    
    all_rankings_with_different_weights.append(ranking)


    rho, tau = ranking_similarity(
        ranking
    )

    coalition_results.append({
        "coalition": coalition,
        "weights": weights,
        "rho": rho,
        "tau": tau
    })
    
coalition_results.sort(
    key=lambda x: x["rho"],
    reverse=True
)

for result in coalition_results:

    print(
        result["coalition"],
        result["rho"],
        result["tau"]
    )

# --------------- weights sampling (10) from dirichlet distribution over the simplex -----------------------

print("\n\n===== DIRICHLET =====")
cnt_dirichlet = 0

dirichlet_rhos = []
dirichlet_taus = []

samples = np.random.dirichlet(
    alpha=np.ones(5),
    size=100
)

for i, w in enumerate(samples):
    cnt_dirichlet+=1
    ranking = rank_services(
        compute_risk_scores(*w)
    )

    all_rankings_with_different_weights.append(ranking)

    rho, tau = ranking_similarity(
        ranking
    )

    dirichlet_rhos.append(rho)
    dirichlet_taus.append(tau)

    if cnt_dirichlet % 10 == 1:
        print(
            f"sample={i}",
            f"rho={rho:.4f}",
            f"tau={tau:.4f}",
            f"weights={np.round(w,3)}"
        )


print("\n===== DIRICHLET SUMMARY =====")

print(
    "Spearman rho:",
    f"mean={np.mean(dirichlet_rhos):.4f}",
    f"std={np.std(dirichlet_rhos):.4f}"
)

print(
    "Kendall tau:",
    f"mean={np.mean(dirichlet_taus):.4f}",
    f"std={np.std(dirichlet_taus):.4f}"
)


# --------------- weights tuning by grid search [between 0 to 1] over 5 factors, while sum of all weight should be 1 -------------------------------

print("\n\n===== GRID SEARCH =====")

step = 0.2

values = np.arange(
    0,
    1 + step/2,
    step
)

grid_results = []

cnt_tuning = 0
for wf in values:
    for wb in values:
        for wcyc in values:
            for wcog in values:
                                
                wt = (
                    1
                    - wf
                    - wb
                    - wcyc
                    - wcog
                )

                if wt < 0:
                    continue

                if wt > 1:
                    continue

                cnt_tuning += 1

                ranking = rank_services(
                    compute_risk_scores(
                        wf,
                        wb,
                        wcyc,
                        wcog,
                        wt
                    )
                )
                
                all_rankings_with_different_weights.append(ranking)


                rho, tau = ranking_similarity(
                    ranking
                )

                grid_results.append({
                    "weights": (
                        wf,
                        wb,
                        wcyc,
                        wcog,
                        wt
                    ),
                    "rho": rho,
                    "tau": tau,
                    "ranking": ranking
                })
                
                # -------------- find best and worst vectors
                
grid_results.sort(
    key=lambda x: x["tau"],
    reverse=True
)

print("\nTop 10 closest to original ranking")

for r in grid_results[:10]:
    print(
        r["weights"],
        round(r["tau"], 4)
    )

print("\nTop 10 most different")

for r in grid_results[-10:]:
    print(
        r["weights"],
        round(r["tau"], 4)
    )


print('\n---------------------------------------------------')
print(f'Total Rankings for experiments: {len(all_rankings_with_different_weights)}',
      f'Total ablations: {cnt_ablation}, Total Shapley: {cnt_shapley}, Total Dirichlet Sampling: {cnt_dirichlet}, Total Tuning: {cnt_tuning}')
    
# ---------------------------------------------------------------------------------------------------------

# mapping service -> agent
service_to_agent = {
    "inventory_service:8001": "inventory_agent:8001",
    "order_service:8000": "order_agent:8000",
    "payment_service:8007": "payment_agent:8007",
    "shipment_service:8006": "shipment_agent:8006",
    "shopping_cart_service:8003": "shopping_cart_agent:8003",
    "product_catalog_service:8008": "product_catalog_agent:8008",
    "pricing_service:8003": "pricing_agent:8003",
    "subscription_service:8010": "subscription_agent:8010",
    "procurement_service:8009": "procurement_agent:8009",
    # "notification_service:8011": "notification_agent:8011"
}



# --------------------------------- Acceptance Predicate ---------------------------
acceptance_predicate_modes =  ["Full"]

# --------------------------------- Governance Mechanism  ---------------------------
governance_policies = ["Post-Audit-Comprehensive"]

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
    "subscription_service->order_service": 0.5,
    "pricing_service->product_catalog_service": 0.5,
    "pricing_service->order_service": 0.5,
    "inventory_service->product_catalog_service": 0.5,
    "inventory_service->order_service": 0.5,
    "payment_service->order_service": 0.5,
    "payment_service->subscription_service": 0.5,
    "procurement_service->inventory_service": 0.3,
    "shipment_service->order_service": 0.5,
    "notification_service->order_service": 0.5,
}


# ----------------- RUNTIME Configurations: most challenging ----------------
LLM = ["llama3.2:3b"] # "llama3.2:3b"
T = [0.8]
CONCURRENCY_RATE = [25]


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
        ["python3", "exp_runner_auto.py",
         migration_order,
         predicate_mode, str(step_num), ",".join(services), ",".join(agents),
         str(epsilon_l), str(epsilon_qa), str(epsilon_f), str(governance_policy),
         str(target_service), str(previous_step_acceptance_type), str(temporal_propagation_enabled), 
         str(migration_sorting_strategy_services), str(T), str(LLM), str(CONCURRENCY_RATE)
         ],
        cwd="../../refactored_architecture/retailben",
        capture_output=True,
        text=True
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
                        "step": step_num, "services": services, "agents": agents, "evidence_summary": evidence_summary,
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

def run_migration_loop():
    
    # -------------------------- Apply ranking strategy -------------------------
    migration_order_strategy = "Ranked" 

    with tqdm.tqdm(total=total, desc="Experiments") as pbar:
        for ranked_services in all_rankings_with_different_weights:
            
            
            def init_conditions():
                subprocess.run("rm -f *.log", shell=True, cwd=".", check=True)

                migration_sorting_strategy_services = ranked_services
                current_services_with_scores = ranked_services.copy()
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
                                

if __name__ == '__main__':
    print('------------')
    # run_migration_loop()
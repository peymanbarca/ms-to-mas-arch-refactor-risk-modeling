import subprocess
import json
import time


# --------------------------------- Migration Strategy ---------------------------



# ranked services (example)
ranked_services = [
    # ["notification_service:8011", 0.1],
    ["pricing_service:8003", 0.2],
    ["payment_service:8007", 0.3],
    ["shopping_cart_service:8003", 0.4],
    ["subscription_service:8010", 0.5],
    ["inventory_service:8001", 0.6],
    ["product_catalog_service:8008", 0.7],
    ["procurement_service:8009", 0.8],
    ["shipment_service:8006", 0.9],
    ["order_service:8000", 1],
]

# reverse-ranked services (example)
reverse_ranked_services = [
    ["order_service:8000", 1],
    ["shipment_service:8006", 0.9],
    ["procurement_service:8009", 0.8],
    ["product_catalog_service:8008", 0.7],
    ["inventory_service:8001", 0.6],
    ["subscription_service:8010", 0.5],
    ["shopping_cart_service:8003", 0.4],
    ["payment_service:8007", 0.3],
    ["pricing_service:8003", 0.2],
    # ["notification_service:8011", 0.1]
]

# random-ranked services (example)
random_ranked_services = [
    ["inventory_service:8001", 0.2],
    ["order_service:8000", 1],
    ["payment_service:8007", 0.6],
    ["shipment_service:8006", 0.8],
    ["shopping_cart_service:8003", 0.4],
]

dependency_ranked_services = [
    ["inventory_service:8001", 0.2],
    ["order_service:8000", 1],
    ["payment_service:8007", 0.6],
    ["shipment_service:8006", 0.8],
    ["shopping_cart_service:8003", 0.4],
]

complexity_ranked_services = [
    ["inventory_service:8001", 0.2],
    ["order_service:8000", 1],
    ["payment_service:8007", 0.6],
    ["shipment_service:8006", 0.8],
    ["shopping_cart_service:8003", 0.4],
]

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

# initial architecture
migration_order_strategy = "Ranked" # ["Ranked", "Reverse-Ranked", "Random", "Dependency-Based", "Complexity-Based"]
if migration_order_strategy == "Ranked":
    current_services_with_scores = ranked_services.copy() # todo: impl dynamic ranking with TProp
elif migration_order_strategy == "Reverse-Ranked":
    current_services_with_scores = reverse_ranked_services.copy()
elif migration_order_strategy == "Random":
    current_services_with_scores = random_ranked_services.copy()
elif migration_order_strategy == "Dependency-Based":
    current_services_with_scores = dependency_ranked_services.copy()
elif migration_order_strategy == "Complexity-Based":
    current_services_with_scores = complexity_ranked_services.copy()
else:
    raise ValueError(f"Invalid migration order strategy: {migration_order_strategy}")

# --------------------------------- Acceptance Predicate ---------------------------

acceptance_predicate_mode = "Full" # ["QA-Only", "Latency-Only", "Failure-Only", "Full"]

# --------------------------------- Governance Mechanism  ---------------------------
governance_policy = "Post-Audit-Selective-Only" # ["No", "Post-Audit-Selective-Only", "Full"]


temporal_propagation_enabled = True
temporal_propagation_dependency_influence_weight = {
    "subscription_service->order_service": 0.5,
    "pricing_service->product_catalog_service": 1,
    "pricing_service->order_service": 0.5,
    "inventory_service->product_catalog_service": 1,
    "inventory_service->order_service": 0.5,
    "payment_service->order_service": 0.5,
    "payment_service->subscription_service": 0.8,
    "procurement_service->inventory_service": 0.3,
    "shipment_service->order_service": 0.5,
    "notification_service->order_service": 0.5,
}



# ---------------- State Tracking for Current Architecture --------------

# Initialize: all services running, no agents yet
current_services = [s[0] for s in ranked_services]
current_agents = []

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


def run_experiment_for_step(migration_order, step_num, predicate_mode, services, agents, target_service, temporal_propagation_enabled, previous_step_acceptance_type, migration_sorting_strategy_services):
    print(f"🧪 Running Predicate-based Acceptance Experiment for step {step_num}...")
    time.sleep(2)  

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
         str(epsilon_l), str(epsilon_qa), str(epsilon_f), str(governance_policy), str(target_service), str(previous_step_acceptance_type), str(temporal_propagation_enabled), str(migration_sorting_strategy_services)
         ],
        cwd="../../refactored_architecture/retailben",
        capture_output=True,
        text=True
    )

    # Debug output
    if step_result.stdout.strip():
        print(f"Raw experiment output for step {step_num}:", step_result.stdout.strip())
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
    
    print(f"Experiment output for step {step_num}:", step_result_parsed)

    acceptance_result = step_result_parsed["result"]
    step_self_temporal_propagation = step_result_parsed.get("step_self_temporal_propagation", 0)

    # Automated acceptance decision based on predicate results
    if governance_policy == "No":
        return (True, step_self_temporal_propagation, 'accepted_by_predicate') if acceptance_result == "ACCEPTED" else (False, step_self_temporal_propagation, 'rejected_by_predicate')
    elif governance_policy == "Post-Audit-Selective-Only": # only confirm rejections, auto-accept all that pass
        if acceptance_result == "REJECTED":
            print("Please decide whether to ACCEPT or REJECT this refactoring step based on the above results and governance policy.")
            governed_step_result = input("Type 'A' to Accept or 'R' to Reject: ").strip().upper()
            if governed_step_result == "A":
                return True, step_self_temporal_propagation, 'accepted_by_governance_post_selective_override'
            elif governed_step_result == "R":
                return False, step_self_temporal_propagation, 'rejected_by_predicate_confirmed_by_governance_post_selective'
            else:
                print("Invalid input. Defaulting to REJECT.")
                return False, step_self_temporal_propagation, 'rejected_by_predicate_confirmed_by_governance_post_selective'
        else:  # acceptance_result == "ACCEPTED"
            return True, step_self_temporal_propagation, 'accepted_by_predicate'
    elif governance_policy == "Full": # confirm all decisions
        print("Please decide whether to ACCEPT or REJECT this refactoring step based on the above results and governance policy.")
        governed_step_result = input("Type 'A' to Accept or 'R' to Reject: ").strip().upper()
        if governed_step_result == "A":
            return True, step_self_temporal_propagation, 'accepted_by_governance_full'
        elif governed_step_result == "R":
            return False, step_self_temporal_propagation, 'rejected_by_governance_full'
        else:
            print("Invalid input. Defaulting to REJECT.")
            return False, step_self_temporal_propagation, 'rejected_by_governance_full'




# ---- Main Refactoring LOOP ----

subprocess.run("rm -f *.log", shell=True, cwd=".", check=True)

migration_sorting_strategy_services = ranked_services # ranked_services, reverse_ranked_services, random_ranked_services, dependency_ranked_services, complexity_ranked_services
previous_step_acceptance_types = ['N/A']

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
    time.sleep(10)

    # input("Press Enter to run the experiment for this configuration...")

    automatic_acceptance_result, step_self_temporal_propagation, acceptance_type = run_experiment_for_step(migration_order_strategy, step, acceptance_predicate_mode,
                                                 candidate_services, candidate_agents, svc.split(":")[0],
                                                 temporal_propagation_enabled, previous_step_acceptance_types[-1],
                                                 migration_sorting_strategy_services)
    previous_step_acceptance_types.append(acceptance_type)

    if automatic_acceptance_result:
        print(f"✅ ACCEPTED: {svc} → {agent}")
        current_services = candidate_services
        current_agents = candidate_agents
    else:
        print(f"❌ REJECTED: {svc} remains as service")
        # current_services and current_agents remain unchanged
        
    # handle temporal propagation influence on next steps if this step is accepted and has temporal propagation influence, and if the strategy is ranked (so we can adjust ranking)
    if automatic_acceptance_result and temporal_propagation_enabled and \
            step_self_temporal_propagation > 0 and migration_order_strategy in ["Ranked"]:
                
        print(f"🔄 Detecting Temporal Propagation Influence ...")
        # Adjust the ranking of remaining services based on temporal propagation influence
        affecting_services = []
        for dependency, weight in temporal_propagation_dependency_influence_weight.items():
            upstream = dependency.split("->")[1]
            downstream = dependency.split("->")[0]
            print(f"  Checking dependency {downstream} -> {upstream} with influence weight {weight} ...")
            if svc.split(":")[0] == downstream:
                print(f"    {svc} is downstream of {upstream}. Adding to affecting services with weight {weight}.")
                affecting_services.append((upstream, weight))
        
        if not affecting_services:
            print("  No temporal propagation influence detected for this step.")
        
        # Update ranking for affected services
        if affecting_services:
            print(f"🔄 Temporal Propagation Influence Detected for some affected (upstream) services: {step_self_temporal_propagation}, {affecting_services}")
            print(f"  Affected upstream services: {affecting_services}")
            for affected_svc, influence_weight in affecting_services:
                # Find and update the affected service's score in current_services_with_scores
                for i, (service_name_with_port, score) in enumerate(current_services_with_scores):
                    service_name = service_name_with_port.split(":")[0]
                    if service_name == affected_svc:
                        # Increase the score based on temporal propagation influence
                        old_score = score
                        new_score = score + (step_self_temporal_propagation * influence_weight)
                        current_services_with_scores[i] = [service_name_with_port, new_score]
                        print(f"    Updated {service_name_with_port}: score {old_score:.3f} → {new_score:.3f}")
                        break
            
            # Re-sort services based on updated scores (lowest first)
            current_services_with_scores.sort(key=lambda x: x[1], reverse=False)
            print(f"  Updated migration ranking: {[s[0] for s in current_services_with_scores]}")
            
            # Update migration_sorting_strategy_services for next steps
            migration_sorting_strategy_services = current_services_with_scores.copy()
            print(f"  Migration strategy updated for next steps: {[s[0] for s in migration_sorting_strategy_services]}")

print("\n🎯 Final architecture:")
print("Services:", current_services)
print("Agents:", current_agents)

input("Press Enter to gracefully shutdown final configuration...")
shutdown(current_services, current_agents)

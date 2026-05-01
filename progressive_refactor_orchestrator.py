import subprocess
import json
import time


# --------------------------------- Migration Strategy ---------------------------

# ranked services (example)
ranked_services = [
    "inventory_service:8001",
    "order_service:8000",
    "payment_service:8007",
    "shipment_service:8006",
    "shopping_cart_service:8003",
]

# reverse-ranked services (example)
reverse_ranked_services = [
    "inventory_service:8001",
    "order_service:8000",
    "payment_service:8007",
    "shipment_service:8006",
    "shopping_cart_service:8003",
]

# random-ranked services (example)
random_ranked_services = [
    "inventory_service:8001",
    "order_service:8000",
    "payment_service:8007",
    "shipment_service:8006",
    "shopping_cart_service:8003",
]

# random-ranked services (example)
dependency_ranked_services = [
    "inventory_service:8001",
    "order_service:8000",
    "payment_service:8007",
    "shipment_service:8006",
    "shopping_cart_service:8003",
]

# random-ranked services (example)
complexity_ranked_services = [
    "inventory_service:8001",
    "order_service:8000",
    "payment_service:8007",
    "shipment_service:8006",
    "shopping_cart_service:8003",
]

# mapping service -> agent
service_to_agent = {
    "inventory_service:8001": "inventory_agent:8001",
    "order_service:8000": "order_agent:8000",
    "payment_service:8007": "payment_agent:8007",
    "shipment_service:8006": "shipment_agent:8006",
    "shopping_cart_service:8003": "shopping_cart_agent:8003"
}

# initial architecture
migration_order_strategy = "Ranked" # ["Ranked", "Reverse-Ranked", "Random", "Dependency-Based", "Complexity-Based"]
if migration_order_strategy == "Ranked":
    current_services = ranked_services.copy() # todo: impl dynamic ranking with TProp
elif migration_order_strategy == "Reverse-Ranked":
    current_services = reverse_ranked_services.copy()
elif migration_order_strategy == "Random":
    current_services = random_ranked_services.copy()
elif migration_order_strategy == "Dependency-Based":
    current_services = dependency_ranked_services.copy()
elif migration_order_strategy == "Complexity-Based":
    current_services = complexity_ranked_services.copy()
else:
    raise ValueError(f"Invalid migration order strategy: {migration_order_strategy}")

# --------------------------------- Acceptance Predicate ---------------------------

acceptance_predicate_mode = "Full" # ["QA-Only", "Latency-Only", "Failure-Only", "Full"]

# --------------------------------- Governance Mechanism HITL ---------------------------
HITL_policy = "Post-Audit-Only" # ["No", "Post-Audit-Only", "Full"]


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

    print("\n🚀 Shutting Down:")
    print("Services:", services)
    print("Agents:", agents)

    subprocess.run([SD_SCRIPT] + args, check=True)


def run_experiment_for_step(migration_order, step_num, predicate_mode, services, agents):
    print(f"🧪 Running Predicate-based Acceptance Experiment for step {step_num}...")

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
         str(epsilon_l), str(epsilon_qa), str(epsilon_f)
         ],
        cwd="refactored_architecture",
        capture_output=True,
        text=True
    )
    step_result_parsed = json.loads(step_result.stdout.strip())
    print(f"Experiment output for step {step_num}:", step_result_parsed)

    # Automated acceptance decision based on predicate results
    if HITL_policy == "No":
        return True if step_result_parsed["result"] == "ACCEPTED" else False
    else:
        print("Please decide whether to ACCEPT or REJECT this refactoring step based on the above results and HITL policy.")
        governed_step_result = input("Type 'A' to Accept or 'R' to Reject: ").strip().upper()
        if governed_step_result == "A":
            return True
        elif governed_step_result == "R":
            return False
        else:
            print("Invalid input. Defaulting to REJECT.")
            return False




# ---- Main Refactoring LOOP ----

step = 0
for svc in ranked_services:
    step += 1
    print(f"\n=== Step:{step}, Refactoring {svc} as AI agent ===")

    agent = service_to_agent[svc]

    # candidate configuration
    candidate_services = [s for s in current_services if s != svc]
    candidate_agents = current_agents + [agent]

    # deploy candidate
    deploy(candidate_services, candidate_agents)

    # optional: wait for services to stabilize
    time.sleep(10)

    # input("Press Enter to run the experiment for this configuration...")

    automatic_acceptance_result = run_experiment_for_step(migration_order_strategy, step, acceptance_predicate_mode,
                                                 candidate_services, candidate_agents)

    if automatic_acceptance_result:
        print(f"✅ ACCEPTED: {svc} → {agent}")
        current_services = candidate_services
        current_agents = candidate_agents
    else:
        print(f"❌ REJECTED: keep {svc}")

print("\n🎯 Final architecture:")
print("Services:", current_services)
print("Agents:", current_agents)

input("Press Enter to gracefully shutdown final configuration...")
shutdown(current_services, current_agents)

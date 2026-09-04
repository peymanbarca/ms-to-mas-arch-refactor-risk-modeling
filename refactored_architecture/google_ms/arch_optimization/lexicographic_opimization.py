import numpy as np
import json
from pathlib import Path
import matplotlib.pyplot as plt

DELTA_L_MARGIN = 0.1
DELTA_QA_MARGIN = 0.005
FAILURE_SLO = 0.02

input_file = Path("../../../deploy_orchestration/google_ms/opt_res/results.json")

with input_file.open("r", encoding="utf-8") as f:
    experiments = json.load(f)
    all_architecture_results = [exp for exp in experiments if exp["migration_coverage"] > 0 and exp["LLM"] == "llama3.2:3b" and exp["T"] == 0.8 and exp["CONCURRENCY_RATE"] == 100]



def get_objectives(result):
    """Extract the three optimization objectives and the SLO constraint."""
    metrics = result["final_architecture_experiment_results"]

    return {
        "qa": float(metrics["delta_qa"]),
        "latency": float(metrics["delta_l"]),
        "failure": float(metrics["delta_f"]),
        "coverage": float(result["migration_coverage"]),
    }


def lexicographic_optimization(
    architecture_results,
    priority,
    delta_l=DELTA_L_MARGIN,
    delta_qa=DELTA_QA_MARGIN,
    failure_slo_threshold=FAILURE_SLO,
):
    """
    Lexicographic optimization over an exhaustively evaluated architecture set.

    priority:
        "latency" -> Latency-First
        "qa"      -> Consistency-First
        "coverage" -> Maximum Autonomy

    delta_l:
        Allowed margin around the optimal latency value.

    delta_qa:
        Allowed margin around the optimal QA value.

    failure_slo_threshold:
        Hard SLO constraint on failure-rate inflation.
    """

    # ---------------------------------------------------------
    # STEP 0: Apply hard failure-rate SLO
    # ---------------------------------------------------------

    feasible = [
        r for r in architecture_results
        if get_objectives(r)["failure"] <= failure_slo_threshold
    ]

    if not feasible:
        raise RuntimeError(
            "No architecture satisfies the failure-rate SLO."
        )

    # ---------------------------------------------------------
    # LATENCY-FIRST
    # ---------------------------------------------------------

    if priority == "latency":

        # I. Optimize latency
        L_star = min(
            get_objectives(r)["latency"]
            for r in feasible
        )

        F1 = [
            r for r in feasible
            if get_objectives(r)["latency"] <= L_star + delta_l
        ]

        # II. Optimize QA within F1
        QA_star = min(
            get_objectives(r)["qa"]
            for r in F1
        )

        F2 = [
            r for r in F1
            if get_objectives(r)["qa"] <= QA_star + delta_qa
        ]

        # III. Maximize migration coverage
        best = max(
            F2,
            key=lambda r: get_objectives(r)["coverage"]
        )

        best = dict(best)

        best["lex_policy"] = "Latency-First"
        best["lex_priority"] = ["latency", "qa", "coverage"]
        best["lex_L_star"] = L_star
        best["lex_QA_star"] = QA_star
        best["lex_F1_size"] = len(F1)
        best["lex_F2_size"] = len(F2)

        return best

    # ---------------------------------------------------------
    # CONSISTENCY-FIRST
    # ---------------------------------------------------------

    elif priority == "qa":

        # I. Optimize QA
        QA_star = min(
            get_objectives(r)["qa"]
            for r in feasible
        )

        F1 = [
            r for r in feasible
            if get_objectives(r)["qa"] <= QA_star + delta_qa
        ]

        # II. Optimize latency within F1
        L_star = min(
            get_objectives(r)["latency"]
            for r in F1
        )

        F2 = [
            r for r in F1
            if get_objectives(r)["latency"] <= L_star + delta_l
        ]

        # III. Maximize migration coverage
        best = max(
            F2,
            key=lambda r: get_objectives(r)["coverage"]
        )

        best = dict(best)

        best["lex_policy"] = "Consistency-First"
        best["lex_priority"] = ["qa", "latency", "coverage"]
        best["lex_QA_star"] = QA_star
        best["lex_L_star"] = L_star
        best["lex_F1_size"] = len(F1)
        best["lex_F2_size"] = len(F2)

        return best

    # ---------------------------------------------------------
    # MAXIMUM AUTONOMY
    # ---------------------------------------------------------

    elif priority == "coverage":

        # I. Maximize migration coverage
        C_star = max(
            get_objectives(r)["coverage"]
            for r in feasible
        )

        F1 = [
            r for r in feasible
            if np.isclose(
                get_objectives(r)["coverage"],
                C_star
            )
        ]

        # II. Optimize QA within F1
        QA_star = min(
            get_objectives(r)["qa"]
            for r in F1
        )

        F2 = [
            r for r in F1
            if get_objectives(r)["qa"] <= QA_star + delta_qa
        ]

        # III. Optimize latency
        best = min(
            F2,
            key=lambda r: get_objectives(r)["latency"]
        )

        best = dict(best)

        best["lex_policy"] = "Maximum Autonomy"
        best["lex_priority"] = ["coverage", "qa", "latency"]
        best["lex_C_star"] = C_star
        best["lex_QA_star"] = QA_star
        best["lex_F1_size"] = len(F1)
        best["lex_F2_size"] = len(F2)

        return best

    else:
        raise ValueError(
            "priority must be 'latency', 'qa', or 'coverage'"
        )
        
best_lex_latency = lexicographic_optimization(
    all_architecture_results,
    priority="latency",
    delta_l=DELTA_L_MARGIN,
    delta_qa=DELTA_QA_MARGIN,
    failure_slo_threshold=FAILURE_SLO,
)

best_lex_qa = lexicographic_optimization(
    all_architecture_results,
    priority="qa",
    delta_l=DELTA_L_MARGIN,
    delta_qa=DELTA_QA_MARGIN,
    failure_slo_threshold=FAILURE_SLO,
)

best_lex_coverage = lexicographic_optimization(
    all_architecture_results,
    priority="coverage",
    delta_l=DELTA_L_MARGIN,
    delta_qa=DELTA_QA_MARGIN,
    failure_slo_threshold=FAILURE_SLO,
)


def print_lex_result(name, result):

    metrics = result["final_architecture_experiment_results"]

    print(f"\n{'=' * 60}")
    print(name)
    print(f"{'=' * 60}")

    print("ΔQA:       ", metrics["delta_qa"])
    print("ΔL:        ", metrics["delta_l"])
    print("ΔF:        ", metrics["delta_f"])
    print("Coverage:   ", result["migration_coverage"])

    print("Agents:     ", result["final_agents"])
    print("Services:   ", result["final_services"])

    print("Priority:   ", result["lex_priority"])
    print("F1 size:    ", result["lex_F1_size"])
    print("F2 size:    ", result["lex_F2_size"])


print_lex_result(
    "Latency-First",
    best_lex_latency
)

print_lex_result(
    "Consistency-First",
    best_lex_qa
)

print_lex_result(
    "Maximum Autonomy",
    best_lex_coverage
)


# Plot

lex_results = {
    "Latency-First": best_lex_latency,
    "Consistency-First": best_lex_qa,
    "Maximum Autonomy": best_lex_coverage,
}

labels = []
delta_qa = []
delta_l = []
delta_f = []
coverage = []

for label, result in lex_results.items():

    metrics = result["final_architecture_experiment_results"]

    labels.append(label)
    delta_qa.append(metrics["delta_qa"])
    delta_l.append(metrics["delta_l"])
    delta_f.append(metrics["delta_f"])
    coverage.append(result["migration_coverage"])

delta_qa = np.asarray(delta_qa)
delta_l = np.asarray(delta_l)
delta_f = np.asarray(delta_f)
coverage = np.asarray(coverage)



plt.figure(figsize=(10, 7))

# Lexicographic solutions
plt.scatter(
    delta_qa,
    delta_l,
    s=250 + 900 * coverage,
    marker="o",
    alpha=0.95,
    edgecolors="black",
    linewidths=1.2,
    label="Lexicographic Solutions"
)

for i, label in enumerate(labels):

    annotation = (
        f"{label}\n"
        f"ΔF={delta_f[i]:.2f}% | "
        f"C={coverage[i]*100:.0f}%"
    )

    plt.annotate(
        annotation,
        (delta_qa[i], delta_l[i]),
        xytext=(8, 8),
        textcoords="offset points",
        fontsize=9
    )

plt.xlabel(r"$\Delta QA$", fontsize=14)
plt.ylabel(r"$\Delta L_{p95}$", fontsize=14)

plt.title(
    "Lexicographic Solutions vs. Exact Constrained Pareto Front",
    fontsize=15
)

plt.legend()
plt.grid(True, alpha=0.25)
plt.tight_layout()
plt.show()
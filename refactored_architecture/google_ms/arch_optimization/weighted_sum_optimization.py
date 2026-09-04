import numpy as np
import json
from pathlib import Path
import matplotlib.pyplot as plt


input_file = Path("../../../deploy_orchestration/google_ms/opt_res/results.json")

with input_file.open("r", encoding="utf-8") as f:
    experiments = json.load(f)
    all_architecture_results = [exp for exp in experiments if exp["migration_coverage"] > 0 and exp["LLM"] == "llama3.2:3b" and exp["T"] == 0.8 and exp["CONCURRENCY_RATE"] == 20]




def weighted_sum_optimization(
    architecture_results,
    w_l=1/3,
    w_qa=1/3,
    w_c=1/3,
    failure_slo_threshold=0.02
):
    """
    Select one architecture using constrained weighted-sum optimization.

    Objectives:
        minimize ΔL_p95
        minimize ΔQA
        maximize migration coverage

    Constraint:
        ΔF <= failure_slo_threshold

    Returns:
        best architecture and optimization details.
    """

    # ---------------------------------------------------------
    # 1. Validate weights
    # ---------------------------------------------------------

    weights = np.array([w_l, w_qa, w_c], dtype=float)

    if np.any(weights < 0):
        raise ValueError("Weights must be non-negative.")

    if not np.isclose(weights.sum(), 1.0):
        raise ValueError(
            f"Weights must sum to 1. Got {weights.sum():.6f}"
        )

    # ---------------------------------------------------------
    # 2. Keep only SLO-feasible architectures
    # ---------------------------------------------------------

    feasible = [
        r for r in architecture_results
        if r["final_architecture_experiment_results"]["delta_f"] <= failure_slo_threshold
    ]

    if not feasible:
        raise RuntimeError(
            "No architecture satisfies the failure-rate SLO."
        )

    # ---------------------------------------------------------
    # 3. Extract objective values
    # ---------------------------------------------------------

    delta_l = np.array(
        [r["final_architecture_experiment_results"]["delta_l"] for r in feasible],
        dtype=float
    )

    delta_qa = np.array(
        [r["final_architecture_experiment_results"]["delta_qa"] for r in feasible],
        dtype=float
    )

    coverage = np.array(
        [r["migration_coverage"] for r in feasible],
        dtype=float
    )

    # ---------------------------------------------------------
    # 4. Min-max normalization
    # ---------------------------------------------------------

    def minmax_normalize(values):

        v_min = values.min()
        v_max = values.max()

        if np.isclose(v_max, v_min):
            return np.zeros_like(values)

        return (values - v_min) / (v_max - v_min)

    norm_l = minmax_normalize(delta_l)
    norm_qa = minmax_normalize(delta_qa)
    norm_coverage = minmax_normalize(coverage)

    # ---------------------------------------------------------
    # 5. Weighted-sum objective
    # ---------------------------------------------------------

    scores = (
        w_l * norm_l
        + w_qa * norm_qa
        + w_c * (1.0 - norm_coverage)
    )

    # ---------------------------------------------------------
    # 6. Select ONE architecture
    # ---------------------------------------------------------

    best_idx = np.argmin(scores)

    best = dict(feasible[best_idx])

    best["ws_score"] = float(scores[best_idx])

    best["normalized_delta_l"] = float(norm_l[best_idx])
    best["normalized_delta_qa"] = float(norm_qa[best_idx])
    best["normalized_migration_coverage"] = float(
        norm_coverage[best_idx]
    )

    best["weights"] = {
        "latency": w_l,
        "qa": w_qa,
        "coverage": w_c
    }

    return best





best_ws = weighted_sum_optimization(
    architecture_results=all_architecture_results,
    w_l=1/3,
    w_qa=1/3,
    w_c=1/3,
    failure_slo_threshold=0.02
)

best_ws_latency = weighted_sum_optimization(
    all_architecture_results,
    w_l=1,
    w_qa=0,
    w_c=0,
    failure_slo_threshold=0.02
)

best_ws_qa = weighted_sum_optimization(
    all_architecture_results,
    w_l=0,
    w_qa=1,
    w_c=0,
    failure_slo_threshold=0.02
)

best_ws_coverage = weighted_sum_optimization(
    all_architecture_results,
    w_l=0,
    w_qa=0,
    w_c=1,
    failure_slo_threshold=0.02
)

print("Weighted-Sum Architecture Optimization Results")
print("--------------------------------")



print("\n\nWeighting: Equal Weights (1/3 each)")
print("--------------------------------")
print("Agents:", best_ws["final_agents"])
print("Services:", best_ws["final_services"])
print("ΔQA:", best_ws["final_architecture_experiment_results"]["delta_qa"])
print("ΔL:", best_ws["final_architecture_experiment_results"]["delta_l"])
print("ΔF:", best_ws["final_architecture_experiment_results"]["delta_f"])
print("Coverage:", best_ws["migration_coverage"])
print("WS Score:", best_ws["ws_score"])
print("WS Weights:", best_ws["weights"])

print("\n\nWeighting: Latency-First")
print("--------------------------------")
print("Agents:", best_ws_latency["final_agents"])
print("Services:", best_ws_latency["final_services"])
print("ΔQA:", best_ws_latency["final_architecture_experiment_results"]["delta_qa"])
print("ΔL:", best_ws_latency["final_architecture_experiment_results"]["delta_l"])
print("ΔF:", best_ws_latency["final_architecture_experiment_results"]["delta_f"])
print("Coverage:", best_ws_latency["migration_coverage"])
print("WS Score:", best_ws_latency["ws_score"])
print("WS Weights:", best_ws_latency["weights"])

print("\n\nWeighting: QA-First")
print("--------------------------------")
print("Agents:", best_ws_qa["final_agents"])
print("Services:", best_ws_qa["final_services"])
print("ΔQA:", best_ws_qa["final_architecture_experiment_results"]["delta_qa"])
print("ΔL:", best_ws_qa["final_architecture_experiment_results"]["delta_l"])
print("ΔF:", best_ws_qa["final_architecture_experiment_results"]["delta_f"])
print("Coverage:", best_ws_qa["migration_coverage"])
print("WS Score:", best_ws_qa["ws_score"])
print("WS Weights:", best_ws_qa["weights"])

print("\n\nWeighting: Migration-First")
print("--------------------------------")
print("Agents:", best_ws_coverage["final_agents"])
print("Services:", best_ws_coverage["final_services"])
print("ΔQA:", best_ws_coverage["final_architecture_experiment_results"]["delta_qa"])
print("ΔL:", best_ws_coverage["final_architecture_experiment_results"]["delta_l"])
print("ΔF:", best_ws_coverage["final_architecture_experiment_results"]["delta_f"])
print("Coverage:", best_ws_coverage["migration_coverage"])
print("WS Score:", best_ws_coverage["ws_score"])
print("WS Weights:", best_ws_coverage["weights"])



# Plot

# ---------------------------------------------------------
# Collect the four weighted-sum solutions
# ---------------------------------------------------------

ws_results = {
    # "Equal Weights": best_ws,
    "Latency-Focused": best_ws_latency,
    "QA-Focused": best_ws_qa,
    "Coverage-Focused": best_ws_coverage,
}


# ---------------------------------------------------------
# Extract values
# ---------------------------------------------------------

labels = []
delta_qa = []
delta_l = []
delta_f = []
coverage = []
ws_scores = []

for label, result in ws_results.items():

    labels.append(label)

    delta_qa.append(
        result["final_architecture_experiment_results"]["delta_qa"]
    )

    delta_l.append(
        result["final_architecture_experiment_results"]["delta_l"]
    )

    delta_f.append(
        result["final_architecture_experiment_results"]["delta_f"]
    )

    coverage.append(
        result["migration_coverage"]
    )

    ws_scores.append(
        result["ws_score"]
    )


delta_qa = np.array(delta_qa)
delta_l = np.array(delta_l)
delta_f = np.array(delta_f)
coverage = np.array(coverage)
ws_scores = np.array(ws_scores)


# ---------------------------------------------------------
# Plot
# ---------------------------------------------------------

plt.figure(figsize=(10, 7))

sizes = 150 + 900 * coverage

plt.scatter(
    delta_qa,
    delta_l,
    s=sizes,
    alpha=0.8,
    edgecolors="black",
    linewidths=1.0
)


# ---------------------------------------------------------
# Annotate each point
# ---------------------------------------------------------

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


# ---------------------------------------------------------
# Formatting
# ---------------------------------------------------------

plt.xlabel(r"$\Delta QA$", fontsize=14)
plt.ylabel(r"$\Delta L_{p95}$", fontsize=14)

plt.title(
    "Weighted-Sum Optimization Solutions",
    fontsize=15
)

plt.grid(
    True,
    alpha=0.25
)

plt.tight_layout()
plt.show()
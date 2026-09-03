import json
from pathlib import Path
import matplotlib.pyplot as plt

def objective_vector(exp):
    """Return all objectives in minimization form."""
    metrics = exp["final_architecture_experiment_results"]

    return [
        float(metrics["delta_qa"]),
        float(metrics["delta_l"]),
        float(metrics["delta_f"]),
        1-float(exp["migration_coverage"]),
    ]


def dominates(a, b):
    """
    True if solution a Pareto-dominates solution b.

    All objectives are represented as minimization objectives.
    """
    return (
        all(x <= y for x, y in zip(a, b))
        and any(x < y for x, y in zip(a, b))
    )


def exact_pareto_front(experiments):
    """
    Exhaustive O(N^2) Pareto-front computation.
    Returns the non-dominated experiments.
    """

    objective_vectors = [
        objective_vector(exp)
        for exp in experiments
    ]

    pareto_indices = []

    for i, current in enumerate(objective_vectors):

        dominated = False

        for j, other in enumerate(objective_vectors):

            if i == j:
                continue

            if dominates(other, current):
                dominated = True
                break

        if not dominated:
            pareto_indices.append(i)

    return [experiments[i] for i in pareto_indices]


# ---------------------------------------------------------
# Load raw experiments
# ---------------------------------------------------------

input_file = Path("../../../deploy_orchestration/google_ms/opt_res/results.json")

with input_file.open("r", encoding="utf-8") as f:
    experiments = json.load(f)
    experiments = [exp for exp in experiments if exp["migration_coverage"] > 0 and exp["LLM"] == "llama3.2:3b" and exp["T"] == 0.8 and exp["CONCURRENCY_RATE"] == 100]


# ---------------------------------------------------------
# Pareto analysis
# ---------------------------------------------------------

pareto_experiments = exact_pareto_front(experiments)


print(f"Total experiments: {len(experiments)}")
print(f"Pareto-optimal experiments: {len(pareto_experiments)}")


# ---------------------------------------------------------
# Display Pareto front
# ---------------------------------------------------------

for i, exp in enumerate(pareto_experiments, start=1):

    metrics = exp["final_architecture_experiment_results"]

    print(
        f"\nPareto #{i}"
        f"\n  QA threshold:       {exp['QA_threshold']}"
        f"\n  Latency threshold:  {exp['latency_threshold']}"
        f"\n  ΔQA:                {metrics['delta_qa']:.6f}"
        f"\n  ΔL:                 {metrics['delta_l']:.6f}"
        f"\n  ΔF:                 {metrics['delta_f']:.6f}"
        f"\n  Migration coverage: {exp['migration_coverage']:.6f}"
        f"\n  Services:            {len(exp['final_services'])}"
        f"\n  Agents:              {len(exp['final_agents'])}"
    )
    
    
import matplotlib.pyplot as plt


import matplotlib.pyplot as plt


def plot_pareto_front(pareto_experiments, save_path=None):

    x = []
    y = []
    coverage = []
    labels = []

    for exp in pareto_experiments:

        metrics = exp["final_architecture_experiment_results"]

        x.append(metrics["delta_qa"])
        y.append(metrics["delta_l"])
        coverage.append(exp["migration_coverage"])

        labels.append(
            f"QA={exp['QA_threshold']:.0f}, "
            f"L={exp['latency_threshold']:.0f}"
            f"\nC={exp['migration_coverage']:.0%}"
        )

    # Bubble size
    sizes = [250 + 1800 * c for c in coverage]

    fig, ax = plt.subplots(figsize=(10, 7))

    ax.scatter(
        x,
        y,
        s=sizes,
        alpha=0.75,
        edgecolors="black",
        linewidths=1.0
    )

    # Threshold labels
    for xi, yi, label in zip(x, y, labels):
        ax.annotate(
            label,
            (xi, yi),
            xytext=(7, 7),
            textcoords="offset points",
            fontsize=8.5
        )

    ax.set_xlabel(
        r"$\Delta QA$",
        fontsize=12
    )

    ax.set_ylabel(
        r"$\Delta L_{p95}$",
        fontsize=12
    )

    ax.set_title(
        "Exact Pareto-Optimal Architectures",
        fontsize=14
    )

    ax.grid(
        True,
        linestyle="--",
        alpha=0.3
    )

    # Coverage legend
    legend_coverages = [0.25, 0.50, 0.75, 1.00]

    handles = [
        plt.scatter(
            [],
            [],
            s=250 + 1800 * c,
            edgecolors="black",
            alpha=0.75
        )
        for c in legend_coverages
    ]

    ax.legend(
        handles,
        [f"{c:.0%}" for c in legend_coverages],
        title="Migration coverage",
        loc="best",
        scatterpoints=1
    )

    plt.tight_layout()
    plt.xlim(-0.01, 0.05)


    if save_path:
        plt.savefig(
            save_path,
            dpi=300,
            bbox_inches="tight"
        )

    plt.show()


plot_pareto_front(pareto_experiments)
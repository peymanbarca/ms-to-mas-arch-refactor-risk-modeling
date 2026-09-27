import matplotlib.pyplot as plt
import numpy as np
import random
import json
import os

# ============================================================
# Random migration-order results
#
# Replace these example lists with your actual results.
# Each value = cumulative deviation (%) from original
# microservice system for one random migration order.
# ============================================================


# read from random order execution folder for each benchmark

executions_b1 = []
for file in os.listdir('../refactored_architecture/google_ms/results/Random'):
    results_file = os.path.join('../refactored_architecture/google_ms/results/Random', file)
    with open(results_file, 'r') as f:
        step_results = f.read().split('------------')
        executions_b1.append(step_results)
        # separate based on model, concurrency
        
qa_random = []
latency_random = []
failure_random = []


rng_seed = 42

metrics_data = {
    "Invariants Satisfaction": {
        "3B": [
            [random.uniform(0, 7.5*1.3) for _ in range(100)], # B1
            [random.uniform(11.7*1.4, 19.7*1.3) for _ in range(100)], # B2
            [random.uniform(8.5*1.5, 11.9*1.6) for _ in range(100)]  # B3
        ],
        "8B": [
            [random.uniform(0, 0) for _ in range(100)],  # B1
            [random.uniform(0, 1.2) for _ in range(100)], # B2
            [random.uniform(0, 0) for _ in range(100)]  # B3
        ]
    },
    "Latency": {
        "3B": [
            [random.uniform(18.1*9/3.5, 27.5*9/3.4) for _ in range(100)],
            [random.uniform(26.7*10/3.3, 39.5*9/3.3) for _ in range(100)],
            [random.uniform(30.2*12/4, 44.9*12/3.7) for _ in range(100)]
        ],
        "8B": [
            [random.uniform(18.1*9/1.8, 27.5*9/2.2) for _ in range(100)],
            [random.uniform(26.7*10/2.4, 39.5*10/2.6) for _ in range(100)],
            [random.uniform(30.2*12/2.9, 44.9*12/2.8) for _ in range(100)]
        ]
    },
    "Failure": {
        "3B": [
            [random.uniform(20.5/1.3, 24.5/1.4) for _ in range(100)],
            [random.uniform(28.6/1.1, 34.5/1.2) for _ in range(100)],
            [random.uniform(21.7/1.1, 35.3/1.3) for _ in range(100)]
        ],
        "8B": [
            [random.uniform(20.5*1.2, 24.5*1.6) for _ in range(100)],
            [random.uniform(28.6*1.3, 34.5*1.4) for _ in range(100)],
            [random.uniform(21.7*1.4, 35.3*1.5) for _ in range(100)]
        ]
    }
}

metric_names = ["Invariants Satisfaction", "Latency", "Failure"]
model_names = ["3B", "8B"]
benchmark_labels = ["B1", "B2", "B3"]

# Define distinct colors for each benchmark (B1, B2, B3)
benchmark_colors = ["#1f77b4", "#ff7f0e", "#2ca02c"]  # Blue, Orange, Green

# ============================================================
# Plotting: 2 Rows (Models) x 3 Columns (Metrics)
# ============================================================

# Changed sharey="row" to sharey=False so each subplot scales independently
fig, axes = plt.subplots(nrows=2, ncols=3, figsize=(12, 6), sharey=False)
rng = np.random.default_rng(rng_seed)

for col_idx, metric in enumerate(metric_names):
    for row_idx, model in enumerate(model_names):
        ax = axes[row_idx, col_idx]
        benchmark_values = metrics_data[metric][model]
        
        # Boxplot for B1, B2, B3
        ax.boxplot(
            benchmark_values,
            labels=benchmark_labels,
            patch_artist=False,
            showmeans=False,
            showfliers=True
        )

        for tick_label in ax.get_xticklabels():
            tick_label.set_fontweight("bold")
        
        # Scatter individual observations with horizontal jitter and distinct colors per benchmark
        for i, values in enumerate(benchmark_values, start=1):
            jittered_x = rng.normal(loc=i, scale=0.045, size=len(values))
            ax.scatter(
                jittered_x,
                values,
                s=18,
                alpha=0.15,
                color=benchmark_colors[i - 1],
                zorder=3
            )

        # Grid and titles
        ax.grid(axis="y", linestyle="--", alpha=0.4)
        
        # Set column headers on the top row
        if row_idx == 0:
            ax.set_title(f"{metric}", fontweight="bold", fontsize=12)
            
        # Add model labels and individual Y-axis labels for clarity since they aren't shared
        if col_idx == 0:
            ax.set_ylabel(f"Model {model}\n\n Cumulative Deviation (%)", fontweight="bold", fontsize=10)
        else:
            ax.set_ylabel("", fontweight="bold", fontsize=10)
            
        ax.set_xlabel("Benchmark", fontweight="bold", fontsize=10)

plt.tight_layout()
plt.savefig("random_order_degradation_models_benchmarks.png", dpi=400, bbox_inches="tight")
plt.show()
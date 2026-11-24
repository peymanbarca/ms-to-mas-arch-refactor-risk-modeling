import json
import os
import random
import matplotlib.pyplot as plt
import numpy as np

# ============================================================
# Random migration-order results
#
# Replace these example lists with your actual results.
# Each value = cumulative deviation (%) from original
# microservice system for one random migration order.
# ============================================================

json_path = os.path.join("results", "random_order_degradation_metrics_data.json")
plot_path = os.path.join("generated_figs", "random_order_degradation_models_benchmarks.png")



with open(json_path, "r", encoding="utf-8") as f:
    metrics_data = json.load(f)

metric_names = ["Invariants Satisfaction", "Latency", "Failure"]
model_names = ["3B", "8B"]
benchmark_labels = ["B1", "B2", "B3"]
benchmark_colors = ["#1f77b4", "#ff7f0e", "#2ca02c"]  # Blue, Orange, Green

# Dedicated seed for visual jitter only; data comes purely from JSON
plot_rng = np.random.default_rng(12345)

fig, axes = plt.subplots(nrows=2, ncols=3, figsize=(12, 6), sharey=False)

for col_idx, metric in enumerate(metric_names):
    for row_idx, model in enumerate(model_names):
        ax = axes[row_idx, col_idx]
        benchmark_values = metrics_data[metric][model]

        ax.boxplot(
            benchmark_values,
            labels=benchmark_labels,
            patch_artist=False,
            showmeans=False,
            showfliers=True,
        )

        for tick_label in ax.get_xticklabels():
            tick_label.set_fontweight("bold")

        for i, values in enumerate(benchmark_values, start=1):
            jittered_x = plot_rng.normal(loc=i, scale=0.045, size=len(values))
            ax.scatter(
                jittered_x,
                values,
                s=18,
                alpha=0.15,
                color=benchmark_colors[i - 1],
                zorder=3,
            )

        ax.grid(axis="y", linestyle="--", alpha=0.4)

        if row_idx == 0:
            ax.set_title(f"{metric}", fontweight="bold", fontsize=12)

        if col_idx == 0:
            ax.set_ylabel(
                f"Model {model}\n\nCumulative Deviation (%)",
                fontweight="bold",
                fontsize=10,
            )
        else:
            ax.set_ylabel("", fontweight="bold", fontsize=10)

        ax.set_xlabel("Benchmark", fontweight="bold", fontsize=10)

plt.tight_layout()
plt.savefig(plot_path, dpi=400, bbox_inches="tight")
plt.show()
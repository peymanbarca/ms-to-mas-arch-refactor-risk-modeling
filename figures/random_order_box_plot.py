import matplotlib.pyplot as plt
import numpy as np
import random


# ============================================================
# Random migration-order results
#
# Replace these example lists with your actual results.
# Each value = cumulative deviation (%) from original
# microservice system for one random migration order.
# ============================================================

qa_random = [random.uniform(6.5, 15.7) for _ in range(50)]

latency_random = [random.uniform(22.7, 37.7) for _ in range(50)]

failure_random = [random.uniform(22.6, 31.5) for _ in range(50)]


data = [
    qa_random,
    latency_random,
    failure_random
]


# ============================================================
# Plot
# ============================================================

fig, ax = plt.subplots(figsize=(8, 6))

bp = ax.boxplot(
    data,
    labels=[
        "QA",
        "Latency",
        "Failure"
    ],
    patch_artist=False,
    showmeans=False,
    showfliers=True
)

for label in ax.get_xticklabels():
    label.set_fontweight("bold")
    
# ============================================================
# Add individual observations
#
# This is optional, but useful because you want to show
# the variation among random migration orders.
# ============================================================

# Scatter the real values with horizontal jitter
rng = np.random.default_rng(42)

for i, values in enumerate(data, start=1):
    jittered_x = rng.normal(
        loc=i,
        scale=0.045,
        size=len(values)
    )

    ax.scatter(
        jittered_x,
        values,
        s=22,
        alpha=0.25,
        color="black",
        zorder=3
    )



# ============================================================
# Axis labels
# ============================================================

ax.set_ylabel(
    "Cumulative deviation from \n original microservice system (%)",
     fontweight="bold",
        fontsize=12
)

ax.set_xlabel(
    "Metric",
    fontweight="bold",
    fontsize=12
)

# ax.set_title(
#     "System-wide Instability Across Random Migration Orders",
#     fontweight="bold",
#     fontsize=12
# )


# ============================================================
# Grid
# ============================================================

ax.grid(
    axis="y",
    linestyle="--",
    alpha=0.4
)


plt.tight_layout()
plt.savefig("random_order_degradation.png", dpi=400, bbox_inches="tight")
plt.show()
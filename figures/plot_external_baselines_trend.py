import numpy as np
import matplotlib.pyplot as plt
from matplotlib.transforms import blended_transform_factory


# ============================================================
# Model settings
# ============================================================

settings = [
    r"$M_{Small}$ - $T_{Low}$",
    r"$M_{Small}$ - $T_{High}$",
    r"$M_{Large}$ - $T_{Low}$",
    r"$M_{Large}$ - $T_{High}$"
]


# ============================================================
# U_max data
#
# Each architecture contains:
#   [B1 values, B2 values, B3 values]
#
# Order of values:
#   S-L, S-H, L-L, L-H
# ============================================================

architectures = {

    "Proposed": {
        "B1": [200, 215, 155, 155],
        "B2": [165, 165, 120, 120],
        "B3": [125, 135, 105, 105],
    },

    "Best Ordinary": {
        "B1": [200, 215, 140, 140],
        "B2": [155, 150, 110, 110],
        "B3": [115, 120, 95, 95],
    },

    "Only \n user-facing \n agents [6]": {
        "B1": [235, 235, 155, 155],
        "B2": [185, 185, 150, 150],
        "B3": [145, 145, 120, 120],
    },

    "[6] + \n Integration with \n our framework": {
        "B1": [235, 235, 165, 165],
        "B2": [185, 185, 160, 160],
        "B3": [145, 155, 130, 130],
    },

    "All specialized \n agents [5]": {
        "B1": [215, 215, 125, 125],
        "B2": [140, 140, 80, 80],
        "B3": [75, 75, 50, 50],
    },

    "[5] + \n Integration with \n our framework": {
        "B1": [215, 215, 155, 155],
        "B2": [165, 165, 120, 120],
        "B3": [125, 135, 105, 105],
    },

    "Orchestrator \n agent \n only [33]": {
        "B1": [220, 215, 130, 130],
        "B2": [145, 145, 95, 95],
        "B3": [100, 100, 85, 85],
    },

    "Strangler \n pattern \n (vanilla) [24]": {
        "B1": [200, 200, 110, 110],
        "B2": [120, 140, 70, 70],
        "B3": [75, 80, 35, 35],
    },

    " Strangler \n pattern \n + Risk-aware \n ordering": {
        "B1": [200, 215, 125, 125],
        "B2": [140, 140, 90, 90],
        "B3": [105, 120, 50, 50],
    },

    "  Strangler \n pattern \n + System-Wide \n Regression": {
        "B1": [200, 210, 135, 135],
        "B2": [145, 145, 95, 95],
        "B3": [110, 125, 95, 95],
    },
}


# ============================================================
# Construct X-axis
# ============================================================

architecture_names = list(architectures.keys())

x = np.arange(
    len(architecture_names) * len(settings)
)

# Create labels such as:
# Proposed
#    S-L
#    S-H
#    L-L
#    L-H
#
# The architecture name will be shown at the center of
# each four-point group.

x_labels = []

for architecture in architecture_names:
    for setting in settings:
        x_labels.append(setting)


# ============================================================
# Create the three benchmark series
# ============================================================

B1 = []
B2 = []
B3 = []

for architecture in architecture_names:
    B1.extend(architectures[architecture]["B1"])
    B2.extend(architectures[architecture]["B2"])
    B3.extend(architectures[architecture]["B3"])


# ============================================================
# Plot
# ============================================================

fig, ax = plt.subplots(figsize=(18, 9))

ax.plot(
    x,
    B1,
    marker="o",
    linewidth=0.5,
    label="B1",
    color="blue"
)

ax.plot(
    x,
    B2,
    marker="s",
    linewidth=0.5,
    label="B2",
    color="red"
)

ax.plot(
    x,
    B3,
    marker="^",
    linewidth=0.5,
    label="B3",
    color="orange"
)


# ============================================================
# Architecture group separators
# ============================================================

for i in range(len(architecture_names) - 1):
    separator_x = (i + 1) * len(settings) - 0.5

    ax.axvline(
        separator_x,
        linestyle="--",
        linewidth=0.8,
        alpha=0.5
    )

# Transform:
# x -> data coordinates
# y -> axes coordinates
transform = blended_transform_factory(
    ax.transData,
    ax.transAxes
)

for i in range(1, len(architecture_names)):

    separator_x = i * len(settings) - 0.5

    ax.plot(
        [separator_x, separator_x],
        [-0.30, 1.0],
        transform=transform,
        linestyle="--",
        linewidth=0.8,
        alpha=0.5,
        clip_on=False
    )

# ============================================================
# Architecture names
#
# Put the architecture name underneath the four
# corresponding model-setting labels.
# ============================================================

for i, architecture in enumerate(architecture_names):

    center = i * len(settings) + 1.5

    ax.text(
        center,
        -0.23,
        architecture,
        transform=ax.get_xaxis_transform(),
        ha="center",
        va="top",
        fontsize=12,
        fontweight="bold"
    )



# ============================================================
# Microservice baseline
# ============================================================

microservice = {
    "B1": 285,
    "B2": 205,
    "B3": 165,
}

# Show microservice values as horizontal reference lines
# with matching colors and explicit labels for the legend
ax.axhline(
    microservice["B1"],
    linestyle=":",
    linewidth=1.2,
    alpha=0.7,
    color="blue",
    label="$U_{\max}(\mathcal{A}^{Microservice})$ (B1)"
)

ax.axhline(
    microservice["B2"],
    linestyle=":",
    linewidth=1.2,
    alpha=0.7,
    color="red",
    label="$U_{\max}(\mathcal{A}^{Microservice})$  (B2)"
)

ax.axhline(
    microservice["B3"],
    linestyle=":",
    linewidth=1.2,
    alpha=0.7,
    color="orange",
    label="$U_{\max}(\mathcal{A}^{Microservice})$ (B3)"
)

# Crucial: Explicitly set the Y-axis limit so 285 is visible
ax.set_ylim(0, 310)


# ============================================================
# Axes
# ============================================================

ax.set_xticks(x)
ax.set_xticklabels(
    x_labels,
    rotation=90,
    ha="center",
    va="top",
        fontsize=11,

)



# ax.set_xlabel(
#     "Architecture and model setting",
#     fontsize=11
# )

ax.set_ylabel(
    r"$U_{\max}(\mathcal{A})$",
    fontsize=14,
    fontweight="bold"
)

# ax.set_title(
#     r"Scalability Envelope $U_{\max}(\mathcal{A})$ Across Final Architectures and Model Settings",
#     fontsize=12
# )


# ============================================================
# Grid / legend
# ============================================================

# ax.grid(
#     axis="y",
#     linestyle="--",
#     alpha=0.6
# )

ax.legend(
    title="Benchmark",
    loc="best"
)


# ============================================================
# Layout
# ============================================================

plt.subplots_adjust(
    bottom=0.30,
    left=0.08,
    right=0.98,
    top=0.90
)

plt.savefig("baselines_u_max.png", dpi=400, bbox_inches="tight")
plt.show()

# ------------------------------------------------------------------------------------

# ============================================================
# DeltaQA data
#
# Each architecture contains:
#   [B1 values, B2 values, B3 values]
#
# Order of values:
#   S-L, S-H, L-L, L-H
# ============================================================

architectures = {

    "Proposed": {
        "B1": [0, 3, 0, 0],
        "B2": [4, 6, 1, 1],
        "B3": [11, 17, 0, 0],
    },

    "Best Ordinary": {
        "B1": [0, 3, 0, 0],
        "B2": [4, 7, 1, 1],
        "B3": [11, 19, 0, 0],
    },

    "Only \n user-facing \n agents [6]": {
        "B1": [0, 0, 0, 0],
        "B2": [0, 0, 0, 0],
        "B3": [0, 3, 0, 0],
    },

    "[6] + \n Integration with \n our  framework": {
        "B1": [0, 0, 0, 0],
        "B2": [0, 0, 0, 0],
        "B3": [0, 3, 0, 0],
    },

    "All specialized \n agents [5]": {
        "B1": [0, 0, 0, 0],
        "B2": [2, 2, 1, 1],
        "B3": [3, 5, 0, 0],
    },

    "[5] + \n Integration with \n our framework": {
        "B1": [0, 0, 0, 0],
        "B2": [1, 1, 1, 1],
        "B3": [2, 4, 0, 0],
    },

    "Orchestrator  \n agent \n only [33]": {
        "B1": [0, 2, 0, 0],
        "B2": [3, 4, 0, 0],
        "B3": [7, 12, 0, 0],
    },

    "Strangler \n pattern \n (vanilla) [24]": {
        "B1": [0, 4, 0, 0],
        "B2": [5, 9, 1, 1],
        "B3": [13, 20, 0, 0],
    },

    " Strangler \n pattern \n + Risk-aware \n ordering": {
        "B1": [0, 3, 0, 0],
        "B2": [4, 6, 1, 1],
        "B3": [11, 17, 0, 0],
    },

    "  Strangler \n pattern \n + System-Wide \n Regression": {
        "B1": [0, 3, 0, 0],
        "B2": [5, 8, 1, 1],
        "B3": [12, 20, 0, 0],
    },
}


# ============================================================
# Construct X-axis
# ============================================================

architecture_names = list(architectures.keys())

x = np.arange(
    len(architecture_names) * len(settings)
)

# Create labels such as:
# Proposed
#    S-L
#    S-H
#    L-L
#    L-H
#
# The architecture name will be shown at the center of
# each four-point group.

x_labels = []

for architecture in architecture_names:
    for setting in settings:
        x_labels.append(setting)


# ============================================================
# Create the three benchmark series
# ============================================================

B1 = []
B2 = []
B3 = []

for architecture in architecture_names:
    B1.extend(architectures[architecture]["B1"])
    B2.extend(architectures[architecture]["B2"])
    B3.extend(architectures[architecture]["B3"])


# ============================================================
# Plot
# ============================================================

fig, ax = plt.subplots(figsize=(18, 8))

ax.plot(
    x,
    B1,
    marker="o",
    linewidth=0.5,
    label="B1",
    color="blue"
)

ax.plot(
    x,
    B2,
    marker="s",
    linewidth=0.5,
    label="B2",
    color="red"
)

ax.plot(
    x,
    B3,
    marker="^",
    linewidth=0.5,
    label="B3",
    color="orange"
)


# ============================================================
# Architecture group separators
# ============================================================

for i in range(len(architecture_names) - 1):
    separator_x = (i + 1) * len(settings) - 0.5

    ax.axvline(
        separator_x,
        linestyle="--",
        linewidth=0.8,
        alpha=0.5
    )

# Transform:
# x -> data coordinates
# y -> axes coordinates
transform = blended_transform_factory(
    ax.transData,
    ax.transAxes
)

for i in range(1, len(architecture_names)):

    separator_x = i * len(settings) - 0.5

    ax.plot(
        [separator_x, separator_x],
        [-0.30, 1.0],
        transform=transform,
        linestyle="--",
        linewidth=0.8,
        alpha=0.5,
        clip_on=False
    )

# ============================================================
# Architecture names
#
# Put the architecture name underneath the four
# corresponding model-setting labels.
# ============================================================

for i, architecture in enumerate(architecture_names):

    center = i * len(settings) + 1.5

    ax.text(
        center,
        -0.23,
        architecture,
        transform=ax.get_xaxis_transform(),
        ha="center",
        va="top",
        fontsize=12,
        fontweight="bold"
    )




# ============================================================
# Axes
# ============================================================

ax.set_xticks(x)
ax.set_xticklabels(
    x_labels,
    rotation=90,
    ha="center",
    va="top",
    fontsize=11,

)



# ax.set_xlabel(
#     "Architecture and model setting",
#     fontsize=11
# )

ax.set_ylabel(
    r"$\sum \Delta QA^{pp}$",
    fontsize=14,
    fontweight="bold"
)

# ax.set_title(
#     r"Cumulative QA Degradation $\sum \Delta QA^{pp}$ Across Final Architectures and Model Settings",
#     fontsize=12
# )


# ============================================================
# Grid / legend
# ============================================================

# ax.grid(
#     axis="y",
#     linestyle="--",
#     alpha=0.6
# )

ax.legend(
    title="Benchmark",
    loc="best"
)


# ============================================================
# Layout
# ============================================================

plt.subplots_adjust(
    bottom=0.30,
    left=0.08,
    right=0.98,
    top=0.90
)

plt.savefig("baselines_delta_qa.png", dpi=400, bbox_inches="tight")
plt.show()


# ------------------------------------------------------------------------------------

# ============================================================
# Migration Coverage data
#
# Each architecture contains:
#   [B1 values, B2 values, B3 values]
#
# Order of values:
#   S-L, S-H, L-L, L-H
# ============================================================

architectures = {

    "Proposed": {
        "B1": [1, 0.88, 0.77, 0.77],
        "B2": [0.8, 0.8, 0.6, 0.6],
        "B3": [0.66, 0.5, 0.58, 0.58],
    },

    "Best Ordinary": {
        "B1": [1, 0.88, 0.66, 0.66],
        "B2": [0.7, 0.7, 0.5, 0.5],
        "B3": [0.58, 0.42, 0.5, 0.5],
    },

    "Only \n user-facing \n agents [6]": {
        "B1": [0.66, 0.66, 0.66, 0.66],
        "B2": [0.3, 0.3, 0.3, 0.3],
        "B3": [0.33, 0.33, 0.33, 0.33],
    },

    "[6] + \n Integration  with \n our  framework": {
        "B1": [0.66, 0.66, 0.55, 0.55],
        "B2": [0.3, 0.3, 0.2, 0.2],
        "B3": [0.33, 0.2, 0.25, 0.25],
    },

    "All specialized \n agents [5]": {
        "B1": [0.88, 0.88, 0.88, 0.88],
        "B2": [0.9, 0.9, 0.9, 0.9],
        "B3": [0.91, 0.91, 0.91, 0.91],
    },

    "[5] + \n Integration with \n our framework": {
        "B1": [0.88, 0.88, 0.77, 0.77],
        "B2": [0.8, 0.8, 0.6, 0.6],
        "B3": [0.66, 0.5, 0.58, 0.58],
    },

    "Orchestrator \n agent \n only [33]": {
        "B1": [0.11, 0.11, 0.11, 0.11],
        "B2": [0.1, 0.1, 0.1, 0.1],
        "B3": [0.08, 0.08, 0.08, 0.08],
    },

    "Strangler \n pattern \n (vanilla) [24]": {
        "B1": [1, 1, 1, 1],
        "B2": [1, 0.9, 1, 1],
        "B3": [0.91, 0.83, 1, 1],
    },

    " Strangler \n pattern \n + Risk-aware \n ordering": {
        "B1": [1, 0.88, 0.88, 0.88],
        "B2": [0.9, 0.9, 0.9, 0.9],
        "B3": [0.83, 0.75, 0.91, 0.91],
    },

    "  Strangler \n pattern \n + System-Wide \n Regression": {
        "B1": [1, 0.77, 0.55, 0.55],
        "B2": [0.7, 0.7, 0.5, 0.5],
        "B3": [0.83, 0.66, 0.58, 0.58],
    },
}


# ============================================================
# Construct X-axis
# ============================================================

architecture_names = list(architectures.keys())

x = np.arange(
    len(architecture_names) * len(settings)
)

# Create labels such as:
# Proposed
#    S-L
#    S-H
#    L-L
#    L-H
#
# The architecture name will be shown at the center of
# each four-point group.

x_labels = []

for architecture in architecture_names:
    for setting in settings:
        x_labels.append(setting)


# ============================================================
# Create the three benchmark series
# ============================================================

B1 = []
B2 = []
B3 = []

for architecture in architecture_names:
    B1.extend(architectures[architecture]["B1"])
    B2.extend(architectures[architecture]["B2"])
    B3.extend(architectures[architecture]["B3"])


# ============================================================
# Plot
# ============================================================

fig, ax = plt.subplots(figsize=(18, 8))

ax.plot(
    x,
    B1,
    marker="o",
    linewidth=0.5,
    label="B1",
    color="blue"
)

ax.plot(
    x,
    B2,
    marker="s",
    linewidth=0.5,
    label="B2",
    color="red"
)

ax.plot(
    x,
    B3,
    marker="^",
    linewidth=0.5,
    label="B3",
    color="orange"
)


# ============================================================
# Architecture group separators
# ============================================================

for i in range(len(architecture_names) - 1):
    separator_x = (i + 1) * len(settings) - 0.5

    ax.axvline(
        separator_x,
        linestyle="--",
        linewidth=0.8,
        alpha=0.5
    )

# Transform:
# x -> data coordinates
# y -> axes coordinates
transform = blended_transform_factory(
    ax.transData,
    ax.transAxes
)

for i in range(1, len(architecture_names)):

    separator_x = i * len(settings) - 0.5

    ax.plot(
        [separator_x, separator_x],
        [-0.30, 1.0],
        transform=transform,
        linestyle="--",
        linewidth=0.8,
        alpha=0.5,
        clip_on=False
    )

# ============================================================
# Architecture names
#
# Put the architecture name underneath the four
# corresponding model-setting labels.
# ============================================================

for i, architecture in enumerate(architecture_names):

    center = i * len(settings) + 1.5

    ax.text(
        center,
        -0.23,
        architecture,
        transform=ax.get_xaxis_transform(),
        ha="center",
        va="top",
        fontsize=12,
        fontweight="bold"
    )




# ============================================================
# Axes
# ============================================================

ax.set_xticks(x)
ax.set_xticklabels(
    x_labels,
    rotation=90,
    ha="center",
    va="top",
    fontsize=11
)



# ax.set_xlabel(
#     "Architecture and model setting",
#     fontsize=11
# )

ax.set_ylabel(
    r"Agentification Coverage",
    fontsize=14,
    fontweight="bold"
)

# ax.set_title(
#     r"Agentification Coverage (Ratio of Total Successfully Migrated Agents) Across Final Architectures and Model Settings",
#     fontsize=12
# )


# ============================================================
# Grid / legend
# ============================================================

# ax.grid(
#     axis="y",
#     linestyle="--",
#     alpha=0.6
# )

ax.legend(
    title="Benchmark",
    loc="best"
)


# ============================================================
# Layout
# ============================================================

plt.subplots_adjust(
    bottom=0.30,
    left=0.08,
    right=0.98,
    top=0.90
)

plt.savefig("baselines_mig_cov.png", dpi=400, bbox_inches="tight")
plt.show()
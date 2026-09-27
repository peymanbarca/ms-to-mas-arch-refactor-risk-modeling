import numpy as np
import matplotlib.pyplot as plt
from matplotlib.transforms import blended_transform_factory

# ============================================================
# Model settings & Architecture names
# ============================================================

settings = [
    r"$M_{Small}$ - $T_{Low}$",
    r"$M_{Small}$ - $T_{High}$",
    r"$M_{Large}$ - $T_{Low}$",
    r"$M_{Large}$ - $T_{High}$"
]

architecture_names = [
    "Proposed",
    "Best Ordinary",
    "Only \n user-facing \n agents [6]",
    "[6] + \n Integration with \n our framework",
    "All specialized \n agents [5]",
    "[5] + \n Integration with \n our framework",
    "Orchestrator \n agent \n only [33]",
    "Strangler \n pattern \n (vanilla) [24]",
    " Strangler \n pattern \n + Risk-aware \n ordering",
    "  Strangler \n pattern \n + System-Wide \n Regression",
]

x = np.arange(len(architecture_names) * len(settings))

x_labels = []
for architecture in architecture_names:
    for setting in settings:
        x_labels.append(setting)


# ============================================================
# Data Definitions
# ============================================================

# 1. U_max data
arch_umax = {
    "Proposed": {"B1": [200, 215, 155, 155], "B2": [165, 165, 120, 120], "B3": [125, 135, 105, 105]},
    "Best Ordinary": {"B1": [200, 215, 140, 140], "B2": [155, 150, 110, 110], "B3": [115, 120, 95, 95]},
    "Only \n user-facing \n agents [6]": {"B1": [235, 235, 155, 155], "B2": [185, 185, 150, 150], "B3": [145, 145, 120, 120]},
    "[6] + \n Integration with \n our framework": {"B1": [235, 235, 165, 165], "B2": [185, 185, 160, 160], "B3": [145, 155, 130, 130]},
    "All specialized \n agents [5]": {"B1": [215, 215, 125, 125], "B2": [140, 140, 80, 80], "B3": [75, 75, 50, 50]},
    "[5] + \n Integration with \n our framework": {"B1": [215, 215, 155, 155], "B2": [165, 165, 120, 120], "B3": [125, 135, 105, 105]},
    "Orchestrator \n agent \n only [33]": {"B1": [220, 215, 130, 130], "B2": [145, 145, 95, 95], "B3": [100, 100, 85, 85]},
    "Strangler \n pattern \n (vanilla) [24]": {"B1": [200, 200, 110, 110], "B2": [120, 140, 70, 70], "B3": [75, 80, 35, 35]},
    " Strangler \n pattern \n + Risk-aware \n ordering": {"B1": [200, 215, 125, 125], "B2": [140, 140, 90, 90], "B3": [105, 120, 50, 50]},
    "  Strangler \n pattern \n + System-Wide \n Regression": {"B1": [200, 210, 135, 135], "B2": [145, 145, 95, 95], "B3": [110, 125, 95, 95]},
}

# 2. DeltaQA data
arch_delta = {
    "Proposed": {"B1": [0, 3, 0, 0], "B2": [4, 6, 1, 1], "B3": [11, 17, 0, 0]},
    "Best Ordinary": {"B1": [0, 3, 0, 0], "B2": [4, 7, 1, 1], "B3": [11, 19, 0, 0]},
    "Only \n user-facing \n agents [6]": {"B1": [0, 0, 0, 0], "B2": [0, 0, 0, 0], "B3": [0, 3, 0, 0]},
    "[6] + \n Integration with \n our framework": {"B1": [0, 0, 0, 0], "B2": [0, 0, 0, 0], "B3": [0, 3, 0, 0]},
    "All specialized \n agents [5]": {"B1": [0, 0, 0, 0], "B2": [2, 2, 1, 1], "B3": [3, 5, 0, 0]},
    "[5] + \n Integration with \n our framework": {"B1": [0, 0, 0, 0], "B2": [1, 1, 1, 1], "B3": [2, 4, 0, 0]},
    "Orchestrator \n agent \n only [33]": {"B1": [0, 2, 0, 0], "B2": [3, 4, 0, 0], "B3": [7, 12, 0, 0]},
    "Strangler \n pattern \n (vanilla) [24]": {"B1": [0, 4, 0, 0], "B2": [5, 9, 1, 1], "B3": [13, 20, 0, 0]},
    " Strangler \n pattern \n + Risk-aware \n ordering": {"B1": [0, 3, 0, 0], "B2": [4, 6, 1, 1], "B3": [11, 17, 0, 0]},
    "  Strangler \n pattern \n + System-Wide \n Regression": {"B1": [0, 3, 0, 0], "B2": [5, 8, 1, 1], "B3": [12, 20, 0, 0]},
}

# 3. Migration Coverage data
arch_cov = {
    "Proposed": {"B1": [1, 0.88, 0.77, 0.77], "B2": [0.8, 0.8, 0.6, 0.6], "B3": [0.66, 0.5, 0.58, 0.58]},
    "Best Ordinary": {"B1": [1, 0.88, 0.66, 0.66], "B2": [0.7, 0.7, 0.5, 0.5], "B3": [0.58, 0.42, 0.5, 0.5]},
    "Only \n user-facing \n agents [6]": {"B1": [0.66, 0.66, 0.66, 0.66], "B2": [0.3, 0.3, 0.3, 0.3], "B3": [0.33, 0.33, 0.33, 0.33]},
    "[6] + \n Integration with \n our framework": {"B1": [0.66, 0.66, 0.55, 0.55], "B2": [0.3, 0.3, 0.2, 0.2], "B3": [0.33, 0.2, 0.25, 0.25]},
    "All specialized \n agents [5]": {"B1": [0.88, 0.88, 0.88, 0.88], "B2": [0.9, 0.9, 0.9, 0.9], "B3": [0.91, 0.91, 0.91, 0.91]},
    "[5] + \n Integration with \n our framework": {"B1": [0.88, 0.88, 0.77, 0.77], "B2": [0.8, 0.8, 0.6, 0.6], "B3": [0.66, 0.5, 0.58, 0.58]},
    "Orchestrator \n agent \n only [33]": {"B1": [0.11, 0.11, 0.11, 0.11], "B2": [0.1, 0.1, 0.1, 0.1], "B3": [0.08, 0.08, 0.08, 0.08]},
    "Strangler \n pattern \n (vanilla) [24]": {"B1": [1, 1, 1, 1], "B2": [1, 0.9, 1, 1], "B3": [0.91, 0.83, 1, 1]},
    " Strangler \n pattern \n + Risk-aware \n ordering": {"B1": [1, 0.88, 0.88, 0.88], "B2": [0.9, 0.9, 0.9, 0.9], "B3": [0.83, 0.75, 0.91, 0.91]},
    "  Strangler \n pattern \n + System-Wide \n Regression": {"B1": [1, 0.77, 0.55, 0.55], "B2": [0.7, 0.7, 0.5, 0.5], "B3": [0.83, 0.66, 0.58, 0.58]},
}

# Helper to flatten dictionary data into lists
def extract_series(arch_dict):
    b1, b2, b3 = [], [], []
    for arch in architecture_names:
        b1.extend(arch_dict[arch]["B1"])
        b2.extend(arch_dict[arch]["B2"])
        b3.extend(arch_dict[arch]["B3"])
    return b1, b2, b3

U1, U2, U3 = extract_series(arch_umax)
D1, D2, D3 = extract_series(arch_delta)
C1, C2, C3 = extract_series(arch_cov)


# ============================================================
# Create Subplots Figure
# ============================================================

fig, (ax1, ax2, ax3) = plt.subplots(3, 1, figsize=(18, 16), sharex=True)
axes = [ax1, ax2, ax3]

# Plot data on each axis
for ax, (b1, b2, b3) in zip(axes, [(U1, U2, U3), (D1, D2, D3), (C1, C2, C3)]):
    ax.plot(x, b1, marker=".", linewidth=0.5, label="B1", color="blue")
    ax.plot(x, b2, marker=".", linewidth=0.5, label="B2", color="red")
    ax.plot(x, b3, marker=".", linewidth=0.5, label="B3", color="orange")
    ax.legend(title="Benchmark", loc="best", fontsize=9)


# ============================================================
# Microservice baselines (Subplot 1 only)
# ============================================================

text_transform = blended_transform_factory(ax1.transAxes, ax1.transData)
microservice = {"B1": 285, "B2": 205, "B3": 165}
# Draw the horizontal baseline lines
ax1.axhline(microservice["B1"], linestyle=":", linewidth=1.2, alpha=0.7, color="blue")
ax1.axhline(microservice["B2"], linestyle=":", linewidth=1.2, alpha=0.7, color="red")
ax1.axhline(microservice["B3"], linestyle=":", linewidth=1.2, alpha=0.7, color="orange")

# Add text labels right on the left side of the lines (in front of them)
ax1.text(0.01, microservice["B1"], "Microservice (B1)", color="blue", va="bottom", ha="left", transform=text_transform, fontsize=6)
ax1.text(0.01, microservice["B2"], "Microservice (B2)", color="red", va="bottom", ha="left", transform=text_transform, fontsize=6)
ax1.text(0.01, microservice["B3"], "Microservice (B3)", color="orange", va="bottom", ha="left", transform=text_transform, fontsize=6)
ax1.set_ylim(0, 310)
ax1.legend(title="Benchmark", loc="best", fontsize=9)


# ============================================================
# Group Separators and Architecture Names Across All Subplots
# ============================================================

for ax in axes:
    for i in range(1, len(architecture_names)):
        separator_x = i * len(settings) - 0.5
        ax.axvline(separator_x, linestyle="--", linewidth=0.8, alpha=0.5)

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
        [-0.50, 1.0],
        transform=transform,
        linestyle="--",
        linewidth=0.8,
        alpha=0.5,
        clip_on=False
    )

# Architecture labels placed underneath the bottom subplot (ax3)
for i, architecture in enumerate(architecture_names):
    center = i * len(settings) + 1.5
    ax3.text(
        center,
        -0.4,
        architecture,
        transform=ax3.get_xaxis_transform(),
        ha="center",
        va="top",
        fontsize=10,
        fontweight="bold"
    )


# ============================================================
# Labels and Styling
# ============================================================

ax1.set_ylabel(r"$U_{\max}(\mathcal{A})$", fontsize=13, fontweight="bold")
ax2.set_ylabel(r"$\sum \Delta QA^{pp}$", fontsize=13, fontweight="bold")
ax3.set_ylabel(r"Agentification Ratio", fontsize=13, fontweight="bold")

# X-ticks only on the bottom plot due to sharex=True
ax3.set_xticks(x)
ax3.set_xticklabels(x_labels, rotation=90, ha="center", va="top", fontsize=10)

# Layout adjustments
plt.subplots_adjust(
    bottom=0.22,
    left=0.08,
    right=0.98,
    top=0.95,
    hspace=0.15
)

plt.savefig("baselines_trend.png", dpi=400, bbox_inches="tight")
plt.show()
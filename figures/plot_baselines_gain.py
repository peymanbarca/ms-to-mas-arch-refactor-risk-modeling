import numpy as np
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.patches import FancyBboxPatch

# ============================================================
# Data
# ============================================================
methods = [
    "Orchestrator",
    "Non- \nOrchestrator",
    "User- \nInteracting",
    "Strangler \n Pattern"
]

# ------------------------------------------------------------
# Baseline U_max
# ------------------------------------------------------------
baseline_umax = np.array([
    122.50,       # Orchestrator
    114.17,       # Non-Orchestrator
    165.00,       # User-Interaction
    126.25        # Strangler: 90% + 50%
])

# Gains stated in the paper text
umax_gain = np.array([
    53.0,
    31.0,
    6.0,
    26.0
])

ours_umax = baseline_umax * (1 + umax_gain / 100.0)

# ------------------------------------------------------------
# Baseline Delta Sigma QA
# ------------------------------------------------------------
baseline_qa = np.array([
    41,       # Orchestrator
    14,       # Non-Orchestrator
    3,        # User-Interaction
    38.5      # Strangler: average of 90% and 50%
])

qa_reduction = np.array([
    7.0,
    2.0,
    0.0,
    5.0
])

ours_qa = baseline_qa - qa_reduction

# ============================================================
# Visual settings
# ============================================================

baseline_color = "#E04935"
ours_color = "#330CDD"
line_color = "#6256D9"
grid_color = "#D9D9D9"

plt.rcParams.update({
    "font.family": "serif",
    "font.size": 12,
    "axes.titlesize": 16,
    "axes.labelsize": 14,
    "xtick.labelsize": 11,
    "ytick.labelsize": 12,
    "legend.fontsize": 11,
})

# REDUCED HEIGHT HERE (from 8 to 4.8)
fig, axes = plt.subplots(
    1, 2,
    figsize=(12, 6),
    gridspec_kw={"wspace": 0.32}
)

# ============================================================
# Helper function
# ============================================================

def add_gain_box(ax, x1, x2, y, text, box_y_offset=0.0):
    xmin = min(x1, x2)
    xmax = max(x1, x2)
    mid = (xmin + xmax) / 2

    ax.text(
        mid,
        y + box_y_offset,
        text,
        ha="center",
        va="center",
        color=ours_color,
        fontsize=11,
        bbox=dict(
            boxstyle="round,pad=0.28",
            facecolor="white",
            edgecolor=ours_color,
            linewidth=1.0
        ),
        zorder=5
    )


def plot_panel(
    ax,
    baseline,
    ours,
    labels,
    annotations,
    xlabel,
    title,
    xlim,
    xticks,
    lower_is_better=False
):
    y = np.arange(len(labels))[::-1]

    # Grid
    ax.set_axisbelow(True)
    ax.grid(
        axis="y",
        linestyle="-",
        linewidth=0.5,
        color=grid_color,
        alpha=0.55
    )

    # Connecting lines
    for i, yi in enumerate(y):
        ax.plot(
            [baseline[i], ours[i]],
            [yi, yi],
            color=line_color,
            linewidth=2.2,
            solid_capstyle="round",
            zorder=2
        )

    # Baseline points
    ax.scatter(
        baseline,
        y,
        s=145,
        facecolor="white",
        edgecolor=baseline_color,
        linewidth=2.5,
        zorder=4
    )
    ax.scatter(
        baseline,
        y,
        s=38,
        color=baseline_color,
        alpha=0.35,
        zorder=5
    )

    # Ours points
    ax.scatter(
        ours,
        y,
        s=145,
        facecolor="white",
        edgecolor=ours_color,
        linewidth=2.5,
        zorder=4
    )
    ax.scatter(
        ours,
        y,
        s=38,
        color=ours_color,
        alpha=0.65,
        zorder=5
    )

# --------------------------------------------------------
    # Numerical labels & annotations
    # --------------------------------------------------------
    for i, yi in enumerate(y):
        # Check if it's the last row (Strangler Pattern)
        is_last = (i == len(y) - 1)
        
        # Set custom offset and alignment for the last row if desired
        text_offset = (yi - 0.22) if is_last else (yi - 0.1)
        rotation = 0 if is_last else 90
        va_align = "bottom" if is_last else "top"

        # baseline value
        ax.text(
            baseline[i],
            text_offset,
            f"{baseline[i]:.1f}",
            ha="center",
            va=va_align,
            fontsize=13.5,
            rotation=rotation,
            color=baseline_color
        )

        # ours value
        ax.text(
            ours[i],
            text_offset,
            f"{ours[i]:.1f}",
            ha="center",
            va=va_align,
            fontsize=13.5,
            rotation=rotation,
            color=ours_color
        )

        add_gain_box(
            ax,
            baseline[i],
            ours[i],
            yi,
            annotations[i],
            box_y_offset=0.2
        )

    ax.set_yticks(y)
    ax.set_yticklabels(labels, fontweight="bold")  # <--- Added fontweight="bold"
    ax.set_xlabel(xlabel)
    ax.set_title(title, fontweight="bold", pad=12)

    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)

    ax.tick_params(axis="y", length=0)
    axes[1].tick_params(axis="y", labelleft=False)
    axes[0].tick_params(axis="y", labelsize=16, rotation=15)

    # REDUCED VERTICAL MARGINS HERE (from 0.15 to 0.08)
    ax.margins(y=0.08)


# ============================================================
# Panel (a): Scalability
# ============================================================
umax_annotations = ["+53%", "+31%", "+6%", "+26%"]

plot_panel(
    axes[0],
    baseline_umax,
    ours_umax,
    methods,
    umax_annotations,
    xlabel=r"$U_{\max}$",
    title=r"Scalability Envelope Gain ($U_{\max} \uparrow$)",
    xlim=(0, 210),
    xticks=np.arange(0, 211, 25)
)

# ============================================================
# Panel (b): QA deviation
# ============================================================
qa_annotations = [r"$-7pp$", r"$-2pp$", r"$0pp$", r"$-5pp$"]

plot_panel(
    axes[1],
    baseline_qa,
    ours_qa,
    methods,
    qa_annotations,
    xlabel=r"$\Sigma\Delta_{QA}$",
    title=r"Cumulative QA Deviation Reduction ($\Sigma\Delta_{QA} \downarrow$)",
    xlim=(0, 4.2),
    xticks=np.arange(0, 4.1, 0.5),
    lower_is_better=True
)

# ============================================================
# Legend
# ============================================================
legend_handles = [
    Line2D(
        [0], [0],
        marker="o",
        linestyle="None",
        markersize=9,
        markerfacecolor="white",
        markeredgecolor=baseline_color,
        markeredgewidth=2,
        label="Baseline",
    ),
    Line2D(
        [0], [0],
        marker="o",
        linestyle="None",
        markersize=9,
        markerfacecolor="white",
        markeredgecolor=ours_color,
        markeredgewidth=2,
        label="Baseline + Integration with our stabilization framework",
    )
]

fig.legend(
    handles=legend_handles,
    loc="lower center",
    ncol=2,
    frameon=False,
    fancybox=False,
    bbox_to_anchor=(0.5, -0.05),
    handletextpad=0.5,
    columnspacing=1.5,
    prop={"size": 18, "family": "serif"}
)

# ============================================================
# Layout / save
# ============================================================
plt.tight_layout(rect=[0, 0.06, 1, 1])

plt.savefig(
    "stabilization_framework_gain.png",
    dpi=600,
    bbox_inches="tight"
)
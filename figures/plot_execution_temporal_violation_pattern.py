import numpy as np
import matplotlib.pyplot as plt


architectures = [
    "Proposed",
    "Best Ordinary",
    "Only user-facing agents",
    "All specialized agents",
    "Specialized + framework",
    "Orchestrator only",
    "Strangler pattern (vanilla)",
    "Strangler + Risk-aware ordering",
    "Strangler + System-Wide Regression"
]

settings = [
    r"$M_S-T_L$",
    r"$M_S-T_H$",
    r"$M_L-T_L$",
    r"$M_L-T_H$"
]

benchmarks = ["B1", "B2", "B3"]


fig, axes = plt.subplots(
    3,
    3,
    figsize=(15, 12),
    sharey=True
)

axes = axes.flatten()


for ax, architecture in zip(axes, architectures):

    for setting_idx, setting in enumerate(settings):

        for benchmark_idx, benchmark in enumerate(benchmarks):

            values = results[
                (results["Architecture"] == architecture) &
                (results["Setting"] == setting) &
                (results["Benchmark"] == benchmark)
            ]["Umax"].dropna().values

            # Three side-by-side violins
            x = (
                setting_idx
                + (benchmark_idx - 1) * 0.22
            )

            if len(values) > 0:

                vp = ax.violinplot(
                    values,
                    positions=[x],
                    widths=0.18,
                    showmeans=False,
                    showmedians=True,
                    showextrema=True
                )

                # Median line
                for body in vp["bodies"]:
                    body.set_alpha(0.6)

    ax.set_title(
        architecture,
        fontweight="bold",
        fontsize=10
    )

    ax.set_xticks(range(len(settings)))
    ax.set_xticklabels(
        settings,
        rotation=45,
        ha="right"
    )

    ax.grid(
        axis="y",
        linestyle="--",
        alpha=0.3
    )


fig.suptitle(
    r"Distribution of $U_{\max}(\mathcal{A})$ Across Repeated Executions",
    fontsize=15,
    fontweight="bold"
)

fig.text(
    0.5,
    0.02,
    "Model–temperature setting",
    ha="center",
    fontweight="bold"
)

fig.text(
    0.02,
    0.5,
    r"$U_{\max}(\mathcal{A})$",
    va="center",
    rotation="vertical",
    fontweight="bold"
)

plt.tight_layout(
    rect=[0.04, 0.04, 1, 0.95]
)

plt.show()
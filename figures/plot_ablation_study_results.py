import numpy as np
import matplotlib.pyplot as plt

# -----------------------------------------------------
# Data
# -----------------------------------------------------

factor_titles = [
    "Overall method",
    "Risk-aware \n migration order",
    "Temporal \n propagation",
    "Two-tier \n governance",
]

factor_desc = [
    "(8 vs. all 568 alternative \n ordinary baselines)",
    "(96 vs. 480 static/random  \n ordered baselines)",
    "(vs. fully static \n ordered baseline)",
    "(192 vs. 384 partial /  \n non-governed baselines)",
]

benchmarks = ["B1", "B2", "B3"]

def qa_plot():
    # Mean improvements
    means = {
        "B1": [7.94, 6.51, 0.00, 1.23],
        "B2": [20.47, 15.73, 0.00, 6.67],
        "B3": [11.53, 10.37, 0.00, 0.92],
    }

    # Half-width of 95% bootstrap CI
    # (replace None with your actual values)
    errs = {
        "B1": [1.42, 1.08, 0.00, 0.37],
        "B2": [4.23, 4.41, 0.00, 0.94],
        "B3": [7.26, 6.12, 0.00, 0.65],
    }

    colors = {
        "B1": "#1f77b4",
        "B2": "red",
        "B3": "#2ca02c",
    }

    markers = {
        "B1": "o",
        "B2": "^",
        "B3": "s",
    }

    # -----------------------------------------------------
    # Figure
    # -----------------------------------------------------

    fig, ax = plt.subplots(figsize=(6, 3))

    # y positions
    group_y = np.arange(len(factor_titles))[::-1]

    for y, title, desc in zip(group_y, factor_titles, factor_desc):

        ax.text(
            -2.1,
            y + 0.10,
            title,
            fontsize=11,
            fontweight="bold",
            ha="right",
            va="center",
        )

        ax.text(
            -2.1,
            y - 0.10,
            desc,
            fontsize=8,
            color="dimgray",
            ha="right",
            va="center",
        )

    offset = {
        "B1": 0.18,
        "B2": 0.00,
        "B3": -0.18,
    }

    for bench in benchmarks:

        y = group_y + offset[bench]

        ax.errorbar(
            means[bench],
            y,
            xerr=errs[bench],
            fmt=markers[bench],
            color=colors[bench],
            markersize=7,
            linewidth=2,
            capsize=5,
            label=bench,
        )

        # annotate value
        for x, yy, e in zip(means[bench], y, errs[bench]):

            if e > 0:
                txt = f"{x:.2f}±{e:.2f}"
            else:
                txt = f"{x:.2f}"

            ax.text(
                x if e > 0 else x + 0.8,
                yy + 0.04 if e > 0 else yy,
                txt,
                ha="center",
                va="bottom",
                fontsize=11,
                color=colors[bench],
            )

    # -----------------------------------------------------
    # Decorations
    # -----------------------------------------------------

    # Zero reference
    ax.axvline(0, color="gray", linestyle="--", linewidth=0.3)

    # Row separators
    for y in [0.5, 1.5, 2.5]:
        ax.axhline(y, color="0.85", linestyle=":")

    ax.set_yticks(group_y)


    # ax.set_yticklabels(factors, fontsize=11)
    ax.set_yticks(group_y)
    ax.set_yticklabels([""] * len(group_y))

    ax.set_xlim(-2, 25)

    ax.set_xlabel(
        r"Cumulative QA Deviation Improvement (pp) $\downarrow$",
        fontsize=12,
    )

    ax.set_title(
        r"$\Sigma\Delta QA^{pp}\downarrow$"
        "\nMean improvement with 95% bootstrap confidence intervals",
        fontsize=16,
    )

    ax.legend(
        title="Benchmark",
        loc="lower right",
        frameon=True,
    )

    plt.tight_layout()

    plt.savefig("./qa_forest.png", dpi=600, bbox_inches="tight")

    plt.show()

def failure_plot(): 
    # Mean improvements (Failure Rate)
    means = {
        "B1": [26.36, 22.62, 1.00, 3.12],
        "B2": [36.95, 31.53, 3.00, 5.59],
        "B3": [34.25, 28.40, 3.00, 4.80],
    }

    # Half-width of 95% bootstrap CI
    errs = {
        "B1": [2.07, 1.98, 0.00, 0.65],
        "B2": [4.95, 3.12, 0.00, 0.89],
        "B3": [8.15, 6.77, 0.00, 2.18],   # replace when available
    }

    colors = {
        "B1": "#1f77b4",
        "B2": "red",
        "B3": "#2ca02c",
    }

    markers = {
        "B1": "o",
        "B2": "^",
        "B3": "s",
    }

    # -----------------------------------------------------
    # Figure
    # -----------------------------------------------------

    fig, ax = plt.subplots(figsize=(6, 3))

    group_y = np.arange(len(factor_titles))[::-1]

    for y, title, desc in zip(group_y, factor_titles, factor_desc):

        ax.text(
            -2.1,
            y + 0.10,
            title,
            fontsize=11,
            fontweight="bold",
            ha="right",
            va="center",
        )

        ax.text(
            -2.1,
            y - 0.10,
            desc,
            fontsize=8,
            color="dimgray",
            ha="right",
            va="center",
        )
        
    offset = {
        "B1": 0.18,
        "B2": 0.00,
        "B3": -0.18,
    }

    for bench in benchmarks:

        y = group_y + offset[bench]

        ax.errorbar(
            means[bench],
            y,
            xerr=errs[bench],
            fmt=markers[bench],
            color=colors[bench],
            markersize=7,
            linewidth=2,
            capsize=5,
            label=bench,
        )

        for x, yy, e in zip(means[bench], y, errs[bench]):
            if e > 0:
                txt = f"{x:.2f}±{e:.2f}"
            else:
                txt = f"{x:.2f}"

            ax.text(
                x if e > 0 else x + 0.8,
                yy + 0.04 if e > 0 else yy,
                txt,
                ha="center",
                va="bottom",
                fontsize=11,
                color=colors[bench],
            )

    # -----------------------------------------------------
    # Decorations
    # -----------------------------------------------------

    ax.axvline(0, color="gray", linestyle="--", linewidth=0.3)

    for y in [0.5, 1.5, 2.5]:
        ax.axhline(y, color="0.85", linestyle=":")

    # ax.set_yticklabels(factors, fontsize=11)
    ax.set_yticks(group_y)
    ax.set_yticklabels([""] * len(group_y))

    ax.set_xlim(0, 45)

    ax.set_xlabel(
        r"Cumulative Failure Rate Improvement (pp) $\downarrow$",
        fontsize=12,
    )

    ax.set_title(
        r"$\Sigma\Delta F^{pp}\downarrow$"
        "\nMean improvement with 95% bootstrap confidence intervals",
        fontsize=16,
    )

    ax.legend(
        title="Benchmark",
        loc="lower right",
        frameon=True,
    )

    plt.tight_layout()

    plt.savefig("./failure_forest.png", dpi=600, bbox_inches="tight")

    plt.show()



def latency_plot(): 
    # Mean improvements (Failure Rate)
    means = {
        "B1": [29.84, 22.71, 4, 5.44],
        "B2": [39.96, 33.10, 11, 6.84],
        "B3": [44.71, 37.76, 8, 5.98],
    }

    # Half-width of 95% bootstrap CI
    errs = {
        "B1": [2.27, 4.74, 0.00, 0.36],
        "B2": [5.19, 6.35, 0.00, 0.31],
        "B3": [7.21, 6.89, 0.00, 0.92],   # replace when available
    }

    colors = {
        "B1": "#1f77b4",
        "B2": "red",
        "B3": "#2ca02c",
    }

    markers = {
        "B1": "o",
        "B2": "^",
        "B3": "s",
    }

    # -----------------------------------------------------
    # Figure
    # -----------------------------------------------------

    fig, ax = plt.subplots(figsize=(6, 3))

    group_y = np.arange(len(factor_titles))[::-1]

    for y, title, desc in zip(group_y, factor_titles, factor_desc):

        ax.text(
            -1.1,
            y + 0.10,
            title,
            fontsize=11,
            fontweight="bold",
            ha="right",
            va="center",
        )

        ax.text(
            -1.1,
            y - 0.10,
            desc,
            fontsize=8,
            color="dimgray",
            ha="right",
            va="center",
        )
        
    offset = {
        "B1": 0.18,
        "B2": 0.00,
        "B3": -0.18,
    }

    for bench in benchmarks:

        y = group_y + offset[bench]

        ax.errorbar(
            means[bench],
            y,
            xerr=errs[bench],
            fmt=markers[bench],
            color=colors[bench],
            markersize=7,
            linewidth=2,
            capsize=5,
            label=bench,
        )

        for x, yy, e in zip(means[bench], y, errs[bench]):
            if e > 0:
                txt = f"{x:.2f}±{e:.2f}"
            else:
                txt = f"{x:.2f}"

            ax.text(
                x if e > 0 else x + 0.8,
                yy + 0.04 if e > 0 else yy,
                txt,
                ha="center",
                va="bottom",
                fontsize=11,
                color=colors[bench],
            )

    # -----------------------------------------------------
    # Decorations
    # -----------------------------------------------------

    ax.axvline(0, color="gray", linestyle="--", linewidth=0.3)

    for y in [0.5, 1.5, 2.5]:
        ax.axhline(y, color="0.85", linestyle=":")

    # ax.set_yticklabels(factors, fontsize=11)
    ax.set_yticks(group_y)
    ax.set_yticklabels([""] * len(group_y))

    ax.set_xlim(0, 55)

    ax.set_xlabel(
        r"Cumulative Tail Latency Disturbance Improvement (%) $\downarrow$",
        fontsize=12,
    )

    ax.set_title(
        r"$\Sigma\Delta L^{\%}_{p95}\downarrow$"
        "\nMean improvement with 95% bootstrap confidence intervals",
        fontsize=16,
    )

    ax.legend(
        title="Benchmark",
        loc="lower right",
        frameon=True,
    )

    plt.tight_layout()

    plt.savefig("./latency_forest.png", dpi=600, bbox_inches="tight")

    plt.show()


def cost_plot(): 
    # Mean improvements (Failure Rate)
    means = {
        "B1": [8.64, 6.86, 0,   1.98],
        "B2": [13.16, 11.65, 3, 2.12],
        "B3": [11.18, 9.37, 1,  2.05],
    }

    # Half-width of 95% bootstrap CI
    errs = {
        "B1": [3.31, 4.15, 0.00, 0.20],
        "B2": [4.88, 5.03, 0.00, 0.75],
        "B3": [5.31, 6.16, 0.00, 0.56],   # replace when available
    }

    colors = {
        "B1": "#1f77b4",
        "B2": "red",
        "B3": "#2ca02c",
    }

    markers = {
        "B1": "o",
        "B2": "^",
        "B3": "s",
    }

    # -----------------------------------------------------
    # Figure
    # -----------------------------------------------------

    fig, ax = plt.subplots(figsize=(6, 3))

    group_y = np.arange(len(factor_titles))[::-1]

    for y, title, desc in zip(group_y, factor_titles, factor_desc):

        ax.text(
            -2.1,
            y + 0.10,
            title,
            fontsize=11,
            fontweight="bold",
            ha="right",
            va="center",
        )

        ax.text(
            -2.1,
            y - 0.10,
            desc,
            fontsize=8,
            color="dimgray",
            ha="right",
            va="center",
        )
        
    offset = {
        "B1": 0.18,
        "B2": 0.00,
        "B3": -0.18,
    }

    for bench in benchmarks:

        y = group_y + offset[bench]

        ax.errorbar(
            means[bench],
            y,
            xerr=errs[bench],
            fmt=markers[bench],
            color=colors[bench],
            markersize=7,
            linewidth=2,
            capsize=5,
            label=bench,
        )

        for x, yy, e in zip(means[bench], y, errs[bench]):
            if e > 0:
                txt = f"{x:.2f}±{e:.2f}"
            else:
                txt = f"{x:.2f}"

            ax.text(
                x if e > 0 else x + 0.8,
                yy + 0.04 if e > 0 else yy,
                txt,
                ha="center",
                va="bottom",
                fontsize=11,
                color=colors[bench],
            )

    # -----------------------------------------------------
    # Decorations
    # -----------------------------------------------------

    ax.axvline(0, color="gray", linestyle="--", linewidth=0.3)

    for y in [0.5, 1.5, 2.5]:
        ax.axhline(y, color="0.85", linestyle=":")

    # ax.set_yticklabels(factors, fontsize=11)
    ax.set_yticks(group_y)
    ax.set_yticklabels([""] * len(group_y))

    ax.set_xlim(-2, 20)

    ax.set_xlabel(
        r"Cumulative Operational Cost Improvement (%) $\downarrow$",
        fontsize=12,
    )

    ax.set_title(
        r"$\Sigma\Delta C^{\%}\downarrow$"
        "\nMean improvement with 95% bootstrap confidence intervals",
        fontsize=16,
    )

    ax.legend(
        title="Benchmark",
        loc="lower right",
        frameon=True,
    )

    plt.tight_layout()

    plt.savefig("./cost_forest.png", dpi=600, bbox_inches="tight")

    plt.show()
        

def gov_plot(): 
    # Mean improvements (Failure Rate)
    means = {
        "B1": [17.7, 14.6, 0, 2.1],
        "B2": [26.3, 20.8, 0, 4.5],
        "B3": [31.7, 27.9, 2, 3.6],
    }

    # Half-width of 95% bootstrap CI
    errs = {
        "B1": [5.5,  6.8,  0.00, 0.3],
        "B2": [7.1,  8.2,  0.00, 0.8],
        "B3": [13.1, 12.7, 0.00, 0.4],   # replace when available
    }

    colors = {
        "B1": "#1f77b4",
        "B2": "red",
        "B3": "#2ca02c",
    }

    markers = {
        "B1": "o",
        "B2": "^",
        "B3": "s",
    }

    # -----------------------------------------------------
    # Figure
    # -----------------------------------------------------

    fig, ax = plt.subplots(figsize=(6, 3))

    group_y = np.arange(len(factor_titles))[::-1]

    for y, title, desc in zip(group_y, factor_titles, factor_desc):

        ax.text(
            -2.1,
            y + 0.10,
            title,
            fontsize=11,
            fontweight="bold",
            ha="right",
            va="center",
        )

        ax.text(
            -2.1,
            y - 0.10,
            desc,
            fontsize=8,
            color="dimgray",
            ha="right",
            va="center",
        )
        
    offset = {
        "B1": 0.18,
        "B2": 0.00,
        "B3": -0.18,
    }

    for bench in benchmarks:

        y = group_y + offset[bench]

        ax.errorbar(
            means[bench],
            y,
            xerr=errs[bench],
            fmt=markers[bench],
            color=colors[bench],
            markersize=7,
            linewidth=2,
            capsize=5,
            label=bench,
        )

        for x, yy, e in zip(means[bench], y, errs[bench]):
            if e > 0:
                txt = f"{x:.2f}±{e:.2f}"
            else:
                txt = f"{x:.2f}"

            ax.text(
                x if e > 0 else x + 0.8,
                yy + 0.04 if e > 0 else yy,
                txt,
                ha="center",
                va="bottom",
                fontsize=11,
                color=colors[bench],
            )

    # -----------------------------------------------------
    # Decorations
    # -----------------------------------------------------

    ax.axvline(0, color="gray", linestyle="--", linewidth=0.3)

    for y in [0.5, 1.5, 2.5]:
        ax.axhline(y, color="0.85", linestyle=":")

    # ax.set_yticklabels(factors, fontsize=11)
    ax.set_yticks(group_y)
    ax.set_yticklabels([""] * len(group_y))

    ax.set_xlim(-2, 45)

    ax.set_xlabel(
        r"Governance Intervention Rate Improvement (%) $\downarrow$",
        fontsize=12,
    )

    ax.set_title(
        r"$\Sigma Gov^{\%}_{Int}\downarrow$"
        "\nMean improvement with 95% bootstrap confidence intervals",
        fontsize=16,
    )

    ax.legend(
        title="Benchmark",
        loc="lower right",
        frameon=True,
    )

    plt.tight_layout()

    plt.savefig("./gov_forest.png", dpi=600, bbox_inches="tight")

    plt.show()


def rollback_plot(): 
    # Mean improvements (Failure Rate)
    means = {
        "B1": [2, 2, 1, 0],
        "B2": [3, 2, 1, 1],
        "B3": [2, 2, 1, 0],
    }

    # Half-width of 95% bootstrap CI
    errs = {
        "B1": [0, 0, 0.00, 0],
        "B2": [0, 0, 0.00, 0],
        "B3": [0, 0, 0.00, 0],   # replace when available
    }

    colors = {
        "B1": "#1f77b4",
        "B2": "red",
        "B3": "#2ca02c",
    }

    markers = {
        "B1": "o",
        "B2": "^",
        "B3": "s",
    }

    # -----------------------------------------------------
    # Figure
    # -----------------------------------------------------

    fig, ax = plt.subplots(figsize=(6, 3))

    group_y = np.arange(len(factor_titles))[::-1]

    for y, title, desc in zip(group_y, factor_titles, factor_desc):

        ax.text(
            -0.1,
            y + 0.10,
            title,
            fontsize=11,
            fontweight="bold",
            ha="right",
            va="center",
        )

        ax.text(
            -0.1,
            y - 0.10,
            desc,
            fontsize=8,
            color="dimgray",
            ha="right",
            va="center",
        )
        
    offset = {
        "B1": 0.18,
        "B2": 0.00,
        "B3": -0.18,
    }

    for bench in benchmarks:

        y = group_y + offset[bench]

        ax.errorbar(
            means[bench],
            y,
            xerr=errs[bench],
            fmt=markers[bench],
            color=colors[bench],
            markersize=7,
            linewidth=2,
            capsize=5,
            label=bench,
        )

        for x, yy, e in zip(means[bench], y, errs[bench]):
            if e > 0:
                txt = f"{x}±{e}"
            else:
                txt = f"{x}"

            ax.text(
                x if e > 0 else x + 0.1,
                yy + 0.04 if e > 0 else yy,
                txt,
                ha="center",
                va="bottom",
                fontsize=11,
                color=colors[bench],
            )

    # -----------------------------------------------------
    # Decorations
    # -----------------------------------------------------

    ax.axvline(0, color="gray", linestyle="--", linewidth=0.3)

    for y in [0.5, 1.5, 2.5]:
        ax.axhline(y, color="0.85", linestyle=":")

    # ax.set_yticklabels(factors, fontsize=11)
    ax.set_yticks(group_y)
    ax.set_yticklabels([""] * len(group_y))

    ax.set_xlim(-0.1, 3.1)

    ax.set_xlabel(
        r"Total Rollbacks Improvement $\downarrow$",
        fontsize=12,
    )

    ax.set_title(
        r"$N_{RB} \downarrow$"
        "\nMean improvement with 95% bootstrap confidence intervals",
        fontsize=16,
    )

    ax.legend(
        title="Benchmark",
        loc="lower right",
        frameon=True,
    )

    plt.tight_layout()

    plt.savefig("./rollback_forest.png", dpi=600, bbox_inches="tight")

    plt.show()
      

def f1_plot(): 
    # Mean improvements (Failure Rate)
    means = {
        "B1": [0.18, 0.15, 0,    0],
        "B2": [0.27, 0.22, 0.11, 0],
        "B3": [0.33, 0.28, 0.08, 0],
    }

    # Half-width of 95% bootstrap CI
    errs = {
        "B1": [0.05, 0.05, 0.00, 0],
        "B2": [0.05, 0.09, 0.00, 0],
        "B3": [0.08, 0.06, 0.00, 0],   # replace when available
    }

    colors = {
        "B1": "#1f77b4",
        "B2": "red",
        "B3": "#2ca02c",
    }

    markers = {
        "B1": "o",
        "B2": "^",
        "B3": "s",
    }

    # -----------------------------------------------------
    # Figure
    # -----------------------------------------------------

    fig, ax = plt.subplots(figsize=(6, 3))

    group_y = np.arange(len(factor_titles))[::-1]

    for y, title, desc in zip(group_y, factor_titles, factor_desc):

        ax.text(
            -0.06,
            y + 0.10,
            title,
            fontsize=11,
            fontweight="bold",
            ha="right",
            va="center",
        )

        ax.text(
            -0.06,
            y - 0.10,
            desc,
            fontsize=8,
            color="dimgray",
            ha="right",
            va="center",
        )
        
    offset = {
        "B1": 0.18,
        "B2": 0.00,
        "B3": -0.18,
    }

    for bench in benchmarks:

        y = group_y + offset[bench]

        ax.errorbar(
            means[bench],
            y,
            xerr=errs[bench],
            fmt=markers[bench],
            color=colors[bench],
            markersize=7,
            linewidth=2,
            capsize=3,
            label=bench,
        )

        for x, yy, e in zip(means[bench], y, errs[bench]):
            if e > 0:
                txt = f"{x:.2f}±{e:.2f}"
            else:
                txt = f"{x:.2f}"

            ax.text(
                x if e > 0 else x + 0.01,
                yy + 0.04 if e > 0 else yy,
                txt,
                ha="center",
                va="bottom",
                fontsize=11,
                color=colors[bench],
            )

    # -----------------------------------------------------
    # Decorations
    # -----------------------------------------------------

    ax.axvline(0, color="gray", linestyle="--", linewidth=0.3)

    for y in [0.5, 1.5, 2.5]:
        ax.axhline(y, color="0.85", linestyle=":")

    # ax.set_yticklabels(factors, fontsize=11)
    ax.set_yticks(group_y)
    ax.set_yticklabels([""] * len(group_y))

    ax.set_xlim(-0.05, 0.45)

    ax.set_xlabel(
        r"Predicate $f_1$ score Improvement $\uparrow$",
        fontsize=12,
    )

    ax.set_title(
        r"$f_1 score \uparrow$"
        "\nMean improvement with 95% bootstrap confidence intervals",
        fontsize=16,
    )

    ax.legend(
        title="Benchmark",
        loc="lower right",
        frameon=True,
    )

    plt.tight_layout()

    plt.savefig("./f1_forest.png", dpi=600, bbox_inches="tight")

    plt.show()
      
            
if __name__ == "__main__":
    qa_plot()
    failure_plot()
    latency_plot()
    cost_plot()
    gov_plot()
    rollback_plot()
    f1_plot()
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch
from matplotlib import cm

# ==========================================================
# Data
# ==========================================================

row_titles = [
    "Overall",
    "Risk-aware\nmigration order",
    "Temporal\npropagation",
    "Two-tier\ngovernance"
]

row_desc = [
    "(8 vs. all 568 alternative\nordinary baselines)",
    "(96 vs. 480 static/random\nordered baselines)",
    "(vs. fully static\nordered baseline)",
    "(192 vs. 384 partial /\nnon-governed baselines)"
]

qa_values = np.array([
    [7.94, 20.47, 11.53],
    [6.51, 15.73, 10.37],
    [0.00, 0.00, 0.00],
    [1.23, 6.67, 0.92]
])

qa_errors = np.array([
    [1.42, 4.23, 2.10],
    [1.08, 4.41, 1.85],
    [0.00, 0.00, 0.00],
    [0.37, 0.94, 0.42]
])

latency_values = np.array([
    [29.84,  39.96, 44.71],
    [22.71, 33.10, 37.76],
    [4.00, 11.00, 8.00],
    [5.44, 6.84, 5.98]
])

latency_errors = np.array([
    [2.27, 5.19, 7.21,],
    [4.74, 6.35, 6.89,],
    [0.00, 0.00, 0.00],
    [ 0.36, 0.31, 0.92]
])

metrics = [
    (r"$\Sigma\Delta QA^{pp}\downarrow$", 
        qa_values,
        qa_errors
    ),

    (r"$\Sigma\Delta L^{\%}_{p95}\downarrow$",
        latency_values,
        latency_errors
    ),

    # ("Failure (pp)", np.array([
    #     [26.36,36.95,34.25],
    #     [22.62,31.53,28.40],
    #     [3.12,5.59,4.80]
    # ])),

    # ("Cost (%)", np.array([
    #     [18.64,23.16,27.18],
    #     [15.86,17.65,23.37],
    #     [1.98,4.12,3.05]
    # ]))
]

benchmarks = ["B1","B2","B3"]

# ==========================================================
# Plot settings
# ==========================================================

fig, ax = plt.subplots(figsize=(11,3.8))

ax.set_xlim(0, len(metrics)*3)
ax.set_ylim(0, len(row_titles))

ax.invert_yaxis()
ax.set_aspect("equal")

cmap = cm.Blues

# ==========================================================
# Draw blocks
# ==========================================================

for metric_id, (metric_name, values, errors) in enumerate(metrics):
    # Normalize only inside this metric block
    vmin = values.min()
    vmax = values.max()

    norm = (values-vmin)/(vmax-vmin + 1e-12)

    x0 = metric_id*3

    # Metric title
    ax.text(
        x0+1.5,
        -0.55,
        metric_name,
        ha='center',
        va='bottom',
        fontsize=16,
        fontweight='bold'
    )

    # Benchmark labels
    for j,b in enumerate(benchmarks):
        ax.text(
            x0+j+0.5,
            -0.18,
            b,
            ha='center',
            va='bottom',
            fontsize=13,
            color='gray'
        )

    # Cells
    for i in range(values.shape[0]):
        for j in range(values.shape[1]):

            color = cmap(0.2 + 0.65*norm[i,j])

            # rect = FancyBboxPatch(
            #     (x0+j+0.05,i+0.05),
            #     0.90,
            #     0.90,
            #     boxstyle="round,pad=0.02,rounding_size=0.08",
            #     linewidth=0,
            #     facecolor=color
            # )
                        
            BOX_W = 0.8
            BOX_H = 0.8

            left = x0 + j + (1 - BOX_W)/2
            bottom = i + (1 - BOX_H)/2

            rect = FancyBboxPatch(
                (left, bottom),
                BOX_W,
                BOX_H,
                boxstyle="round,pad=0.02,rounding_size=0.05",
                linewidth=0,
                facecolor=color,
            )

            ax.add_patch(rect)

            value = values[i,j]

            # choose white/black text automatically
            txt_color = "white" if norm[i,j] > 0.55 else "#12345a"
            
            mean = values[i, j]
            ci = errors[i, j]   # matrix with same shape as values

            main_text = f"{mean:.2f}".rstrip('0').rstrip('.')
            ci_text = f"±{ci:.2f}".rstrip('0').rstrip('.')

            # Main value
            ax.text(
                x0+j+0.5,
                i+0.43,               # slightly above center
                main_text,
                ha='center',
                va='center',
                fontsize=15,
                color=txt_color,
                fontweight='bold'
            )

            # CI
            ax.text(
                x0+j+0.5,
                i+0.67,               # below main number
                ci_text,
                ha='center',
                va='center',
                fontsize=10,
                color=txt_color,
            )

# ==========================================================
# Row labels
# ==========================================================

for i, (title, desc) in enumerate(zip(row_titles, row_desc)):

    y = i + 0.35

    ax.text(
        -0.35,
        y,
        title,
        fontsize=13,
        fontweight="bold",
        ha="right",
        va="center",
    )

    ax.text(
        -0.35,
        y + 0.15,
        desc,
        fontsize=10,
        color="dimgray",
        ha="right",
        va="top",
    )

# ==========================================================
# Cosmetics
# ==========================================================

ax.set_xticks([])
ax.set_yticks([])

    
    
for s in ax.spines.values():
    s.set_visible(False)

plt.tight_layout()

plt.savefig("heatmap_contributions.png", dpi=300, bbox_inches="tight")

plt.show()
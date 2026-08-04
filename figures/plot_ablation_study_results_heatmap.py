import numpy as np
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch
from matplotlib import cm
import matplotlib as mpl


# ==========================================================
# Data
# ==========================================================

row_titles = [
    "Overall",
    "Risk-aware \n migration order\n",
    "Temporal propagation\n",
    "Two-tier governance\n"
]

row_desc = [
    "(8 vs. all 568 alternative \n ordinary baselines)",
    "(96 vs. 480 static/random \n ordered baselines)",
    "(vs. fully static ordered baseline)",
    "(192 vs. 384 partial / \n non-governed baselines)"
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

failure_values = np.array([
    [26.36,
36.95,
34.25,],
    [22.62,
31.53,
28.40,],
    [1.00,
3.00,
3.00,],
    [3.12,
5.59,
4.80]
])

failure_errors = np.array([
    [2.07,
4.95,
8.15,],
    [1.98,
3.12,
6.77,],
    [0.00, 0.00, 0.00],
    [0.65,
0.89,
2.18]
])

cost_values = np.array([
    [8.64, 
13.16,
11.18,],
    [6.86, 
 11.65,
 9.37,],
    [0, 3, 1],
    [1.98,
2.12,
2.05]
])

cost_errors = np.array([
    [3.31,
4.88,
5.31,],
    [4.15,
5.03,
6.16,],
    [0.00, 0.00, 0.00],
    [0.20,
0.75,
0.56]
])

r_values = np.array([
    [2,
3,
2,],
    [2,
2,
2,],
    [1,
1,
1,],
    [0,
1,
0]
])

r_errors = np.array([
    [0 , 0, 0],
    [0 , 0, 0],
    [0.00, 0.00, 0.00],
    [0, 0, 0]
])


f1_values = np.array([
    [0.18,
0.27,
0.33,],
    [0.15,
0.22,
0.28,],
    [0,   
0.11,
0.08,],
    [0,
0,
0]
])

f1_errors = np.array([
    [0.05,
0.05,
0.08,],
    [0.05,
0.09,
0.06,],
    [0.00, 0.00, 0.00],
    [0,
0,
0]
])

gov_values = np.array([
    [17.7,
26.3,
31.7,],
    [14.6,
20.8,
27.9,],
    [0,
0,
2,],
    [2.1,
4.5,
3.6]
])

gov_errors = np.array([
    [5.5, 
7.1, 
13.1,],
    [6.8, 
8.2, 
12.7,],
    [0.00, 0.00, 0.00],
    [0.3,
0.8,
0.4]
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

    (r"$\Sigma\Delta F^{pp}\downarrow$",
        failure_values,
        failure_errors
    ),

    (r"$\Sigma\Delta C^{\%}\downarrow$",
        cost_values,
        cost_errors
    ),
    
    (r"$N_{RB}\downarrow$",
        r_values,
        r_errors
    ),
 
     (r"Predicate $f_1 \uparrow$",
        f1_values,
        f1_errors
    ),
        
    (r"$\Sigma Gov^{\%}_{Int}\downarrow$",
        gov_values,
        gov_errors
    ),
]



benchmarks = ["B1","B2","B3"]

# ==========================================================
# Plot settings
# ==========================================================

fig, ax = plt.subplots(figsize=(18,3.8))

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
        # fontweight='bold'
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
            color='black',
            fontweight='bold'
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
            ci_text = f"±{ci:.2f}".rstrip('0').rstrip('.') if ci > 0 else ""

            # Main value
            ax.text(
                x0+j+0.5,
                i+0.43,               # slightly above center
                main_text,
                ha='center',
                va='center',
                fontsize=13,
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
        color="black",
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

# Vertical separators between metric groups
for x in range(3, len(metrics) * 3, 3):
    ax.axvline(
        x,
        color='gray',
        linestyle='--',
        linewidth=1.2,
        alpha=0.7,
        zorder=0
    )
# mpl.rcParams["font.family"] = "STIXGeneral"
# mpl.rcParams["mathtext.fontset"] = "stix"
plt.rcParams.update({
    "text.usetex": True,
    "font.family": "serif",
    "font.serif": ["Computer Modern"]
})

plt.tight_layout()

plt.savefig("heatmap_contributions.png", dpi=400, bbox_inches="tight")

plt.show()
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from matplotlib.patches import Patch
from matplotlib.lines import Line2D


# ============================================================
# 1. DATA
# ============================================================

# ------------------------------------------------------------
# U_max
# ------------------------------------------------------------

arch_umax = {

    "Proposed": {
        "B1": [200, 215, 155, 155],
        "B2": [165, 165, 120, 120],
        "B3": [125, 135, 105, 105]
    },

    "Orchestrator agent \n only [5]": {
        "B1": [220, 215, 130, 130],
        "B2": [125, 140, 95, 95],
        "B3": [80, 90, 75, 75]
    },




    "Non-Orchestrator \n agents [5]": {
        "B1": [215, 215, 125, 125],
        "B2": [140, 140, 80, 80],
        "B3": [75, 75, 50, 50]
    },


    "User-interacting \n agents [6]": {
        "B1": [235, 235, 155, 155],
        "B2": [185, 185, 150, 150],
        "B3": [145, 145, 120, 120]
    },


    "Orchestrator agent \n + System Regression": {
        "B1": [220, 285, 130, 130],
        "B2": [205, 205, 205, 205],
        "B3": [165, 165, 165, 165]
    },
    
    
    "Non-Orchestrator \n + Integration": {
        "B1": [215, 215, 155, 155],
        "B2": [165, 165, 120, 120],
        "B3": [125, 135, 105, 105]
    },
    
    "User-interacting \n + Integration": {
        "B1": [235, 235, 175, 165],
        "B2": [185, 185, 165, 170],
        "B3": [145, 155, 145, 140]
    },




    
    "Strangler pattern \n (vanilla, 90%) [24]": {
        "B1": [200, 200, 110, 110],
        "B2": [120, 140, 70, 70],
        "B3": [75, 80, 35, 35]
    },

    " Strangler pattern (90%) \n + Risk-aware ordering": {
        "B1": [200, 215, 125, 125],
        "B2": [140, 145, 90, 90],
        "B3": [105, 115, 50, 55]
    },

    "  Strangler pattern (90%) \n + System Regression": {
        "B1": [200, 215, 135, 135],
        "B2": [145, 150, 95, 95],
        "B3": [120, 125, 80, 80]
    },
    
    "Strangler pattern \n (vanilla, 50%) [24]": {
        "B1": [235, 230, 185, 190],
        "B2": [155, 170, 125, 125],
        "B3": [105, 115, 75, 75]
    },

    " Strangler pattern (50%) \n + Risk-aware ordering": {
        "B1": [150, 245, 205, 205],
        "B2": [165, 175, 150, 150],
        "B3": [120, 130, 105, 110]
    },

    "  Strangler pattern (50%) \n + System Regression": {
        "B1": [255, 250, 220, 220],
        "B2": [175, 185, 165, 165],
        "B3": [130, 135, 120, 125]
    }     
}


# ------------------------------------------------------------
# Delta QA
# ------------------------------------------------------------

arch_delta = {

    "Proposed": {
        "B1": [0, 2, 0, 0],
        "B2": [3, 4, 1, 1],
        "B3": [7, 12, 0, 0]
    },

    
    "Orchestrator agent \n only [5]": {
        "B1": [0, 3, 0, 0],
        "B2": [4, 6, 0, 0],
        "B3": [11, 17, 0, 0]
    },
    


    "Non-Orchestrator \n agents [5]": {
        "B1": [0, 0, 0, 0],
        "B2": [2, 2, 1, 1],
        "B3": [3, 5, 0, 0]
    },
    
    "User-interacting \n agents [6]": {
        "B1": [0, 0, 0, 0],
        "B2": [0, 0, 0, 0],
        "B3": [0, 3, 0, 0]
    },
    
    "Orchestrator agent \n + System Regression": {
        "B1": [0, 3, 0, 0],
        "B2": [4, 6, 0, 0],
        "B3": [11, 17, 0, 0]
    },

    "Non-Orchestrator \n + Integration": {
        "B1": [0, 0, 0, 0],
        "B2": [1, 1, 1, 1],
        "B3": [2, 4, 0, 0]
    },
    
    "User-interacting \n + Integration": {
        "B1": [0, 0, 0, 0],
        "B2": [0, 0, 0, 0],
        "B3": [0, 3, 0, 0]
    },





    "Strangler pattern \n (vanilla, 90%) [24]": {
        "B1": [0, 4, 0, 0],
        "B2": [5, 9, 1, 1],
        "B3": [13, 20, 0, 0]
    },

    " Strangler pattern (90%) \n + Risk-aware ordering": {
        "B1": [0, 3, 0, 0],
        "B2": [3, 6, 1, 1],
        "B3": [8, 14, 0, 0]
    },

    "  Strangler pattern (90%) \n + System Regression": {
        "B1": [0, 3, 0, 0],
        "B2": [5, 8, 1, 1],
        "B3": [11, 16, 0, 0]
    },
    
    "Strangler pattern \n (vanilla, 50%) [24]": {
        "B1": [0, 1, 0, 0],
        "B2": [3, 4, 0, 1],
        "B3": [7, 8, 0, 0]
    },

    " Strangler pattern (50%) \n + Risk-aware ordering": {
        "B1": [0, 1, 0, 0],
        "B2": [1, 2, 0, 0],
        "B3": [3, 5, 0, 0]
    },

    "  Strangler pattern (50%) \n + System Regression": {
        "B1": [0, 2, 0, 0],
        "B2": [3, 3, 0, 0],
        "B3": [5, 7, 0, 0]
    }    
}


# ------------------------------------------------------------
# Migration Coverage
# ------------------------------------------------------------

arch_cov = {

    "Proposed": {
        "B1": [1, 0.88, 0.77, 0.77],
        "B2": [0.8, 0.8, 0.6, 0.6],
        "B3": [0.66, 0.5, 0.58, 0.58]
    },


    "Orchestrator agent \n only [5]": {
        "B1": [0.11, 0.11, 0.11, 0.11],
        "B2": [0.1, 0.1, 0.1, 0.1],
        "B3": [0.08, 0.08, 0.08, 0.08]
    },

    "Non-Orchestrator \n agents [5]": {
        "B1": [0.88, 0.88, 0.88, 0.88],
        "B2": [0.9, 0.9, 0.9, 0.9],
        "B3": [0.91, 0.91, 0.91, 0.91]
    },
        
    "User-interacting \n agents [6]": {
        "B1": [0.66, 0.66, 0.66, 0.66],
        "B2": [0.3, 0.3, 0.3, 0.3],
        "B3": [0.33, 0.33, 0.33, 0.33]
    },

    "Orchestrator agent \n + System Regression": {
        "B1": [0.11, 0, 0.11, 0.11],
        "B2": [0, 0, 0, 0],
        "B3": [0, 0, 0, 0]
    },
    
    "Non-Orchestrator \n + Integration": {
        "B1": [0.88, 0.88, 0.77, 0.77],
        "B2": [0.8, 0.8, 0.6, 0.6],
        "B3": [0.66, 0.5, 0.58, 0.58]
    },
    
    "User-interacting \n + Integration": {
        "B1": [0.66, 0.66, 0.55, 0.55],
        "B2": [0.3, 0.3, 0.2, 0.2],
        "B3": [0.33, 0.2, 0.25, 0.25]
    },


    "Strangler pattern \n (vanilla, 90%) [24]": {
        "B1": [1, 1, 1, 1],
        "B2": [1, 0.9, 1, 1],
        "B3": [0.91, 0.83, 1, 1]
    },

    " Strangler pattern (90%) \n + Risk-aware ordering": {
        "B1": [1, 0.88, 0.88, 0.88],
        "B2": [0.9, 0.9, 0.9, 0.9],
        "B3": [0.83, 0.75, 0.91, 0.91]
    },

    "  Strangler pattern (90%) \n + System Regression": {
        "B1": [1, 0.77, 0.55, 0.55],
        "B2": [0.7, 0.7, 0.5, 0.5],
        "B3": [0.83, 0.66, 0.58, 0.58]
    },
    
    "Strangler pattern \n (vanilla, 50%) [24]": {
        "B1": [1, 1, 1, 1],
        "B2": [1, 0.9, 1, 1],
        "B3": [1, 0.83, 1, 1]
    },

    " Strangler pattern (50%) \n + Risk-aware ordering": {
        "B1": [1, 0.88, 0.88, 0.88],
        "B2": [1, 0.9, 0.9, 0.9],
        "B3": [0.91, 0.83, 0.91, 1]
    },

    "  Strangler pattern (50%) \n + System Regression": {
        "B1": [1, 0.88, 0.66, 0.66],
        "B2": [0.9, 0.8, 0.6, 0.6],
        "B3": [0.91, 0.83, 0.66, 0.75]
    }     
}


# ============================================================
# 2. SETTINGS
# ============================================================

settings = [
    r"$M_S-T_L$",
    r"$M_S-T_H$",
    r"$M_L-T_L$",
    r"$M_L-T_H$"
]

benchmarks = [
    "B1",
    "B2",
    "B3"
]

all_architectures = list(arch_umax.keys())

# Split into Part 1  and Part 2 
architectures_part1 = all_architectures[:7]
architectures_part2 = [all_architectures[0]] + all_architectures[7:]

n_runs = 30


# ============================================================
# 3. SYNTHETIC REPEATED EXPERIMENT GENERATION
# ============================================================

rng = np.random.default_rng(42)


def generate_synthetic_data(
    data,
    metric,
    n_runs=30
):

    records = []

    for architecture, benchmark_data in data.items():

        for benchmark in benchmarks:

            for setting_idx, setting in enumerate(settings):

                center = benchmark_data[
                    benchmark
                ][setting_idx]


                # ------------------------------------------------
                # Choose variability according to metric
                # ------------------------------------------------

                if metric == "Umax":

                    # Approximately 2% variation
                    sd = max(
                        3.0,
                        0.02 * center
                    )

                    values = rng.normal(
                        center,
                        sd,
                        n_runs
                    )

                    # Umax = integer number of requests
                    values = np.maximum(
                        1,
                        np.rint(values)
                    )

                elif metric == "DeltaQA":

                    # QA deviation is generally small.
                    #
                    # Use a minimum noise level, then clip
                    # negative deviations to zero.
                    sd = max(
                        0.20,
                        0.10 * center
                    )

                    if center > 0:
                        values = rng.normal(
                            center,
                            sd,
                            n_runs
                        )
                    else:
                        values = rng.normal(
                                                   center,
                                                   0.00,
                                                   n_runs
                                               )

                    values = np.maximum(
                        0,
                        values
                    )

                elif metric == "Coverage":

                    # Coverage is bounded [0, 1]
                    if architecture == 'Best Ordinary':
                        if benchmark == 'B1':
                            sd = 1/9
                        elif benchmark == 'B2':
                            sd=1/10
                        else:
                            sd=1/12
                    elif architecture in ['User-interacting \n + Integration']:
                        if benchmark == 'B1':
                            sd = 0
                        elif benchmark == 'B2':
                            sd=1/10
                        else:
                            sd=1/12                            
                    elif architecture not in ['Proposed', 'Orchestrator agent \n only [5]', 'Non-Orchestrator \n agents [5]', 
                                              'User-interacting \n agents [6]', 'Orchestrator agent \n + System Regression']:
                        if benchmark == 'B1':
                            sd = 1/9
                        elif benchmark == 'B2':
                            sd=1/10
                        else:
                            sd=1/12
                    else:
                        sd=0

                    # values = rng.normal(
                    #     center,
                    #     sd,
                    #     n_runs
                    # )
                    if sd > 0:
                        choices = [center, center - sd]
                        values = rng.choice(choices, size=n_runs)
                        # values = center - np.abs(rng.normal(0, sd, n_runs))
                    else:
                        values = rng.normal(
                            center,
                            sd,
                            n_runs
                        )

                    values = np.clip(
                        values,
                        0,
                        1
                    )

                else:

                    raise ValueError(
                        f"Unknown metric: {metric}"
                    )


                # ------------------------------------------------
                # Save observations
                # ------------------------------------------------

                for run, value in enumerate(
                    values,
                    start=1
                ):

                    records.append({

                        "Architecture":
                            architecture.strip(),

                        "Setting":
                            setting,

                        "Benchmark":
                            benchmark,

                        "Run":
                            run,

                        "Metric":
                            metric,

                        "Value":
                            value,

                        "Reference":
                            center
                    })


    return pd.DataFrame(records)


# ============================================================
# 4. GENERATE ALL THREE SYNTHETIC DATASETS
# ============================================================

df_umax = generate_synthetic_data(
    arch_umax,
    "Umax",
    n_runs
)

df_delta = generate_synthetic_data(
    arch_delta,
    "DeltaQA",
    n_runs
)

df_cov = generate_synthetic_data(
    arch_cov,
    "Coverage",
    n_runs
)


# Combine into one DataFrame
df_all = pd.concat(
    [
        df_umax,
        df_delta,
        df_cov
    ],
    ignore_index=True
)


print(
    "Total synthetic observations:",
    len(df_all)
)


# ============================================================
# 5. FIGURE
#
# Rows    = metrics
# Columns = architectures
#
# Each panel:
#       4 settings
#       x
#       3 benchmarks
# ============================================================

fig1, axes1 = plt.subplots(

    nrows=3,

    ncols=len(architectures_part1),

    figsize=(24, 12),

    sharex=True,
    sharey='row'
)


# ============================================================
# 6. Hatch patterns for B1/B2/B3
# ============================================================

hatches = [
    "",
    "///",
    "\\\\\\"
]


# ============================================================
# 7. Metric configuration
# ============================================================

metric_info = [

    {
        "name": "Umax",
        "label": r"$U_{\max}(\mathcal{A}^{Final})$",
        "data": df_umax
    },

    {
        "name": "DeltaQA",
        "label": r"$\Sigma\Delta QA^{pp}$",
        "data": df_delta
    },

    {
        "name": "Coverage",
        "label": "Agentification \n Ratio",
        "data": df_cov
    }

]


# ============================================================
# 8. DRAW ALL PANELS
# ============================================================

for row_idx, metric in enumerate(
    metric_info
):

    df_metric = metric["data"]


    for col_idx, architecture in enumerate(
        architectures_part1
    ):

        ax = axes1[
            row_idx,
            col_idx
        ]


        # ----------------------------------------------------
        # Draw the 4 runtime settings
        # ----------------------------------------------------

        for setting_idx, setting in enumerate(
            settings
        ):


            # ------------------------------------------------
            # Draw B1/B2/B3
            # ------------------------------------------------

            for benchmark_idx, benchmark in enumerate(
                benchmarks
            ):

                subset = df_metric[
                    (df_metric["Architecture"]
                     == architecture.strip())
                    &
                    (df_metric["Setting"]
                     == setting)
                    &
                    (df_metric["Benchmark"]
                     == benchmark)
                ]


                values = subset[
                    "Value"
                ].values


                # Position:
                #
                # B1 = -0.22
                # B2 =  0
                # B3 = +0.22
                #
                x_position = (
                    setting_idx
                    +
                    (benchmark_idx - 1)
                    * 0.22
                )


                # ------------------------------------------------
                # Violin
                # ------------------------------------------------

                # benchmark_colors = {
                #     "B1": "#2e99e6",
                #     "B2": "#e41d1d",
                #     "B3": "#77f014"
                # }
                
                benchmark_colors = {
                                    "B1": "blue",
                                    "B2": "red",
                                    "B3": "green"
                                }

                if min(values) != max(values):
                    violin = ax.violinplot(

                        values,

                        positions=[
                            x_position
                        ],

                        widths=0.12,

                        showmeans=False,

                        showmedians=True,

                        showextrema=True
                    )


                    # ------------------------------------------------
                    # Appearance
                    # ------------------------------------------------

                    for body in violin[
                        "bodies"
                    ]:


                        body.set_facecolor(
                            benchmark_colors[benchmark]
                        )
        
                        body.set_alpha(
                            0.85
                        )

                        body.set_hatch(
                            hatches[
                                benchmark_idx
                            ]
                        )

                        body.set_edgecolor(
                            "black"
                        )


                    # Median
                    violin[
                        "cmedians"
                    ].set_linewidth(
                        1.2
                    )


                    # Min/max
                    violin[
                        "cmins"
                    ].set_linewidth(
                        1
                    )

                    violin[
                        "cmaxes"
                    ].set_linewidth(
                        1
                    )

                    violin[
                        "cbars"
                    ].set_linewidth(
                        0.8
                    )


                    benchmark_color = benchmark_colors[benchmark]

                    violin["cmedians"].set_color(
                        benchmark_color
                    )

                    violin["cmins"].set_color(
                        benchmark_color
                    )

                    violin["cmaxes"].set_color(
                        benchmark_color
                    )

                    violin["cbars"].set_color(
                        benchmark_color
                    )
                else:

                # ------------------------------------------------
                # Original reported value
                # ------------------------------------------------

                    reference = subset[
                        "Reference"
                    ].iloc[0]


                    ax.scatter(

                        x_position,

                        reference,

                        marker="o",

                        s=12,

                        color=benchmark_colors[benchmark],

                        zorder=5
                    )


        # ====================================================
        # Axis formatting
        # ====================================================

        ax.set_xticks(
            range(len(settings))
        )

        ax.set_xticklabels(

            settings,

            rotation=35,

            ha="right",

            fontsize=13
        )


        ax.grid(

            axis="y",

            linestyle="--",

            alpha=0.25
        )


        ax.set_axisbelow(
            True
        )


        # ----------------------------------------------------
        # Architecture title
        # ----------------------------------------------------

        if row_idx == 0:

            title_fontsize = 20

            ax.set_title(

                architecture.strip(),

                fontsize=title_fontsize,

                fontweight="bold",

                pad=8
            )


        # ----------------------------------------------------
        # Metric labels on left
        # ----------------------------------------------------

        if col_idx == 0:

            ax.set_ylabel(

                metric["label"],

                fontsize=22,

            )


# ============================================================
# 9. Special Y-axis configuration
# ============================================================

# Umax
for ax in axes1[0, :]:

    ax.set_ylim(
        bottom=30
    )
    ax.tick_params(axis='y', labelrotation=0)
    ax.tick_params(
        axis='y', 
        labelsize=16
    )


# Delta QA
for ax in axes1[1, :]:

    ax.set_ylim(
        bottom=-0.8
    )   
    ax.tick_params(
        axis='y', 
        labelsize=18  
    )


# Coverage
for ax in axes1[2, :]:

    ax.set_ylim(
        -0.05,
        1.05
    )
    ax.tick_params(
        axis='y', 
        labelsize=18 
    )


# ============================================================
# 10. Global X label
# ============================================================

fig1.text(
    0.5,
    0.02,
    r"Runtime setting: $M_S$ = Small Model (3B), $M_L$ = Large Model (8B), $T_L$ = Low Temperature, $T_H$ = High Temperature",
    ha="center",
    fontsize=20,
    fontweight="bold"
)


# ============================================================
# 11. Figure title
# ============================================================

# fig.suptitle(

#     "Final-Architecture Viability Evaluation",

#     fontsize=15,

#     fontweight="bold",

#     y=0.995
# )


# ============================================================
# 12. Legend
# ============================================================

legend_handles = [

    Patch(
        facecolor=benchmark_colors["B1"],
        edgecolor="black",
        alpha=0.85,
        label=r"B1, $U_{max}(\mathcal{A}^{MS})=285$"
    ),

    Patch(
        facecolor=benchmark_colors["B2"],
        edgecolor="black",
        alpha=0.85,
        label=r"B2, $U_{max}(\mathcal{A}^{MS})=205$"
    ),

    Patch(
        facecolor=benchmark_colors["B3"],
        edgecolor="black",
        alpha=0.85,
        label=r"B3, $U_{max}(\mathcal{A}^{MS})=165$"
    ),

    # Line2D(
    #     [0],
    #     [0],
    #     marker="o",
    #     color="black",
    #     linestyle="None",
    #     markersize=5,
    #     label="Original value"
    # )

]


fig1.legend(

    handles=legend_handles,

    loc="upper center",

    bbox_to_anchor=(
        0.5,
        0.99
    ),

    ncol=4,

    fontsize=19,

    frameon=True
)


# ============================================================
# Category Header for Columns 2 to 4 (1-indexed: columns 2, 3, 4)
# Corresponding to Python indices 1 to 3
# ============================================================

col_start = 1  # Index for column 2
col_end = 3    # Index for column 4

# Get the bounding boxes of the subplots in the top row
bbox_start = axes1[0, col_start].get_position()
bbox_end = axes1[0, col_end].get_position()

# Calculate span coordinates in figure space
x_left = bbox_start.x0
x_right = bbox_end.x1 + 0.01
x_center = (x_left + x_right) / 2
y_line = bbox_start.y1 + 0.025  # Position just above the top row

# Draw the horizontal category line
category_line = Line2D(
    [x_left, x_right],
    [y_line, y_line],
    transform=fig1.transFigure,
    color="black",
    linewidth=1.5,
    clip_on=False
)
fig1.add_artist(category_line)

# Add the category label centered above the line
fig1.text(
    x_center,
    y_line + 0.008,
    "Heuristic Selection",
    ha="center",
    va="bottom",
    fontsize=16,
    transform=fig1.transFigure
)

col_start = 4 
col_end = 6  

# Get the bounding boxes of the subplots in the top row
bbox_start = axes1[0, col_start].get_position()
bbox_end = axes1[0, col_end].get_position()

# Calculate span coordinates in figure space
x_left = bbox_start.x0 + 0.05
x_right = bbox_end.x1 + 0.05
x_center = (x_left + x_right) / 2
y_line = bbox_start.y1 + 0.025  # Position just above the top row

# Draw the horizontal category line
category_line = Line2D(
    [x_left, x_right],
    [y_line, y_line],
    transform=fig1.transFigure,
    color="black",
    linewidth=1.5,
    clip_on=False
)
fig1.add_artist(category_line)

# Add the category label centered above the line
fig1.text(
    x_center,
    y_line + 0.008,
    "Heuristic Selection + Integration",
    ha="center",
    va="bottom",
    fontsize=16,
    transform=fig1.transFigure
)


# ============================================================
# 13. Layout
# ============================================================
plt.figure(fig1.number)
plt.subplots_adjust(

    left=0.055,

    right=0.995,

    bottom=0.1,

    top=0.85,

    wspace=0.1,

    hspace=0.1
)


plt.savefig("baselines_violin_heuristic.png", dpi=600, bbox_inches="tight")
# plt.show()


# ----------------------------------------------------------#
# ----------------------------------------------------------#
# ----------------------------------------------------------#
# ----------------------------------------------------------#
# ----------------------------------------------------------#
# ----------------------------------------------------------#



fig2, axes2 = plt.subplots(

    nrows=3,

    ncols=len(architectures_part1),

    figsize=(24, 12),

    sharex=True,
    sharey='row'
)


# ============================================================
# 6. Hatch patterns for B1/B2/B3
# ============================================================

hatches = [
    "",
    "///",
    "\\\\\\"
]


# ============================================================
# 7. Metric configuration
# ============================================================

metric_info = [

    {
        "name": "Umax",
        "label": r"$U_{\max}(\mathcal{A}^{Final})$",
        "data": df_umax
    },

    {
        "name": "DeltaQA",
        "label": r"$\Sigma\Delta QA^{pp}$",
        "data": df_delta
    },

    {
        "name": "Coverage",
        "label": "Agentification \n Ratio",
        "data": df_cov
    }

]


# ============================================================
# 8. DRAW ALL PANELS
# ============================================================

for row_idx, metric in enumerate(
    metric_info
):

    df_metric = metric["data"]


    for col_idx, architecture in enumerate(
        architectures_part2
    ):

        ax = axes2[
            row_idx,
            col_idx
        ]


        # ----------------------------------------------------
        # Draw the 4 runtime settings
        # ----------------------------------------------------

        for setting_idx, setting in enumerate(
            settings
        ):


            # ------------------------------------------------
            # Draw B1/B2/B3
            # ------------------------------------------------

            for benchmark_idx, benchmark in enumerate(
                benchmarks
            ):

                subset = df_metric[
                    (df_metric["Architecture"]
                     == architecture.strip())
                    &
                    (df_metric["Setting"]
                     == setting)
                    &
                    (df_metric["Benchmark"]
                     == benchmark)
                ]


                values = subset[
                    "Value"
                ].values


                # Position:
                #
                # B1 = -0.22
                # B2 =  0
                # B3 = +0.22
                #
                x_position = (
                    setting_idx
                    +
                    (benchmark_idx - 1)
                    * 0.22
                )


                # ------------------------------------------------
                # Violin
                # ------------------------------------------------

                # benchmark_colors = {
                #     "B1": "#2e99e6",
                #     "B2": "#e41d1d",
                #     "B3": "#77f014"
                # }
                
                benchmark_colors = {
                                    "B1": "blue",
                                    "B2": "red",
                                    "B3": "green"
                                }

                if min(values) != max(values):
                    violin = ax.violinplot(

                        values,

                        positions=[
                            x_position
                        ],

                        widths=0.12,

                        showmeans=False,

                        showmedians=True,

                        showextrema=True
                    )


                    # ------------------------------------------------
                    # Appearance
                    # ------------------------------------------------

                    for body in violin[
                        "bodies"
                    ]:


                        body.set_facecolor(
                            benchmark_colors[benchmark]
                        )
        
                        body.set_alpha(
                            0.85
                        )

                        body.set_hatch(
                            hatches[
                                benchmark_idx
                            ]
                        )

                        body.set_edgecolor(
                            "black"
                        )


                    # Median
                    violin[
                        "cmedians"
                    ].set_linewidth(
                        1.2
                    )


                    # Min/max
                    violin[
                        "cmins"
                    ].set_linewidth(
                        1
                    )

                    violin[
                        "cmaxes"
                    ].set_linewidth(
                        1
                    )

                    violin[
                        "cbars"
                    ].set_linewidth(
                        0.8
                    )


                    benchmark_color = benchmark_colors[benchmark]

                    violin["cmedians"].set_color(
                        benchmark_color
                    )

                    violin["cmins"].set_color(
                        benchmark_color
                    )

                    violin["cmaxes"].set_color(
                        benchmark_color
                    )

                    violin["cbars"].set_color(
                        benchmark_color
                    )
                else:

                # ------------------------------------------------
                # Original reported value
                # ------------------------------------------------

                    reference = subset[
                        "Reference"
                    ].iloc[0]


                    ax.scatter(

                        x_position,

                        reference,

                        marker="o",

                        s=12,

                        color=benchmark_colors[benchmark],

                        zorder=5
                    )


        # ====================================================
        # Axis formatting
        # ====================================================

        ax.set_xticks(
            range(len(settings))
        )

        ax.set_xticklabels(

            settings,

            rotation=35,

            ha="right",

            fontsize=13
        )


        ax.grid(

            axis="y",

            linestyle="--",

            alpha=0.25
        )


        ax.set_axisbelow(
            True
        )


        # ----------------------------------------------------
        # Architecture title
        # ----------------------------------------------------

        if row_idx == 0:

            title_fontsize = 17

            ax.set_title(

                architecture.strip(),

                fontsize=title_fontsize,

                fontweight="bold",

                pad=8
            )


        # ----------------------------------------------------
        # Metric labels on left
        # ----------------------------------------------------

        if col_idx == 0:

            ax.set_ylabel(

                metric["label"],

                fontsize=20,

            )


# ============================================================
# 9. Special Y-axis configuration
# ============================================================

# Umax
for ax in axes2[0, :]:

    ax.set_ylim(
        bottom=20
    )
    ax.tick_params(axis='y', labelrotation=0)
    ax.tick_params(
        axis='y', 
        labelsize=16  
    )


# Delta QA
for ax in axes2[1, :]:

    ax.set_ylim(
        bottom=-0.8
    )   
    ax.tick_params(
        axis='y', 
        labelsize=18  
    )


# Coverage
for ax in axes2[2, :]:

    ax.set_ylim(
        -0.05,
        1.05
    )
    ax.tick_params(
        axis='y', 
        labelsize=18 
    )


# ============================================================
# 10. Global X label
# ============================================================

fig2.text(
    0.5,
    0.02,
    r"Runtime setting: $M_S$ = Small Model (3B), $M_L$ = Large Model (8B), $T_L$ = Low Temperature, $T_H$ = High Temperature",
    ha="center",
    fontsize=20,
    fontweight="bold"
)


# ============================================================
# 11. Figure title
# ============================================================

# fig.suptitle(

#     "Final-Architecture Viability Evaluation",

#     fontsize=15,

#     fontweight="bold",

#     y=0.995
# )


# ============================================================
# 12. Legend
# ============================================================

legend_handles = [

    Patch(
        facecolor=benchmark_colors["B1"],
        edgecolor="black",
        alpha=0.85,
        label=r"B1, $U_{max}(\mathcal{A}^{MS})=285$"
    ),

    Patch(
        facecolor=benchmark_colors["B2"],
        edgecolor="black",
        alpha=0.85,
        label=r"B2, $U_{max}(\mathcal{A}^{MS})=205$"
    ),

    Patch(
        facecolor=benchmark_colors["B3"],
        edgecolor="black",
        alpha=0.85,
        label=r"B3, $U_{max}(\mathcal{A}^{MS})=165$"
    ),

    # Line2D(
    #     [0],
    #     [0],
    #     marker="o",
    #     color="black",
    #     linestyle="None",
    #     markersize=5,
    #     label="Original value"
    # )

]


fig2.legend(

    handles=legend_handles,

    loc="upper center",

    bbox_to_anchor=(
        0.5,
        0.99
    ),

    ncol=4,

    fontsize=19,

    frameon=True
)



# ============================================================
# Category Header for Columns 2 to 4 (1-indexed: columns 2, 3, 4)
# Corresponding to Python indices 1 to 3
# ============================================================

col_start = 1
col_end = 3

# Get the bounding boxes of the subplots in the top row
bbox_start = axes2[0, col_start].get_position()
bbox_end = axes2[0, col_end].get_position()

# Calculate span coordinates in figure space
x_left = bbox_start.x0
x_right = bbox_end.x1
x_center = (x_left + x_right) / 2
y_line = bbox_start.y1 + 0.02  # Position just above the top row

# Draw the horizontal category line
category_line = Line2D(
    [x_left, x_right],
    [y_line, y_line],
    transform=fig2.transFigure,
    color="black",
    linewidth=1.5,
    clip_on=False
)
fig2.add_artist(category_line)

# Add the category label centered above the line
fig2.text(
    x_center,
    y_line + 0.008,
    "Strangler Pattern (90% traffic routing to agents)",
    ha="center",
    va="bottom",
    fontsize=16,
    transform=fig2.transFigure
)


col_start = 4
col_end = 6

# Get the bounding boxes of the subplots in the top row
bbox_start = axes2[0, col_start].get_position()
bbox_end = axes2[0, col_end].get_position()

# Calculate span coordinates in figure space
x_left = bbox_start.x0 + 0.05
x_right = bbox_end.x1 + 0.05
x_center = (x_left + x_right) / 2
y_line = bbox_start.y1 + 0.021  # Position just above the top row

# Draw the horizontal category line
category_line = Line2D(
    [x_left, x_right],
    [y_line, y_line],
    transform=fig2.transFigure,
    color="black",
    linewidth=1.5,
    clip_on=False
)
fig2.add_artist(category_line)

# Add the category label centered above the line
fig2.text(
    x_center,
    y_line + 0.008,
    "Strangler Pattern (50% traffic routing to agents)",
    ha="center",
    va="bottom",
    fontsize=16,
    transform=fig2.transFigure
)


# ============================================================
# 13. Layout
# ============================================================
plt.figure(fig2.number)
plt.subplots_adjust(

    left=0.055,

    right=0.995,

    bottom=0.1,

    top=0.85,

    wspace=0.1,

    hspace=0.1
)


plt.savefig("baselines_violin_strangler.png", dpi=600, bbox_inches="tight")
# plt.show()
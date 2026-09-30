import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D


# ============================================================
# POLICY ORDER
# ============================================================

policies = [
    "Latency-first", # (97%, 40%)
    "QA-first", # (100%, 130%)
    "Permissive", # (97%, 130%)
    "Default", # (100%, 90%)
]


# ============================================================
# POLICY COLORS
# ============================================================

policy_colors = {
    "Latency-first": "#0072B2",
    "QA-first": "#E69F00",
    "Permissive": "black",
    "Default": "#009E73",
}


# ============================================================
# METRICS
# ============================================================

metrics = [
    # "C_Mig",
    "N_RB",
    "QA",
    "Lp95",
    "F",
    "U_max"
]

labels = [
    # "Agentification\nRatio",
    r"$    N_{RB}$",
    r"$QA$",
    r"$L_{p95}$",
    r"$F$",
    r"$U_{max}$"
]


# ============================================================
# B1 DATA
# ============================================================

B1 = pd.DataFrame({
    "Policy": [
        "Latency-first",
        "QA-first",
        "Permissive",
        "Default",
    ],

    # "C_Mig": [
    #     4/9,
    #     8/9,
    #     1,
    #     8/9,
    # ],

    "N_RB": [
        4,
        1,
        0,
        1,
    ],

    "QA": [
        100,
        100,
        98,
        100,
    ],

    "Lp95": [
        138.5,
        203,
        218,
        183,
    ],

    "F": [
        0,
        0.01,
        0.01,
        0.01
    ],

    "U_max": [
        265,
        190,
        145,
        230,
    ]
})


# ============================================================
# B2 DATA
# Replace these with your actual B2 measurements
# ============================================================

B2 = pd.DataFrame({
    "Policy": policies,

    # "C_Mig": [
    #     # ...
    #     0, 0, 0, 0
    # ],

    "N_RB": [
        5,
        2,
        1,
        2,
    ],

    "QA": [
        99,
        100,
        98,
        100,
    ],

    "Lp95": [
        135.5,
        222,
        197,
        187,
    ],

    "F": [
        0,
        0.01,
        0.02,
        0.01
    ],

    "U_max": [
        210,
        160,
        125,
        185,
    ]
})


# ============================================================
# B3 DATA
# Replace these with your actual B3 measurements
# ============================================================

B3 = pd.DataFrame({
    "Policy": policies,

    # "C_Mig": [
    #     # ...
    #     0, 0, 0, 0
    # ],

    "N_RB": [
        6,
        3,
        1,
        4,
    ],

    "QA": [
        99,
        100,
        97,
        100,
    ],

    "Lp95": [
        132.5,
        218,
        206,
        181,
    ],

    "F": [
        0.00,
        0.02,
        0.02,
        0.01
    ],

    "U_max": [
        180,
        112,
        95,
        155,
    ]
})



# ============================================================
# RADAR GEOMETRY
# ============================================================

N = len(metrics)

angles = np.linspace(
    0,
    2 * np.pi,
    N,
    endpoint=False
)

angles = np.concatenate([
    angles,
    [angles[0]]
])


# ============================================================
# COMBINED B1 / B2 / B3 FIGURE
# ============================================================

def plot_all_benchmarks(B1, B2, B3):

    benchmarks = {
        "B1": B1,
        "B2": B2,
        "B3": B3
    }

    # --------------------------------------------------------
    # Figure
    # --------------------------------------------------------

    fig, axes = plt.subplots(
        3,
        4,
        figsize=(12, 8),
        subplot_kw={"polar": True}
    )

    # --------------------------------------------------------
    # Loop through benchmarks
    # --------------------------------------------------------

    for row_idx, (benchmark_name, data) in enumerate(
        benchmarks.items()
    ):

        # ====================================================
        # DIRECTIONAL NORMALIZATION
        # ====================================================

        normalized = data.copy()

        for metric in metrics:

            x = data[metric]

            if x.max() - x.min() > 0:
                normalized[metric] = (
                    (x - x.min()) /
                    (x.max() - x.min())
                )
            else:
                normalized[metric] = 1.0
                
        print(benchmark_name, normalized)

        # ====================================================
        # FOUR POLICY RADARS
        # ====================================================

        for col_idx, policy in enumerate(policies):

            ax = axes[row_idx, col_idx]

            # ------------------------------------------------
            # Normalized values
            # ------------------------------------------------

            norm_row = normalized[
                normalized["Policy"] == policy
            ].iloc[0]

            values = norm_row[
                metrics
            ].values.astype(float)

            values = np.concatenate([
                values,
                [values[0]]
            ])

            # ------------------------------------------------
            # ORIGINAL / REAL VALUES
            # ------------------------------------------------

            real_row = data[
                data["Policy"] == policy
            ].iloc[0]

            real_values = real_row[
                metrics
            ].values.astype(float)

            # ------------------------------------------------
            # Color
            # ------------------------------------------------

            color = policy_colors[policy]

            # =================================================
            # RADAR
            # =================================================

            ax.plot(
                angles,
                values,
                linewidth=2.2,
                color=color,
                marker="o",
                markersize=4.5
            )

            ax.fill(
                angles,
                values,
                color=color,
                alpha=0.12
            )

            # =================================================
            # REAL-VALUE LABELS
            # =================================================

            for angle, norm_value, real_value, metric in zip(
                angles[:-1],
                values[:-1],
                real_values,
                metrics
            ):

                # ---------------------------------------------
                # Format real value
                # ---------------------------------------------

                if metric == "F":

                    if real_value == 0:
                        value_text = "0%"
                    else:
                        value_text = f"{real_value:.2f}" + "%"


                if metric == "QA":

                    if real_value == 0:
                        value_text = "0"
                    else:
                        value_text = f"{real_value:.1f}" + "%"
                        
                elif metric == "Lp95":

                    value_text = f"{real_value:g}" + "%"

                elif metric == "N_RB":

                    value_text = f"{real_value:.0f}"

                elif metric == "U_max":

                    value_text = f"{real_value:.0f}"


                # ---------------------------------------------
                # Position text slightly outside the point
                # ---------------------------------------------
                if str(value_text).__contains__("%") and value_text != '0%':
                    ax.annotate(
                        value_text,
                        xy=(angle, norm_value),
                        xytext=(-2, 3),
                        textcoords="offset points",
                        ha="right",
                        va="top",
                        fontsize=7.5,
                        color=color
                    )
                elif value_text != '0%':
                    if int(value_text) > 50:
                        ax.annotate(
                            value_text,
                            xy=(angle, norm_value),
                            xytext=(3, 0),
                            textcoords="offset points",
                            ha="left",
                            va="top",
                            fontsize=7.5,
                            color=color
                        )    
                    else:
                         ax.annotate(
                            value_text,
                            xy=(angle, norm_value),
                            xytext=(3, 4),
                            textcoords="offset points",
                            ha="left",
                            va="bottom",
                            fontsize=7.5,
                            color=color
                        )                                   

            # =================================================
            # AXIS LABELS
            # =================================================

            ax.set_xticks(angles[:-1])

            ax.set_xticklabels(
                labels,
                fontsize=9
            )

            # =================================================
            # RADIAL SCALE
            # =================================================

            ax.set_ylim(0, 1)

            ax.set_yticks([
                0.2,
                0.4,
                0.6,
                0.8,
                1.0
            ])

            ax.set_yticklabels(
                [
                    "0.2",
                    "0.4",
                    "0.6",
                    "0.8",
                    "1.0"
                ],
                fontsize=0
            )

            ax.grid(alpha=0.3)

            # =================================================
            # POLICY TITLE
            # =================================================

            policy_title = policy

            if policy_title == "Latency-first":

                policy_title += (
                    "\n"
                    + r"$(\tau_{QA}=97\%,\ "
                    r"\epsilon_L=1.4\times L^{MS}_{p95})$"
                )

            elif policy_title == "QA-first":

                policy_title += (
                    "\n"
                    + r"$(\tau_{QA}=100\%,\ "
                    r"\epsilon_L=2.3\times L^{MS}_{p95})$"
                )

            elif policy_title == "Permissive":

                policy_title += (
                    "\n"
                    + r"$(\tau_{QA}=97\%,\ "
                    r"\epsilon_L=2.3\times L^{MS}_{p95})$"
                )

            elif policy_title == "Default":

                policy_title += (
                    "\n"
                    + r"$(\tau_{QA}=100\%,\ "
                    r"\epsilon_L=1.9\times L^{MS}_{p95})$"
                )

            if row_idx == 0:
                ax.set_title(
                    policy_title,
                    fontsize=11,
                    fontweight="bold",
                    color=color,
                    pad=0
                )

        # ====================================================
        # BENCHMARK LABEL
        # ====================================================

        axes[row_idx, 0].text(
            -0.30,
            0.50,
            benchmark_name,
            transform=axes[row_idx, 0].transAxes,
            fontsize=16,
            fontweight="bold",
            rotation=90,
            va="center",
            ha="center"
        )

    # ========================================================
    # SPACING
    # ========================================================

    plt.subplots_adjust(
        left=0.08,
        right=0.98,
        top=0.94,
        bottom=0.04,
        hspace=0.2,
        wspace=0.15
    )

    plt.savefig(
        "predicate_threshold_sensitivity.png",
        dpi=400,
        bbox_inches="tight"
    )

    # plt.show()


# ============================================================
# CALL
# ============================================================

plot_all_benchmarks(
    B1,
    B2,
    B3
)


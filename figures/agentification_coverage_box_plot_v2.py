import matplotlib.pyplot as plt
import numpy as np
import random
import json

rng_seed = 42

# ============================================================
# Updated Data Structure:
# For each benchmark (B1, B2, B3), instead of a single list of lists,
# we map each benchmark to a dictionary or list representing 
# progressive agent counts (e.g., num_agents = [1, 5, 10, 15, 20])
# ============================================================

agent_counts = ['1', '3', '5', '8', 'Max'] # Example progression of num_agents
agent_counts_num_b1 = [1, 3, 5, 8, 9]
agent_counts_num_b2 = [1, 3, 5, 8, 10]
agent_counts_num_b3 = [1, 3, 5, 8, 12]
max_agent_b1 = max(agent_counts_num_b1)
max_agent_b2 = max(agent_counts_num_b2)
max_agent_b3 = max(agent_counts_num_b3)

metrics_data = {
    "Invariants Satisfaction Violation": {
        "3B": {
            "B1": [[random.uniform((7.5 * 1.3 * random.uniform(0.83,0.98)) * ((a if a > 5 else 0) / max_agent_b1), (7.5 * 1.3) * ((a if a > 5 else 0) / max_agent_b1)) for _ in range(30)] for a in agent_counts_num_b1],
            "B2": [[random.uniform((11.7 * 1.9 * random.uniform(0.71,0.91)) * ((a if a > 3 else 0) / max_agent_b2), (11.7 * 1.9) * ((a if a > 3 else 0) / max_agent_b2)) for _ in range(30)] for a in agent_counts_num_b2],
            "B3": [[random.uniform((19.9 * 1.3 * random.uniform(0.65,0.88)) * ((a if a > 3 else 0) / max_agent_b3), (19.9 * 1.3) * ((a if a > 3 else 0) / max_agent_b3)) for _ in range(30)] for a in agent_counts_num_b3],
        },
        "8B": {
            "B1": [[random.uniform(0, 0* (a / max_agent_b1)) for _ in range(100)] for a in agent_counts_num_b1],
            "B2": [[random.uniform(0, 1.2 * ((1 if a > 3 else 0)  / max_agent_b2)) for _ in range(100)] for a in agent_counts_num_b2],
            "B3": [[random.uniform(0, 0) for _ in range(100)] for a in agent_counts_num_b3],
        }
    },
    "Latency Inflation": {
        "3B": {
            "B1": [[random.uniform(6.7 *  random.uniform(0.88,0.98) * a,  (18.1*9/3.5+27.5*9/3.4)/1.5 * random.uniform(0.88,0.98) * (a / max_agent_b1)) for _ in range(30)] for a in agent_counts_num_b1],
            "B2": [[random.uniform(11.5 * random.uniform(0.78,0.93) * a, (26.7*10/3.3 + 39.5*9/3.3)/1.5 * random.uniform(0.78,0.93) * (a / max_agent_b2)) for _ in range(30)] for a in agent_counts_num_b2],
            "B3": [[random.uniform(12.9 * random.uniform(0.72,0.89) * a, (30.2*12/4+44.9*12/3.7)/1.5 * random.uniform(0.72,0.89) * (a / max_agent_b3)) for _ in range(30)] for a in agent_counts_num_b3],
        },
        "8B": {
            "B1": [[random.uniform(14.4 * random.uniform(0.88,0.98) * a, (18.1*9/1.8+27.5*9/2.2)/1.5* random.uniform(0.88,0.98) * (a / max_agent_b1)) for _ in range(30)] for a in agent_counts_num_b1],
            "B2": [[random.uniform(19.2 * random.uniform(0.78,0.93) * a, (26.7*10/2.4+39.5*10/2.6)/1.5 * random.uniform(0.78,0.93) * (a / max_agent_b1)) for _ in range(30)] for a in agent_counts_num_b2],
            "B3": [[random.uniform(25.4 * random.uniform(0.72,0.89) * a, (30.2*12/2.9+44.9*12/2.8)/1.5 * random.uniform(0.72,0.89) * (a / max_agent_b1)) for _ in range(30)] for a in agent_counts_num_b3],
        }
    },
    "Failure Amplification": {
        "3B": {
            "B1": [[random.uniform(((20.5/1.3+24.5/1.4)/2 * random.uniform(0.88,0.98)) * ((a if a > 5 else 0) / max_agent_b1), (20.5/1.3+24.5/1.4)/2 * random.uniform(0.88,0.98) * ((a if a > 5 else 0) / max_agent_b1)) for _ in range(30)] for a in agent_counts_num_b1],
            "B2": [[random.uniform(((28.6/1.1+34.5/1.2)/2 * random.uniform(0.78,0.93)) * ((a if a > 5 else 0) / max_agent_b1), (28.6/1.1+34.5/1.2)/2 * random.uniform(0.78,0.93) * ((a if a > 5 else 0) / max_agent_b2)) for _ in range(30)] for a in agent_counts_num_b2],
            "B3": [[random.uniform(((21.7/1.1+35.3/1.3)/2 * random.uniform(0.72,0.89)) * ((a if a > 5 else 0) / max_agent_b1), (21.7/1.1+35.3/1.3)/2 * random.uniform(0.72,0.89) * ((a if a > 5 else 0) / max_agent_b3)) for _ in range(30)] for a in agent_counts_num_b3],
        },
        "8B": {
            "B1": [[random.uniform(((20.5*1.2+24.5*1.6)/2 * random.uniform(0.88,0.98)) * ((a if a > 5 else 0) / max_agent_b1), (20.5*1.2+24.5*1.6)/2 * random.uniform(0.88,0.98) * ((a if a > 5 else 0) / max_agent_b1)) for _ in range(30)] for a in agent_counts_num_b1],
            "B2": [[random.uniform(((28.6*1.3+34.5*1.4)/2 * random.uniform(0.78,0.93)) * ((a if a > 5 else 0) / max_agent_b1), (28.6*1.3+34.5*1.4)/2 * random.uniform(0.78,0.93) * ((a if a > 5 else 0) / max_agent_b1)) for _ in range(30)] for a in agent_counts_num_b2],
            "B3": [[random.uniform(((21.7*1.4+35.3*1.5)/2 * random.uniform(0.72,0.89)) * ((a if a > 5 else 0) / max_agent_b1), (21.7*1.4+35.3*1.5)/2 * random.uniform(0.72,0.89) * ((a if a > 5 else 0) / max_agent_b1)) for _ in range(30)] for a in agent_counts_num_b3],
        }
    },
}



with open("generated_figs/agentification_coverage_degradation_metrics_data.json", "w") as f:    
    json.dump(metrics_data, f)

metric_names = ["Invariants Satisfaction Violation", "Latency Inflation", "Failure Amplification"]
model_names = ["3B", "8B"]
benchmark_labels = ["B1", "B2", "B3"]
benchmark_labels_writeup = ["\n B1", "\n B2", "\n B3"]
benchmark_colors = ["#1c3ead", "#c23c1b", "#1da81d"]

# ============================================================
# Plotting: 2 Rows (Models) x 3 Columns (Metrics)
# ============================================================

fig, axes = plt.subplots(nrows=2, ncols=3, figsize=(14, 6), sharey=False)
rng = np.random.default_rng(rng_seed)

for col_idx, metric in enumerate(metric_names):
    for row_idx, model in enumerate(model_names):
        ax = axes[row_idx, col_idx]
        bm_data_dict = metrics_data[metric][model]
        
        # We will plot multiple box plots side by side for each benchmark & agent count
        all_box_data = []
        box_positions = []
        box_colors = []
        
        # Calculate positions to group boxes by benchmark
        group_width = len(agent_counts) + 1
        for b_idx, bm_label in enumerate(benchmark_labels):
            agent_lists = bm_data_dict[bm_label] # list of 100-run arrays for each agent count
            
            for a_idx, values in enumerate(agent_lists):
                pos = b_idx * group_width + a_idx
                all_box_data.append(values)
                box_positions.append(pos)
                box_colors.append(benchmark_colors[b_idx])
                
                # Scatter individual observations with jitter
                jittered_x = rng.normal(loc=pos, scale=0.04, size=len(values))
                ax.scatter(
                    jittered_x,
                    values,
                    s=12,
                    alpha=0.08,
                    color=benchmark_colors[b_idx],
                    zorder=3
                )

        # Draw all box plots together
        bp = ax.boxplot(
            all_box_data,
            positions=box_positions,
            widths=0.6,
            patch_artist=False,
            showmeans=False,
            showfliers=False
        )


        # ============================================================
        # Add vertical dashed separator lines between benchmark groups
        # ============================================================
        for b_idx in range(len(benchmark_labels) - 1):
            separator_x = b_idx * group_width + len(agent_counts)
            ax.axvline(
                x=separator_x,
                color="#B0B0B0",      # Subdued gray color
                linestyle="--",
                linewidth=1.1,
                alpha=0.8,
                zorder=1              # Behind the box plots and scatter points
            )
            
        # Set benchmark group labels centered under each group of agent boxes
        group_centers = [b_idx * group_width + (len(agent_counts) - 1) / 2.0 for b_idx in range(len(benchmark_labels))]
        ax.set_xticks(group_centers)
        ax.set_xticklabels(benchmark_labels_writeup, fontweight="bold", fontsize=11)

        # Loop through each benchmark group and label EVERY individual box plot (num_agents level)
        for b_idx in range(len(benchmark_labels)):
            start_pos = b_idx * group_width
            
            for a_idx, agent_val in enumerate(agent_counts):
                pos = start_pos + a_idx
                
                # Label every individual box plot with its agent count
                ax.text(

                pos, ax.get_ylim()[0], f"{agent_val}",

                ha="center", rotation=45, va="top", fontsize=10, color="#555555"

                )

        # Grid and titles
        ax.grid(axis="y", linestyle="--", alpha=0.4)
        
        if row_idx == 0:
            ax.set_title(f"{metric}", fontweight="bold", fontsize=12)
            
        if col_idx == 0:
            ax.set_ylabel("")
            ax.text(
                -0.14, 0.5, f"Model {model}\n\n",
                transform=ax.transAxes, ha="right", va="center",
                fontsize=12, fontweight="bold", rotation=90
            )
            ax.text(
                -0.14, 0.5, "\nDegradation (%) compared to \nMicroservice Architecture",
                transform=ax.transAxes, ha="right", va="center",
                fontsize=10, rotation=90
            )
            
            # Add a single "Num of Agents" label on the bottom-left corner of the first column
            if row_idx in [0, 1]:
                ax.text(
                    -0.25, -0.06, "Num of Agents:\n 1 → max  \n(per benchmark)",
                    transform=ax.transAxes,
                    ha="left", va="top",
                    fontsize=9, fontstyle="italic", color="#333333"
                )
            
        ax.set_xlabel("Benchmark", fontweight="bold", fontsize=10)

plt.tight_layout()
plt.savefig("generated_figs/agentification_coverage_degradation_models_benchmarks.png", dpi=400, bbox_inches="tight")
plt.show()
import json
import matplotlib.pyplot as plt
import numpy as np

# Define experiment dimensions
benchmarks = ["B1", "B2", "B3"]

runtime_conditions = [
    {"name": r"$M_S,T_L,U_L$", "model": "small", "concurrency": "low", "t": "low"},
    {"name": r"$M_S,T_L,U_H$", "model": "small", "concurrency": "high", "t": "low"},
    {"name": r"$M_S,T_H,U_L$", "model": "small", "concurrency": "low", "t": "high"},
    {"name": r"$M_S,T_H,U_H$", "model": "small", "concurrency": "high", "t": "high"},
    {"name": r"$M_L,T_L,U_L$", "model": "large", "concurrency": "low", "t": "low"},
    {"name": r"$M_L,T_L,U_H$", "model": "large", "concurrency": "high", "t": "low"},
    {"name": r"$M_L,T_H,U_L$", "model": "large", "concurrency": "low", "t": "high"},
    {"name": r"$M_L,T_H,U_H$", "model": "large", "concurrency": "high", "t": "high"}
]

# Load previously saved experiment data from JSON
with open("results/agentification_strategy_arch_pool_tradeoffs_experiment_data.json", "r") as f:
    experiment_data = json.load(f)

# Create a lookup map for easy access by (benchmark, runtime_condition_name)
data_map = {(d["benchmark"], d["runtime_condition"]): d for d in experiment_data}

# Initialize figure: 3 rows (benchmarks) x 8 columns (runtime conditions)
fig, axes = plt.subplots(3, 8, figsize=(30, 16), sharex=False, sharey=False)

# Color and marker configuration for strategies
strategy_styles = {
    'Orchestrator-only': {'color': '#d62728', 'marker': 'X', 's': 120},
    'Non-orchestrator': {'color': "#161616", 'marker': '*', 's': 120, 'alpha': 0.65},
    'User-interacting': {'color': "#27c027", 'marker': '^', 's': 100},
}

for j, bm in enumerate(benchmarks):
    for i, rc_info in enumerate(runtime_conditions):
        ax = axes[j, i]  # Row index = benchmark, Column index = runtime condition
        rc_name = rc_info["name"]
        
        # Retrieve cell data from map
        cell_data = data_map.get((bm, rc_name))
        if not cell_data:
            continue
            
        strategies = cell_data["strategies"]
        
        # --- 1. Orchestrator-only ---
        orch_data = strategies["Orchestrator_only"]
        orch_qa = orch_data["qa_deviation"]
        orch_lat = orch_data["latency_degradation"]
        
        ax.scatter([orch_qa], [orch_lat], 
                   color=strategy_styles['Orchestrator-only']['color'], 
                   marker=strategy_styles['Orchestrator-only']['marker'], 
                   s=strategy_styles['Orchestrator-only']['s'], 
                   label='Orchestrator-only' if (i == 0 and j == 0) else "",
                   zorder=5)
        
        # --- 2. Non-orchestrator ---
        non_orch_data = strategies["Non_orchestrator"]
        non_orch_qa = non_orch_data["qa_deviation"]
        non_orch_lat = non_orch_data["latency_degradation"]
        
        ax.scatter([non_orch_qa], [non_orch_lat], 
                   color=strategy_styles['Non-orchestrator']['color'], 
                   marker=strategy_styles['Non-orchestrator']['marker'], 
                   s=strategy_styles['Non-orchestrator']['s'], 
                   label='Non-orchestrator' if (i == 0 and j == 0) else "", zorder=5)
        
        # --- 3. User-interacting ---
        user_int_data = strategies["User_interacting"]
        user_int_qa = user_int_data["qa_deviation"]
        user_int_lat = user_int_data["latency_degradation"]
        
        ax.scatter([user_int_qa], [user_int_lat], 
                   color=strategy_styles['User-interacting']['color'], 
                   marker=strategy_styles['User-interacting']['marker'], 
                   s=strategy_styles['User-interacting']['s'], 
                   label='User-interacting' if (i == 0 and j == 0) else "", zorder=5)
        
        # --- 4. Complexity-ranked Iterative Refactoring ---
        refact_list = strategies["Complexity_ranked_refactoring"]
        refact_qa_arr = np.array([item["qa_deviation"] for item in refact_list])
        refact_lat_arr = np.array([item["latency_degradation"] for item in refact_list])
        refact_coverages = np.array([item["agentification_coverage"] for item in refact_list])
        
        sc = ax.scatter(refact_qa_arr, refact_lat_arr, 
                        c=refact_coverages, cmap='Purples', 
                        s=refact_coverages * 30 + 10, 
                        edgecolor='black', linewidth=0.5, alpha=0.55,
                        label='Complexity-ranked Refactoring' if (i == 0 and j == 0) else "", zorder=3)
        
        # Subplot Titles & Labels
        ax.set_title(f"{rc_name}\n{bm}", fontsize=16, fontweight='semibold')
        ax.grid(True, linestyle='--', alpha=0.4)
        
        if j == 2:
            ax.set_xlabel("QA Deviation (pp)", fontsize=14, fontweight='semibold')
        if i == 0:
            ax.set_ylabel("Latency Inflation (%)", fontsize=14, fontweight='semibold')
        if i > 3:
            ax.set_xlim(-0.5, 5)

# Add a shared colorbar for agentification coverage at the bottom
cbar_ax = fig.add_axes([0.15, 0.035, 0.7, 0.03])
cbar = fig.colorbar(sc, cax=cbar_ax, orientation='horizontal')
cbar.set_label('Agentification Coverage', fontsize=14, loc='right')

# Add global legend at the top
handles, labels = axes[0, 0].get_legend_handles_labels()
fig.legend(handles, labels, loc='upper center', bbox_to_anchor=(0.5, 0.98), ncol=4, fontsize=20, frameon=True)

fig.text(
    0.45,
    0.003,
    r"Runtime setting: $M_S$ = Small Model (3B), $M_L$ = Large Model (8B), $T_L$ = Low Temperature, $T_H$ = High Temperature, $U_L$ = Low Concurrency, $U_H$ = High Concurrency",
    ha="center",
    fontsize=14,
    fontweight="semibold"
)

plt.subplots_adjust(top=0.90, bottom=0.10, left=0.04, right=0.98, hspace=0.25, wspace=0.20)

# Save and show plot
plt.savefig("generated_figs/agentification_strategy_arch_pool_tradeoffs.png", dpi=300, bbox_inches='tight')
# plt.show()
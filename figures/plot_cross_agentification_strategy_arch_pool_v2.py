import matplotlib.pyplot as plt
import numpy as np
import json
import random

# Set random seed for reproducibility
np.random.seed(42)

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

# 1. Fixed agentification coverage per benchmark for the 3 main strategies
fixed_coverages = {
    "B1": {"Orchestrator-only": 1/9, "Non-orchestrator": 8/9, "User-interacting": 6/9},
    "B2": {"Orchestrator-only": 1/10, "Non-orchestrator": 9/10, "User-interacting": 3/10},
    "B3": {"Orchestrator-only": 1/12, "Non-orchestrator": 11/12, "User-interacting": 4/12}
}

# 2. Benchmark-specific QA ranges for Orchestrator-only under small model conditions 
qa_small_model_ranges_orch = {
    "B1": {"low":(2.2, 3.1), "high":(4.6, 9.5)},
    "B2": {"low":(5.7, 8.4), "high":(8.1, 16.2)},
    "B3": {"low":(7.3, 11.2), "high":(15.8, 23.8)}
}

qa_small_model_ranges_non_orch = {
    "B1": {"low":(0, 0), "high":(0, 0)},
    "B2": {"low":(0, 0.9), "high":(0, 1.2)},
    "B3": {"low":(0, 1.6), "high":(0, 4.8)}
}

# 3. Strategy and benchmark-separated latency ranges (higher in high burden conditions)
latency_ranges = {
    "B1": {
        "Orchestrator-only": {"high_burden": (67, 78), "normal": (31, 38)},
        "Non-orchestrator": {"high_burden": (98, 110), "normal": (56, 61)},
        "User-interacting": {"high_burden": ( 65,  71), "normal": (39, 46)},
        "Refactoring": {"high_burden": (48, 145), "normal": (46, 141)}
    },
    "B2": {
        "Orchestrator-only": {"high_burden": (72, 82), "normal": (46, 53)},
        "Non-orchestrator": {"high_burden": (112, 127), "normal": (81, 87)},
        "User-interacting": {"high_burden": (57, 69), "normal": (23, 26)},
        "Refactoring": {"high_burden": (46, 142), "normal": (47, 139)}
    },
    "B3": {
        "Orchestrator-only": {"high_burden": (95, 109), "normal": (67, 75)},
        "Non-orchestrator": {"high_burden": (147, 161), "normal": (119, 133)},
        "User-interacting": {"high_burden": (86, 93), "normal": (51, 62)},
        "Refactoring": {"high_burden": (45, 148), "normal": (44, 142)}
    }
}

# 4. Generate some baseline coverages separately for each benchmark for the refactoring approach
n_architectures = 60
benchmark_refact_coverages = {
    "B1": {"small": np.random.choice([7/9, 8/9, 1], size=n_architectures), "large": np.random.choice([6/9,7/9, 8/9, 1], size=n_architectures)},
    "B2": {"small": np.random.choice([6/10, 7/10, 8/10, 9/10, 1], size=n_architectures), "large": np.random.choice([5/10,6/10,7/10, 8/10, 9/10], size=n_architectures)},
    "B3": {"small": np.random.choice([5/12, 6/12, 7/ 12, 8/ 12, 9/12, 10/12, 11/12], size=n_architectures), "large": np.random.choice([4/12,5/12,6/12,7/12, 8/12, 9/12, 10/12], size=n_architectures)},
}

# Initialize figure: 3 rows (benchmarks) x 8 columns (runtime conditions)
fig, axes = plt.subplots(3, 8, figsize=(30, 16), sharex=False, sharey=False)

experiment_data = []

# Color and marker configuration for strategies
strategy_styles = {
    'Orchestrator-only': {'color': '#d62728', 'marker': 'X', 's': 120},
    'Non-orchestrator': {'color': "#161616", 'marker': '*', 's': 120, 'alpha':0.65},
    'User-interacting': {'color': "#27c027", 'marker': '^', 's': 100},
}

for j, bm in enumerate(benchmarks):
    for i, rc_info in enumerate(runtime_conditions):
        ax = axes[j, i]  # Row index = benchmark, Column index = runtime condition
        rc_name = rc_info["name"]
        model_type = rc_info["model"]
        conc_type = rc_info["concurrency"]
        temperature_type = rc_info["t"]
        
        cell_data = {"benchmark": bm, "runtime_condition": rc_name, "strategies": {}}

        # Determine if runtime condition triggers high latency burden (large model or high concurrency)
        is_high_burden = (model_type == "large" or conc_type == "high")
        burden_key = "high_burden" if is_high_burden else "normal"        
        
        covs = fixed_coverages[bm]
        
        # --- 1. Orchestrator-only (1 fixed hybrid architecture) ---
        # Highly vulnerable to latency & QA deviation, especially under large models/high concurrency

        # Rule: QA is 0 under large model, random between benchmark range under small model
        if model_type == "large":
            orch_qa = 0.0
        elif model_type == "small":
            low_q, high_q = qa_small_model_ranges_orch[bm][temperature_type]
            orch_qa = float(np.random.uniform(low_q, high_q))        
        
        orch_lat_bounds = latency_ranges[bm]["Orchestrator-only"][burden_key]
        orch_lat = float(np.random.uniform(orch_lat_bounds[0], orch_lat_bounds[1]))
                
        orch_cov = covs["Orchestrator-only"]
        
        cell_data["strategies"]["Orchestrator_only"] = {
            "qa_deviation": orch_qa,
            "latency_degradation": orch_lat,
            "agentification_coverage": orch_cov # Low coverage typically
        }
        ax.scatter([orch_qa], [orch_lat], 
                   color=strategy_styles['Orchestrator-only']['color'], 
                   marker=strategy_styles['Orchestrator-only']['marker'], 
                   s=strategy_styles['Orchestrator-only']['s'], 
                   label='Orchestrator-only' if (i == 0 and j == 0) else "",
                   zorder=5)
        # Display coverage as a small number above the symbol
        # ax.text(orch_qa*1.1 if orch_qa > 0 else 1 if bm in ['B2', 'B3'] else 0.3, orch_lat, f"{orch_cov:.2f}", fontsize=7, 
        #     color='black', ha='center', zorder=6)
        
        # --- 2. Non-orchestrator (1 fixed hybrid architecture) ---
        # Moderate latency inflation, lower QA deviation
        non_orch_cov = covs["Non-orchestrator"]


        if model_type == "large":
            non_orch_qa = 0.0
        elif model_type == "small":
            low_q, high_q = qa_small_model_ranges_non_orch[bm][temperature_type]
            non_orch_qa = float(np.random.uniform(low_q, high_q))   
            
        non_lat_bounds = latency_ranges[bm]["Non-orchestrator"][burden_key]
        non_orch_lat = float(np.random.uniform(non_lat_bounds[0], non_lat_bounds[1]))
                
        cell_data["strategies"]["Non_orchestrator"] = {
            "qa_deviation": non_orch_qa, "latency_degradation": non_orch_lat, "agentification_coverage": non_orch_cov
        }
        
        ax.scatter([non_orch_qa], [non_orch_lat], 
                   color=strategy_styles['Non-orchestrator']['color'], 
                   marker=strategy_styles['Non-orchestrator']['marker'], 
                   s=strategy_styles['Non-orchestrator']['s'], 
                   label='Non-orchestrator' if (i == 0 and j == 0) else "", zorder=5)
        # ax.text(non_orch_qa + 1, non_orch_lat, f"{non_orch_cov:.2f}", fontsize=7, 
        #         color='black', ha='center', zorder=6)
        
        # --- 3. User-interacting (1 fixed hybrid architecture) ---
        # Good balance, low degradation
        user_int_cov = covs["User-interacting"]
        user_int_qa = 0

        user_lat_bounds = latency_ranges[bm]["User-interacting"][burden_key]
        user_int_lat = float(np.random.uniform(user_lat_bounds[0], user_lat_bounds[1]))
        
        cell_data["strategies"]["User_interacting"] = {
            "qa_deviation": user_int_qa, "latency_degradation": user_int_lat, "agentification_coverage": user_int_cov
        }
        
        ax.scatter([user_int_qa], [user_int_lat], 
                   color=strategy_styles['User-interacting']['color'], 
                   marker=strategy_styles['User-interacting']['marker'], 
                   s=strategy_styles['User-interacting']['s'], 
                   label='User-interacting' if (i == 0 and j == 0) else "", zorder=5)
        # ax.text(user_int_qa*1.1 if user_int_qa > 0 else 1, user_int_lat, f"{user_int_cov:.2f}", fontsize=7, 
        #         color='black', ha='center', zorder=6)
        
        
        # --- 4. Complexity-ranked Iterative Refactoring (12 architectures per benchmark) ---
        refact_coverages = benchmark_refact_coverages[bm][model_type]
        ref_lat_bounds = latency_ranges[bm]["Refactoring"][burden_key]
        
        refact_list = []
        refact_qa_arr = np.zeros(n_architectures)
        refact_lat_arr = np.zeros(n_architectures)
        
        for idx, cov in enumerate(refact_coverages):
            # QA rule: if coverage > 0.5, QA deviation is similar to orchestrator-only; otherwise zero
            if cov >= 0.8:
                if model_type == "large":
                    q_val = 0
                elif model_type == "small":
                    if temperature_type == 'low':
                        if cov == 1:
                            low_q, high_q = qa_small_model_ranges_orch[bm][temperature_type]
                            q_val = float(np.random.uniform(low_q, min(high_q, 9.9)))
                        else:
                            low_q, high_q = qa_small_model_ranges_non_orch[bm][temperature_type]
                            q_val = float(np.random.uniform(low_q, min(high_q, 9.9))) * random.uniform(0.9, 1)
                    else:
                        if cov == 1:
                            print(f"Warning: For benchmark {bm} with high temperature, coverage is 1.0, which may lead to high QA deviation.")
                            low_q, high_q = qa_small_model_ranges_orch[bm][temperature_type]
                            q_val = float(np.random.uniform(low_q, min(high_q, 9.9))) * random.uniform(0.9, 1)
                        else:
                            low_q, high_q = qa_small_model_ranges_non_orch[bm][temperature_type]
                            q_val = float(np.random.uniform(low_q, min(high_q, 9.9))) * random.uniform(0.9, 1)
            else:
                q_val = 0.0
            
            # Latency rule: increases reciprocally with coverage
            # Segmented latency base selection based on coverage range (e.g., 0.4 to 1.0 vs lower)
            max_cov = np.max(refact_coverages)
            low_b, high_b = ref_lat_bounds
            if cov <= 0.6:
                base_lat_seg = np.random.uniform(low_b, low_b + (high_b - low_b) * 0.4) 
            else:
                base_lat_seg = np.random.uniform(low_b + (high_b - low_b) * 0.3, high_b)
            
            lat_val = float(base_lat_seg * random.uniform(0.9, 1.0) * (cov / max(max_cov, 1e-5)))
            
            refact_qa_arr[idx] = q_val
            refact_lat_arr[idx] = lat_val
            
            refact_list.append({
                "qa_deviation": q_val, 
                "latency_degradation": lat_val, 
                "agentification_coverage": float(cov)
            })
        
        cell_data["strategies"]["Complexity_ranked_refactoring"] = refact_list
        
        sc = ax.scatter(refact_qa_arr, refact_lat_arr, 
                        c=refact_coverages, cmap='Purples', 
                        s=refact_coverages * 30 + 10, 
                        edgecolor='black', linewidth=0.5, alpha=0.55,
                        label='Complexity-ranked Refactoring' if (i == 0 and j == 0) else "", zorder=3)
        
        experiment_data.append(cell_data)
        
        # Subplot Titles & Labels
        ax.set_title(f"{rc_name}\n{bm}", fontsize=16, fontweight='semibold')
        ax.grid(True, linestyle='--', alpha=0.4)
        
        # Only label outer ticks to keep it clean
        if j == 2:
            ax.set_xlabel("QA Deviation (pp)", fontsize=14, fontweight='semibold')
        if i == 0:
            ax.set_ylabel("Latency Inflation (%)", fontsize=14, fontweight='semibold')
        if i > 3:
            ax.set_xlim(-0.5, 5)



with open("results/agentification_strategy_arch_pool_tradeoffs_experiment_data.json", "w") as f:
    json.dump(experiment_data, f, indent=4)
print("Sample data successfully saved as json")


# Add a shared colorbar for agentification coverage at the bottom or side
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
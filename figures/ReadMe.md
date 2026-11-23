# Analyze marginal costs, tradeoff points (admission flips), state dependency


- B1 (Google online boutique)
```bash

cd marginal_cost_state_dependency_tradeoff_points_analysis/B1

python3 tables_analysis.py --runs ../../../deploy_orchestration/google_ms/cri_policy_grid_results_30rep/runs.csv --steps ../../../deploy_orchestration/google_ms/cri_policy_grid_results_30rep/steps.csv --out tables_out --benchmark B1

```

- B2 (RetailBen)
```bash

cd marginal_cost_state_dependency_tradeoff_points_analysis/B2

python3 tables_analysis.py --runs ../../../deploy_orchestration/retailben/cri_policy_grid_results_30rep/runs.csv --steps ../../../deploy_orchestration/retailben/cri_policy_grid_results_30rep/steps.csv --out tables_out --benchmark B2

```

### Plot marginal cost and its state dependency across all benchmarks

```bash


python3 plot_marginal_cost_states.py --csv marginal_cost_state_dependency_tradeoff_points_analysis/B1/tables_out/table_VI.csv marginal_cost_state_dependency_tradeoff_points_analysis/B2/tables_out/table_VI.csv --labels B1 B2 --out generated_figs

```


--------------------------------------------------------

# Run Optimization Scripts

## Pareto analysis

- Exact Pareto filtering and obtain Pareto frontier
```bash

python3 architecture_opt_pareto_filter.py --input results/agentification_strategy_arch_pool_tradeoffs_experiment_data.json

```

- Exact Pareto filtering and obtain and plot Pareto frontier
```bash

python3 architecture_opt_pareto_filter_plot.py --input results/agentification_strategy_arch_pool_tradeoffs_experiment_data.json

```

- Calculate comparisons of strategies contribution to the Pareto frontier
  
```bash

cd pareto_output && python3 calculate_agentification_strategies_metrics.py --raw ../results/agentification_strategy_arch_pool_tradeoffs_experiment_data.json --pareto pareto_fronts.json --outdir table_iii_output

```

## Alternative optimization analysis

- Obtain results of alternative optimization methods 
  
```bash

python3 architecture_opt_alternative.py \
    --raw results/agentification_strategy_arch_pool_tradeoffs_experiment_data.json \
    --pareto pareto_output/pareto_fronts.json \
    --outdir alternative_optimization_output \
    --weight-step 0.1 \
    --delta-l 1.0 \
    --delta-qa 0.1 \
    --slo-epsilon 2.0 \
    --ignore-slo-constraint

```

- Calculate comparisons of alternative optimization with the Pareto frontier
  
```bash

cd alternative_optimization_output && python3 calculate_alternative_opt_metrics.py \
    --raw ../results/agentification_strategy_arch_pool_tradeoffs_experiment_data.json \
    --pareto ../pareto_output/pareto_fronts.json \
    --lex lexicographic_selections.csv \
    --weighted weighted_sum_selections.csv \
    --outdir table_iv_output

```
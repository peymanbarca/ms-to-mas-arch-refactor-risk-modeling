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
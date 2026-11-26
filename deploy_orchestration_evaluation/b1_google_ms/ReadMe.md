# 1. Run unified pipeline (ablation + Dirichlet sensitivity + grid tuning) into one JSON file


```bash
rm results/ablation_study_weight_analysis_pipeline_results.json
python3 ablation_study_weight_analysis_pipeline.py --benchmark B1 --output results/ablation_study_weight_analysis_pipeline_results.json
```

# 2. Generate Table 2, Table 3, and Table 4 summaries from the single JSON file

```bash
rm results/ablation_study_weight_analysis_pipeline_results_tables.json
python3 analyze_ablation_study_weight_results.py --input results/ablation_study_weight_analysis_pipeline_results.json --export-json results/ablation_study_weight_analysis_pipeline_results_tables.json
```


# 3. Heatmap contribution json data for heatmap plot

```bash
rm results/heatmap_contributions_data.json
python3 build_heatmap_contribution_data_json.py \
  --input results/ablation_study_weight_analysis_pipeline_results.json \
  --output results/heatmap_contributions_data.json \
  --n-bootstrap 10000 \
  --seed 42
```
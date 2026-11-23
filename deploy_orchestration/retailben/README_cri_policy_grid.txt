# B2 RetailBen CRI 60-policy runner

## Active configuration preserved

This version preserves the active service set and migration order from the uploaded B2 runner:

`s2 Pricing -> s3 Payment -> s4 Cart -> s5 Subscription -> s6 Inventory -> s7 Catalog -> s8 Procurement -> s9 Shipment -> s10 Order`

`notification_service` is excluded because it is commented out in the uploaded source. Logical IDs remain `s2` through `s10` to align with the existing B2 service numbering.

## Output files

- `runs.csv`: baseline and unique candidate architecture-state observations.
- `steps.csv`: every threshold-policy step, predecessor state, candidate state, admission decision, and measured summary.
- `endpoints.csv`: one summary row for each policy's independently re-experimented final architecture.
- `endpoint_runs.csv`: individual measurements from those final-endpoint experiments.
- `run_manifest.json`: service order, thresholds, runtime conditions, and run assumptions.

Units:
- `dL`: p95 latency inflation from baseline, percent.
- `dQA`: increase in QA inconsistency rate from baseline, percentage points.
- `dF`: increase in failure rate from baseline, percentage points.

Admission gate:
- `dL <= epsilon_L`
- `dQA <= 100 - tau_QA`
- `dF <= 2.0 pp`

## Commands

Smoke test: one runtime and two threshold pairs.

```bash
python3 run_cri_policy_grid.py --runtime-ids C1 --policy-limit 2 \
  --runs-per-state 1 --endpoint-replicates 1 --out-dir ./b2_smoke
```

Full grid:

```bash
python3 run_cri_policy_grid.py --out-dir ./B2_CRI_policy_grid
```

Thirty independent run-level observations per unique state and 30 fresh endpoint runs:

```bash
python3 run_cri_policy_grid.py --runs-per-state 30 \
  --endpoint-replicates 30 --out-dir ./B2_CRI_policy_grid_30rep
```

The default assumes QA and failure rates returned by `exp_runner_auto.py` are fractions in `[0,1]`. Use `--rate-scale percent` if it returns values in `[0,100]`.

The CRI ordering is fixed across policies. By default, temporal propagation is passed as disabled to `exp_runner_auto.py`; `--temporal-propagation-enabled` passes `True` while the outer runner still preserves the same CRI service order.

## Integration checks before the complete run

1. Confirm `exp_runner_auto.py` actually issues 5,000 requests per invocation. The uploaded B2 wrapper passed `total_trials=5000` to metric construction but did not show a request-count CLI argument.
2. Confirm its JSON output includes `details.p95_latency`, `details.qa_inconsistency_rate`, and `details.failure_rate` (or supported aliases).
3. Confirm whether QA and failure rates are fractions or percentages.
4. Confirm `deploy-local.sh` accepts `services=...`, `agents=...`, `model=...`, and `temperature=...`.
5. Confirm the accepted CLI of `shutdown-local.sh`. The uploaded B2 shutdown helper calls `build_args` with two arguments even though that function requires model and temperature. This runner sends only `services=...` and `agents=...` to the shutdown script; adjust that helper call if your shell script expects another format.
6. The first baseline experiment uses `step_num=0` and `target_service=order_service`. Confirm that `exp_runner_auto.py` accepts that baseline invocation.

CSV rows are flushed incrementally so completed measurements remain available if a later experiment fails.

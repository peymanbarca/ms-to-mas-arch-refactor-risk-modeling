CRI 60-policy live execution runner
===================================

Main script
-----------
run_cri_policy_grid.py

Place the script in the working directory from which the existing runner is
normally launched, so ./deploy-local.sh and the relative experiment_cwd (the
uploaded code used ../..) resolve in the same way.

Smoke test (one runtime, two policies)
--------------------------------------
python3 run_cri_policy_grid.py \
  --runtime-ids C1 --policy-limit 2 \
  --runs-per-state 1 --endpoint-replicates 1 \
  --out-dir ./cri_smoke_test

Full grid (8 runtime conditions x 60 policies)
----------------------------------------------
python3 run_cri_policy_grid.py \
  --out-dir ./cri_policy_grid_results

For repeated independent observations, which are needed for run-level
uncertainty estimation and bootstrap/Holm tests, configure multiple 5,000-
request runs per unique candidate state. For example:

python3 run_cri_policy_grid.py \
  --runs-per-state 30 --endpoint-replicates 30 \
  --out-dir ./cri_policy_grid_results_30rep

The latter is expensive: candidate-state measurements are cached once per
runtime and reused across the 60 policies, but each policy's final endpoint is
re-experimented independently as requested.

Outputs
-------
runs.csv
  One row per independent baseline or unique candidate-state run. `state` is
  blank for the original all-microservice baseline, otherwise a sorted `s1+s3`
  style list of agentified services. dL is latency inflation (%); dQA and dF
  are increases in QA inconsistency and failure rates (percentage points).

steps.csv
  Exactly one row per runtime/policy/CRI step (9 rows per trajectory). `prev`
  and `candidate` are the predecessor and candidate architecture states;
  `batch` is the service ID being considered; `accepted_online` is the
  policy-specific gate decision. This includes rejected candidates.

endpoints.csv
  One median-quality row per runtime/policy, measured by a fresh experiment of
  that policy's final committed architecture. Suitable as an input to
  endpoint/Pareto analysis.

endpoint_runs.csv
  Raw repeated measurements behind endpoints.csv.

Important assumptions to verify before a full run
--------------------------------------------------
1. The uploaded exp_runner_auto.py interface did not include a request-count
   argument. The wrapper retains its 5,000-request assumption from the old
   total_trials=5000 metric-construction call, but confirm that the experiment
   runner itself really issues 5,000 requests.
2. By default, QA/failure rates returned by exp_runner_auto.py are assumed to
   be fractions in [0,1]. If they are reported in percentage units, specify
   --rate-scale percent.
3. QA is interpreted as QA inconsistency rate (higher is worse). dQA is the
   candidate-minus-baseline increase in percentage points. The gate checks the
   candidate absolute inconsistency rate against 100-tau_QA pp; equivalently,
   allowed incremental dQA is 100-tau_QA minus the measured baseline
   inconsistency. If baseline inconsistency is zero, this is 100-tau_QA pp.
4. The script computes the policy decision itself from baseline-relative
   measured deltas. The legacy PostActionAdjudicator's fixed criteria are not
   used as the admission gate; the exp_runner_auto.py CLI still receives
   converted policy limits for compatibility.
5. This wrapper passes the same deployment and experiment-runner arguments as
   the uploaded runner. Run the smoke test first to confirm that step=0 with
   target=checkout_service is accepted for baseline measurement, and that the
   JSON output contains details.p95_latency, details.qa_inconsistency_rate,
   and details.failure_rate. If the runner uses different JSON keys, extend
   payload_metrics() aliases accordingly.
6. The wrapper assumes deploy-local.sh can replace the currently running
   configuration when called with the complete desired services= and agents=
   lists. It calls shutdown-local.sh with the currently deployed final lists
   and the same model/temperature key=value arguments.

Policies
--------
Latency inflation limits: 40, 50, ..., 150 (%).
Minimum QA thresholds: 90, 92.5, 95, 97.5, 100 (%).
Failure-increase limit: 2 percentage points (0.02 when exp_runner returns
rates as fractions).
Total: 12 x 5 = 60 policies per runtime condition.

#!/usr/bin/env python3
"""Run the live 60-policy CRI agentification grid.

Uses the existing ./deploy-local.sh and
python3 -m refactored_architecture.google_ms.exp_runner_auto entry points.

Outputs (incrementally written):
  runs.csv          Baseline and unique candidate-state run observations.
  steps.csv         One row for every policy x runtime x CRI service candidate.
  endpoints.csv     One median final-endpoint row per policy x runtime.
  endpoint_runs.csv Raw fresh final-endpoint repetitions.
  run_manifest.json Runtime/policy/configuration metadata.

CSV degradation units: dL (% p95 latency inflation), dQA (QA inconsistency
increase in percentage points), dF (failure-rate increase in percentage points).
The authoritative gate is median(dL) <= epsilon_L, baseline QA-inconsistency
+ median(dQA) <= 100-tau_QA, and median(dF) <= 2 pp. All measurements are
live; none are synthesized.

The uploaded experiment-runner CLI does not expose a request-count argument.
This wrapper therefore records the 5,000-request count assumed by the original
runner's create_execution_metrics_from_step_result(total_trials=5000). Confirm
that exp_runner_auto.py itself is configured to issue exactly 5,000 requests.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

# Fixed CRI order and mappings copied from the uploaded runner.
# Logical IDs s1..s9 are emitted in CSV states; endpoints are used at runtime.
SERVICE_ROWS = [
    ("s1", "currency_service:5053", "currency_agent:5053", "Currency"),
    ("s2", "product_catalog_service:5055", "product_catalog_agent:5055", "Catalog"),
    ("s3", "ad_service:5057", "ad_agent:5057", "Ad"),
    ("s4", "cart_service:5054", "cart_agent:5054", "Cart"),
    ("s5", "recommendation_service:5058", "recommendation_agent:5058", "Recommend"),
    ("s6", "shipping_service:5051", "shipping_agent:5051", "Shipping"),
    ("s7", "email_service:5056", "email_agent:5056", "Email"),
    ("s8", "payment_service:5052", "payment_agent:5052", "Payment"),
    ("s9", "checkout_service:5050", "checkout_agent:5050", "Checkout"),
]
SERVICE_IDS = [r[0] for r in SERVICE_ROWS]
ALL_SERVICES = [r[1] for r in SERVICE_ROWS]
ID_TO_NAME = {r[0]: r[3] for r in SERVICE_ROWS}

# Reproduce the original runner's weighted scores. Its ranked_services list
# was explicitly ordered as s1..s9; this script retains that exact CRI order.
FANOUT = [0,  0,  0,  0,  1,  1,  1,  1,  6]
BC     = [0,  0,  0,  0,  0,  0,  0,  0,  0.56]
C_CYC  = [8, 12, 17, 17, 16, 21, 23, 41, 65]
C_COG  = [4,  6, 14, 17, 12, 14, 25, 36, 50]
WEIGHTS = (0.2, 0.2, 0.2, 0.2, 0.2)

def norm(a: list[float], i: int) -> float:
    return a[i] / sum(a) if sum(a) else 0.0

RANKED_SERVICES = [
    [r[1], WEIGHTS[0] * norm(FANOUT, i) + WEIGHTS[1] * norm(BC, i)
     + WEIGHTS[2] * norm(C_CYC, i) + WEIGHTS[3] * norm(C_COG, i)]
    for i, r in enumerate(SERVICE_ROWS)
]

LATENCY_THRESHOLDS = list(range(40, 151, 10))
QA_THRESHOLDS = [90.0, 92.5, 95.0, 97.5, 100.0]
EPSILON_SLO_PP = 2.0
REQUESTS_PER_EXPERIMENT = 5000
RUNTIMES = [
    {"runtime": "C1", "model": "llama3.2:3b", "temperature": 0.0, "concurrency": 20},
    {"runtime": "C2", "model": "llama3.2:3b", "temperature": 0.0, "concurrency": 100},
    {"runtime": "C3", "model": "llama3.2:3b", "temperature": 0.8, "concurrency": 20},
    {"runtime": "C4", "model": "llama3.2:3b", "temperature": 0.8, "concurrency": 100},
    {"runtime": "C5", "model": "llama3:8b", "temperature": 0.0, "concurrency": 20},
    {"runtime": "C6", "model": "llama3:8b", "temperature": 0.0, "concurrency": 100},
    {"runtime": "C7", "model": "llama3:8b", "temperature": 0.8, "concurrency": 20},
    {"runtime": "C8", "model": "llama3:8b", "temperature": 0.8, "concurrency": 100},
]
MIGRATION_ORDER = "Ranked"
PREDICATE_MODE = "Full"
GOVERNANCE_POLICY = "No"
TEMPORAL_PROPAGATION = False


def state_key(state: set[str] | frozenset[str]) -> str:
    return "+".join(s for s in SERVICE_IDS if s in state)


def config_for_state(state: set[str] | frozenset[str]) -> tuple[list[str], list[str]]:
    services = [endpoint for sid, endpoint, _, _ in SERVICE_ROWS if sid not in state]
    agents = [agent for sid, _, agent, _ in SERVICE_ROWS if sid in state]
    return services, agents


def deploy(services: list[str], agents: list[str], runtime: dict, script: str) -> None:
    args = [f"services={','.join(services)}", f"agents={','.join(agents)}",
            f"model={runtime['model']}", f"temperature={runtime['temperature']}"]
    print("\n🚀 Deploying:", args, flush=True)
    subprocess.run([script] + args, check=True)


def shutdown(services: list[str], agents: list[str], runtime: dict, script: str) -> None:
    # The uploaded shutdown() accidentally called build_args() without the
    # required model and temperature. Keep the same key=value convention.
    if not Path(script).exists():
        print(f"⚠️ {script} not found; skipping shutdown.", flush=True)
        return
    args = [f"services={','.join(services)}", f"agents={','.join(agents)}",
            f"model={runtime['model']}", f"temperature={runtime['temperature']}"]
    subprocess.run([script] + args, check=True)


def parse_payload(stdout: str) -> dict[str, Any]:
    text = stdout.strip()
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    found: list[dict[str, Any]] = []
    for i, ch in enumerate(text):
        if ch == "{":
            try:
                obj, _ = decoder.raw_decode(text[i:])
                if isinstance(obj, dict):
                    found.append(obj)
            except json.JSONDecodeError:
                continue
    if not found:
        raise ValueError("exp_runner_auto.py emitted no JSON object. stdout tail:\n" + text[-3000:])
    return found[-1]


def payload_metrics(payload: dict[str, Any]) -> dict[str, float]:
    details = payload.get("details")
    if not isinstance(details, dict):
        result = payload.get("result")
        details = result.get("details") if isinstance(result, dict) else None
    if not isinstance(details, dict):
        details = payload

    def pick(keys: tuple[str, ...], label: str) -> float:
        for k in keys:
            if k in details and details[k] is not None:
                try:
                    x = float(details[k])
                    if math.isfinite(x):
                        return x
                except (TypeError, ValueError):
                    pass
        raise KeyError(f"Missing numeric {label}; available metric keys: {list(details)}")

    return {
        "p95_latency": pick(("p95_latency", "latency_p95", "p95"), "p95_latency"),
        "qa_inconsistency_rate": pick(("qa_inconsistency_rate", "qa_failure_rate", "inconsistency_rate"), "qa_inconsistency_rate"),
        "failure_rate": pick(("failure_rate", "slo_failure_rate", "request_failure_rate"), "failure_rate"),
    }


def as_pp(value: float, rate_scale: str) -> float:
    return value * 100.0 if rate_scale == "fraction" else value


def deltas(raw: dict[str, float], baseline: dict[str, float], rate_scale: str) -> dict[str, float]:
    if baseline["p95_latency"] <= 0:
        raise ValueError(f"Invalid baseline p95 latency: {baseline['p95_latency']}")
    return {
        "dL": 100.0 * (raw["p95_latency"] - baseline["p95_latency"]) / baseline["p95_latency"],
        "dQA": as_pp(raw["qa_inconsistency_rate"], rate_scale) - as_pp(baseline["qa_inconsistency_rate"], rate_scale),
        "dF": as_pp(raw["failure_rate"], rate_scale) - as_pp(baseline["failure_rate"], rate_scale),
    }


def as_runner_rate(pp: float, rate_scale: str) -> float:
    return pp / 100.0 if rate_scale == "fraction" else pp


def run_experiment(*, services: list[str], agents: list[str], runtime: dict,
                   step: int, target: str, previous: str, epsilon_l_absolute: float,
                   allowed_qa_pp: float, rate_scale: str, args: argparse.Namespace) -> dict[str, Any]:
    # Preserve the exact positional contract used by the user's existing runner.
    cmd = [
        sys.executable, "-m", args.runner_module,
        MIGRATION_ORDER, PREDICATE_MODE, str(step),
        ",".join(services), ",".join(agents),
        str(epsilon_l_absolute), str(as_runner_rate(allowed_qa_pp, rate_scale)),
        str(as_runner_rate(EPSILON_SLO_PP, rate_scale)),
        GOVERNANCE_POLICY, target, previous, str(TEMPORAL_PROPAGATION),
        str(RANKED_SERVICES), str(runtime["temperature"]), str(runtime["model"]),
        str(runtime["concurrency"]),
    ]
    print(f"\n🧪 Running exp_runner step={step}, target={target}", flush=True)
    proc = subprocess.run(cmd, cwd=args.experiment_cwd, capture_output=True,
                          text=True, check=False, timeout=args.timeout_seconds)
    if proc.returncode != 0:
        raise RuntimeError(
            f"exp_runner_auto.py exited with code {proc.returncode}.\n"
            f"stdout tail:\n{proc.stdout[-2500:]}\n"
            f"stderr tail:\n{proc.stderr[-2500:]}"
        )
    if proc.stderr.strip():
        print("⚠️ exp_runner stderr tail:\n" + proc.stderr[-1500:], flush=True)
    payload = parse_payload(proc.stdout)
    payload_metrics(payload)  # validate the metric fields immediately
    return payload


class CsvSink:
    def __init__(self, path: Path, fields: list[str]):
        self.file = path.open("w", newline="", encoding="utf-8")
        self.writer = csv.DictWriter(self.file, fieldnames=fields, extrasaction="ignore")
        self.writer.writeheader()
        self.file.flush()

    def write(self, row: dict[str, Any]) -> None:
        self.writer.writerow(row)
        self.file.flush()

    def close(self) -> None:
        self.file.close()


RUN_FIELDS = ["runtime", "model", "temperature", "concurrency", "state", "run",
              "dL", "dQA", "dF", "p95_latency", "qa_inconsistency_rate", "failure_rate",
              "requests_per_experiment", "measurement_role"]
STEP_FIELDS = ["runtime", "model", "temperature", "concurrency", "policy", "epsilon_L",
               "tau_QA", "epsilon_SLO", "t", "batch", "service_name", "service_endpoint",
               "prev", "candidate", "accepted_online", "decision_type", "decision_reason",
               "dL", "dQA", "dF", "p95_latency", "qa_inconsistency_rate", "failure_rate",
               "allowed_QA_degradation_pp", "n_state_runs"]
ENDPOINT_RUN_FIELDS = ["runtime", "model", "temperature", "concurrency", "policy", "epsilon_L",
                      "tau_QA", "epsilon_SLO", "endpoint_run", "final_state", "n_agentified_services",
                      "final_services", "final_agents", "dL", "dQA", "dF", "p95_latency",
                      "qa_inconsistency_rate", "failure_rate", "requests_per_experiment"]
ENDPOINT_FIELDS = ["runtime", "model", "temperature", "concurrency", "policy", "epsilon_L", "tau_QA",
                  "epsilon_SLO", "final_state", "n_agentified_services", "final_services", "final_agents",
                  "endpoint_dL", "endpoint_dQA", "endpoint_dF", "endpoint_p95_latency",
                  "endpoint_qa_inconsistency_rate", "endpoint_failure_rate", "endpoint_runs"]


def median_raw(rows: list[dict[str, float]]) -> dict[str, float]:
    return {k: float(statistics.median(r[k] for r in rows)) for k in
            ("p95_latency", "qa_inconsistency_rate", "failure_rate")}


def median_delta(rows: list[dict[str, float]], baseline: dict[str, float], scale: str) -> dict[str, float]:
    ds = [deltas(r, baseline, scale) for r in rows]
    return {k: float(statistics.median(d[k] for d in ds)) for k in ("dL", "dQA", "dF")}


def accept(summary: dict[str, float], epsilon_l: float, tau_qa: float,
           baseline_qa_inconsistency_pp: float) -> tuple[bool, str]:
    failed = []
    if summary["dL"] > epsilon_l:
        failed.append("LATENCY")
    # QA is represented as an inconsistency rate (higher is worse). Compare
    # the candidate's absolute inconsistency rate with 100-tau_QA, rather than
    # assuming the measured baseline has exactly zero inconsistency.
    candidate_qa_inconsistency_pp = baseline_qa_inconsistency_pp + summary["dQA"]
    if candidate_qa_inconsistency_pp > 100.0 - tau_qa:
        failed.append("QA")
    if summary["dF"] > EPSILON_SLO_PP:
        failed.append("FAILURE")
    return (not failed, "ALL_PREDICATES_SATISFIED" if not failed else "+".join(failed))


def run_grid(args: argparse.Namespace) -> None:
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    runtime_list = RUNTIMES
    if args.runtime_ids:
        wanted = set(args.runtime_ids.split(","))
        unknown = wanted - {r["runtime"] for r in RUNTIMES}
        if unknown:
            raise ValueError(f"Unknown runtime IDs: {sorted(unknown)}")
        runtime_list = [r for r in RUNTIMES if r["runtime"] in wanted]
    policies = [(l, q) for l in LATENCY_THRESHOLDS for q in QA_THRESHOLDS]
    if args.policy_limit is not None:
        policies = policies[:args.policy_limit]

    sinks = {
        "runs": CsvSink(out / "runs.csv", RUN_FIELDS),
        "steps": CsvSink(out / "steps.csv", STEP_FIELDS),
        "endpoint_runs": CsvSink(out / "endpoint_runs.csv", ENDPOINT_RUN_FIELDS),
        "endpoints": CsvSink(out / "endpoints.csv", ENDPOINT_FIELDS),
    }
    manifest = {
        "created_unix": time.time(), "requests_per_experiment_assumed": REQUESTS_PER_EXPERIMENT,
        "runs_per_unique_candidate_state": args.runs_per_state,
        "endpoint_replicates_per_policy": args.endpoint_replicates,
        "rate_scale": args.rate_scale, "latency_thresholds_pct": LATENCY_THRESHOLDS,
        "minimum_QA_thresholds_pct": QA_THRESHOLDS, "failure_threshold_pp": EPSILON_SLO_PP,
        "runtimes": runtime_list, "policies_executed_per_runtime": len(policies),
        "cri_order": [{"id": x[0], "endpoint": x[1], "agent": x[2], "name": x[3]} for x in SERVICE_ROWS],
        "gate": "median dL <= epsilon_L; baseline QA inconsistency pp + median dQA <= 100-tau_QA; median dF <= 2 pp",
        "note": "Check that exp_runner_auto.py sends 5000 requests and that rate_scale matches its output.",
    }
    (out / "run_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    try:
        for runtime in runtime_list:
            rid = runtime["runtime"]
            print("\n" + "=" * 88 + f"\nRUNTIME {rid}: {runtime}\n" + "=" * 88, flush=True)
            state_cache: dict[frozenset[str], list[dict[str, float]]] = {}

            # Baseline for this runtime: all original services, no agents.
            deploy(ALL_SERVICES.copy(), [], runtime, args.deploy_script)
            baseline_rows = []
            for rep in range(1, args.runs_per_state + 1):
                payload = run_experiment(
                    services=ALL_SERVICES.copy(), agents=[], runtime=runtime, step=0,
                    target=args.baseline_target, previous="N/A", epsilon_l_absolute=1e9,
                    allowed_qa_pp=100.0, rate_scale=args.rate_scale, args=args)
                raw = payload_metrics(payload)
                baseline_rows.append(raw)
            baseline = median_raw(baseline_rows)
            state_cache[frozenset()] = baseline_rows

            for rep, raw in enumerate(baseline_rows, start=1):
                sinks["runs"].write({
                    "runtime": rid, "model": runtime["model"], "temperature": runtime["temperature"],
                    "concurrency": runtime["concurrency"], "state": "", "run": rep,
                    **deltas(raw, baseline, args.rate_scale), **raw,
                    "requests_per_experiment": REQUESTS_PER_EXPERIMENT, "measurement_role": "BASELINE"})
            print(f"Baseline medians: p95={baseline['p95_latency']:.5f}s, "
                  f"QA-rate={baseline['qa_inconsistency_rate']}, failure-rate={baseline['failure_rate']}", flush=True)

            last_final_services, last_final_agents = ALL_SERVICES.copy(), []
            for epsilon_l, tau_qa in policies:
                policy = f"CRI_L{epsilon_l}_QA{tau_qa:.1f}"
                committed: set[str] = set()
                previous_decision = "N/A"
                print(f"\n--- {rid} / {policy} ---", flush=True)

                for step, (sid, endpoint, _agent, service_name) in enumerate(SERVICE_ROWS, start=1):
                    prev = frozenset(committed)
                    candidate = frozenset(committed | {sid})
                    candidate_services, candidate_agents = config_for_state(candidate)
                    deploy(candidate_services, candidate_agents, runtime, args.deploy_script)

                    # By default the same architecture state is sampled once per
                    # runtime and reused across policies. --no-state-cache disables this.
                    if candidate not in state_cache or args.no_state_cache:
                        observations = []
                        for rep in range(1, args.runs_per_state + 1):
                            payload = run_experiment(
                                services=candidate_services, agents=candidate_agents, runtime=runtime,
                                step=step, target=endpoint.split(":")[0], previous=previous_decision,
                                epsilon_l_absolute=baseline["p95_latency"] * (1.0 + epsilon_l / 100.0),
                                allowed_qa_pp=100.0 - tau_qa, rate_scale=args.rate_scale, args=args)
                            raw = payload_metrics(payload)
                            observations.append(raw)
                            sinks["runs"].write({
                                "runtime": rid, "model": runtime["model"], "temperature": runtime["temperature"],
                                "concurrency": runtime["concurrency"], "state": state_key(candidate), "run": rep,
                                **deltas(raw, baseline, args.rate_scale), **raw,
                                "requests_per_experiment": REQUESTS_PER_EXPERIMENT,
                                "measurement_role": "CRI_CANDIDATE"})
                        state_cache[candidate] = observations
                    observations = state_cache[candidate]

                    summary = median_delta(observations, baseline, args.rate_scale)
                    raw_med = median_raw(observations)
                    baseline_qa_pp = as_pp(baseline["qa_inconsistency_rate"], args.rate_scale)
                    accepted, reason = accept(summary, epsilon_l, tau_qa, baseline_qa_pp)
                    sinks["steps"].write({
                        "runtime": rid, "model": runtime["model"], "temperature": runtime["temperature"],
                        "concurrency": runtime["concurrency"], "policy": policy,
                        "epsilon_L": epsilon_l, "tau_QA": tau_qa, "epsilon_SLO": EPSILON_SLO_PP,
                        "t": step, "batch": sid, "service_name": service_name,
                        "service_endpoint": endpoint, "prev": state_key(prev), "candidate": state_key(candidate),
                        "accepted_online": accepted, "decision_type": "CRI_GATE", "decision_reason": reason,
                        **summary, **raw_med, "allowed_QA_degradation_pp": 100.0 - tau_qa,
                        "n_state_runs": len(observations)})

                    if accepted:
                        committed.add(sid)
                        previous_decision = "ACCEPTED"
                    else:
                        previous_decision = "REJECTED"
                    print(f"step {step:02d} {sid}: {'ACCEPT' if accepted else 'REJECT'} {reason}; "
                          f"dL={summary['dL']:.2f}%, dQA={summary['dQA']:.3f}pp, "
                          f"dF={summary['dF']:.3f}pp; committed={state_key(committed)}", flush=True)

                # Fresh measurement of this policy's final committed endpoint.
                final_state = frozenset(committed)
                final_services, final_agents = config_for_state(final_state)
                last_final_services, last_final_agents = final_services, final_agents
                deploy(final_services, final_agents, runtime, args.deploy_script)
                endpoint_rows = []
                for rep in range(1, args.endpoint_replicates + 1):
                    payload = run_experiment(
                        services=final_services, agents=final_agents, runtime=runtime,
                        step=len(SERVICE_ROWS) + 1, target=args.endpoint_target,
                        previous=previous_decision,
                        epsilon_l_absolute=baseline["p95_latency"] * (1.0 + epsilon_l / 100.0),
                        allowed_qa_pp=100.0 - tau_qa, rate_scale=args.rate_scale, args=args)
                    raw = payload_metrics(payload)
                    endpoint_rows.append(raw)
                    delta = deltas(raw, baseline, args.rate_scale)
                    sinks["endpoint_runs"].write({
                        "runtime": rid, "model": runtime["model"], "temperature": runtime["temperature"],
                        "concurrency": runtime["concurrency"], "policy": policy,
                        "epsilon_L": epsilon_l, "tau_QA": tau_qa, "epsilon_SLO": EPSILON_SLO_PP,
                        "endpoint_run": rep, "final_state": state_key(final_state),
                        "n_agentified_services": len(final_state), "final_services": ";".join(final_services),
                        "final_agents": ";".join(final_agents), **delta, **raw,
                        "requests_per_experiment": REQUESTS_PER_EXPERIMENT})
                end_delta = median_delta(endpoint_rows, baseline, args.rate_scale)
                end_raw = median_raw(endpoint_rows)
                sinks["endpoints"].write({
                    "runtime": rid, "model": runtime["model"], "temperature": runtime["temperature"],
                    "concurrency": runtime["concurrency"], "policy": policy,
                    "epsilon_L": epsilon_l, "tau_QA": tau_qa, "epsilon_SLO": EPSILON_SLO_PP,
                    "final_state": state_key(final_state), "n_agentified_services": len(final_state),
                    "final_services": ";".join(final_services), "final_agents": ";".join(final_agents),
                    "endpoint_dL": end_delta["dL"], "endpoint_dQA": end_delta["dQA"],
                    "endpoint_dF": end_delta["dF"], "endpoint_p95_latency": end_raw["p95_latency"],
                    "endpoint_qa_inconsistency_rate": end_raw["qa_inconsistency_rate"],
                    "endpoint_failure_rate": end_raw["failure_rate"], "endpoint_runs": len(endpoint_rows)})
                print(f"ENDPOINT {rid}/{policy}: state={state_key(final_state)}, "
                      f"dL={end_delta['dL']:.2f}%, dQA={end_delta['dQA']:.3f}pp, "
                      f"dF={end_delta['dF']:.3f}pp", flush=True)

            try:
                shutdown(last_final_services, last_final_agents, runtime, args.shutdown_script)
            except Exception as exc:
                print(f"⚠️ Runtime cleanup failed: {exc}", flush=True)
    finally:
        for sink in sinks.values():
            sink.close()

    print("\nCompleted. CSV outputs:")
    for filename in ("runs.csv", "steps.csv", "endpoints.csv", "endpoint_runs.csv", "run_manifest.json"):
        print(f"  {out / filename}")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out-dir", default="cri_policy_grid_results")
    p.add_argument("--runtime-ids", default="", help="Optional subset: C1,C2,...,C8")
    p.add_argument("--policy-limit", type=int, default=None, help="First N policies; for smoke tests only")
    p.add_argument("--runs-per-state", type=int, default=1,
                   help="Independent 5000-request runs per unique candidate state and baseline")
    p.add_argument("--endpoint-replicates", type=int, default=1,
                   help="Fresh endpoint runs per runtime/policy")
    p.add_argument("--rate-scale", choices=("fraction", "percent"), default="fraction",
                   help="Scale returned for QA/failure rates; default assumes 0..1 fractions")
    p.add_argument("--deploy-script", default="./deploy-local.sh")
    p.add_argument("--shutdown-script", default="./shutdown-local.sh")
    p.add_argument("--runner-module", default="refactored_architecture.google_ms.exp_runner_auto")
    p.add_argument("--experiment-cwd", default="../..",
                   help="Working directory used by the existing exp_runner module invocation")
    p.add_argument("--baseline-target", default="checkout_service")
    p.add_argument("--endpoint-target", default="checkout_service")
    p.add_argument("--timeout-seconds", type=int, default=7200)
    p.add_argument("--no-state-cache", action="store_true",
                   help="Re-run a candidate state for each policy instead of caching it per runtime")
    a = p.parse_args()
    if a.runs_per_state < 1 or a.endpoint_replicates < 1:
        p.error("--runs-per-state and --endpoint-replicates must be >= 1")
    if a.policy_limit is not None and a.policy_limit < 1:
        p.error("--policy-limit must be >= 1")
    return a


if __name__ == "__main__":
    run_grid(parse_args())

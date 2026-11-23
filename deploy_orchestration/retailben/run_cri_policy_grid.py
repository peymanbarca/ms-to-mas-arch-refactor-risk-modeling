#!/usr/bin/env python3
"""Live Benchmark 2 (RetailBen) CRI 60-policy runner.

Preserves the active nine-service configuration and fixed order from the
uploaded RetailBen runner. notification_service is intentionally excluded
because it is commented out in that source file. Logical IDs retain the B2
numbering s2..s10.

For each runtime: measure the all-microservice baseline; execute every
(epsilon_L, tau_QA) pair in the 60-policy grid; deploy every CRI candidate;
measure each unique candidate state once per runtime (unless caching is
explicitly disabled); record every policy/step transition; and independently
re-run each policy's final architecture for endpoint/Pareto analysis.

CSV units: dL = p95 latency inflation from baseline (%); dQA and dF are
increases in QA inconsistency and failure rate from baseline (percentage
points). Gate: median(dL) <= epsilon_L; median(dQA) <= 100-tau_QA;
median(dF) <= 2 pp.

The outer runner keeps the CRI order fixed. Temporal propagation is disabled
by default to avoid dynamic reordering; --temporal-propagation-enabled passes
True to exp_runner_auto but still does not reorder this outer CRI sequence.

Verify exp_runner_auto.py actually executes 5,000 requests per invocation.
The uploaded wrapper passed total_trials=5000 to metric construction but did
not expose a request-count argument. Default rate scale is fraction (0..1).
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

# Exact active service set and ranking in the uploaded B2 runner.
# Notification (s1) is omitted because its entries are commented out.
SERVICE_ROWS = [
    ("s2", "pricing_service:8002", "pricing_agent:8002", "Pricing"),
    ("s3", "payment_service:8007", "payment_agent:8007", "Payment"),
    ("s4", "shopping_cart_service:8003", "shopping_cart_agent:8003", "Cart"),
    ("s5", "subscription_service:8010", "subscription_agent:8010", "Subscription"),
    ("s6", "inventory_service:8001", "inventory_agent:8001", "Inventory"),
    ("s7", "product_catalog_service:8008", "product_catalog_agent:8008", "Catalog"),
    ("s8", "procurement_service:8009", "procurement_agent:8009", "Procurement"),
    ("s9", "shipment_service:8006", "shipment_agent:8006", "Shipment"),
    ("s10", "order_service:8000", "order_agent:8000", "Order"),
]
SERVICE_IDS = [r[0] for r in SERVICE_ROWS]
SERVICE_ENDPOINTS = [r[1] for r in SERVICE_ROWS]

# Static risk metrics from the uploaded source, in its metric-table order:
# Pricing, Catalog, Inventory, Cart, Payment, Order, Subscription,
# Procurement, Shipment. Preserve the explicit ranked_services list order.
METRIC_ENDPOINT_ORDER = [
    "pricing_service:8002", "product_catalog_service:8008",
    "inventory_service:8001", "shopping_cart_service:8003",
    "payment_service:8007", "order_service:8000",
    "subscription_service:8010", "procurement_service:8009",
    "shipment_service:8006",
]
FANOUT = [0, 2, 1, 0, 1, 7, 1, 1, 1]
BC = [0, 0.05, 0.22, 0, 0, 0.7, 0.016, 0, 0]
C_CYC = [10, 22, 10, 18, 10, 16, 12, 36, 33]
C_COG = [8, 18, 14, 14, 6, 28, 10, 26, 30]
WEIGHTS = (0.2, 0.2, 0.2, 0.2, 0.2)


def _norm(values: list[float], i: int) -> float:
    return values[i] / sum(values) if sum(values) else 0.0


risk_by_endpoint = {}
for i, endpoint in enumerate(METRIC_ENDPOINT_ORDER):
    risk_by_endpoint[endpoint] = (
        WEIGHTS[0] * _norm(FANOUT, i)
        + WEIGHTS[1] * _norm(BC, i)
        + WEIGHTS[2] * _norm(C_CYC, i)
        + WEIGHTS[3] * _norm(C_COG, i)
        + WEIGHTS[4] * 0.0
    )
RANKED_SERVICES = [[r[1], risk_by_endpoint[r[1]]] for r in SERVICE_ROWS]

LATENCY_THRESHOLDS = list(range(40, 151, 10))
QA_THRESHOLDS = [90.0, 92.5, 95.0, 97.5, 100.0]
FAILURE_THRESHOLD_PP = 2.0
REQUESTS_PER_EXPERIMENT_ASSUMED = 5000
MIGRATION_ORDER_STRATEGY = "Ranked"
PREDICATE_MODE = "Full"
GOVERNANCE_POLICY = "No"

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


def canonical_state(state: set[str] | frozenset[str]) -> str:
    return "+".join(sid for sid in SERVICE_IDS if sid in state)


def services_for_state(state: set[str] | frozenset[str]) -> list[str]:
    return [endpoint for sid, endpoint, _agent, _name in SERVICE_ROWS if sid not in state]


def agents_for_state(state: set[str] | frozenset[str]) -> list[str]:
    return [agent for sid, _endpoint, agent, _name in SERVICE_ROWS if sid in state]


def deploy(state: set[str] | frozenset[str], runtime: dict[str, Any], args: argparse.Namespace) -> None:
    services, agents = services_for_state(state), agents_for_state(state)
    cmd = [args.deploy_script,
           f"services={','.join(services)}", f"agents={','.join(agents)}",
           f"model={runtime['model']}", f"temperature={runtime['temperature']}"]
    print(f"\n🚀 DEPLOY {runtime['runtime']} state={canonical_state(state) or 'BASELINE'}", flush=True)
    print(f"   services={services}\n   agents={agents}", flush=True)
    subprocess.run(cmd, cwd=args.deploy_cwd, check=True)


def shutdown(state: set[str] | frozenset[str], args: argparse.Namespace) -> None:
    script_path = Path(args.deploy_cwd) / args.shutdown_script
    if not script_path.exists():
        print(f"⚠️ Shutdown script not found: {script_path}; cleanup skipped.", flush=True)
        return
    services, agents = services_for_state(state), agents_for_state(state)
    # The uploaded B2 shutdown helper called build_args(services, agents),
    # although build_args requires model and T. Use architecture args only.
    cmd = [args.shutdown_script, f"services={','.join(services)}", f"agents={','.join(agents)}"]
    print(f"\n🧹 SHUTDOWN state={canonical_state(state) or 'BASELINE'}", flush=True)
    subprocess.run(cmd, cwd=args.deploy_cwd, check=True)


def parse_json_payload(stdout: str) -> dict[str, Any]:
    text = stdout.strip()
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    found = []
    for i, char in enumerate(text):
        if char == "{":
            try:
                obj, _ = decoder.raw_decode(text[i:])
                if isinstance(obj, dict):
                    found.append(obj)
            except json.JSONDecodeError:
                continue
    if found:
        return found[-1]
    raise ValueError(f"No JSON object found in exp_runner_auto.py stdout:\n{text[-3000:]}")


def get_details(payload: dict[str, Any]) -> dict[str, Any]:
    details = payload.get("details")
    if isinstance(details, dict):
        return details
    result = payload.get("result")
    if isinstance(result, dict) and isinstance(result.get("details"), dict):
        return result["details"]
    return payload


def numeric_field(details: dict[str, Any], keys: tuple[str, ...], label: str) -> float:
    for key in keys:
        value = details.get(key)
        if value is not None:
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(number):
                return number
    raise KeyError(f"Missing {label}; tried {keys}; available keys={list(details)}")


def extract_metrics(payload: dict[str, Any]) -> dict[str, float]:
    details = get_details(payload)
    return {
        "p95_latency": numeric_field(details, ("p95_latency", "latency_p95", "p95"), "p95 latency"),
        "qa_inconsistency_rate": numeric_field(
            details, ("qa_inconsistency_rate", "qa_failure_rate", "inconsistency_rate"),
            "QA inconsistency rate"),
        "failure_rate": numeric_field(
            details, ("failure_rate", "slo_failure_rate", "request_failure_rate"),
            "failure rate"),
    }


def to_pp(value: float, rate_scale: str) -> float:
    return value * 100.0 if rate_scale == "fraction" else value


def to_runner_units(value_pp: float, rate_scale: str) -> float:
    return value_pp / 100.0 if rate_scale == "fraction" else value_pp


def calculate_deltas(raw: dict[str, float], baseline: dict[str, float], rate_scale: str) -> dict[str, float]:
    if baseline["p95_latency"] <= 0:
        raise ValueError(f"Baseline p95 latency must be positive, got {baseline['p95_latency']}")
    return {
        "dL": 100.0 * (raw["p95_latency"] - baseline["p95_latency"]) / baseline["p95_latency"],
        "dQA": to_pp(raw["qa_inconsistency_rate"], rate_scale) - to_pp(baseline["qa_inconsistency_rate"], rate_scale),
        "dF": to_pp(raw["failure_rate"], rate_scale) - to_pp(baseline["failure_rate"], rate_scale),
    }


def run_exp_runner(state: set[str] | frozenset[str], runtime: dict[str, Any], step_num: int,
                   target_service: str, previous_decision: str, epsilon_l_pct: float,
                   tau_qa: float, args: argparse.Namespace) -> dict[str, Any]:
    services, agents = services_for_state(state), agents_for_state(state)
    if args._baseline_latency is None:
        epsilon_l_absolute = 1.0e9
    else:
        epsilon_l_absolute = args._baseline_latency * (1.0 + epsilon_l_pct / 100.0)
    allowed_qa_pp = 100.0 - tau_qa
    epsilon_qa = to_runner_units(allowed_qa_pp, args.rate_scale)
    epsilon_failure = to_runner_units(FAILURE_THRESHOLD_PP, args.rate_scale)
    cmd = [
        sys.executable, args.runner_script,
        MIGRATION_ORDER_STRATEGY, PREDICATE_MODE, str(step_num),
        ",".join(services), ",".join(agents),
        str(epsilon_l_absolute), str(epsilon_qa), str(epsilon_failure),
        GOVERNANCE_POLICY, target_service, previous_decision,
        str(args.temporal_propagation_enabled), str(RANKED_SERVICES),
        str(runtime["temperature"]), str(runtime["model"]), str(runtime["concurrency"]),
    ]
    print(f"🧪 RUN {runtime['runtime']} step={step_num} target={target_service} "
          f"epsilon_L={epsilon_l_pct}% tau_QA={tau_qa}", flush=True)
    result = subprocess.run(cmd, cwd=args.experiment_cwd, capture_output=True,
                            text=True, check=True, timeout=args.timeout_seconds)
    if result.stderr.strip():
        print("⚠️ exp_runner_auto.py stderr:\n" + result.stderr[-2000:], flush=True)
    payload = parse_json_payload(result.stdout)
    extract_metrics(payload)
    return payload


def median_metrics(observations: list[dict[str, float]]) -> dict[str, float]:
    return {k: float(statistics.median(x[k] for x in observations))
            for k in ("p95_latency", "qa_inconsistency_rate", "failure_rate")}


def summarize_state(observations: list[dict[str, float]], baseline: dict[str, float],
                    rate_scale: str) -> dict[str, float]:
    raw = median_metrics(observations)
    return {**raw, **calculate_deltas(raw, baseline, rate_scale)}


def decide(summary: dict[str, float], epsilon_l: float, tau_qa: float) -> tuple[bool, str]:
    violations = []
    if summary["dL"] > epsilon_l:
        violations.append("LATENCY")
    if summary["dQA"] > 100.0 - tau_qa:
        violations.append("QA")
    if summary["dF"] > FAILURE_THRESHOLD_PP:
        violations.append("FAILURE")
    return not violations, ("PASS" if not violations else "+".join(violations))


RUN_FIELDS = ["runtime", "model", "temperature", "concurrency", "state", "run",
              "dL", "dQA", "dF", "p95_latency", "qa_inconsistency_rate",
              "failure_rate", "requests_per_experiment", "measurement_role"]
STEP_FIELDS = ["runtime", "model", "temperature", "concurrency", "policy", "epsilon_L",
               "tau_QA", "epsilon_SLO", "t", "batch", "prev", "candidate",
               "accepted_online", "decision_type", "service_name", "service_endpoint",
               "decision_reason", "dL", "dQA", "dF", "p95_latency",
               "qa_inconsistency_rate", "failure_rate", "allowed_QA_degradation_pp", "n_state_runs"]
ENDPOINT_FIELDS = ["runtime", "model", "temperature", "concurrency", "policy", "epsilon_L",
                   "tau_QA", "epsilon_SLO", "final_state", "n_agentified_services",
                   "final_services", "final_agents", "endpoint_dL", "endpoint_dQA",
                   "endpoint_dF", "endpoint_p95_latency", "endpoint_qa_inconsistency_rate",
                   "endpoint_failure_rate", "endpoint_runs"]
ENDPOINT_RUN_FIELDS = ["runtime", "model", "temperature", "concurrency", "policy", "epsilon_L",
                       "tau_QA", "epsilon_SLO", "endpoint_run", "final_state",
                       "n_agentified_services", "final_services", "final_agents", "dL", "dQA",
                       "dF", "p95_latency", "qa_inconsistency_rate", "failure_rate",
                       "requests_per_experiment"]


def open_csv(path: Path, fields: list[str]):
    handle = path.open("w", newline="", encoding="utf-8")
    writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    handle.flush()
    return handle, writer


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out-dir", default="B2_CRI_policy_grid")
    p.add_argument("--runtime-ids", default="", help="Subset, e.g. C1,C2")
    p.add_argument("--policy-limit", type=int, default=None, help="First N policies for smoke test")
    p.add_argument("--runs-per-state", type=int, default=1,
                   help="Independent 5k-request repetitions per unique state and baseline")
    p.add_argument("--endpoint-replicates", type=int, default=1,
                   help="Fresh endpoint repetitions for every policy")
    p.add_argument("--rate-scale", choices=("fraction", "percent"), default="fraction")
    p.add_argument("--runner-script", default="exp_runner_auto.py")
    p.add_argument("--experiment-cwd", default="../../refactored_architecture/retailben")
    p.add_argument("--deploy-script", default="./deploy-local.sh")
    p.add_argument("--shutdown-script", default="./shutdown-local.sh")
    p.add_argument("--deploy-cwd", default=".")
    p.add_argument("--timeout-seconds", type=int, default=7200)
    p.add_argument("--baseline-target", default="order_service")
    p.add_argument("--endpoint-target", default="order_service")
    p.add_argument("--temporal-propagation-enabled", action="store_true",
                   help="Pass True to exp_runner_auto; outer CRI order remains fixed")
    p.add_argument("--no-state-cache", action="store_true",
                   help="Re-measure identical candidate states separately per policy")
    a = p.parse_args()
    if a.runs_per_state < 1 or a.endpoint_replicates < 1:
        p.error("--runs-per-state and --endpoint-replicates must be >= 1")
    if a.policy_limit is not None and a.policy_limit < 1:
        p.error("--policy-limit must be >= 1")
    if a.timeout_seconds < 1:
        p.error("--timeout-seconds must be positive")
    a._baseline_latency = None
    return a


def run_grid(args: argparse.Namespace) -> None:
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    runtimes = RUNTIMES
    if args.runtime_ids:
        wanted = set(args.runtime_ids.split(","))
        unknown = wanted - {r["runtime"] for r in RUNTIMES}
        if unknown:
            raise ValueError(f"Unknown runtime IDs: {sorted(unknown)}")
        runtimes = [r for r in RUNTIMES if r["runtime"] in wanted]
    policies = [(l, q) for l in LATENCY_THRESHOLDS for q in QA_THRESHOLDS]
    if args.policy_limit is not None:
        policies = policies[:args.policy_limit]
    if not policies:
        raise ValueError("No policies selected")

    manifest = {
        "benchmark": "B2_RetailBen",
        "active_services": [{"id": sid, "service": ep, "agent": ag, "name": name}
                            for sid, ep, ag, name in SERVICE_ROWS],
        "notification_service_included": False,
        "notification_note": "Excluded because it is commented out in the uploaded B2 runner.",
        "cri_order": SERVICE_IDS,
        "latency_thresholds_pct": LATENCY_THRESHOLDS,
        "qa_minimum_thresholds_pct": QA_THRESHOLDS,
        "failure_threshold_pp": FAILURE_THRESHOLD_PP,
        "runs_per_unique_state": args.runs_per_state,
        "endpoint_replicates_per_policy": args.endpoint_replicates,
        "requests_per_experiment_assumed": REQUESTS_PER_EXPERIMENT_ASSUMED,
        "rate_scale": args.rate_scale,
        "temporal_propagation_enabled_in_exp_runner": args.temporal_propagation_enabled,
        "outer_cri_order_reordering": False,
        "runtime_conditions": runtimes,
        "policy_count_per_runtime": len(policies),
        "created_unix": time.time(),
        "warning": "Verify request count and rate units inside exp_runner_auto.py.",
    }
    (out / "run_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    specs = {
        "runs": ("runs.csv", RUN_FIELDS),
        "steps": ("steps.csv", STEP_FIELDS),
        "endpoints": ("endpoints.csv", ENDPOINT_FIELDS),
        "endpoint_runs": ("endpoint_runs.csv", ENDPOINT_RUN_FIELDS),
    }
    streams = {key: open_csv(out / filename, fields) for key, (filename, fields) in specs.items()}

    def emit(key: str, row: dict[str, Any]) -> None:
        handle, writer = streams[key]
        writer.writerow(row)
        handle.flush()

    try:
        for runtime in runtimes:
            rid = runtime["runtime"]
            print("\n" + "=" * 94, flush=True)
            print(f"RUNTIME {rid}: {runtime['model']} T={runtime['temperature']} C={runtime['concurrency']}", flush=True)
            cache: dict[frozenset[str], list[dict[str, float]]] = {}
            baseline_state = frozenset()
            last_deployed_state = baseline_state
            try:
                # Runtime-specific baseline.
                deploy(baseline_state, runtime, args)
                baseline_observations = []
                args._baseline_latency = None
                for _run_id in range(1, args.runs_per_state + 1):
                    payload = run_exp_runner(
                        baseline_state, runtime, 0, args.baseline_target, "N/A",
                        max(LATENCY_THRESHOLDS), min(QA_THRESHOLDS), args)
                    baseline_observations.append(extract_metrics(payload))
                baseline = median_metrics(baseline_observations)
                if baseline["p95_latency"] <= 0:
                    raise ValueError(f"{rid}: baseline p95 latency must be > 0")
                args._baseline_latency = baseline["p95_latency"]
                cache[baseline_state] = baseline_observations
                print(f"BASELINE p95={baseline['p95_latency']:.5f}; "
                      f"QA={baseline['qa_inconsistency_rate']}; failure={baseline['failure_rate']}", flush=True)
                for run_id, raw in enumerate(baseline_observations, 1):
                    emit("runs", {
                        "runtime": rid, "model": runtime["model"],
                        "temperature": runtime["temperature"], "concurrency": runtime["concurrency"],
                        "state": "", "run": run_id,
                        **calculate_deltas(raw, baseline, args.rate_scale), **raw,
                        "requests_per_experiment": REQUESTS_PER_EXPERIMENT_ASSUMED,
                        "measurement_role": "BASELINE",
                    })

                # All 60 policy trajectories, each with the same fixed CRI order.
                for epsilon_l, tau_qa in policies:
                    policy = f"CRI_L{epsilon_l}_QA{tau_qa:.1f}"
                    committed: set[str] = set()
                    previous_decision = "N/A"
                    print(f"\n{'-' * 70}\n{rid} POLICY {policy}", flush=True)
                    for step, (service_id, endpoint, agent, service_name) in enumerate(SERVICE_ROWS, 1):
                        prev_state = frozenset(committed)
                        candidate_state = frozenset(committed | {service_id})
                        deploy(candidate_state, runtime, args)
                        last_deployed_state = candidate_state

                        if candidate_state not in cache or args.no_state_cache:
                            observations = []
                            for run_id in range(1, args.runs_per_state + 1):
                                payload = run_exp_runner(
                                    candidate_state, runtime, step, endpoint.split(":")[0],
                                    previous_decision, epsilon_l, tau_qa, args)
                                raw = extract_metrics(payload)
                                observations.append(raw)
                                emit("runs", {
                                    "runtime": rid, "model": runtime["model"],
                                    "temperature": runtime["temperature"], "concurrency": runtime["concurrency"],
                                    "state": canonical_state(candidate_state), "run": run_id,
                                    **calculate_deltas(raw, baseline, args.rate_scale), **raw,
                                    "requests_per_experiment": REQUESTS_PER_EXPERIMENT_ASSUMED,
                                    "measurement_role": "CRI_CANDIDATE",
                                })
                            cache[candidate_state] = observations
                        else:
                            observations = cache[candidate_state]

                        summary = summarize_state(observations, baseline, args.rate_scale)
                        accepted, reason = decide(summary, epsilon_l, tau_qa)
                        emit("steps", {
                            "runtime": rid, "model": runtime["model"],
                            "temperature": runtime["temperature"], "concurrency": runtime["concurrency"],
                            "policy": policy, "epsilon_L": epsilon_l, "tau_QA": f"{tau_qa:.1f}",
                            "epsilon_SLO": FAILURE_THRESHOLD_PP, "t": step,
                            "batch": service_id, "prev": canonical_state(prev_state),
                            "candidate": canonical_state(candidate_state),
                            "accepted_online": accepted, "decision_type": "CRI_GATE",
                            "service_name": service_name, "service_endpoint": endpoint,
                            "decision_reason": reason, "dL": summary["dL"],
                            "dQA": summary["dQA"], "dF": summary["dF"],
                            "p95_latency": summary["p95_latency"],
                            "qa_inconsistency_rate": summary["qa_inconsistency_rate"],
                            "failure_rate": summary["failure_rate"],
                            "allowed_QA_degradation_pp": 100.0 - tau_qa,
                            "n_state_runs": len(observations),
                        })
                        if accepted:
                            committed.add(service_id)
                            previous_decision = "ACCEPTED"
                        else:
                            previous_decision = "REJECTED"
                        print(f"step {step} {service_id}/{service_name}: "
                              f"{'ACCEPT' if accepted else 'REJECT'} [{reason}] "
                              f"dL={summary['dL']:.3f}% dQA={summary['dQA']:.3f}pp "
                              f"dF={summary['dF']:.3f}pp state={canonical_state(committed)}", flush=True)

                    # Independent endpoint experiment for this threshold pair.
                    final_state = frozenset(committed)
                    deploy(final_state, runtime, args)
                    last_deployed_state = final_state
                    final_services, final_agents = services_for_state(final_state), agents_for_state(final_state)
                    endpoint_observations = []
                    for endpoint_run in range(1, args.endpoint_replicates + 1):
                        payload = run_exp_runner(
                            final_state, runtime, len(SERVICE_ROWS) + 1,
                            args.endpoint_target, previous_decision, epsilon_l, tau_qa, args)
                        raw = extract_metrics(payload)
                        endpoint_observations.append(raw)
                        emit("endpoint_runs", {
                            "runtime": rid, "model": runtime["model"],
                            "temperature": runtime["temperature"], "concurrency": runtime["concurrency"],
                            "policy": policy, "epsilon_L": epsilon_l, "tau_QA": f"{tau_qa:.1f}",
                            "epsilon_SLO": FAILURE_THRESHOLD_PP, "endpoint_run": endpoint_run,
                            "final_state": canonical_state(final_state),
                            "n_agentified_services": len(final_state),
                            "final_services": ";".join(final_services),
                            "final_agents": ";".join(final_agents),
                            **calculate_deltas(raw, baseline, args.rate_scale), **raw,
                            "requests_per_experiment": REQUESTS_PER_EXPERIMENT_ASSUMED,
                        })
                    ep = summarize_state(endpoint_observations, baseline, args.rate_scale)
                    emit("endpoints", {
                        "runtime": rid, "model": runtime["model"],
                        "temperature": runtime["temperature"], "concurrency": runtime["concurrency"],
                        "policy": policy, "epsilon_L": epsilon_l, "tau_QA": f"{tau_qa:.1f}",
                        "epsilon_SLO": FAILURE_THRESHOLD_PP, "final_state": canonical_state(final_state),
                        "n_agentified_services": len(final_state),
                        "final_services": ";".join(final_services),
                        "final_agents": ";".join(final_agents),
                        "endpoint_dL": ep["dL"], "endpoint_dQA": ep["dQA"],
                        "endpoint_dF": ep["dF"], "endpoint_p95_latency": ep["p95_latency"],
                        "endpoint_qa_inconsistency_rate": ep["qa_inconsistency_rate"],
                        "endpoint_failure_rate": ep["failure_rate"],
                        "endpoint_runs": len(endpoint_observations),
                    })
                    print(f"ENDPOINT {rid}/{policy}: {canonical_state(final_state) or 'BASELINE'} "
                          f"dL={ep['dL']:.3f}% dQA={ep['dQA']:.3f}pp dF={ep['dF']:.3f}pp", flush=True)
            finally:
                try:
                    shutdown(last_deployed_state, args)
                except Exception as cleanup_error:
                    print(f"⚠️ Cleanup failed for {rid}: {cleanup_error}", flush=True)
    finally:
        for handle, _writer in streams.values():
            handle.close()

    print("\nB2 CRI policy-grid execution completed.")
    for filename in ("runs.csv", "steps.csv", "endpoints.csv", "endpoint_runs.csv", "run_manifest.json"):
        print(f"  {out / filename}")


if __name__ == "__main__":
    run_grid(parse_args())

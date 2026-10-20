#!/usr/bin/env python3
"""Run lexicographic and weighted-sum optimizers over agentification candidates.

Input schema is compatible with the raw results consumed by pareto_filter.py:
[
  {
    "benchmark": "B1",
    "runtime_condition": "...",
    "strategies": {
      "Orchestrator_only": {"qa_deviation": ..., "latency_degradation": ..., "agentification_coverage": ..., ...},
      "Complexity_ranked_refactoring": [{...}, {...}]
    }
  }
]

The script optimizes each benchmark/runtime-condition group independently.
All candidates in a group compete together across the four strategy sources.

Methods:
  * Lexicographic: latency-first, QA-first, autonomy/coverage-first.
  * Weighted sum: all strictly positive 3-objective weight vectors on a regular
    simplex grid. Default grid step is 0.1, resulting in 36 vectors.

SLO handling:
  The paper formulation constrains candidates by failure-rate degradation <
  epsilon_SLO. By default, the script requires a failure degradation metric for
  every candidate. If the raw JSON has no such field/rates, pass
  --ignore-slo-constraint only for an explicitly unconstrained sensitivity run;
  all outputs then mark that the SLO constraint was disabled.

Only the Python standard library is required.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
from collections import OrderedDict, defaultdict
from pathlib import Path
from typing import Any

OBJECTIVE_FIELDS = ("latency_degradation", "qa_deviation", "agentification_coverage")
FAILURE_DEGRADATION_KEYS = (
    "failure_rate_degradation", "failure_rate_degradation_pct",
    "failure_rate_degradation_percent", "delta_failure_rate_pct",
    "delta_failure_rate_percent", "failure_rate_increase_pct",
    "failure_rate_increase_percent", "failure_rate_amplification_pct",
    "failure_degradation_pct", "delta_f_pct", "delta_f_percent",
)
CANDIDATE_FAILURE_KEYS = ("failure_rate", "failure_rate_pct", "failure_rate_percent", "fail_rate")
BASELINE_FAILURE_KEYS = (
    "baseline_failure_rate", "baseline_failure_rate_pct", "baseline_failure_rate_percent",
    "original_failure_rate", "microservice_failure_rate", "baseline_fail_rate",
    "failure_rate_baseline",
)


def as_number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def payload_of(candidate: dict[str, Any]) -> dict[str, Any]:
    out = dict(candidate)
    metrics = candidate.get("metrics")
    if isinstance(metrics, dict):
        for k, v in metrics.items():
            out.setdefault(k, v)
    return out


def failure_degradation(candidate: dict[str, Any], explicit_field: str | None = None) -> float | None:
    p = payload_of(candidate)
    if explicit_field:
        val = as_number(p.get(explicit_field))
        if val is not None:
            return val
    for key in FAILURE_DEGRADATION_KEYS:
        val = as_number(p.get(key))
        if val is not None:
            return val
    cf = next((as_number(p.get(k)) for k in CANDIDATE_FAILURE_KEYS if as_number(p.get(k)) is not None), None)
    bf = next((as_number(p.get(k)) for k in BASELINE_FAILURE_KEYS if as_number(p.get(k)) is not None), None)
    # Equation in the draft: (F(A)-F(A_MS))/F(A_MS)*100. Relative change is undefined if baseline is 0.
    if cf is not None and bf is not None and bf > 0:
        return (cf - bf) / bf * 100.0
    return None


def canonical_strategy(raw: str) -> str:
    s = str(raw).strip().lower().replace("-", "_").replace(" ", "_")
    if "complexity" in s or ("ranked" in s and "refactor" in s):
        return "Complexity_ranked_refactoring"
    if "user" in s and ("interact" in s or "facing" in s):
        return "User_interacting"
    if "non_orchestrator" in s or "nonorchestrator" in s or s in {"worker_only", "specialized_services"}:
        return "Non_orchestrator"
    if "orchestrator" in s:
        return "Orchestrator_only"
    return str(raw)


def load_candidates(path: Path, failure_field: str | None) -> list[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Invalid JSON in {path} at line {exc.lineno}, column {exc.colno}; use the complete result file, not a truncated excerpt."
        ) from exc
    if not isinstance(data, list):
        raise ValueError("Expected raw JSON root to be a list of benchmark/runtime-condition records.")

    rows: list[dict[str, Any]] = []
    seen_group_record: set[tuple[str, str]] = set()
    for record_i, record in enumerate(data):
        if not isinstance(record, dict):
            raise ValueError(f"Record {record_i} is not an object.")
        if not all(k in record for k in ("benchmark", "runtime_condition", "strategies")):
            raise ValueError(f"Record {record_i} must contain benchmark, runtime_condition, and strategies.")
        benchmark, runtime = str(record["benchmark"]), str(record["runtime_condition"])
        group = (benchmark, runtime)
        if group in seen_group_record:
            raise ValueError(f"Duplicate raw record for {benchmark}/{runtime}; merge it or disambiguate the groups first.")
        seen_group_record.add(group)
        strategies = record["strategies"]
        if not isinstance(strategies, dict):
            raise ValueError(f"strategies in record {record_i} must be an object.")

        for raw_strategy, value in strategies.items():
            strategy = canonical_strategy(str(raw_strategy))
            if isinstance(value, dict):
                items = [(None, value)]
            elif isinstance(value, list):
                items = list(enumerate(value))
            else:
                raise ValueError(f"Strategy {raw_strategy!r} in {benchmark}/{runtime} must be an object or list.")

            for candidate_i, cand in items:
                if not isinstance(cand, dict):
                    raise ValueError(f"Candidate {raw_strategy}[{candidate_i}] in {benchmark}/{runtime} is not an object.")
                p = payload_of(cand)
                values = {}
                for f in OBJECTIVE_FIELDS:
                    num = as_number(p.get(f))
                    if num is None:
                        raise ValueError(f"Missing/non-numeric {f} in {benchmark}/{runtime}/{raw_strategy}[{candidate_i}].")
                    values[f] = num
                candidate_id = str(cand.get("candidate_id")) if cand.get("candidate_id") is not None else (
                    f"{raw_strategy}[{candidate_i}]" if candidate_i is not None else str(raw_strategy)
                )
                uid = str(cand.get("candidate_uid")) if cand.get("candidate_uid") is not None else f"record{record_i}:{candidate_id}"
                rows.append({
                    "benchmark": benchmark,
                    "runtime_condition": runtime,
                    "record_index": record_i,
                    "raw_strategy": str(raw_strategy),
                    "strategy": strategy,
                    "candidate_index": candidate_i,
                    "candidate_id": candidate_id,
                    "candidate_uid": uid,
                    **values,
                    "failure_rate_degradation": failure_degradation(cand, failure_field),
                })
    if not rows:
        raise ValueError("No candidate architectures were loaded.")
    return rows


def read_pareto_fronts(path: Path) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Read the output format produced by pareto_filter.py for optional Table IV metrics."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid Pareto JSON at line {exc.lineno}, column {exc.colno}.") from exc
    groups = data.get("groups") if isinstance(data, dict) else data
    if not isinstance(groups, list):
        raise ValueError("Pareto JSON must be a list or an object containing a 'groups' list.")
    fronts: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for g in groups:
        if not isinstance(g, dict) or "benchmark" not in g or "runtime_condition" not in g:
            raise ValueError("Every Pareto group needs benchmark and runtime_condition fields.")
        front = g.get("pareto_set", g.get("pareto_front", g.get("front")))
        if not isinstance(front, list):
            raise ValueError(f"Pareto group {g['benchmark']}/{g['runtime_condition']} lacks a list-valued pareto_set.")
        key = (str(g["benchmark"]), str(g["runtime_condition"]))
        if key in fronts:
            raise ValueError(f"Duplicate Pareto group {key}.")
        fronts[key] = front
    return fronts


def normalize_scalar(value: float, values: list[float]) -> float:
    lo, hi = min(values), max(values)
    return 0.0 if hi == lo else (value - lo) / (hi - lo)


def add_pareto_alignment(selection_rows: list[dict[str, Any]], normalized_rows: list[dict[str, Any]],
                          fronts: dict[tuple[str, str], list[dict[str, Any]]]) -> None:
    """Annotate selected architectures with Pareto membership and normalized Euclidean distance."""
    by_group_uid = {(r["benchmark"], r["runtime_condition"], r["candidate_uid"]): r for r in normalized_rows}
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in normalized_rows:
        grouped[(row["benchmark"], row["runtime_condition"])].append(row)

    for row in selection_rows:
        row["is_pareto"] = None
        row["d_pareto"] = None
        if row.get("status") != "selected":
            continue
        key = (row["benchmark"], row["runtime_condition"])
        if key not in fronts:
            raise ValueError(f"Pareto JSON is missing group {key} needed for selection alignment.")
        members = grouped.get(key, [])
        if not members:
            raise ValueError(f"No raw candidates found for Pareto group {key}.")
        front = fronts[key]
        if not front:
            raise ValueError(f"Pareto front for {key} is empty.")
        lows_highs = {
            field: [r[field] for r in members]
            for field in ("latency_degradation", "qa_deviation", "agentification_coverage")
        }
        candidate = by_group_uid.get((key[0], key[1], row["selected_candidate_uid"]))
        if candidate is None:
            raise ValueError(f"Selected candidate UID {row['selected_candidate_uid']!r} not found in normalized pool {key}.")

        front_uids = {str(p["candidate_uid"]) for p in front if p.get("candidate_uid") is not None}
        chosen_uid = row["selected_candidate_uid"]
        chosen_vector = (
            row["latency_degradation"], row["qa_deviation"], row["agentification_coverage"]
        )
        front_vectors = []
        front_metric_vectors = set()
        for point in front:
            try:
                raw_vector = tuple(float(point[f]) for f in ("latency_degradation", "qa_deviation", "agentification_coverage"))
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"Malformed Pareto candidate in group {key}: {point!r}") from exc
            front_metric_vectors.add(raw_vector)
            front_vectors.append(tuple(normalize_scalar(raw_vector[i], lows_highs[field]) for i, field in enumerate(
                ("latency_degradation", "qa_deviation", "agentification_coverage")
            )))

        vector = tuple(candidate[f"norm_{alias}"] for alias in ("latency", "qa", "coverage"))
        distances = [math.sqrt(sum((vector[i] - pvec[i]) ** 2 for i in range(3))) for pvec in front_vectors]
        is_member = chosen_uid in front_uids or chosen_vector in front_metric_vectors
        row["is_pareto"] = bool(is_member)
        row["d_pareto"] = 0.0 if is_member else min(distances)


def minmax(values: list[float]) -> list[float]:
    lo, hi = min(values), max(values)
    if math.isclose(hi, lo, rel_tol=0.0, abs_tol=0.0):
        return [0.0] * len(values)
    return [(x - lo) / (hi - lo) for x in values]


def all_positive_weight_vectors(step: float) -> list[tuple[float, float, float]]:
    if not math.isfinite(step) or step <= 0 or step > 1:
        raise ValueError("--weight-step must be in the range (0, 1].")
    units_float = 1.0 / step
    units = round(units_float)
    if units < 3 or not math.isclose(units_float, units, rel_tol=0.0, abs_tol=1e-8):
        raise ValueError("--weight-step must divide 1 exactly and permit three strictly positive weights (e.g. 0.1, 0.05).")
    vectors = []
    for i in range(1, units - 1):
        for j in range(1, units - i):
            k = units - i - j
            if k >= 1:
                # Integer units avoid floating-point sum ambiguity.
                vectors.append((i / units, j / units, k / units))
    return vectors


def filter_slo(rows: list[dict[str, Any]], epsilon: float, ignore_slo: bool) -> tuple[list[dict[str, Any]], str]:
    if ignore_slo:
        return list(rows), "disabled_by_flag"
    missing = [r for r in rows if r["failure_rate_degradation"] is None]
    if missing:
        examples = ", ".join(f"{r['benchmark']}/{r['runtime_condition']}/{r['candidate_id']}" for r in missing[:4])
        raise ValueError(
            f"Cannot enforce failure-rate SLO: {len(missing)}/{len(rows)} candidates have no recognized failure-rate degradation. "
            f"Examples: {examples}. Add an explicit Delta-F field or candidate/baseline failure rates; "
            "only pass --ignore-slo-constraint if you intentionally want an unconstrained analysis."
        )
    return [r for r in rows if r["failure_rate_degradation"] < epsilon], f"enabled: delta_F < {epsilon:g}%"


def deterministic_choice(rows: list[dict[str, Any]], sort_key) -> dict[str, Any]:
    if not rows:
        raise ValueError("Internal error: cannot select from an empty candidate set.")
    return sorted(rows, key=sort_key)[0]


def choose_lexicographic(rows: list[dict[str, Any]], policy: str, delta_l: float, delta_qa: float) -> dict[str, Any]:
    if policy == "Latency-first":
        best_l = min(r["latency_degradation"] for r in rows)
        f1 = [r for r in rows if r["latency_degradation"] <= best_l + delta_l]
        best_q = min(r["qa_deviation"] for r in f1)
        f2 = [r for r in f1 if r["qa_deviation"] <= best_q + delta_qa]
        return deterministic_choice(f2, lambda r: (-r["agentification_coverage"], r["latency_degradation"], r["qa_deviation"], r["candidate_uid"]))
    if policy == "QA-first":
        best_q = min(r["qa_deviation"] for r in rows)
        f1 = [r for r in rows if r["qa_deviation"] <= best_q + delta_qa]
        best_l = min(r["latency_degradation"] for r in f1)
        f2 = [r for r in f1 if r["latency_degradation"] <= best_l + delta_l]
        return deterministic_choice(f2, lambda r: (-r["agentification_coverage"], r["latency_degradation"], r["qa_deviation"], r["candidate_uid"]))
    if policy == "Autonomy-first":
        best_c = max(r["agentification_coverage"] for r in rows)
        f1 = [r for r in rows if r["agentification_coverage"] == best_c]
        best_q = min(r["qa_deviation"] for r in f1)
        f2 = [r for r in f1 if r["qa_deviation"] <= best_q + delta_qa]
        return deterministic_choice(f2, lambda r: (r["latency_degradation"], r["qa_deviation"], r["candidate_uid"]))
    raise ValueError(f"Unknown lexicographic policy {policy!r}.")


def normalize_group(rows: list[dict[str, Any]]) -> None:
    # Normalization is independently applied within each benchmark/runtime group,
    # in accordance with the runtime-specific architecture pool in the paper.
    nl = minmax([r["latency_degradation"] for r in rows])
    nq = minmax([r["qa_deviation"] for r in rows])
    nc = minmax([r["agentification_coverage"] for r in rows])
    for r, l, q, c in zip(rows, nl, nq, nc):
        r["norm_latency"] = l
        r["norm_qa"] = q
        r["norm_coverage"] = c


def optimize_group(group_rows: list[dict[str, Any]], weights: list[tuple[float, float, float]], delta_l: float, delta_qa: float, epsilon_slo: float, ignore_slo: bool):
    # Don't mutate source rows (normalized columns are added to a copy).
    candidates = [dict(r) for r in group_rows]
    normalize_group(candidates)
    feasible, slo_status = filter_slo(candidates, epsilon_slo, ignore_slo)
    group_key = (candidates[0]["benchmark"], candidates[0]["runtime_condition"])
    lex_results = []
    if feasible:
        for policy in ("Latency-first", "QA-first", "Autonomy-first"):
            chosen = choose_lexicographic(feasible, policy, delta_l, delta_qa)
            lex_results.append(make_result(
                chosen, method="Lexicographic", configuration=policy,
                group_key=group_key, feasible_count=len(feasible), slo_status=slo_status,
                delta_l=delta_l, delta_qa=delta_qa,
            ))
    else:
        for policy in ("Latency-first", "QA-first", "Autonomy-first"):
            lex_results.append(empty_result(group_key, "Lexicographic", policy, len(feasible), slo_status, delta_l, delta_qa))

    weight_results = []
    if feasible:
        for wi, (w_l, w_q, w_c) in enumerate(weights, start=1):
            scored = []
            for r in feasible:
                score = w_l * r["norm_latency"] + w_q * r["norm_qa"] + w_c * (1.0 - r["norm_coverage"])
                scored.append((score, r))
            # Deterministic tie break after the stated scalar objective: low normalized latency,
            # low normalized QA deviation, then high coverage, then stable candidate UID.
            score, chosen = sorted(scored, key=lambda item: (
                item[0], item[1]["norm_latency"], item[1]["norm_qa"],
                1.0 - item[1]["norm_coverage"], item[1]["candidate_uid"]
            ))[0]
            result = make_result(
                chosen, method="Weighted-sum", configuration=f"W{wi:03d}",
                group_key=group_key, feasible_count=len(feasible), slo_status=slo_status,
                delta_l=delta_l, delta_qa=delta_qa,
            )
            result.update({"weight_index": wi, "w_L": w_l, "w_QA": w_q, "w_C": w_c, "weighted_sum_score": score})
            weight_results.append(result)
    else:
        for wi, (w_l, w_q, w_c) in enumerate(weights, start=1):
            result = empty_result(group_key, "Weighted-sum", f"W{wi:03d}", len(feasible), slo_status, delta_l, delta_qa)
            result.update({"weight_index": wi, "w_L": w_l, "w_QA": w_q, "w_C": w_c, "weighted_sum_score": None})
            weight_results.append(result)
    return candidates, lex_results, weight_results


def make_result(chosen: dict[str, Any], method: str, configuration: str, group_key: tuple[str, str], feasible_count: int,
                slo_status: str, delta_l: float, delta_qa: float) -> dict[str, Any]:
    return {
        "benchmark": group_key[0],
        "runtime_condition": group_key[1],
        "method": method,
        "configuration": configuration,
        "status": "selected",
        "selected_candidate_uid": chosen["candidate_uid"],
        "selected_candidate_id": chosen["candidate_id"],
        "selected_strategy": chosen["strategy"],
        "selected_raw_strategy": chosen["raw_strategy"],
        "selected_candidate_index": chosen["candidate_index"],
        "latency_degradation": chosen["latency_degradation"],
        "qa_deviation": chosen["qa_deviation"],
        "agentification_coverage": chosen["agentification_coverage"],
        "failure_rate_degradation": chosen["failure_rate_degradation"],
        "norm_latency": chosen["norm_latency"],
        "norm_qa": chosen["norm_qa"],
        "norm_coverage": chosen["norm_coverage"],
        "is_pareto": None,
        "d_pareto": None,
        "feasible_candidate_count": feasible_count,
        "slo_status": slo_status,
        "slo_epsilon_percent": None if slo_status == "disabled_by_flag" else epsilon_from_status(slo_status),
        "delta_l": delta_l,
        "delta_qa": delta_qa,
    }


def epsilon_from_status(status: str) -> float | None:
    try:
        return float(status.rsplit("<", 1)[1].strip().removesuffix("%"))
    except (IndexError, ValueError):
        return None


def empty_result(group_key, method, config, feasible_count, slo_status, delta_l, delta_qa):
    return {
        "benchmark": group_key[0], "runtime_condition": group_key[1],
        "method": method, "configuration": config, "status": "no_slo_feasible_candidate",
        "selected_candidate_uid": None, "selected_candidate_id": None,
        "selected_strategy": None, "selected_raw_strategy": None, "selected_candidate_index": None,
        "latency_degradation": None, "qa_deviation": None, "agentification_coverage": None,
        "failure_rate_degradation": None, "norm_latency": None, "norm_qa": None,
        "norm_coverage": None, "is_pareto": None, "d_pareto": None, "feasible_candidate_count": feasible_count,
        "slo_status": slo_status,
        "slo_epsilon_percent": None if slo_status == "disabled_by_flag" else epsilon_from_status(slo_status),
        "delta_l": delta_l, "delta_qa": delta_qa,
    }


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str] | None = None) -> None:
    if fields is None:
        seen = []
        for row in rows:
            for k in row:
                if k not in seen:
                    seen.append(k)
        fields = seen
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run lexicographic and weighted-sum architecture selection.")
    parser.add_argument("--raw", required=True, help="Complete raw results JSON file")
    parser.add_argument("--outdir", default="alternative_optimization_output", help="Output directory")
    parser.add_argument("--pareto", default=None, help="Optional Pareto-front JSON from pareto_filter.py; adds is_pareto and d_pareto for Table IV")
    parser.add_argument("--weight-step", type=float, default=0.1,
                        help="Simplex grid increment; default 0.1 generates 36 strictly positive 3-objective weight vectors")
    parser.add_argument("--delta-l", type=float, default=1.0,
                        help="Lexicographic latency tolerance in latency-degradation percentage points (default: 1.0)")
    parser.add_argument("--delta-qa", type=float, default=0.1,
                        help="Lexicographic QA tolerance in QA-deviation units/percentage points (default: 0.1)")
    parser.add_argument("--slo-epsilon", type=float, default=2.0,
                        help="Failure-rate degradation limit in percent; feasibility is strict Delta-F < epsilon (default: 2.0)")
    parser.add_argument("--failure-field", default=None,
                        help="Exact field name for Delta-F (%%); otherwise common aliases are auto-detected")
    parser.add_argument("--ignore-slo-constraint", action="store_true",
                        help="Explicitly disable the Delta-F SLO constraint, e.g. when raw JSON has no failure metrics")
    args = parser.parse_args()

    raw_path, outdir = Path(args.raw), Path(args.outdir)
    if not raw_path.is_file():
        parser.error(f"Raw JSON file not found: {raw_path}")
    if args.delta_l < 0 or args.delta_qa < 0:
        parser.error("--delta-l and --delta-qa must be non-negative.")
    if args.slo_epsilon <= 0:
        parser.error("--slo-epsilon must be positive.")

    candidates = load_candidates(raw_path, args.failure_field)
    if args.pareto and not Path(args.pareto).is_file():
        parser.error(f"Pareto JSON file not found: {args.pareto}")
    pareto_fronts = read_pareto_fronts(Path(args.pareto)) if args.pareto else None
    weights = all_positive_weight_vectors(args.weight_step)
    weight_total = len(weights)
    groups: OrderedDict[tuple[str, str], list[dict[str, Any]]] = OrderedDict()
    for row in candidates:
        groups.setdefault((row["benchmark"], row["runtime_condition"]), []).append(row)

    # Enforce complete failure-rate data unless explicitly opting out. Do this globally
    # before optimizing so one group cannot silently use a different feasibility rule.
    missing_failure_count = sum(1 for r in candidates if r["failure_rate_degradation"] is None)
    if not args.ignore_slo_constraint and missing_failure_count:
        examples = ", ".join(
            f"{r['benchmark']}/{r['runtime_condition']}/{r['candidate_id']}"
            for r in candidates if r["failure_rate_degradation"] is None
        )
        example_list = ", ".join(examples.split(", ")[:4])
        raise ValueError(
            f"Cannot enforce Delta-F < {args.slo_epsilon:g}%: {missing_failure_count}/{len(candidates)} candidates lack failure-rate degradation. "
            f"Examples: {example_list}. Add the metric to the raw JSON or run with --ignore-slo-constraint if intentionally omitting the SLO constraint."
        )

    lex_rows: list[dict[str, Any]] = []
    weighted_rows: list[dict[str, Any]] = []
    normalized_rows: list[dict[str, Any]] = []
    for group_rows in groups.values():
        normalized, lex, weighted = optimize_group(
            group_rows, weights, args.delta_l, args.delta_qa,
            args.slo_epsilon, args.ignore_slo_constraint,
        )
        normalized_rows.extend(normalized)
        lex_rows.extend(lex)
        weighted_rows.extend(weighted)

    if pareto_fronts is not None:
        add_pareto_alignment(lex_rows, normalized_rows, pareto_fronts)
        add_pareto_alignment(weighted_rows, normalized_rows, pareto_fronts)

    outdir.mkdir(parents=True, exist_ok=True)
    write_csv(outdir / "lexicographic_selections.csv", lex_rows)
    write_csv(outdir / "weighted_sum_selections.csv", weighted_rows)
    weight_rows = [
        {"weight_index": i, "w_L": wl, "w_QA": wq, "w_C": wc, "sum": wl + wq + wc}
        for i, (wl, wq, wc) in enumerate(weights, start=1)
    ]
    write_csv(outdir / "weight_vectors.csv", weight_rows)
    write_csv(outdir / "normalized_candidates.csv", normalized_rows)

    group_summary = []
    for (benchmark, runtime), group_rows in groups.items():
        lrows = [r for r in lex_rows if r["benchmark"] == benchmark and r["runtime_condition"] == runtime]
        wrows = [r for r in weighted_rows if r["benchmark"] == benchmark and r["runtime_condition"] == runtime]
        group_summary.append({
            "benchmark": benchmark,
            "runtime_condition": runtime,
            "candidate_count": len(group_rows),
            "slo_feasible_candidate_count": (lrows[0]["feasible_candidate_count"] if lrows else 0),
            "lexicographic_selection_rows": sum(r["status"] == "selected" for r in lrows),
            "weighted_sum_selection_rows": sum(r["status"] == "selected" for r in wrows),
            "weighted_sum_unique_selected_candidates": len({r["selected_candidate_uid"] for r in wrows if r["status"] == "selected"}),
        })
    write_csv(outdir / "optimizer_group_summary.csv", group_summary)

    successful_lex = [r for r in lex_rows if r["status"] == "selected"]
    successful_weighted = [r for r in weighted_rows if r["status"] == "selected"]
    total_group_count = len(groups)
    benchmark_count = len({r["benchmark"] for r in candidates})
    runtime_counts = {b: len({r["runtime_condition"] for r in candidates if r["benchmark"] == b})
                      for b in sorted({r["benchmark"] for r in candidates})}
    global_summary = {
        "raw_file": str(raw_path.resolve()),
        "pareto_file": str(Path(args.pareto).resolve()) if args.pareto else None,
        "pareto_alignment_metrics_added": pareto_fronts is not None,
        "benchmark_count": benchmark_count,
        "benchmark_runtime_group_count": total_group_count,
        "runtime_conditions_per_benchmark": runtime_counts,
        "candidate_count": len(candidates),
        "weight_step": args.weight_step,
        "strictly_positive_weight_vector_count": weight_total,
        "weight_vectors": weight_rows,
        "lexicographic_policies": ["Latency-first", "QA-first", "Autonomy-first"],
        "lexicographic_tolerance": {"delta_l": args.delta_l, "delta_qa": args.delta_qa},
        "slo_constraint": "disabled by explicit --ignore-slo-constraint" if args.ignore_slo_constraint else f"failure_rate_degradation < {args.slo_epsilon}%",
        "missing_failure_metric_candidate_count": missing_failure_count,
        "lexicographic_selection_rows_requested": 3 * total_group_count,
        "lexicographic_selection_rows_successful": len(successful_lex),
        "weighted_sum_selection_rows_requested": weight_total * total_group_count,
        "weighted_sum_selection_rows_successful": len(successful_weighted),
        "weighted_sum_unique_selected_candidates_global": len({r["selected_candidate_uid"] for r in successful_weighted}),
        "note": "Selection rows are optimizer decisions. Multiple weight vectors can select the same candidate, so unique selected architectures can be fewer than the number of weight vectors.",
    }
    (outdir / "optimizer_run_summary.json").write_text(json.dumps(global_summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    paper_count_text = (
        f"With a {args.weight_step:g} simplex grid and strictly positive weights, "
        f"the three-objective weighted-sum search evaluates {weight_total} weight vectors per benchmark/runtime-condition group. "
        f"Across {benchmark_count} benchmarks and {total_group_count // max(benchmark_count, 1)} runtime conditions per benchmark, "
        f"this yields {weight_total * total_group_count} weighted-sum selection instances (one selected architecture per weight vector and group), "
        f"alongside {3 * total_group_count} lexicographic selection instances for the three priority policies. "
        "These are selection instances; the number of distinct architecture candidates selected may be lower because different vectors/policies can select the same candidate."
    )
    (outdir / "paper_weight_count.txt").write_text(paper_count_text + "\n", encoding="utf-8")
    (outdir / "optimizer_validation.txt").write_text(
        "Input: " + str(raw_path.resolve()) + "\n"
        + f"Candidates loaded: {len(candidates)}\n"
        + (f"Pareto alignment: {Path(args.pareto).resolve()} (is_pareto and d_pareto added)\n" if args.pareto else "Pareto alignment: not calculated (pass --pareto to add d_pareto and Pareto membership)\n")
        + f"Benchmark/runtime groups: {total_group_count}\n"
        + "Runtime conditions by benchmark: " + ", ".join(f"{b}={n}" for b, n in runtime_counts.items()) + "\n"
        + f"Strictly positive weight vectors: {weight_total} (step={args.weight_step:g})\n"
        + f"Weight selections requested/successful: {weight_total * total_group_count}/{len(successful_weighted)}\n"
        + f"Lexicographic selections requested/successful: {3 * total_group_count}/{len(successful_lex)}\n"
        + f"SLO rule: {global_summary['slo_constraint']}\n"
        + f"Candidates missing failure metric: {missing_failure_count}\n"
        + ("WARNING: expected 3 benchmarks x 8 runtime conditions (=24 groups); outputs include only observed groups.\n"
           if benchmark_count != 3 or total_group_count != 24 or any(n != 8 for n in runtime_counts.values()) else "")
        + "Lexicographic tolerances: delta_L=" + str(args.delta_l) + ", delta_QA=" + str(args.delta_qa) + "\n"
        + "Weighted-sum normalization: min-max independently within each benchmark/runtime-condition candidate pool.\n"
        + "Tie breaking: deterministic objective-order tie breaking after the specified optimization criterion.\n",
        encoding="utf-8",
    )

    print(f"Wrote optimizer outputs to: {outdir.resolve()}")
    print(f"  Weight vectors: {weight_total}")
    print(f"  Weighted-sum selection rows: {len(successful_weighted)} successful / {weight_total * total_group_count} expected")
    print(f"  Lexicographic selection rows: {len(successful_lex)} successful / {3 * total_group_count} expected")
    print(f"  Distinct candidates selected by weighted sum: {global_summary['weighted_sum_unique_selected_candidates_global']}")
    print(f"  SLO: {global_summary['slo_constraint']}")
    if benchmark_count != 3 or total_group_count != 24 or any(n != 8 for n in runtime_counts.values()):
        print("WARNING: input does not contain exactly 3 benchmarks x 8 runtime conditions; see optimizer_validation.txt", file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)

#!/usr/bin/env python3
"""Calculate Table IV for alternative optimizers vs. the Pareto frontier.

Inputs
------
--raw      Complete raw experiment results JSON (same format as pareto_filter.py).
--pareto   Exact Pareto-front JSON produced by pareto_filter.py.
--lex      lexicographic_selections.csv produced by run_alternative_optimizers.py.
--weighted weighted_sum_selections.csv produced by run_alternative_optimizers.py.

Table aggregation
-----------------
For each benchmark/configuration, the three lexicographic policies and four
specific weighted-sum configurations summarize one selected architecture per
observed runtime condition (normally 8). An additional "All weight vectors" row
summarizes every successful weighted-sum selection across every weight vector and
runtime condition. Thus, repeated selections of the same candidate by different
weights count as separate optimization outcomes in the medians and Pareto-member
count; the row separately reports the number of unique selected architectures and
the number of duplicate selection instances. The Pareto-set baseline summarizes all
Pareto-front candidates over the observed runtime conditions; its count is the number
of frontier candidates and its median distance is zero.

Weighted-sum configurations
---------------------------
The heavy configurations choose ONE weight vector globally (the same weights are
then used under every benchmark/runtime condition). Candidate vectors are restricted
to those where the named weight is the unique largest weight. Among those vectors:
- Latency-heavy: lowest median latency_degradation across all groups; then lowest QA;
  then highest coverage.
- QA-heavy: lowest median QA deviation; then lowest latency; then highest coverage.
- Autonomy-heavy: highest median coverage; then lowest QA; then lowest latency.
Ties prefer the larger primary weight, then stable numeric weight order.
Balanced is fixed at (w_L, w_QA, w_C) = (0.3, 0.4, 0.3), corresponding to the
user-specified (w_QA, w_L, w_C) = (0.4, 0.3, 0.3).

Distance d_Pareto follows Eq. (12)-(14) in the draft: min-max normalize the three
objectives separately within each benchmark/runtime candidate pool, then calculate
the minimum Euclidean distance from the selected candidate to its group's Pareto set.
No external Python packages are required.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

OBJECTIVE_FIELDS = ("latency_degradation", "qa_deviation", "agentification_coverage")
WEIGHT_FIELDS = ("w_L", "w_QA", "w_C")
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

CONFIG_ORDER = [
    ("Lexicographic", "Latency-first"),
    ("Lexicographic", "QA-first"),
    ("Lexicographic", "Autonomy-first"),
    ("Weighted sum", "All weight vectors"),
    ("Weighted sum", "Latency-heavy"),
    ("Weighted sum", "QA-heavy"),
    ("Weighted sum", "Autonomy-heavy"),
    ("Weighted sum", "Balanced"),
    ("Pareto", "Pareto-set"),
]


def as_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def candidate_payload(candidate: dict[str, Any]) -> dict[str, Any]:
    result = dict(candidate)
    metrics = candidate.get("metrics")
    if isinstance(metrics, dict):
        for key, value in metrics.items():
            result.setdefault(key, value)
    return result


def failure_degradation(candidate: dict[str, Any], explicit_field: str | None = None) -> float | None:
    payload = candidate_payload(candidate)
    if explicit_field:
        value = as_number(payload.get(explicit_field))
        if value is not None:
            return value
    for field in FAILURE_DEGRADATION_KEYS:
        value = as_number(payload.get(field))
        if value is not None:
            return value
    candidate_failure = next((as_number(payload.get(k)) for k in CANDIDATE_FAILURE_KEYS
                              if as_number(payload.get(k)) is not None), None)
    baseline_failure = next((as_number(payload.get(k)) for k in BASELINE_FAILURE_KEYS
                             if as_number(payload.get(k)) is not None), None)
    if candidate_failure is not None and baseline_failure is not None and baseline_failure > 0:
        return (candidate_failure - baseline_failure) / baseline_failure * 100.0
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


def load_raw(path: Path, failure_field: str | None) -> tuple[list[dict[str, Any]], dict[tuple[str, str], list[dict[str, Any]]]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid raw JSON at line {exc.lineno}, column {exc.colno}; use the complete results JSON.") from exc
    if not isinstance(data, list):
        raise ValueError("Raw JSON root must be a list of benchmark/runtime-condition records.")

    rows: list[dict[str, Any]] = []
    seen_groups: set[tuple[str, str]] = set()
    for record_index, record in enumerate(data):
        if not isinstance(record, dict) or not all(k in record for k in ("benchmark", "runtime_condition", "strategies")):
            raise ValueError(f"Raw record {record_index} must contain benchmark, runtime_condition, and strategies.")
        benchmark, runtime = str(record["benchmark"]), str(record["runtime_condition"])
        group_key = (benchmark, runtime)
        if group_key in seen_groups:
            raise ValueError(f"Duplicate raw benchmark/runtime record: {group_key}.")
        seen_groups.add(group_key)
        strategies = record["strategies"]
        if not isinstance(strategies, dict):
            raise ValueError(f"strategies at raw record {record_index} must be an object.")
        for raw_strategy, value in strategies.items():
            strategy = canonical_strategy(str(raw_strategy))
            if isinstance(value, dict):
                entries = [(None, value)]
            elif isinstance(value, list):
                entries = list(enumerate(value))
            else:
                raise ValueError(f"{raw_strategy} at {group_key} must be an object or list of objects.")
            for candidate_index, candidate in entries:
                if not isinstance(candidate, dict):
                    raise ValueError(f"Candidate {raw_strategy}[{candidate_index}] at {group_key} is not an object.")
                payload = candidate_payload(candidate)
                metrics = {}
                for field in OBJECTIVE_FIELDS:
                    number = as_number(payload.get(field))
                    if number is None:
                        raise ValueError(f"Missing/non-numeric {field} in {group_key}/{raw_strategy}[{candidate_index}].")
                    metrics[field] = number
                candidate_id = str(candidate.get("candidate_id")) if candidate.get("candidate_id") is not None else (
                    f"{raw_strategy}[{candidate_index}]" if candidate_index is not None else str(raw_strategy)
                )
                uid = str(candidate.get("candidate_uid")) if candidate.get("candidate_uid") is not None else f"record{record_index}:{candidate_id}"
                row = {
                    "benchmark": benchmark,
                    "runtime_condition": runtime,
                    "raw_strategy": str(raw_strategy),
                    "strategy": strategy,
                    "candidate_index": candidate_index,
                    "candidate_id": candidate_id,
                    "candidate_uid": uid,
                    **metrics,
                    "failure_rate_degradation": failure_degradation(candidate, failure_field),
                }
                rows.append(row)
    if not rows:
        raise ValueError("No candidates found in raw JSON.")
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(row["benchmark"], row["runtime_condition"])].append(row)
    return rows, dict(grouped)


def read_csv_rows(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise ValueError(f"Required CSV not found: {path}")
    with path.open(newline="", encoding="utf-8-sig") as stream:
        return list(csv.DictReader(stream))


def read_fronts(path: Path) -> dict[tuple[str, str], list[dict[str, Any]]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid Pareto JSON at line {exc.lineno}, column {exc.colno}.") from exc
    groups = data.get("groups") if isinstance(data, dict) else data
    if not isinstance(groups, list):
        raise ValueError("Pareto JSON must be a list or an object containing a 'groups' list.")
    result: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for group in groups:
        if not isinstance(group, dict) or "benchmark" not in group or "runtime_condition" not in group:
            raise ValueError("Every Pareto group must contain benchmark and runtime_condition.")
        front = group.get("pareto_set", group.get("pareto_front", group.get("front")))
        if not isinstance(front, list):
            raise ValueError(f"Pareto group {group['benchmark']}/{group['runtime_condition']} has no list-valued pareto_set.")
        key = (str(group["benchmark"]), str(group["runtime_condition"]))
        if key in result:
            raise ValueError(f"Duplicate Pareto group: {key}.")
        for point in front:
            if not isinstance(point, dict) or any(as_number(point.get(field)) is None for field in OBJECTIVE_FIELDS):
                raise ValueError(f"Malformed Pareto point in group {key}: {point!r}")
        if not front:
            raise ValueError(f"Pareto front for {key} is empty.")
        result[key] = front
    return result


def float_key(value: Any) -> float | None:
    n = as_number(value)
    return None if n is None else round(n, 8)


def weight_key(row: dict[str, Any]) -> tuple[float, float, float] | None:
    vals = tuple(float_key(row.get(k)) for k in WEIGHT_FIELDS)
    if any(v is None for v in vals):
        return None
    return vals  # type: ignore[return-value]


def normalize(value: float, minimum: float, maximum: float) -> float:
    if math.isclose(minimum, maximum, rel_tol=0.0, abs_tol=0.0):
        return 0.0
    return (value - minimum) / (maximum - minimum)


def metric_vector(row: dict[str, Any]) -> tuple[float, float, float]:
    return tuple(float(row[field]) for field in OBJECTIVE_FIELDS)  # type: ignore[return-value]


def nearest_pareto_metrics(
    selected_row: dict[str, Any],
    group_key: tuple[str, str],
    raw_group: list[dict[str, Any]],
    front: list[dict[str, Any]],
    raw_uid_map: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    uid = selected_row.get("selected_candidate_uid") or selected_row.get("candidate_uid")
    matched = raw_uid_map.get(str(uid)) if uid not in (None, "") else None
    if matched is None:
        vector = tuple(as_number(selected_row.get(f)) for f in OBJECTIVE_FIELDS)
        candidates = [r for r in raw_group if all(v is not None for v in vector) and all(
            math.isclose(r[f], float(v), rel_tol=0.0, abs_tol=1e-10)
            for f, v in zip(OBJECTIVE_FIELDS, vector)
        )]
        if not candidates:
            raise ValueError(f"Cannot match optimizer selection to raw candidate for {group_key}: {selected_row}")
        matched = candidates[0]

    minima = {f: min(r[f] for r in raw_group) for f in OBJECTIVE_FIELDS}
    maxima = {f: max(r[f] for r in raw_group) for f in OBJECTIVE_FIELDS}
    point = {f: normalize(matched[f], minima[f], maxima[f]) for f in OBJECTIVE_FIELDS}
    front_points = [{f: normalize(float(p[f]), minima[f], maxima[f]) for f in OBJECTIVE_FIELDS} for p in front]
    front_uids = {str(p.get("candidate_uid")) for p in front if p.get("candidate_uid") not in (None, "")}
    vector = metric_vector(matched)
    front_vectors = [metric_vector(p) for p in front]
    is_pareto = (matched["candidate_uid"] in front_uids) or any(
        all(math.isclose(a, b, rel_tol=0.0, abs_tol=1e-10) for a, b in zip(vector, fv))
        for fv in front_vectors
    )
    distance = 0.0 if is_pareto else min(
        math.sqrt(sum((point[f] - p[f]) ** 2 for f in OBJECTIVE_FIELDS)) for p in front_points
    )
    return {
        "selected_candidate_uid": matched["candidate_uid"],
        **matched,
        "is_pareto": bool(is_pareto),
        "d_pareto": float(distance),
    }


def annotate_optimizer_rows(
    optimizer_rows: list[dict[str, Any]], raw_groups: dict[tuple[str, str], list[dict[str, Any]]],
    fronts: dict[tuple[str, str], list[dict[str, Any]]], raw_uid_map: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    annotated = []
    for row in optimizer_rows:
        key = (str(row.get("benchmark", "")), str(row.get("runtime_condition", "")))
        if row.get("status", "selected") != "selected" or not row.get("selected_candidate_uid"):
            continue
        if key not in raw_groups or key not in fronts:
            raise ValueError(f"Optimizer result refers to missing raw/Pareto group {key}.")
        chosen = nearest_pareto_metrics(row, key, raw_groups[key], fronts[key], raw_uid_map)
        # Prefer raw data for metrics (including failure rate), while preserve optimizer metadata.
        out = dict(row)
        for field in (*OBJECTIVE_FIELDS, "failure_rate_degradation"):
            out[field] = chosen.get(field)
        out.update({"selected_candidate_uid": chosen["selected_candidate_uid"],
                    "is_pareto": chosen["is_pareto"], "d_pareto": chosen["d_pareto"]})
        annotated.append(out)
    return annotated


def choose_weight_vectors(
    weighted_rows: list[dict[str, Any]], group_keys: list[tuple[str, str]], balanced: tuple[float, float, float]
) -> tuple[dict[str, tuple[float, float, float]], list[dict[str, Any]]]:
    selected = [r for r in weighted_rows if r.get("status", "selected") == "selected"]
    by_weight_group: dict[tuple[float, float, float], dict[tuple[str, str], dict[str, Any]]] = defaultdict(dict)
    for row in selected:
        wk = weight_key(row)
        if wk is None:
            raise ValueError("Weighted-sum selection CSV has selected rows without w_L, w_QA, and w_C fields.")
        group = (str(row["benchmark"]), str(row["runtime_condition"]))
        if group in by_weight_group[wk]:
            # Duplicate rows can happen only in malformed/concatenated results.
            raise ValueError(f"Duplicate selection for weight vector {wk} in group {group}.")
        by_weight_group[wk][group] = row

    expected = set(group_keys)
    ranking_rows: list[dict[str, Any]] = []
    complete: dict[tuple[float, float, float], list[dict[str, Any]]] = {}
    for wk, group_map in by_weight_group.items():
        missing = expected - set(group_map)
        if missing:
            continue
        group_rows = [group_map[key] for key in group_keys]
        values = {
            "median_latency_degradation": statistics.median(float(r["latency_degradation"]) for r in group_rows),
            "median_qa_deviation": statistics.median(float(r["qa_deviation"]) for r in group_rows),
            "median_agentification_coverage": statistics.median(float(r["agentification_coverage"]) for r in group_rows),
        }
        complete[wk] = group_rows
        ranking_rows.append({"w_L": wk[0], "w_QA": wk[1], "w_C": wk[2],
                             "selected_group_count": len(group_rows), **values})

    if not complete:
        raise ValueError(
            "No weight vector has a selected architecture for every raw benchmark/runtime group. "
            "Supply the full weighted_sum_selections.csv generated by run_alternative_optimizers.py."
        )

    by_key = {weight_key(r): r for r in ranking_rows}
    categories: dict[str, tuple[float, float, float]] = {}
    specs = [
        ("Latency-heavy", 0, lambda r: (r["median_latency_degradation"], r["median_qa_deviation"], -r["median_agentification_coverage"], -r["w_L"], r["w_QA"], r["w_C"])),
        ("QA-heavy", 1, lambda r: (r["median_qa_deviation"], r["median_latency_degradation"], -r["median_agentification_coverage"], -r["w_QA"], r["w_L"], r["w_C"])),
        ("Autonomy-heavy", 2, lambda r: (-r["median_agentification_coverage"], r["median_qa_deviation"], r["median_latency_degradation"], -r["w_C"], r["w_L"], r["w_QA"])),
    ]
    for label, target_idx, sort_key in specs:
        candidates = []
        for wk, row in by_key.items():
            vals = wk
            # Restrict to unique largest weight to keep the preference categories distinct.
            if vals[target_idx] > max(vals[i] for i in range(3) if i != target_idx):
                candidates.append(row)
        if not candidates:
            raise ValueError(f"No strictly {label} vectors found. Check the weight grid used for optimization.")
        best = sorted(candidates, key=sort_key)[0]
        categories[label] = (float(best["w_L"]), float(best["w_QA"]), float(best["w_C"]))

    balanced_key = tuple(round(v, 8) for v in balanced)
    if balanced_key not in complete:
        available = ", ".join(str(k) for k in sorted(complete)[:8])
        raise ValueError(
            f"Balanced weight vector (w_L,w_QA,w_C)={balanced_key} is not present in weighted-sum results. "
            "Run the optimizer with a grid that contains this vector (default step 0.1). "
            f"Available examples: {available}"
        )
    categories["Balanced"] = balanced_key

    # Annotate ranking rows with strict-largest category membership and chosen flags.
    for row in ranking_rows:
        wk = weight_key(row)
        assert wk is not None
        for label, idx, _ in specs:
            row[f"is_{label.lower().replace('-', '_')}_eligible"] = wk[idx] > max(wk[j] for j in range(3) if j != idx)
            row[f"selected_as_{label.lower().replace('-', '_')}"] = wk == categories[label]
        row["selected_as_balanced"] = wk == balanced_key
    ranking_rows.sort(key=lambda r: (r["w_L"], r["w_QA"], r["w_C"]))
    return categories, ranking_rows


def select_configurations(
    lex_rows: list[dict[str, Any]], weighted_rows: list[dict[str, Any]],
    chosen_weights: dict[str, tuple[float, float, float]], group_keys: list[tuple[str, str]],
    raw_groups: dict[tuple[str, str], list[dict[str, Any]]], fronts: dict[tuple[str, str], list[dict[str, Any]]],
    raw_uid_map: dict[str, dict[str, Any]],
) -> tuple[dict[tuple[str, str], list[dict[str, Any]]], list[dict[str, Any]]]:
    all_configs: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    diagnostics: list[dict[str, Any]] = []

    # Lexicographic configurations use their fixed policy name.
    for row in lex_rows:
        if row.get("status", "selected") != "selected" or not row.get("selected_candidate_uid"):
            continue
        policy = str(row.get("configuration", ""))
        if policy not in {"Latency-first", "QA-first", "Autonomy-first"}:
            continue
        key = (str(row["benchmark"]), str(row["runtime_condition"]))
        enriched = annotate_optimizer_rows([row], raw_groups, fronts, raw_uid_map)[0]
        all_configs[("Lexicographic", policy)].append(enriched)
        diagnostics.append({"method": "Lexicographic", "configuration": policy, **enriched})

    # Weighted choices use globally fixed weights chosen by objective-specific median.
    weighted_by_group_weight = {}
    for row in weighted_rows:
        if row.get("status", "selected") != "selected" or not row.get("selected_candidate_uid"):
            continue
        wk = weight_key(row)
        if wk is None:
            continue
        group = (str(row["benchmark"]), str(row["runtime_condition"]))
        weighted_by_group_weight[(wk, group)] = row

    for label, wk in chosen_weights.items():
        for group in group_keys:
            row = weighted_by_group_weight.get((wk, group))
            if row is None:
                # It can only be missing if the optimizer had no feasible candidate.
                diagnostics.append({"method": "Weighted sum", "configuration": label,
                                    "benchmark": group[0], "runtime_condition": group[1],
                                    "status": "missing_selection", "w_L": wk[0], "w_QA": wk[1], "w_C": wk[2]})
                continue
            enriched = annotate_optimizer_rows([row], raw_groups, fronts, raw_uid_map)[0]
            enriched["configuration"] = label
            enriched["w_L"], enriched["w_QA"], enriched["w_C"] = wk
            all_configs[("Weighted sum", label)].append(enriched)
            diagnostics.append({"method": "Weighted sum", "configuration": label, **enriched})

    # Aggregate every successful weighted-sum selection across the full grid.
    # A candidate can be selected by several weight vectors; those repeated
    # selections are retained in diagnostics but deduplicated for architecture-
    # level summary statistics below.
    for row in weighted_rows:
        if row.get("status", "selected") != "selected" or not row.get("selected_candidate_uid"):
            continue
        group = (str(row.get("benchmark", "")), str(row.get("runtime_condition", "")))
        if group not in raw_groups or group not in fronts:
            raise ValueError(f"Weighted-sum result refers to missing raw/Pareto group {group}.")
        source_configuration = str(row.get("configuration", ""))
        enriched = annotate_optimizer_rows([row], raw_groups, fronts, raw_uid_map)[0]
        enriched["weight_configuration"] = source_configuration
        enriched["configuration"] = "All weight vectors"
        all_configs[("Weighted sum", "All weight vectors")].append(enriched)
        diagnostics.append({
            "method": "Weighted sum",
            "configuration": "All weight vectors",
            "weight_configuration": source_configuration,
            **enriched,
        })

    return all_configs, diagnostics


def median_value(rows: Iterable[dict[str, Any]], field: str) -> float | None:
    values = [as_number(r.get(field)) for r in rows]
    values = [v for v in values if v is not None]
    return statistics.median(values) if values else None


def sum_value(rows: Iterable[dict[str, Any]], field: str) -> float | None:
    values = [as_number(r.get(field)) for r in rows]
    values = [v for v in values if v is not None]
    return sum(values) if values else None

def mean_value(rows: Iterable[dict[str, Any]], field: str) -> float | None:
    values = [as_number(r.get(field)) for r in rows]
    values = [v for v in values if v is not None]
    return statistics.mean(values) if values else None

def summarize_selected(rows: list[dict[str, Any]], benchmark: str, method: str, config: str,
                       weights: tuple[float, float, float] | None = None) -> dict[str, Any]:
    return {
        "benchmark": benchmark,
        "method": method,
        "configuration": config,
        "w_L": weights[0] if weights else None,
        "w_QA": weights[1] if weights else None,
        "w_C": weights[2] if weights else None,
        "median_latency_degradation": median_value(rows, "latency_degradation"),
        "median_qa_deviation": median_value(rows, "qa_deviation"),
        "median_agentification_coverage": median_value(rows, "agentification_coverage"),
        "median_failure_rate_degradation": median_value(rows, "failure_rate_degradation"),
        "total_pareto_members": sum(1 for r in rows if r.get("is_pareto") is True),
        "unique_pareto_architecture_count": len({
            (str(r.get("benchmark", benchmark)), str(r.get("runtime_condition", "")),
             str(r.get("selected_candidate_uid", r.get("candidate_uid", ""))))
            for r in rows if r.get("is_pareto") is True
        }),
        "median_d_pareto": mean_value(rows, "d_pareto"),
        "selection_count": len(rows),
        "selection_instance_count": len(rows),
        "unique_architecture_count": len({
            (str(r.get("benchmark", benchmark)), str(r.get("runtime_condition", "")),
             str(r.get("selected_candidate_uid", r.get("candidate_uid", ""))))
            for r in rows
        }),
        "duplicate_selection_instances": len(rows) - len({
            (str(r.get("benchmark", benchmark)), str(r.get("runtime_condition", "")),
             str(r.get("selected_candidate_uid", r.get("candidate_uid", ""))))
            for r in rows
        }),
        "runtime_condition_count": len({r["runtime_condition"] for r in rows}),
        "failure_metric_count": sum(1 for r in rows if as_number(r.get("failure_rate_degradation")) is not None),
    }


def enrich_pareto_baseline(
    raw_groups: dict[tuple[str, str], list[dict[str, Any]]], fronts: dict[tuple[str, str], list[dict[str, Any]]],
    raw_uid_map: dict[str, dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    by_benchmark: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for key, front in fronts.items():
        for point in front:
            uid = point.get("candidate_uid")
            matched = raw_uid_map.get(str(uid)) if uid not in (None, "") else None
            if matched is None:
                vec = tuple(as_number(point.get(f)) for f in OBJECTIVE_FIELDS)
                choices = [r for r in raw_groups[key] if all(v is not None for v in vec) and all(
                    math.isclose(r[f], float(v), rel_tol=0.0, abs_tol=1e-10)
                    for f, v in zip(OBJECTIVE_FIELDS, vec)
                )]
                if not choices:
                    raise ValueError(f"Could not match Pareto member to raw candidate for {key}: {point}")
                # Prefer candidate ID/index when available to preserve duplicate objective vectors.
                wanted_id = point.get("candidate_id")
                wanted_idx = point.get("candidate_index")
                if wanted_id is not None:
                    exact = [r for r in choices if r["candidate_id"] == str(wanted_id)]
                    if exact:
                        choices = exact
                if wanted_idx is not None:
                    exact = [r for r in choices if r["candidate_index"] == wanted_idx]
                    if exact:
                        choices = exact
                matched = choices[0]
            row = dict(matched)
            row["is_pareto"] = True
            row["d_pareto"] = 0.0
            row["runtime_condition"] = key[1]
            by_benchmark[key[0]].append(row)
    return dict(by_benchmark)


def latex_num(value: Any, digits: int) -> str:
    number = as_number(value)
    return "--" if number is None else f"{number:.{digits}f}"


def latex_cell(summary_map: dict[str, dict[str, Any]], field: str, digits: int, benchmarks: list[str]) -> str:
    parts = [f"{b}: {latex_num(summary_map[b].get(field), digits)}" if b in summary_map else f"{b}: --" for b in benchmarks]
    return r"\shortstack{" + r"\\".join(parts) + "}"


def write_latex(path: Path, summary_rows: list[dict[str, Any]], benchmarks: list[str], weights: dict[str, tuple[float, float, float]]) -> None:
    by_config: dict[tuple[str, str], dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in summary_rows:
        by_config[(row["method"], row["configuration"])][row["benchmark"]] = row

    cols = [
        ("median_latency_degradation", 2), ("median_qa_deviation", 3),
        ("median_agentification_coverage", 3), ("median_failure_rate_degradation", 2),
        ("total_pareto_members", 0), ("median_d_pareto", 4),
    ]
    lines = [
        "% Generated by calculate_table_iv.py. Specific optimizer configurations use selections across runtime conditions.",
        "% All weight vectors summarizes every successful (weight vector, runtime condition) selection; repeated selections count as repeated outcomes.",
        "% Weighted-heavy weight vectors are chosen globally by best median primary-objective result among vectors with a unique largest weight.",
        f"% Balanced weights in (w_L,w_QA,w_C) order: {weights['Balanced'][0]:.1f}, {weights['Balanced'][1]:.1f}, {weights['Balanced'][2]:.1f}.",
        "% Weight footnotes below use (w_L,w_QA,w_C) order.",
    ]
    row_meta = [
        ("Lexicographic", "Latency-first", "Lexicographic", "Latency-first"),
        ("Lexicographic", "QA-first", "", "QA-first"),
        ("Lexicographic", "Autonomy-first", "", "Autonomy-first"),
        ("Weighted sum", "All weight vectors", "Weighted sum", "All weight vectors"),
        ("Weighted sum", "Latency-heavy", "", r"Latency-heavy$^{1}$"),
        ("Weighted sum", "QA-heavy", "", r"QA-heavy$^{2}$"),
        ("Weighted sum", "Autonomy-heavy", "", r"Autonomy-heavy$^{3}$"),
        ("Weighted sum", "Balanced", "", r"Balanced$^{4}$"),
        ("Pareto", "Pareto-set", "Pareto-set", "-"),
    ]
    for method, config, _method_cell, config_label in row_meta:
        if (method, config) not in by_config:
            continue
        summary_map = by_config[(method, config)]
        cells = [method if _method_cell else "", config_label]
        for field, digits in cols:
            cells.append(latex_cell(summary_map, field, digits, benchmarks))
        lines.append(" & ".join(cells) + r" \\")
        if config in {"Autonomy-first", "Balanced"}:
            lines.append(r"\midrule")
    for foot_num, name in [(1, "Latency-heavy"), (2, "QA-heavy"), (3, "Autonomy-heavy"), (4, "Balanced")]:
        w = weights[name]
        lines.append(f"% {foot_num}: {name}, $(w_L,w_{{QA}},w_C)=({w[0]:.1f},{w[1]:.1f},{w[2]:.1f})$.")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Calculate Table IV metrics for alternative optimizers vs. exact Pareto sets.")
    parser.add_argument("--raw", required=True, help="Complete raw experiment results JSON")
    parser.add_argument("--pareto", required=True, help="Pareto-front JSON from pareto_filter.py")
    parser.add_argument("--lex", required=True, help="lexicographic_selections.csv from run_alternative_optimizers.py")
    parser.add_argument("--weighted", required=True, help="weighted_sum_selections.csv from run_alternative_optimizers.py")
    parser.add_argument("--outdir", default="table_iv_output", help="Output directory")
    parser.add_argument("--failure-field", default=None, help="Optional exact raw candidate field containing Delta F (%%)")
    parser.add_argument("--balanced-weights", default="0.3,0.4,0.3", help="Balanced weights in w_L,w_QA,w_C order; default corresponds to user vector w_QA,w_L,w_C = 0.4,0.3,0.3")
    parser.add_argument("--expect-24-groups", action="store_true", help="Exit if inputs do not contain exactly 3 benchmarks x 8 runtime conditions")
    args = parser.parse_args()

    raw_path, pareto_path, lex_path, weighted_path = map(Path, (args.raw, args.pareto, args.lex, args.weighted))
    for p in (raw_path, pareto_path, lex_path, weighted_path):
        if not p.is_file():
            parser.error(f"Input file not found: {p}")
    try:
        balanced_vals = tuple(float(x.strip()) for x in args.balanced_weights.split(","))
    except ValueError as exc:
        parser.error("--balanced-weights must be three comma-separated numbers in w_L,w_QA,w_C order.")
    if len(balanced_vals) != 3 or any(not math.isfinite(v) or v <= 0 for v in balanced_vals) or not math.isclose(sum(balanced_vals), 1.0, abs_tol=1e-8):
        parser.error("--balanced-weights must contain three strictly positive weights that sum to one.")
    balanced = tuple(round(v, 8) for v in balanced_vals)

    raw_rows, raw_groups = load_raw(raw_path, args.failure_field)
    fronts = read_fronts(pareto_path)
    if set(raw_groups) != set(fronts):
        missing = sorted(set(raw_groups) - set(fronts))
        extra = sorted(set(fronts) - set(raw_groups))
        raise ValueError(f"Raw/Pareto group mismatch. Missing fronts={missing}; fronts without raw groups={extra}.")

    raw_uid_map = {r["candidate_uid"]: r for r in raw_rows}
    if len(raw_uid_map) != len(raw_rows):
        raise ValueError("Raw candidate_uid values are not unique; supply unique candidate_uid values in raw JSON.")

    lex_rows = read_csv_rows(lex_path)
    weighted_rows = read_csv_rows(weighted_path)
    group_keys = sorted(raw_groups)
    chosen_weights, weight_ranking = choose_weight_vectors(weighted_rows, group_keys, balanced)
    selected_configs, diagnostics = select_configurations(
        lex_rows, weighted_rows, chosen_weights, group_keys, raw_groups, fronts, raw_uid_map
    )

    benchmarks = sorted({b for b, _ in group_keys})
    pareto_by_benchmark = enrich_pareto_baseline(raw_groups, fronts, raw_uid_map)
    summary_rows: list[dict[str, Any]] = []
    all_config_keys = [(m, c) for m, c in CONFIG_ORDER if m != "Pareto"]
    for benchmark in benchmarks:
        for method, config in all_config_keys:
            rows = [r for r in selected_configs.get((method, config), []) if r["benchmark"] == benchmark]
            weights = chosen_weights.get(config) if method == "Weighted sum" else None
            if method == "Weighted sum" and config == "All weight vectors":
                # Treat each (weight vector, runtime condition) selection as an
                # optimizer outcome. Report unique architecture counts separately
                # so repeated selections remain visible without being mistaken for
                # additional distinct architectures.
                summary = summarize_selected(rows, benchmark, method, config, None)
                summary["selection_count"] = len(rows)
                summary["selection_instance_count"] = len(rows)
                summary["unique_architecture_count"] = len({
                    (str(r.get("benchmark", benchmark)), str(r.get("runtime_condition", "")),
                     str(r.get("selected_candidate_uid", r.get("candidate_uid", ""))))
                    for r in rows
                })
                summary["duplicate_selection_instances"] = (
                    summary["selection_instance_count"] - summary["unique_architecture_count"]
                )
                summary["unique_pareto_architecture_count"] = len({
                    (str(r.get("benchmark", benchmark)), str(r.get("runtime_condition", "")),
                     str(r.get("selected_candidate_uid", r.get("candidate_uid", ""))))
                    for r in rows if r.get("is_pareto") is True
                })
                summary["weight_vector_count"] = len({weight_key(r) for r in rows if weight_key(r) is not None})
                summary_rows.append(summary)
            else:
                summary_rows.append(summarize_selected(rows, benchmark, method, config, weights))
        pareto_rows = pareto_by_benchmark.get(benchmark, [])
        pareto_summary = {
            "benchmark": benchmark, "method": "Pareto", "configuration": "Pareto-set",
            "w_L": None, "w_QA": None, "w_C": None,
            "median_latency_degradation": median_value(pareto_rows, "latency_degradation"),
            "median_qa_deviation": median_value(pareto_rows, "qa_deviation"),
            "median_agentification_coverage": median_value(pareto_rows, "agentification_coverage"),
            "median_failure_rate_degradation": median_value(pareto_rows, "failure_rate_degradation"),
            "total_pareto_members": len(pareto_rows),
            "unique_pareto_architecture_count": len({
                (str(r.get("benchmark", benchmark)), str(r.get("runtime_condition", "")),
                 str(r.get("candidate_uid", ""))) for r in pareto_rows
            }),
            "median_d_pareto": 0.0 if pareto_rows else None,
            "selection_count": len(pareto_rows),
            "selection_instance_count": len(pareto_rows),
            "unique_architecture_count": len({
                (str(r.get("benchmark", benchmark)), str(r.get("runtime_condition", "")),
                 str(r.get("candidate_uid", ""))) for r in pareto_rows
            }),
            "duplicate_selection_instances": 0,
            "runtime_condition_count": len({r["runtime_condition"] for r in pareto_rows}),
            "failure_metric_count": sum(1 for r in pareto_rows if as_number(r.get("failure_rate_degradation")) is not None),
        }
        summary_rows.append(pareto_summary)

    # Preferred row order in CSV and LaTeX.
    order_map = {key: i for i, key in enumerate(CONFIG_ORDER)}
    summary_rows.sort(key=lambda r: (r["benchmark"], order_map.get((r["method"], r["configuration"]), 999)))
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
        fields: list[str] = []
        for row in rows:
            for key in row:
                if key not in fields:
                    fields.append(key)
        with path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)

    write_csv(outdir / "table_iv_summary.csv", summary_rows)
    write_csv(outdir / "table_iv_runtime_condition.csv", diagnostics)
    write_csv(outdir / "table_iv_weight_vector_ranking.csv", weight_ranking)

    # Add the selected weight triple to each weighted configuration record for a compact lookup.
    weight_choice_rows = []
    for config, values in chosen_weights.items():
        weight_choice_rows.append({"configuration": config, "w_L": values[0], "w_QA": values[1], "w_C": values[2],
                                   "selection_rule": {
                                       "Latency-heavy": "among unique-largest-w_L vectors, minimize global median latency, then QA, then maximize coverage",
                                       "QA-heavy": "among unique-largest-w_QA vectors, minimize global median QA, then latency, then maximize coverage",
                                       "Autonomy-heavy": "among unique-largest-w_C vectors, maximize global median coverage, then minimize QA, then latency",
                                       "Balanced": "fixed as (w_L,w_QA,w_C)=(0.3,0.4,0.3), per requested (w_QA,w_L,w_C)=(0.4,0.3,0.3)",
                                   }[config]})
    (outdir / "table_iv_weight_choices.json").write_text(json.dumps(weight_choice_rows, indent=2) + "\n", encoding="utf-8")
    write_latex(outdir / "table_iv_latex_rows.tex", summary_rows, benchmarks, chosen_weights)

    runtime_counts = {b: len({r for bb, r in group_keys if bb == b}) for b in benchmarks}
    incomplete_configs = []
    for method, config in all_config_keys:
        for benchmark in benchmarks:
            sr = next(r for r in summary_rows if r["benchmark"] == benchmark and r["method"] == method and r["configuration"] == config)
            expected = runtime_counts[benchmark]
            if sr["runtime_condition_count"] != expected:
                incomplete_configs.append(f"{benchmark}/{config}: got {sr['runtime_condition_count']} selected conditions, expected {expected}")

    validation_lines = [
        f"Raw input: {raw_path.resolve()}", f"Pareto input: {pareto_path.resolve()}",
        f"Lexicographic input: {lex_path.resolve()}", f"Weighted-sum input: {weighted_path.resolve()}",
        f"Raw candidate count: {len(raw_rows)}", f"Benchmark/runtime groups: {len(group_keys)}",
        "Runtime conditions by benchmark: " + ", ".join(f"{b}={n}" for b, n in runtime_counts.items()),
        "Global heavy-weight selection rule: choose one weight vector across all groups by median primary-objective performance.",
        "Balanced vector: (w_L,w_QA,w_C)=" + str(balanced),
        "Chosen weights: " + "; ".join(f"{name}=({v[0]:.1f},{v[1]:.1f},{v[2]:.1f})" for name, v in chosen_weights.items()),
        "Pareto membership: selected point UID match or objective-vector match with the supplied exact front.",
        "d_Pareto: nearest Euclidean distance after min-max normalization within the raw benchmark/runtime candidate pool.",
        "Table IV metric aggregation: specific optimizer configurations use one result per runtime condition; the 'All weight vectors' row aggregates all successful weight-vector/runtime-condition selections.",
        "For 'All weight vectors', medians and total_pareto_members count selection instances, including repeated outcomes for the same candidate. unique_architecture_count, unique_pareto_architecture_count, and duplicate_selection_instances distinguish distinct architectures from repeated selections.",
    ]
    if not any(as_number(r.get("failure_rate_degradation")) is not None for r in raw_rows):
        validation_lines.append("WARNING: No failure-rate degradation available; median Delta F (%) will be blank.")
    elif any(as_number(r.get("failure_rate_degradation")) is None for r in raw_rows):
        validation_lines.append("WARNING: Some raw candidates lack failure-rate degradation; medians use available values only.")
    if len(benchmarks) != 3 or len(group_keys) != 24 or any(n != 8 for n in runtime_counts.values()):
        validation_lines.append("WARNING: Expected 3 benchmarks x 8 runtime conditions (=24 groups); summaries include observed groups only.")
    if incomplete_configs:
        validation_lines.append("WARNING: Missing optimizer selections: " + " | ".join(incomplete_configs))
    (outdir / "table_iv_validation.txt").write_text("\n".join(validation_lines) + "\n", encoding="utf-8")

    if args.expect_24_groups and (len(benchmarks) != 3 or len(group_keys) != 24 or any(n != 8 for n in runtime_counts.values())):
        raise ValueError("--expect-24-groups was set, but inputs do not include exactly 3 x 8 groups; see validation report.")

    print(f"Wrote Table IV outputs to: {outdir.resolve()}")
    print(f"  summary rows: {len(summary_rows)} ({len(benchmarks)} benchmarks x 9 configurations)")
    print(f"  LaTeX rows: {outdir / 'table_iv_latex_rows.tex'}")
    print("  Chosen weighted-sum vectors (w_L,w_QA,w_C):")
    for name, values in chosen_weights.items():
        print(f"    {name}: ({values[0]:.1f}, {values[1]:.1f}, {values[2]:.1f})")
    if incomplete_configs:
        print(f"WARNING: {len(incomplete_configs)} configuration/benchmark summaries are incomplete. See table_iv_validation.txt", file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)

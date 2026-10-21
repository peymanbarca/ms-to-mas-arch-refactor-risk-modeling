#!/usr/bin/env python3
"""Calculate the agentification-strategy summary table from raw results + Pareto fronts.

The script follows the metrics in the uploaded paper draft's Table III and Eq. (12)-(14):
- Medians are calculated per benchmark and strategy, pooling candidates across all runtime
  conditions for that benchmark. For Complexity-ranked refactoring, all generated candidate
  architectures across the eight runtime conditions are included.
- Total Pareto members counts strategy candidates that belong to the supplied exact Pareto
  set, summed across runtime conditions.
- d_Pareto for every candidate is its minimum Euclidean distance to that runtime group's
  Pareto set after min-max normalization of latency degradation, QA deviation, and coverage
  over all candidates in that same benchmark/runtime group.
- The Pareto-set row summarizes only Pareto members; by definition, its median d_Pareto is 0.

Inputs:
  1) Raw JSON list, each record has benchmark, runtime_condition, and strategies.
     A strategy value can be a single candidate object or a list of candidate objects.
  2) Pareto JSON produced by pareto_filter.py (root object with a `groups` array, each group
     carrying benchmark, runtime_condition, and pareto_set).

Outputs in --outdir:
  table_iii_summary.csv                 one row per benchmark/strategy + Pareto-set row
  table_iii_runtime_condition.csv       per benchmark/runtime-condition/strategy diagnostics
  table_iii_candidate_distances.csv     each candidate's Pareto membership and d_Pareto
  table_iii_latex_rows.tex              LaTeX rows to replace the TBD body of Table III
  table_iii_validation.txt              group/metric validation report

Failure rate: the sample schema shown in the draft excerpt contains only the three objective
fields. The script looks for explicit relative-degradation fields and can derive relative
failure-rate degradation when both candidate and baseline failure rates are available. If no
failure-rate metric is present, it leaves Median(Delta F%) blank instead of inventing a value.

Uses Python standard library only.
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

OBJECTIVE_FIELDS = (
    "latency_degradation",
    "qa_deviation",
    "agentification_coverage",
)

STRATEGY_ORDER = [
    "Orchestrator_only",
    "Non_orchestrator",
    "User_interacting",
    "Complexity_ranked_refactoring",
]

STRATEGY_LABELS = {
    "Orchestrator_only": "Orchestrator-only",
    "Non_orchestrator": "Non-orchestrator",
    "User_interacting": "User-interacting",
    "Complexity_ranked_refactoring": "Complexity-ranked iterative refactoring",
}

# Only keys that explicitly denote a degradation/increase are treated as Delta F (%).
FAILURE_DEGRADATION_KEYS = (
    "failure_rate_degradation",
    "failure_rate_degradation_pct",
    "failure_rate_degradation_percent",
    "delta_failure_rate_pct",
    "delta_failure_rate_percent",
    "failure_rate_increase_pct",
    "failure_rate_increase_percent",
    "failure_rate_amplification_pct",
    "failure_degradation_pct",
    "delta_f_pct",
)

CANDIDATE_FAILURE_KEYS = (
    "failure_rate",
    "failure_rate_pct",
    "failure_rate_percent",
    "fail_rate",
)
BASELINE_FAILURE_KEYS = (
    "baseline_failure_rate",
    "baseline_failure_rate_pct",
    "baseline_failure_rate_percent",
    "original_failure_rate",
    "microservice_failure_rate",
    "baseline_fail_rate",
    "failure_rate_baseline",
)


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _candidate_payload(candidate: dict[str, Any]) -> dict[str, Any]:
    """Support direct fields (the shown schema) and an optional nested `metrics` object."""
    metrics = candidate.get("metrics")
    if isinstance(metrics, dict):
        merged = dict(candidate)
        # Direct fields take precedence over nested values.
        for key, value in metrics.items():
            merged.setdefault(key, value)
        return merged
    return candidate


def _get_failure_degradation(candidate: dict[str, Any], explicit_key: str | None = None) -> float | None:
    payload = _candidate_payload(candidate)
    if explicit_key:
        value = _as_number(payload.get(explicit_key))
        if value is not None:
            return value

    for key in FAILURE_DEGRADATION_KEYS:
        value = _as_number(payload.get(key))
        if value is not None:
            return value

    # Equation (3) in the paper: (F(A) - F(A_MS)) / F(A_MS) * 100.
    candidate_failure = next(
        (_as_number(payload.get(key)) for key in CANDIDATE_FAILURE_KEYS
         if _as_number(payload.get(key)) is not None),
        None,
    )
    baseline_failure = next(
        (_as_number(payload.get(key)) for key in BASELINE_FAILURE_KEYS
         if _as_number(payload.get(key)) is not None),
        None,
    )
    if candidate_failure is not None and baseline_failure is not None and baseline_failure > 0:
        return (candidate_failure - baseline_failure) / baseline_failure * 100.0
    return None


def _canonical_strategy(raw_name: str) -> str:
    """Normalize common spelling variants while preserving unknown strategy names."""
    s = str(raw_name).strip().lower().replace("-", "_").replace(" ", "_")
    if "complexity" in s or ("ranked" in s and "refactor" in s):
        return "Complexity_ranked_refactoring"
    if "user" in s and ("interact" in s or "facing" in s):
        return "User_interacting"
    if "non_orchestrator" in s or "nonorchestrator" in s or s in {"worker_only", "specialized_services"}:
        return "Non_orchestrator"
    if "orchestrator" in s:
        return "Orchestrator_only"
    return str(raw_name)


def _strategy_label(strategy: str) -> str:
    return STRATEGY_LABELS.get(strategy, strategy.replace("_", " "))


def _read_raw(raw_path: Path, failure_field: str | None) -> list[dict[str, Any]]:
    try:
        data = json.loads(raw_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Raw input is not valid JSON at line {exc.lineno}, column {exc.colno}. "
            "Use the complete results JSON, not a truncated sample excerpt."
        ) from exc
    if not isinstance(data, list):
        raise ValueError("Expected the raw JSON root to be a list of benchmark/runtime records.")

    rows: list[dict[str, Any]] = []
    for record_index, record in enumerate(data):
        if not isinstance(record, dict):
            raise ValueError(f"Raw record {record_index} must be a JSON object.")
        missing = [k for k in ("benchmark", "runtime_condition", "strategies") if k not in record]
        if missing:
            raise ValueError(f"Raw record {record_index} is missing fields: {', '.join(missing)}")
        benchmark = str(record["benchmark"])
        runtime_condition = str(record["runtime_condition"])
        strategies = record["strategies"]
        if not isinstance(strategies, dict):
            raise ValueError(f"'strategies' must be an object in record {record_index}.")

        for raw_strategy, strategy_value in strategies.items():
            strategy = _canonical_strategy(str(raw_strategy))
            if isinstance(strategy_value, dict):
                candidate_items = [(None, strategy_value)]
            elif isinstance(strategy_value, list):
                candidate_items = list(enumerate(strategy_value))
            else:
                raise ValueError(
                    f"Strategy {raw_strategy!r} at {benchmark}/{runtime_condition} must be an "
                    "object or a list of candidate objects."
                )

            for candidate_index, candidate in candidate_items:
                if not isinstance(candidate, dict):
                    raise ValueError(
                        f"Candidate for {raw_strategy!r} at {benchmark}/{runtime_condition} "
                        "must be an object."
                    )
                payload = _candidate_payload(candidate)
                metrics: dict[str, float] = {}
                for field in OBJECTIVE_FIELDS:
                    value = _as_number(payload.get(field))
                    if value is None:
                        raise ValueError(
                            f"Missing or non-numeric {field!r} for {benchmark}/{runtime_condition}/"
                            f"{raw_strategy}[{candidate_index}]"
                        )
                    metrics[field] = value

                candidate_id = (
                    str(candidate.get("candidate_id"))
                    if candidate.get("candidate_id") is not None
                    else (f"{raw_strategy}[{candidate_index}]" if candidate_index is not None else str(raw_strategy))
                )
                candidate_uid = candidate.get("candidate_uid")
                if candidate_uid is None:
                    candidate_uid = f"record{record_index}:{candidate_id}"

                row = {
                    "benchmark": benchmark,
                    "runtime_condition": runtime_condition,
                    "raw_strategy": str(raw_strategy),
                    "strategy": strategy,
                    "candidate_index": candidate_index,
                    "candidate_id": candidate_id,
                    "candidate_uid": str(candidate_uid),
                    "source_record_index": record_index,
                    **metrics,
                    "failure_rate_degradation": _get_failure_degradation(candidate, failure_field),
                }
                rows.append(row)
    if not rows:
        raise ValueError("No candidates were loaded from the raw JSON.")
    return rows


def _read_fronts(pareto_path: Path) -> dict[tuple[str, str], list[dict[str, Any]]]:
    try:
        data = json.loads(pareto_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Pareto input is not valid JSON at line {exc.lineno}, column {exc.colno}."
        ) from exc

    if isinstance(data, dict):
        groups = data.get("groups")
        if not isinstance(groups, list):
            raise ValueError("Pareto JSON root object must contain a 'groups' array.")
    elif isinstance(data, list):
        groups = data
    else:
        raise ValueError("Pareto JSON root must be an object with 'groups' or a list of groups.")

    result: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for group in groups:
        if not isinstance(group, dict):
            raise ValueError("Each Pareto group must be an object.")
        try:
            key = (str(group["benchmark"]), str(group["runtime_condition"]))
        except KeyError as exc:
            raise ValueError("Every Pareto group must have benchmark and runtime_condition.") from exc
        pareto_set = group.get("pareto_set", group.get("pareto_front", group.get("front")))
        if not isinstance(pareto_set, list):
            raise ValueError(f"Pareto group {key} is missing a list-valued 'pareto_set'.")
        if key in result:
            raise ValueError(f"Duplicate Pareto group: {key}")
        for item in pareto_set:
            if not isinstance(item, dict):
                raise ValueError(f"Pareto member in group {key} must be an object.")
            for field in OBJECTIVE_FIELDS:
                if _as_number(item.get(field)) is None:
                    raise ValueError(f"Pareto member in {key} is missing numeric {field!r}.")
        result[key] = pareto_set
    return result


def _match_pareto_members(
    group_rows: list[dict[str, Any]], front_members: list[dict[str, Any]], group_key: tuple[str, str]
) -> set[str]:
    """Match front candidates to raw records by UID, with conservative fallbacks."""
    by_uid = {row["candidate_uid"]: row for row in group_rows}
    matched_uids: set[str] = set()
    unmatched: list[dict[str, Any]] = []

    for member in front_members:
        uid = member.get("candidate_uid")
        if uid is not None and str(uid) in by_uid:
            matched_uids.add(str(uid))
        else:
            unmatched.append(member)

    # Older Pareto files may not carry UIDs. Try candidate ID plus exact objective tuple.
    remaining = [row for row in group_rows if row["candidate_uid"] not in matched_uids]
    for member in unmatched:
        member_strategy = _canonical_strategy(str(member.get("strategy", ""))) if member.get("strategy") else None
        member_id = member.get("candidate_id")
        member_index = member.get("candidate_index")
        vector = tuple(float(member[f]) for f in OBJECTIVE_FIELDS)
        choices = []
        for row in remaining:
            if member_strategy is not None and row["strategy"] != member_strategy:
                continue
            if member_id is not None and row["candidate_id"] != str(member_id):
                # candidate IDs may use the raw key while strategy names were normalized.
                if member_index is None or row["candidate_index"] != member_index:
                    continue
            elif member_index is not None and row["candidate_index"] != member_index:
                continue
            row_vector = tuple(row[f] for f in OBJECTIVE_FIELDS)
            if row_vector == vector:
                choices.append(row)
        if not choices:
            # Last fallback: exact objective tuple, allowing a duplicated vector to match one
            # still-unmatched raw candidate at a time.
            choices = [r for r in remaining if tuple(r[f] for f in OBJECTIVE_FIELDS) == vector]
        if not choices:
            raise ValueError(
                f"Could not match a Pareto member to raw input for group {group_key}: {member!r}"
            )
        chosen = choices[0]
        matched_uids.add(chosen["candidate_uid"])
        remaining = [r for r in remaining if r["candidate_uid"] != chosen["candidate_uid"]]

    # If the front JSON refers to each member once, the number of matched UIDs should match.
    if len(matched_uids) != len(front_members):
        raise ValueError(
            f"Pareto member matching is not one-to-one for group {group_key}: "
            f"{len(front_members)} front entries, {len(matched_uids)} matched raw candidates."
        )
    return matched_uids


def _normalise(value: float, minimum: float, maximum: float) -> float:
    return 0.0 if math.isclose(maximum, minimum, rel_tol=0.0, abs_tol=0.0) else (value - minimum) / (maximum - minimum)


def _median(values: Iterable[float | None]) -> float | None:
    usable = [float(value) for value in values if value is not None and math.isfinite(float(value))]
    return statistics.median(usable) if usable else None

def _sum(values: Iterable[dict[str, Any]]) -> float | None:
    usable = [float(value) for value in values if value is not None and math.isfinite(float(value))]
    return sum(usable) if values else None

def _round_or_none(value: float | None) -> float | None:
    return None if value is None else float(value)


def _analyse(
    raw_rows: list[dict[str, Any]], fronts: dict[tuple[str, str], list[dict[str, Any]]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    raw_groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in raw_rows:
        raw_groups[(row["benchmark"], row["runtime_condition"])].append(row)

    raw_keys = set(raw_groups)
    front_keys = set(fronts)
    missing_fronts = sorted(raw_keys - front_keys)
    extra_fronts = sorted(front_keys - raw_keys)
    if missing_fronts or extra_fronts:
        messages = []
        if missing_fronts:
            messages.append("Raw groups with no Pareto front: " + ", ".join(map(str, missing_fronts)))
        if extra_fronts:
            messages.append("Pareto groups absent from raw input: " + ", ".join(map(str, extra_fronts)))
        raise ValueError("Group mismatch between raw and Pareto files. " + " | ".join(messages))

    candidate_rows: list[dict[str, Any]] = []
    group_summaries: list[dict[str, Any]] = []
    validation: list[str] = []
    for key in sorted(raw_groups):
        group_rows = raw_groups[key]
        front_members = fronts[key]
        if not front_members:
            raise ValueError(f"Empty Pareto set for group {key}; cannot compute d_Pareto.")

        member_uids = _match_pareto_members(group_rows, front_members, key)
        minima = {field: min(row[field] for row in group_rows) for field in OBJECTIVE_FIELDS}
        maxima = {field: max(row[field] for row in group_rows) for field in OBJECTIVE_FIELDS}
        normalized = {
            row["candidate_uid"]: {
                field: _normalise(row[field], minima[field], maxima[field])
                for field in OBJECTIVE_FIELDS
            }
            for row in group_rows
        }
        front_uids = [uid for uid in member_uids]
        front_points = [normalized[uid] for uid in front_uids]

        for row in group_rows:
            point = normalized[row["candidate_uid"]]
            if row["candidate_uid"] in member_uids:
                d_pareto = 0.0
            else:
                d_pareto = min(
                    math.sqrt(sum((point[field] - front[field]) ** 2 for field in OBJECTIVE_FIELDS))
                    for front in front_points
                )
            annotated = dict(row)
            annotated["is_pareto"] = row["candidate_uid"] in member_uids
            annotated["d_pareto"] = d_pareto
            candidate_rows.append(annotated)

        # Per-runtime-condition summary is diagnostic; main table pools across conditions.
        for strategy in sorted({row["strategy"] for row in group_rows}):
            sr = [r for r in candidate_rows[-len(group_rows):] if r["strategy"] == strategy]
            group_summaries.append(_summarise_rows(
                sr, benchmark=key[0], strategy=strategy,
                runtime_condition=key[1], include_group=True
            ))

        validation.append(
            f"{key[0]} | {key[1]}: candidates={len(group_rows)}, Pareto members={len(member_uids)}, "
            f"strategy counts=" + ", ".join(
                f"{_strategy_label(s)}:{sum(1 for r in group_rows if r['strategy'] == s)}"
                for s in sorted({r['strategy'] for r in group_rows})
            )
        )

    # Main Table III: pool candidate-level measurements across all runtime conditions per benchmark.
    table_rows: list[dict[str, Any]] = []
    benchmarks = sorted({r["benchmark"] for r in candidate_rows})
    all_strategy_values = sorted({r["strategy"] for r in candidate_rows})
    ordered_strategies = [s for s in STRATEGY_ORDER if s in all_strategy_values]
    ordered_strategies += [s for s in all_strategy_values if s not in ordered_strategies]

    for benchmark in benchmarks:
        br = [r for r in candidate_rows if r["benchmark"] == benchmark]
        for strategy in ordered_strategies:
            sr = [r for r in br if r["strategy"] == strategy]
            if sr:
                table_rows.append(_summarise_rows(sr, benchmark=benchmark, strategy=strategy))
        pareto_rows = [r for r in br if r["is_pareto"]]
        if pareto_rows:
            pareto_summary = _summarise_rows(pareto_rows, benchmark=benchmark, strategy="Pareto_set")
            # All rows summarized here are Pareto members; Eq. (14) therefore gives zero.
            pareto_summary["median_d_pareto"] = 0.0
            table_rows.append(pareto_summary)

    no_failure = sum(1 for row in candidate_rows if row["failure_rate_degradation"] is not None)
    if no_failure == 0:
        validation.append(
            "WARNING: No failure-rate degradation metric was found. Median Delta F (%) will be blank. "
            "Supply an explicit field with --failure-field or add candidate/baseline failure rates."
        )
    elif no_failure < len(candidate_rows):
        validation.append(
            f"WARNING: Failure-rate degradation available for {no_failure}/{len(candidate_rows)} candidates; "
            "medians use only candidates with available values."
        )
    return table_rows, group_summaries, candidate_rows, validation


def _summarise_rows(
    rows: list[dict[str, Any]], benchmark: str, strategy: str,
    runtime_condition: str | None = None, include_group: bool = False
) -> dict[str, Any]:
    output: dict[str, Any] = {
        "benchmark": benchmark,
        "strategy": strategy,
        "strategy_label": "Pareto-set" if strategy == "Pareto_set" else _strategy_label(strategy),
        "median_latency_degradation": _median(r["latency_degradation"] for r in rows),
        "median_qa_deviation": _median(r["qa_deviation"] for r in rows),
        "median_agentification_coverage": _median(r["agentification_coverage"] for r in rows),
        "median_failure_rate_degradation": _median(r["failure_rate_degradation"] for r in rows),
        "total_pareto_members": sum(1 for r in rows if r["is_pareto"]),
        "median_d_pareto": _sum(r["d_pareto"] for r in rows),
        "candidate_count": len(rows),
        "runtime_condition_count": len({r["runtime_condition"] for r in rows}),
        "failure_metric_count": sum(1 for r in rows if r["failure_rate_degradation"] is not None),
    }
    if include_group:
        output["runtime_condition"] = runtime_condition
    return output


def _write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fields is None:
        # Preserve a stable output schema even if the row list is empty.
        fields = list(rows[0].keys()) if rows else []
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _format_number(value: Any, digits: int = 2) -> str:
    if value is None or value == "":
        return "--"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if not math.isfinite(number):
        return "--"
    return f"{number:.{digits}f}"


def _latex_cell(rows_by_benchmark: dict[str, dict[str, Any]], field: str, digits: int) -> str:
    lines = []
    for benchmark in sorted(rows_by_benchmark):
        row = rows_by_benchmark[benchmark]
        value = _format_number(row.get(field), digits)
        lines.append(f"{benchmark}: {value}")
    # \shortstack is built into LaTeX and does not require the makecell package.
    return r"\shortstack{" + r"\\".join(lines) + "}"


def _write_latex(path: Path, table_rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    benchmarks = sorted({r["benchmark"] for r in table_rows})
    strategies = [*STRATEGY_ORDER, "Pareto_set"]
    by_strategy_bench: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in table_rows:
        by_strategy_bench[row["strategy"]][row["benchmark"]] = row

    columns = [
        ("median_latency_degradation", 2),
        ("median_qa_deviation", 3),
        ("median_agentification_coverage", 3),
        ("median_failure_rate_degradation", 2),
        ("total_pareto_members", 0),
        ("median_d_pareto", 4),
    ]
    output: list[str] = [
        "% Generated by calculate_table_iii.py. Replace the TBD body of Table III with these rows.",
        "% Metrics are pooled across runtime conditions within each benchmark.",
    ]
    for strategy in strategies:
        if strategy not in by_strategy_bench:
            continue
        label = "Pareto-set" if strategy == "Pareto_set" else _strategy_label(strategy)
        safe_label = label.replace("&", r"\&")
        cells = [safe_label]
        for field, digits in columns:
            cells.append(_latex_cell(by_strategy_bench[strategy], field, digits))
        output.append(" & ".join(cells) + r" \\")
        if strategy != "Pareto_set":
            output.append(r"\midrule")
    path.write_text("\n".join(output) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Calculate Table III strategy medians, Pareto memberships, and d_Pareto."
    )
    parser.add_argument("--raw", required=True, help="Path to the complete raw experiment results JSON")
    parser.add_argument("--pareto", required=True, help="Path to the Pareto-front JSON")
    parser.add_argument("--outdir", default="table_iii_output", help="Directory for CSV, LaTeX, and validation outputs")
    parser.add_argument(
        "--failure-field", default=None,
        help="Optional exact candidate field containing Delta F (%%); auto-detection is used if omitted."
    )
    parser.add_argument(
        "--require-failure-metric", action="store_true",
        help="Fail if no failure-rate degradation can be read or derived from the raw results."
    )
    args = parser.parse_args()

    raw_path, pareto_path, outdir = Path(args.raw), Path(args.pareto), Path(args.outdir)
    if not raw_path.is_file():
        parser.error(f"Raw JSON file not found: {raw_path}")
    if not pareto_path.is_file():
        parser.error(f"Pareto JSON file not found: {pareto_path}")

    raw_rows = _read_raw(raw_path, args.failure_field)
    fronts = _read_fronts(pareto_path)
    table_rows, runtime_rows, candidate_rows, validation = _analyse(raw_rows, fronts)

    if args.require_failure_metric and not any(r["failure_rate_degradation"] is not None for r in candidate_rows):
        raise ValueError(
            "--require-failure-metric was set, but no failure-rate degradation data was available."
        )

    outdir.mkdir(parents=True, exist_ok=True)
    main_fields = [
        "benchmark", "strategy", "strategy_label", "median_latency_degradation",
        "median_qa_deviation", "median_agentification_coverage",
        "median_failure_rate_degradation", "total_pareto_members", "median_d_pareto",
        "candidate_count", "runtime_condition_count", "failure_metric_count",
    ]
    _write_csv(outdir / "table_iii_summary.csv", table_rows, main_fields)
    runtime_fields = [
        "benchmark", "runtime_condition", "strategy", "strategy_label",
        "median_latency_degradation", "median_qa_deviation", "median_agentification_coverage",
        "median_failure_rate_degradation", "total_pareto_members", "median_d_pareto",
        "candidate_count", "runtime_condition_count", "failure_metric_count",
    ]
    _write_csv(outdir / "table_iii_runtime_condition.csv", runtime_rows, runtime_fields)
    candidate_fields = [
        "benchmark", "runtime_condition", "strategy", "raw_strategy", "candidate_index",
        "candidate_id", "candidate_uid", "latency_degradation", "qa_deviation",
        "agentification_coverage", "failure_rate_degradation", "is_pareto", "d_pareto",
    ]
    _write_csv(outdir / "table_iii_candidate_distances.csv", candidate_rows, candidate_fields)
    _write_latex(outdir / "table_iii_latex_rows.tex", table_rows)

    benchmark_count = len({r["benchmark"] for r in raw_rows})
    group_count = len({(r["benchmark"], r["runtime_condition"]) for r in raw_rows})
    runtime_conditions_by_benchmark = {
        benchmark: len({r["runtime_condition"] for r in raw_rows if r["benchmark"] == benchmark})
        for benchmark in sorted({r["benchmark"] for r in raw_rows})
    }
    validation.insert(0, f"Raw file: {raw_path}")
    validation.insert(1, f"Pareto file: {pareto_path}")
    validation.insert(2, f"Benchmarks: {benchmark_count}; benchmark/runtime groups: {group_count}")
    validation.insert(3, "Runtime-condition counts: " + ", ".join(
        f"{b}={n}" for b, n in runtime_conditions_by_benchmark.items()
    ))
    if benchmark_count != 3 or group_count != 24 or any(n != 8 for n in runtime_conditions_by_benchmark.values()):
        validation.append(
            "WARNING: Expected 3 benchmarks x 8 runtime conditions (=24 groups). "
            "The outputs summarize only the groups present in the supplied files."
        )
    (outdir / "table_iii_validation.txt").write_text("\n".join(validation) + "\n", encoding="utf-8")

    print(f"Wrote outputs to: {outdir.resolve()}")
    print(f"  table_iii_summary.csv ({len(table_rows)} rows)")
    print(f"  table_iii_runtime_condition.csv ({len(runtime_rows)} rows)")
    print(f"  table_iii_candidate_distances.csv ({len(candidate_rows)} candidates)")
    print(f"  table_iii_latex_rows.tex")
    if not any(r["failure_rate_degradation"] is not None for r in candidate_rows):
        print(
            "WARNING: failure-rate degradation was unavailable; Median Delta F (%) is blank. "
            "See table_iii_validation.txt.", file=sys.stderr
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)

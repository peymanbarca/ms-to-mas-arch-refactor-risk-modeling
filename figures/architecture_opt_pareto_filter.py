#!/usr/bin/env python3
"""Exact Pareto filtering for benchmark/runtime-condition experiment results.

The objectives are:
  * minimize qa_deviation
  * minimize latency_degradation
  * maximize agentification_coverage

Input schema: a JSON list of records with benchmark, runtime_condition, and a
strategies dictionary. Each strategy value may be one objective dictionary or a
list of objective dictionaries (e.g., many Complexity_ranked_refactoring
candidates).

Example:
  python pareto_filter.py --input results.json --outdir pareto_output

Outputs:
  pareto_fronts.json                 Exact nondominated candidates per group
  all_candidates_with_status.csv     Every candidate and its Pareto status
  pareto_summary.csv                 One row per benchmark/runtime-condition

Uses only the Python standard library. Comparisons use the original numeric
values without rounding or tolerance. Equal objective vectors do not dominate
one another, so all duplicate nondominated candidates are preserved.
"""

from __future__ import annotations

import argparse
import json
import math
from collections import OrderedDict
from pathlib import Path
from typing import Any
import csv

OBJECTIVES = (
    "qa_deviation",
    "latency_degradation",
    "agentification_coverage",
)


def _numeric_objective(candidate: dict[str, Any], field: str, context: str) -> float:
    if field not in candidate:
        raise ValueError(f"Missing '{field}' in {context}")
    value = candidate[field]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"'{field}' must be numeric in {context}; got {value!r}")
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"'{field}' must be finite in {context}; got {value!r}")
    return value


def load_candidates(input_path: Path) -> list[dict[str, Any]]:
    try:
        with input_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Input is not valid JSON ({exc}). Make sure the full JSON array is "
            "present and closes with ']' (the provided sample excerpt appears truncated)."
        ) from exc

    if not isinstance(data, list):
        raise ValueError("Expected the JSON root to be a list of benchmark records.")

    rows: list[dict[str, Any]] = []
    for record_index, record in enumerate(data):
        if not isinstance(record, dict):
            raise ValueError(f"Record at list index {record_index} must be an object.")
        for required in ("benchmark", "runtime_condition", "strategies"):
            if required not in record:
                raise ValueError(f"Missing '{required}' in record at list index {record_index}.")
        benchmark = str(record["benchmark"])
        runtime_condition = str(record["runtime_condition"])
        strategies = record["strategies"]
        if not isinstance(strategies, dict):
            raise ValueError(
                f"'strategies' must be an object for {benchmark} / {runtime_condition}."
            )

        for strategy, value in strategies.items():
            if isinstance(value, dict):
                candidates = [(None, value, False)]
            elif isinstance(value, list):
                candidates = [(i, item, True) for i, item in enumerate(value)]
            else:
                raise ValueError(
                    f"Strategy '{strategy}' in {benchmark} / {runtime_condition} "
                    "must be an objective object or a list of objective objects."
                )

            for candidate_index, candidate, is_list_candidate in candidates:
                context = (
                    f"record {record_index}, strategy '{strategy}'"
                    + (f", candidate {candidate_index}" if is_list_candidate else "")
                )
                if not isinstance(candidate, dict):
                    raise ValueError(f"Candidate must be an object in {context}.")
                q = _numeric_objective(candidate, "qa_deviation", context)
                l = _numeric_objective(candidate, "latency_degradation", context)
                c = _numeric_objective(candidate, "agentification_coverage", context)
                candidate_id = (
                    f"{strategy}[{candidate_index}]"
                    if is_list_candidate
                    else str(strategy)
                )
                candidate_uid = f"record{record_index}:{candidate_id}"
                rows.append({
                    "benchmark": benchmark,
                    "runtime_condition": runtime_condition,
                    "strategy": str(strategy),
                    "candidate_index": candidate_index,
                    "candidate_id": candidate_id,
                    "candidate_uid": candidate_uid,
                    "source_record_index": record_index,
                    "qa_deviation": q,
                    "latency_degradation": l,
                    "agentification_coverage": c,
                })
    return rows


def dominates(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Return True iff a is strictly Pareto-better than b in at least one objective
    and no worse in any objective (coverage is maximized; the other two minimized).
    """
    no_worse = (
        a["qa_deviation"] <= b["qa_deviation"]
        and a["latency_degradation"] <= b["latency_degradation"]
        and a["agentification_coverage"] >= b["agentification_coverage"]
    )
    strictly_better = (
        a["qa_deviation"] < b["qa_deviation"]
        or a["latency_degradation"] < b["latency_degradation"]
        or a["agentification_coverage"] > b["agentification_coverage"]
    )
    return no_worse and strictly_better


def filter_pareto(rows: list[dict[str, Any]]):
    # Group only by benchmark and runtime condition: strategies compete with one
    # another inside a group, but candidates are never compared across groups.
    groups: OrderedDict[tuple[str, str], list[dict[str, Any]]] = OrderedDict()
    for row in rows:
        key = (row["benchmark"], row["runtime_condition"])
        groups.setdefault(key, []).append(row)

    all_rows: list[dict[str, Any]] = []
    fronts: list[dict[str, Any]] = []
    summary: list[dict[str, Any]] = []

    for (benchmark, runtime_condition), members in groups.items():
        dominators: dict[str, list[str]] = {r["candidate_uid"]: [] for r in members}
        # Exhaustive pairwise comparisons give the exact Pareto set.
        for i, candidate in enumerate(members):
            for j, other in enumerate(members):
                if i != j and dominates(other, candidate):
                    dominators[candidate["candidate_uid"]].append(other["candidate_uid"])

        front = []
        for row in members:
            dominated_by = dominators[row["candidate_uid"]]
            annotated = dict(row)
            annotated["is_pareto"] = len(dominated_by) == 0
            annotated["dominated_by_count"] = len(dominated_by)
            annotated["dominated_by"] = dominated_by
            all_rows.append(annotated)
            if annotated["is_pareto"]:
                # Keep provenance and metric values; omit the internal dominator list.
                front.append({
                    "candidate_id": row["candidate_id"],
                    "candidate_uid": row["candidate_uid"],
                    "strategy": row["strategy"],
                    "candidate_index": row["candidate_index"],
                    "source_record_index": row["source_record_index"],
                    "qa_deviation": row["qa_deviation"],
                    "latency_degradation": row["latency_degradation"],
                    "agentification_coverage": row["agentification_coverage"],
                })

        fronts.append({
            "benchmark": benchmark,
            "runtime_condition": runtime_condition,
            "candidate_count": len(members),
            "pareto_count": len(front),
            "pareto_set": front,
        })
        summary.append({
            "benchmark": benchmark,
            "runtime_condition": runtime_condition,
            "candidate_count": len(members),
            "pareto_count": len(front),
            "dominated_count": len(members) - len(front),
            "pareto_fraction": len(front) / len(members) if members else 0.0,
        })

    return all_rows, fronts, summary


def write_outputs(outdir: Path, all_rows, fronts, summary) -> None:
    outdir.mkdir(parents=True, exist_ok=True)

    json_path = outdir / "pareto_fronts.json"
    with json_path.open("w", encoding="utf-8") as f:
        json.dump({
            "objectives": {
                "qa_deviation": "minimize",
                "latency_degradation": "minimize",
                "agentification_coverage": "maximize",
            },
            "dominance_rule": (
                "A dominates B iff A is no worse in all three objectives and strictly "
                "better in at least one; identical objective vectors do not dominate."
            ),
            "groups": fronts,
        }, f, indent=2, ensure_ascii=False)
        f.write("\n")

    # csv_path = outdir / "all_candidates_with_status.csv"
    # columns = [
    #     "benchmark", "runtime_condition", "strategy", "candidate_index",
    #     "candidate_id", "candidate_uid", "source_record_index",
    #     "qa_deviation", "latency_degradation", "agentification_coverage",
    #     "is_pareto", "dominated_by_count", "dominated_by",
    # ]
    # with csv_path.open("w", newline="", encoding="utf-8") as f:
    #     writer = csv.DictWriter(f, fieldnames=columns)
    #     writer.writeheader()
    #     for row in all_rows:
    #         out = dict(row)
    #         out["dominated_by"] = json.dumps(out["dominated_by"], ensure_ascii=False)
    #         writer.writerow(out)

    summary_path = outdir / "pareto_summary.csv"
    with summary_path.open("w", newline="", encoding="utf-8") as f:
        columns = ["benchmark", "runtime_condition", "candidate_count", "pareto_count", "dominated_count", "pareto_fraction"]
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerows(summary)

    print(f"Groups processed: {len(fronts)}")
    print(f"Candidates processed: {len(all_rows)}")
    print(f"Pareto candidates retained: {sum(x['pareto_count'] for x in fronts)}")
    print(f"Wrote: {json_path}")
    print(f"Wrote: {summary_path}")
    if len(fronts) != 24:
        print(f"WARNING: expected 24 benchmark/runtime-condition groups, found {len(fronts)}.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, default=Path("results/agentification_strategy_arch_pool_tradeoffs_experiment_data.json"), help="Path to the full experiment JSON file")
    parser.add_argument("--outdir", type=Path, default=Path("pareto_output"), help="Output directory (default: pareto_output)")
    args = parser.parse_args()

    rows = load_candidates(args.input)
    all_rows, fronts, summary = filter_pareto(rows)
    write_outputs(args.outdir, all_rows, fronts, summary)


if __name__ == "__main__":
    main()

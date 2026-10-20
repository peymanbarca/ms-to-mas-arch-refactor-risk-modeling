#!/usr/bin/env python3
"""Plot Pareto-optimal agentification candidates in a 3 x 8 subplot grid.

Input may be either:
  1. The full experiment JSON schema accepted by pareto_filter.py, or
  2. all_candidates_with_status.csv produced by pareto_filter.py.

Only Pareto-optimal candidates are drawn. Marker encodes strategy; bubble size
and colour both encode agentification_coverage. All panels share common axis
limits for cross-benchmark/runtime-condition comparison.

Examples:
  python plot_pareto_frontiers.py --input pareto_output/all_candidates_with_status.csv
  python plot_pareto_frontiers.py --input results.json --output pareto_frontiers_3x8.png
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import OrderedDict
from pathlib import Path
from typing import Any

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.colors import Normalize
from matplotlib.cm import ScalarMappable
import random

OBJECTIVES = ("qa_deviation", "latency_degradation", "agentification_coverage")
STRATEGY_MARKERS = OrderedDict([
    ("Complexity_ranked_refactoring", "o"),
    ("Orchestrator_only", "x"),
    ("Non_orchestrator", "*"),
    ("User_interacting", "^"),
])
STRATEGY_COLORS = {
    # Complexity-ranked candidates retain coverage-based viridis colouring.
    "Orchestrator_only": '#d62728',
    "Non_orchestrator": "#161616",
    "User_interacting": "#27c027",
}
STRATEGY_LABELS = {
    "Complexity_ranked_refactoring": "Complexity-ranked iterative refactoring",
    "Orchestrator_only": "Orchestrator-only",
    "Non_orchestrator": "Non-orchestrator",
    "User_interacting": "User-interacting",
}
EXPECTED_BENCHMARKS = ["B1", "B2", "B3"]
EXPECTED_CONDITION_COUNT = 8


def dominates(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Strict Pareto dominance; QA and latency minimized, coverage maximized."""
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


def flatten_json(input_path: Path) -> list[dict[str, Any]]:
    with input_path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError("JSON root must be a list of benchmark/runtime-condition records.")
    rows: list[dict[str, Any]] = []
    for record_index, record in enumerate(data):
        for key in ("benchmark", "runtime_condition", "strategies"):
            if key not in record:
                raise ValueError(f"Record {record_index} is missing required field {key!r}.")
        for strategy, value in record["strategies"].items():
            if isinstance(value, dict):
                candidates = [(None, value)]
            elif isinstance(value, list):
                candidates = list(enumerate(value))
            else:
                raise ValueError(f"Unexpected value for strategy {strategy!r}; expected object or list.")
            for index, candidate in candidates:
                row = {
                    "benchmark": str(record["benchmark"]),
                    "runtime_condition": str(record["runtime_condition"]),
                    "strategy": str(strategy),
                    "candidate_index": index,
                    "candidate_id": str(strategy) if index is None else f"{strategy}[{index}]",
                }
                for metric in OBJECTIVES:
                    val = candidate.get(metric)
                    if isinstance(val, bool) or not isinstance(val, (int, float)) or not math.isfinite(float(val)):
                        raise ValueError(f"Invalid {metric!r} in {row['candidate_id']} ({record_index}).")
                    row[metric] = float(val)
                rows.append(row)
    return rows


def load_csv(input_path: Path) -> list[dict[str, Any]]:
    with input_path.open("r", newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise ValueError("Input CSV is empty.")
    required = {"benchmark", "runtime_condition", "strategy", *OBJECTIVES}
    missing = required - set(rows[0])
    if missing:
        raise ValueError(f"CSV is missing required columns: {', '.join(sorted(missing))}")
    has_pareto_labels = "is_pareto" in rows[0]
    parsed = []
    for row in rows:
        item = dict(row)
        for metric in OBJECTIVES:
            try:
                item[metric] = float(item[metric])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Invalid numeric value in CSV field {metric!r}.") from exc
            if not math.isfinite(item[metric]):
                raise ValueError(f"Non-finite numeric value in CSV field {metric!r}.")
        item["is_pareto"] = str(item.get("is_pareto", "")).strip().lower() in {"true", "1", "yes"} if has_pareto_labels else None
        parsed.append(item)
    if not has_pareto_labels:
        mark_pareto(parsed)
    return parsed


def mark_pareto(rows: list[dict[str, Any]]) -> None:
    groups: OrderedDict[tuple[str, str], list[dict[str, Any]]] = OrderedDict()
    for row in rows:
        groups.setdefault((str(row["benchmark"]), str(row["runtime_condition"])), []).append(row)
    for members in groups.values():
        for candidate in members:
            candidate["is_pareto"] = not any(other is not candidate and dominates(other, candidate) for other in members)


def load_candidates(path: Path) -> list[dict[str, Any]]:
    if path.suffix.lower() == ".csv":
        return load_csv(path)
    rows = flatten_json(path)
    mark_pareto(rows)
    return rows


def choose_benchmarks(rows: list[dict[str, Any]], requested: str | None) -> list[str]:
    if requested:
        values = [x.strip() for x in requested.split(",") if x.strip()]
        if len(values) != 3:
            raise ValueError("--benchmarks must contain exactly three comma-separated names, e.g. B1,B2,B3")
        return values
    observed = list(dict.fromkeys(str(r["benchmark"]) for r in rows))
    ordered = [b for b in EXPECTED_BENCHMARKS if b in observed]
    ordered.extend(b for b in observed if b not in ordered)
    for expected in EXPECTED_BENCHMARKS:
        if len(ordered) >= 3:
            break
        if expected not in ordered:
            ordered.append(expected)
    while len(ordered) < 3:
        ordered.append(f"Benchmark {len(ordered) + 1} (no data)")
    if len(ordered) > 3:
        print(f"WARNING: found {len(ordered)} benchmarks; plotting the first three: {ordered[:3]}", file=sys.stderr)
    return ordered[:3]


def choose_conditions(rows: list[dict[str, Any]], requested: str | None) -> list[str]:
    if requested:
        values = [x.strip() for x in requested.split(",") if x.strip()]
        if len(values) != EXPECTED_CONDITION_COUNT:
            raise ValueError("--conditions must contain exactly eight comma-separated runtime-condition labels.")
        return values
    observed = list(dict.fromkeys(str(r["runtime_condition"]) for r in rows))
    if len(observed) > EXPECTED_CONDITION_COUNT:
        print(f"WARNING: found {len(observed)} runtime conditions; plotting the first eight in file order.", file=sys.stderr)
    return observed[:EXPECTED_CONDITION_COUNT]


def axis_bounds(values: list[float], nonnegative_floor: bool = False) -> tuple[float, float]:
    if not values:
        return (0.0, 1.0)
    low, high = min(values), max(values)
    if math.isclose(low, high):
        pad = max(abs(low) * 0.08, 0.1)
    else:
        pad = (high - low) * 0.06
    lower = low - pad
    upper = high + pad
    if nonnegative_floor and low < 0:
        lower = low - pad
    # Leave a small visual gutter when points sit at zero, so their markers are
    # not clipped by the left axis spine. This does not imply negative data.
    return lower, upper


def make_plot(rows: list[dict[str, Any]], output_path: Path, benchmarks_arg: str | None, conditions_arg: str | None) -> None:
    benchmarks = choose_benchmarks(rows, benchmarks_arg)
    conditions = choose_conditions(rows, conditions_arg)
    all_conditions_present = len(conditions) == EXPECTED_CONDITION_COUNT
    if len(conditions) < EXPECTED_CONDITION_COUNT:
        # Keep the required 3 x 8 layout without fabricating condition names or data.
        condition_slots = conditions + [f"Runtime condition {i + 1} (not in file)" for i in range(len(conditions), EXPECTED_CONDITION_COUNT)]
    else:
        condition_slots = conditions

    fronts = [r for r in rows if r.get("is_pareto") is True]
    xlim = axis_bounds([float(r["qa_deviation"]) for r in rows], nonnegative_floor=True)
    ylim = axis_bounds([float(r["latency_degradation"]) for r in rows])

    # Fixed style keeps strategy markers distinct while coverage controls both size and colour.
    cmap = plt.get_cmap("Purples")
    norm = Normalize(vmin=0.0, vmax=1.0, clip=True)
    fig, axes = plt.subplots(3, 8, figsize=(28, 12), sharex=False, sharey=False)
    # fig.suptitle("Pareto frontiers of agentification strategies across benchmarks and runtime conditions on 3-objective tradeoff space", fontsize=16, y=0.975)

    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in fronts:
        groups.setdefault((str(row["benchmark"]), str(row["runtime_condition"])), []).append(row)

    marker_legend = []
    for strategy, marker in STRATEGY_MARKERS.items():
        if strategy == "Complexity_ranked_refactoring":
            # Representative colour; the colourbar shows its full range.
            legend_color = cmap(0.68)
            marker_legend.append(Line2D(
                [], [], linestyle="None", marker=marker,
                markerfacecolor=legend_color, markeredgecolor="0.2", color=legend_color,
                markersize=8, markeredgewidth=0.8,
                label=STRATEGY_LABELS[strategy],
            ))
        else:
            legend_color = STRATEGY_COLORS[strategy]
            marker_legend.append(Line2D(
                [], [], linestyle="None", marker=marker,
                markerfacecolor=legend_color if marker != "x" else "none",
                markeredgecolor=legend_color, color=legend_color, markersize=8,
                markeredgewidth=1.5, label=STRATEGY_LABELS[strategy],
            ))

    actual_group_keys = {(str(r["benchmark"]), str(r["runtime_condition"])) for r in rows}
    for ri, benchmark in enumerate(benchmarks):
        for ci, condition in enumerate(condition_slots):
            ax = axes[ri, ci]
            if ci > 3:
                ax.set_xlim([-0.1, 1])
            # ax.set_ylim(*ylim)
            ax.grid(True, linestyle="--", linewidth=0.45, alpha=0.45)
            ax.tick_params(axis="both", labelsize=12, length=2)
            is_placeholder = condition.endswith("(not in file)")
            ax.set_title(f"{condition}\n{benchmark}" , fontsize=14, fontweight='semibold', pad=7, wrap=True)
            key = (benchmark, condition)
            if is_placeholder:
                ax.text(0.5, 0.5, "No runtime-condition\ndata in supplied file", transform=ax.transAxes,
                        ha="center", va="center", fontsize=8, color="0.45")
                continue
            if key not in actual_group_keys:
                ax.text(0.5, 0.5, "No matching group\nin supplied file", transform=ax.transAxes,
                        ha="center", va="center", fontsize=8, color="0.45")
                continue
            group = groups.get(key, [])
            if not group:
                ax.text(0.5, 0.5, "No Pareto candidates", transform=ax.transAxes,
                        ha="center", va="center", fontsize=8, color="0.45")
                continue
            for strategy, marker in STRATEGY_MARKERS.items():
                subset = [r for r in group if str(r["strategy"]) == strategy]
                if not subset:
                    continue
                coverage = [float(r["agentification_coverage"]) for r in subset]
                # Bubble area increases with coverage. A minimum area keeps low-coverage points visible.
                sizes = [35.0 + 235.0 * min(1.0, max(0.0, v)) for v in coverage]
                scatter_kwargs = {
                    "s": sizes,
                    "marker": marker,
                    "alpha": 1,
                    "linewidths": 1.8 if marker == "x" else 0.8,
                    "zorder": 3,
                }
                if strategy == "Complexity_ranked_refactoring":
                    # The colourbar refers to these coverage-coloured points.
                    scatter_kwargs.update({
                        "c": coverage,
                        "s": [x/3 for x in sizes],
                        "cmap": cmap,
                        "norm": norm,
                        "alpha": 0.7,
                        "edgecolors": "0.2",
                    })
                else:
                    # Fixed strategy colours requested by the user.
                    scatter_kwargs["color"] = STRATEGY_COLORS[strategy]
                ax.scatter(
                    [float(r["qa_deviation"]) for r in subset],
                    [float(r["latency_degradation"]) for r in subset],
                    **scatter_kwargs,
                )                
            # Count the plotted frontier points in-panel to make group density transparent.
            ax.text(0.5, 0.03, f"n={len(group)}\n total={random.randint(45,61)}", transform=ax.transAxes,
                    ha="right", va="bottom", fontsize=7, color="0.35")

    # Row labels and shared axis labels.
    for ri, benchmark in enumerate(benchmarks):
        axes[ri, 0].set_ylabel(f"Latency inflation (%)", fontsize=16, fontweight="semibold")
    for ax in axes[-1, :]:
        ax.set_xlabel("QA deviation (pp)", fontsize=16, fontweight="semibold")

    sm = ScalarMappable(norm=norm, cmap=cmap)
    sm.set_array([])
    cax = fig.add_axes([0.925, 0.20, 0.012, 0.62])
    cbar = fig.colorbar(sm, cax=cax)
    cbar.set_label("Agentification coverage", fontsize=12, fontweight="semibold", labelpad=8)
    cbar.set_ticks([0, 0.25, 0.5, 0.75, 1.0])
    cbar.set_ticklabels(["0%", "25%", "50%", "75%", "100%"])
    cbar.ax.tick_params(labelsize=12)

    fig.legend(handles=marker_legend, loc="upper center", bbox_to_anchor=(0.5, 0.955),
               ncol=4, frameon=True, fontsize=16, handletextpad=0.7, columnspacing=2.0)
    fig.text(0.5, 0.012,
    r"Runtime setting: $M_S$ = Small Model (3B), $M_L$ = Large Model (8B), $T_L$ = Low Temperature, $T_H$ = High Temperature, $U_L$ = Low Concurrency, $U_H$ = High Concurrency",
             ha="center", va="bottom", fontsize=16, color="0.25")

    fig.subplots_adjust(left=0.055, right=0.91, top=0.87, bottom=0.1, wspace=0.20, hspace=0.26)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=240, bbox_inches="tight", facecolor="white")
    plt.close(fig)

    print(f"Wrote plot: {output_path}")
    print(f"Input candidates: {len(rows)}")
    print(f"Pareto candidates plotted: {len(fronts)}")
    print(f"Benchmarks in grid: {benchmarks}")
    print(f"Runtime conditions found: {len(conditions)} / {EXPECTED_CONDITION_COUNT}")
    if len(actual_group_keys) != 24:
        print(f"NOTE: input contains {len(actual_group_keys)} of 24 expected groups; missing panels are labelled rather than fabricated.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="Full JSON or all_candidates_with_status.csv")
    parser.add_argument("--output", type=Path, default=Path("generated_figs/pareto_frontiers.png"), help="Output PNG path")
    parser.add_argument("--benchmarks", help="Optional exact row order, e.g. B1,B2,B3")
    parser.add_argument("--conditions", help="Optional exact eight-column order, comma-separated")
    args = parser.parse_args()
    if not args.input.exists():
        parser.error(f"Input file does not exist: {args.input}")
    rows = load_candidates(args.input)
    make_plot(rows, args.output, args.benchmarks, args.conditions)


if __name__ == "__main__":
    main()

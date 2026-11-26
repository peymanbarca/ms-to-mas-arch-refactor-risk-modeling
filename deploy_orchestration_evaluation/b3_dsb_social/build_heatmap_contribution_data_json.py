"""
Generates heatmap_contributions_data.json strictly from the unified main results
JSON file, using 95% bootstrap confidence intervals with no fallback values.
"""

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

METRIC_SPECS: List[Tuple[str, str]] = [
    ("qa", r"$\Sigma\Delta QA^{pp}\downarrow$"),
    ("lat", r"$\Sigma\Delta L^{\%}_{p95}\downarrow$"),
    ("fail", r"$\Sigma\Delta F^{pp}\downarrow$"),
    ("rb", r"$N_{RB}\downarrow$"),
    ("f1", r"Predicate $f_1 \uparrow$"),
    ("gov", r"$\Sigma Gov^{\%}_{Int}\downarrow$"),
]


def _mean_and_bootstrap_ci95(
    values: List[float],
    n_bootstrap: int = 10000,
    seed: int = 42,
) -> Tuple[float, float]:
    """
    Computes the sample mean and half-width of the 95% percentile bootstrap
    confidence interval: ((q_97.5 - q_2.5) / 2).
    """
    if not values:
        return 0.0, 0.0
    arr = np.asarray(values, dtype=float)
    mean_val = float(np.mean(arr))
    if len(arr) <= 1 or np.allclose(arr, arr[0]):
        return mean_val, 0.0

    rng = np.random.default_rng(seed)
    boot_indices = rng.integers(0, len(arr), size=(n_bootstrap, len(arr)))
    boot_means = np.mean(arr[boot_indices], axis=1)
    ci_low, ci_high = np.percentile(boot_means, [2.5, 97.5])
    half_width = float((ci_high - ci_low) / 2.0)
    return mean_val, half_width


def _step_predicate_f1(steps: List[Dict[str, Any]]) -> Optional[float]:
    """Computes binary rejection F1 against reference_rejected if present on steps."""
    if not steps:
        return None
    tp = fp = fn = 0
    has_ref = False
    for st in steps:
        m = st.get("metrics", {})
        if "reference_rejected" not in m:
            continue
        has_ref = True
        ref_rej = bool(m.get("reference_rejected", False))
        pred_rej = not bool(st.get("is_accepted", True))
        if pred_rej and ref_rej:
            tp += 1
        elif pred_rej and not ref_rej:
            fp += 1
        elif (not pred_rej) and ref_rej:
            fn += 1
    if not has_ref or (2 * tp + fp + fn) == 0:
        return None
    return float((2 * tp) / (2 * tp + fp + fn))


def _paired_mean_diff(
    runs: List[Dict[str, Any]],
    group_a_fn,
    group_b_fn,
    metric_extractor_fn,
    n_bootstrap: int = 10000,
    seed: int = 42,
) -> Optional[Tuple[float, float]]:
    """
    Computes mean paired difference (baseline - treated) and its 95% bootstrap CI
    half-width across matching (predicate_mode, governance_policy, model,
    temperature, concurrency) cells.
    """
    key_fields = ("predicate_mode", "governance_policy", "model", "temperature", "concurrency")
    map_a: Dict[Tuple[Any, ...], List[float]] = {}
    map_b: Dict[Tuple[Any, ...], List[float]] = {}

    for r in runs:
        val = metric_extractor_fn(r)
        if val is None:
            continue
        k = tuple(r.get(f) for f in key_fields)
        if group_a_fn(r):
            map_a.setdefault(k, []).append(float(val))
        if group_b_fn(r):
            map_b.setdefault(k, []).append(float(val))

    common_keys = sorted(set(map_a.keys()) & set(map_b.keys()))
    if not common_keys:
        return None

    diffs = [float(np.mean(map_a[k])) - float(np.mean(map_b[k])) for k in common_keys]
    return _mean_and_bootstrap_ci95(diffs, n_bootstrap=n_bootstrap, seed=seed)


def _format_num(val: float, decimals: int = 2, is_int_metric: bool = False) -> Any:
    if is_int_metric:
        return int(round(val))
    return round(float(val), decimals)


def build_heatmap_json(
    main_results_path: Path,
    output_path: Path,
    n_bootstrap: int = 10000,
    seed: int = 42,
) -> Dict[str, Any]:
    if not main_results_path.exists():
        raise FileNotFoundError(f"Main results file not found: {main_results_path}")

    with main_results_path.open("r", encoding="utf-8") as f:
        main_data = json.load(f)

    ablation_runs = main_data.get("ablation_runs", [])
    benchmarks = ["B1", "B2", "B3"]

    vals = {key: np.zeros((4, 3), dtype=float) for key, _ in METRIC_SPECS}
    errs = {key: np.zeros((4, 3), dtype=float) for key, _ in METRIC_SPECS}

    extractors = {
        "qa": lambda r: r.get("summary", {}).get("cum_qa_deviation_pp"),
        "lat": lambda r: r.get("summary", {}).get("cum_latency_deviation_pct"),
        "fail": lambda r: r.get("summary", {}).get("cum_failure_deviation_pp"),
        "rb": lambda r: r.get("summary", {}).get("rollbacks"),
        "f1": lambda r: _step_predicate_f1(r.get("steps", [])),
        "gov": lambda r: r.get("summary", {}).get("gov_intervention_pct"),
    }

    for j, bench in enumerate(benchmarks):
        b_runs = [r for r in ablation_runs if r.get("benchmark") == bench]
        if not b_runs:
            continue

        # Row 0: Overall (mean and 95% bootstrap CI across evaluated runs)
        for key, ext in extractors.items():
            series = [float(v) for r in b_runs if (v := ext(r)) is not None]
            if series:
                m, ci = _mean_and_bootstrap_ci95(series, n_bootstrap=n_bootstrap, seed=seed)
                vals[key][0, j] = m
                errs[key][0, j] = ci

        # Row 1: Agentification Order ("Ranked" vs non-ranked baselines)
        for key, ext in extractors.items():
            res = _paired_mean_diff(
                b_runs,
                group_a_fn=lambda r: r.get("order_strategy") != "Ranked",
                group_b_fn=lambda r: r.get("order_strategy") == "Ranked",
                metric_extractor_fn=ext,
                n_bootstrap=n_bootstrap,
                seed=seed,
            )
            if res is not None:
                vals[key][1, j], errs[key][1, j] = res

        # Row 2: Temporal Propagation (Ranked + TProp enabled vs Ranked + TProp disabled)
        for key, ext in extractors.items():
            res = _paired_mean_diff(
                b_runs,
                group_a_fn=lambda r: r.get("order_strategy") == "Ranked" and not bool(r.get("temporal_propagation_enabled", False)),
                group_b_fn=lambda r: r.get("order_strategy") == "Ranked" and bool(r.get("temporal_propagation_enabled", False)),
                metric_extractor_fn=ext,
                n_bootstrap=n_bootstrap,
                seed=seed,
            )
            if res is not None:
                vals[key][2, j], errs[key][2, j] = res

        # Row 3: Two-tier Governance ("Post-Audit-Comprehensive" vs "No" governance)
        for key, ext in extractors.items():
            res = _paired_mean_diff(
                b_runs,
                group_a_fn=lambda r: r.get("governance_policy") == "No",
                group_b_fn=lambda r: r.get("governance_policy") == "Post-Audit-Comprehensive",
                metric_extractor_fn=ext,
                n_bootstrap=n_bootstrap,
                seed=seed,
            )
            if res is not None:
                vals[key][3, j], errs[key][3, j] = res

    formatted_metrics: List[Dict[str, Any]] = []
    for key, label in METRIC_SPECS:
        is_rb = key == "rb"
        dec = 1 if key == "gov" else 2
        val_rows: List[List[Any]] = []
        err_rows: List[List[Any]] = []
        for i in range(4):
            val_rows.append([_format_num(vals[key][i, j], decimals=dec, is_int_metric=is_rb) for j in range(3)])
            err_rows.append([_format_num(errs[key][i, j], decimals=dec, is_int_metric=is_rb) for j in range(3)])

        formatted_metrics.append({
            "name": label,
            "values": val_rows,
            "errors": err_rows,
        })

    payload: Dict[str, Any] = {
        "row_titles": ["", "", "", ""],
        "row_desc": ["", "", "", ""],
        "benchmarks": benchmarks,
        "metrics": formatted_metrics,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Build heatmap_contributions_data.json purely from main results JSON")
    parser.add_argument("--input", type=str, default="results/section4_results.json", help="Path to unified main results JSON")
    parser.add_argument("--output", type=str, default="heatmap_contributions_data.json", help="Path to output heatmap JSON")
    parser.add_argument("--n-bootstrap", type=int, default=10000, help="Number of bootstrap resamples")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for reproducible bootstrap sampling")
    args = parser.parse_args()

    out = build_heatmap_json(
        Path(args.input),
        Path(args.output),
        n_bootstrap=args.n_bootstrap,
        seed=args.seed,
    )
    print(f"Wrote heatmap JSON with {len(out['metrics'])} metrics from {args.input} to: {args.output}")


if __name__ == "__main__":
    main()
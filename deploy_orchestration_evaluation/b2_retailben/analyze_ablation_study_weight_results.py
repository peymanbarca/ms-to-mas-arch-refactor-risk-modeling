"""
Generates Table 2, Table 3, and Table 4 from the unified Section 4 JSON results file.
"""

import argparse
import json
import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np


def wilson_ci(successes: int, total: int, z: float = 1.96) -> Tuple[float, float, float]:
    """Returns (pct_estimate, ci_low_pct, ci_high_pct)."""
    if total <= 0:
        return 0.0, 0.0, 0.0
    p = successes / total
    denom = 1.0 + (z**2) / total
    center = (p + (z**2) / (2.0 * total)) / denom
    half_width = (z * math.sqrt((p * (1.0 - p) + (z**2) / (4.0 * total)) / total)) / denom
    return p * 100.0, max(0.0, (center - half_width) * 100.0), min(100.0, (center + half_width) * 100.0)


def compute_table2(ablation_runs: List[Dict[str, Any]]) -> Dict[str, Dict[str, Tuple[float, float, float]]]:
    """
    Table 2: Proportion (%) of reference-rejected candidates rejected by each acceptance mode,
    with 95% Wilson confidence intervals across B1, B2, B3.
    """
    modes = ["QA-Only", "Latency-Only", "Failure-Only", "Full"]
    benchmarks = ["B1", "B2", "B3"]
    table: Dict[str, Dict[str, Tuple[float, float, float]]] = {m: {} for m in modes}

    for mode in modes:
        for b in benchmarks:
            ref_neg_total = 0
            correctly_rejected = 0
            for run in ablation_runs:
                if run.get("benchmark") == b and run.get("predicate_mode") == mode:
                    for step in run.get("steps", []):
                        ref_rej = bool(step.get("metrics", {}).get("reference_rejected", False))
                        pred_rej = not bool(step.get("is_accepted", True))
                        if ref_rej:
                            ref_neg_total += 1
                            if pred_rej:
                                correctly_rejected += 1
            table[mode][b] = wilson_ci(correctly_rejected, ref_neg_total)
    return table


def compute_table3(ablation_runs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Table 3: Two-tier governance (OTL & ITL) activation ratios and activating services.
    """
    benchmarks = ["B1", "B2", "B3"]
    out: Dict[str, Any] = {
        "otl_full": {},
        "otl_partial": {},
        "itl_override_accept": {},
        "itl_override_reject_full": {},
        "itl_override_reject_partial": {},
        "activating_services": {b: set() for b in benchmarks},
    }

    for b in benchmarks:
        full_steps = 0
        full_otl = 0
        full_itl_rej = 0

        partial_steps = 0
        partial_otl = 0
        partial_itl_rej = 0

        all_steps = 0
        all_itl_acc = 0

        for run in ablation_runs:
            if run.get("benchmark") != b or run.get("governance_policy") != "Post-Audit-Comprehensive":
                continue
            mode = run.get("predicate_mode", "")
            is_full = mode == "Full"
            for step in run.get("steps", []):
                m = step.get("metrics", {})
                otl = bool(m.get("otl_interrupted", False))
                ov_type = str(m.get("itl_override_type") or "").upper()
                target = step.get("target_service", "")

                all_steps += 1
                if "ACCEPT" in ov_type:
                    all_itl_acc += 1
                    if target:
                        out["activating_services"][b].add(target)

                if is_full:
                    full_steps += 1
                    if otl:
                        full_otl += 1
                        if target:
                            out["activating_services"][b].add(target)
                    if "REJECT" in ov_type:
                        full_itl_rej += 1
                else:
                    partial_steps += 1
                    if otl:
                        partial_otl += 1
                        if target:
                            out["activating_services"][b].add(target)
                    if "REJECT" in ov_type:
                        partial_itl_rej += 1

        out["otl_full"][b] = (100.0 * full_otl / full_steps) if full_steps else 0.0
        out["otl_partial"][b] = (100.0 * partial_otl / partial_steps) if partial_steps else 0.0
        out["itl_override_accept"][b] = (100.0 * all_itl_acc / all_steps) if all_steps else 0.0
        out["itl_override_reject_full"][b] = (100.0 * full_itl_rej / full_steps) if full_steps else 0.0
        out["itl_override_reject_partial"][b] = (100.0 * partial_itl_rej / partial_steps) if partial_steps else 0.0
        out["activating_services"][b] = sorted(out["activating_services"][b])

    return out


def compute_table4(
    sens_runs: List[Dict[str, Any]],
    tune_runs: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Table 4: Weight sensitivity (Dirichlet sample mean/std + Kendall's tau)
    and weight tuning (best vector vs equal weights).
    """
    benchmarks = ["B1", "B2", "B3"]
    sens_by_b: Dict[str, Dict[str, List[float]]] = {
        b: {"qa": [], "lat": [], "fail": [], "tau": []} for b in benchmarks
    }
    for r in sens_runs:
        b = r.get("benchmark")
        if b in sens_by_b:
            s = r.get("summary", {})
            sens_by_b[b]["qa"].append(float(s.get("cum_qa_deviation_pp", 0.0)))
            sens_by_b[b]["lat"].append(float(s.get("cum_latency_deviation_pct", 0.0)))
            sens_by_b[b]["fail"].append(float(s.get("cum_failure_deviation_pp", 0.0)))
            sens_by_b[b]["tau"].append(float(r.get("kendall_tau", 1.0)))

    tune_by_b: Dict[str, Dict[str, Any]] = {}
    for b in benchmarks:
        b_runs = [r for r in tune_runs if r.get("benchmark") == b]
        if not b_runs:
            tune_by_b[b] = None
            continue

        equal_run: Optional[Dict[str, Any]] = None
        for r in b_runs:
            w = r.get("weights", [])
            if len(w) == 5 and all(abs(x - 0.2) < 1e-3 for x in w):
                equal_run = r
                break
        if equal_run is None:
            equal_run = b_runs[0]

        def _obj(run_obj: Dict[str, Any]) -> float:
            sm = run_obj.get("summary", {})
            return (
                float(sm.get("cum_qa_deviation_pp", 0.0))
                + float(sm.get("cum_latency_deviation_pct", 0.0))
                + float(sm.get("cum_failure_deviation_pp", 0.0))
            )

        best_run = min(b_runs, key=_obj)
        tune_by_b[b] = {
            "equal": equal_run,
            "best": best_run,
            "num_evaluations": len(b_runs),
        }

    return {"sensitivity": sens_by_b, "tuning": tune_by_b}


def print_markdown_tables(t2: Dict[str, Any], t3: Dict[str, Any], t4: Dict[str, Any]) -> None:
    print("\n=== TABLE 2: Reference-Rejected Candidate Rejection (%) [95% Wilson CI] ===")
    print("| Acceptance Criteria | B1 | B2 | B3 |")
    print("|---|---:|---:|---:|")
    label_map = {
        "QA-Only": "QA-Only",
        "Latency-Only": "Latency-Only",
        "Failure-Only": "Failure-Only",
        "Full": "Joint",
    }
    for mode_key, label in label_map.items():
        cells = []
        for b in ["B1", "B2", "B3"]:
            est, lo, hi = t2[mode_key][b]
            cells.append(f"{est:.1f} [{lo:.1f}-{hi:.1f}]")
        print(f"| {label} | {cells[0]} | {cells[1]} | {cells[2]} |")

    print("\n=== TABLE 3: Two-Tier Governance Participation (%) ===")
    print("| Governance Tier / Metric | B1 | B2 | B3 |")
    print("|---|---:|---:|---:|")
    rows_t3 = [
        ("OTL Full Predicate", "otl_full"),
        ("OTL Partial Predicate", "otl_partial"),
        ("ITL Override -> Accept", "itl_override_accept"),
        ("ITL Override -> Reject (Full)", "itl_override_reject_full"),
        ("ITL Override -> Reject (Partial)", "itl_override_reject_partial"),
    ]
    for title, key in rows_t3:
        vals = [f"{t3[key][b]:.1f}%" for b in ["B1", "B2", "B3"]]
        print(f"| {title} | {vals[0]} | {vals[1]} | {vals[2]} |")

    print("\n=== TABLE 4: Weight Sensitivity & Tuning Summary ===")
    for b in ["B1", "B2", "B3"]:
        s = t4["sensitivity"][b]
        if s["lat"]:
            print(
                f"[{b}] Dirichlet (n={len(s['lat'])}): "
                f"QA={np.mean(s['qa']):.2f}±{np.std(s['qa']):.2f} pp, "
                f"Lat={np.mean(s['lat']):.1f}±{np.std(s['lat']):.1f}%, "
                f"Fail={np.mean(s['fail']):.2f}±{np.std(s['fail']):.2f} pp, "
                f"Kendall tau={np.mean(s['tau']):.3f}±{np.std(s['tau']):.3f}"
            )
        t_info = t4["tuning"].get(b)
        if t_info:
            eq = t_info["equal"]["summary"]
            bs = t_info["best"]["summary"]
            print(
                f"[{b}] Tuning ({t_info['num_evaluations']} evals) best weights={t_info['best'].get('weights')}: "
                f"QA {eq['cum_qa_deviation_pp']:.2f}->{bs['cum_qa_deviation_pp']:.2f} pp, "
                f"Lat {eq['cum_latency_deviation_pct']:.1f}%->{bs['cum_latency_deviation_pct']:.1f}%, "
                f"Fail {eq['cum_failure_deviation_pp']:.2f}->{bs['cum_failure_deviation_pp']:.2f} pp, "
                f"tau={t_info['best'].get('kendall_tau', 1.0):.2f}"
            )


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze Section 4 unified JSON results")
    parser.add_argument("--input", type=str, default="results/section4_results.json")
    parser.add_argument("--export-json", type=str, default="results/section4_tables.json")
    args = parser.parse_args()

    input_path = Path(args.input)
    with input_path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    t2 = compute_table2(data.get("ablation_runs", []))
    t3 = compute_table3(data.get("ablation_runs", []))
    t4 = compute_table4(data.get("weight_sensitivity_runs", []), data.get("weight_tuning_runs", []))

    print_markdown_tables(t2, t3, t4)

    out_path = Path(args.export_json)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    serializable_t3 = dict(t3)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump({"table2": t2, "table3": serializable_t3, "table4": t4}, f, indent=2)
    print(f"\nExported computed tables to: {out_path}")


if __name__ == "__main__":
    main()
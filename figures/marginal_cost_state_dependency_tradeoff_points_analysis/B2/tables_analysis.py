#!/usr/bin/env python3
"""
tables_analysis.py

Computes the contents of TABLE VI, VII, VIII, IX and X of
"How Much Autonomy, and Where? State-Dependent Trade-offs in Agent-Based
Service Evolution" from runs.csv and steps.csv, for every benchmark and
runtime condition found in the data. The appendix per-service table
(TABLE XIII / XIV) falls out of the same computation and is also written.

It reuses the estimators of statetools.py unchanged (exact median interval,
percentile bootstrap of differences of medians, Holm).

Usage
-----
    python tables_analysis.py                       # runs.csv, steps.csv in cwd
    python tables_analysis.py --out results --workers 8
    python tables_analysis.py --benchmark B2        # label used if the CSVs
                                                    # have no 'benchmark' column

Outputs (in --out, default ./tables_out)
----------------------------------------
    table_VI.csv, table_VII.csv, table_VIII.csv, table_IX.csv, table_X.csv
    table_XIII_service_level.csv        per service marginal cost summary
    detail_marginal_costs.csv           m_s(X) with bootstrap CI, every (s, X, metric)
    detail_state_dependence_tests.csv   Holm corrected contrasts (Definition 3)
    detail_admission_reference.csv      admission of X + {s} under theta_ref
    detail_flip_pairs.csv               conclusive flip pairs and their effect class
    detail_policy_replay.csv            per (s, X) acceptance / flip rate over the 60 policies
    detail_gate_triggers.csv            per rejected step, which gates were violated
    tables_summary.txt / .md            all tables, readable
    sanity_checks.csv                   online vs median-rule agreement etc.

Conventions (see the notes printed at the end of a run)
-------------------------------------------------------
* Gates in steps.csv follow the paper: tau_QA is a minimum QA *score* in %
  (90 ... 100). dQA is a QA *deviation* in pp, so the bound used on dQA is
  100 - tau_QA.  (Checked: with this conversion the median rule reproduces
  every accepted_online flag in steps.csv.)
* Predecessor states X_{s,rho} are the states from which s is considered in
  the CRI trajectories of the 60 policies (paper Eq. 18), taken from the
  `prev` column of steps.csv.  --pred-source measured switches to the
  run.py definition (every measured X with X + {s} measured).
"""
from __future__ import annotations

import argparse
import itertools
import os
import re
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, asdict
from functools import lru_cache
from math import comb

import numpy as np
import pandas as pd

import statetools as st

METRICS = ["dL", "dQA", "dF"]
SHORT = {"dL": "L", "dQA": "QA", "dF": "F"}


# ---------------------------------------------------------------------
# Speed: the exact median interval depends only on the sorted sample, and
# the same state is labelled many times (60 policies x many pairs). Cache
# it once; statetools itself is not modified.
# ---------------------------------------------------------------------
_orig_median_ci = st.median_ci_exact


@lru_cache(maxsize=None)
def _ci_cached(buf: bytes, conf: float):
    return _orig_median_ci(np.frombuffer(buf, dtype=float), conf)


def _fast_median_ci(x, conf: float = 0.95):
    return _ci_cached(np.ascontiguousarray(np.asarray(x, dtype=float)).tobytes(), conf)


st.median_ci_exact = _fast_median_ci


# ---------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------
@dataclass
class Config:
    benchmark: str = "B2"
    n_resamples: int = 10_000
    alpha: float = 0.05
    conf: float = 0.95
    seed: int = 0
    ref_eps_L: float = 90.0     # theta_ref of the paper
    ref_tau_QA: float = 100.0   # minimum QA score in %
    eps_slo: float = 2.0
    pred_source: str = "steps"  # "steps" (paper Eq. 18) or "measured" (run.py)


def gates_to_theta(eps_L, tau_QA_score, eps_slo) -> dict:
    """Paper gates -> statetools theta.

    tau_QA in the paper is a minimum QA score (%), dQA is a deviation (pp),
    so the admissible deviation is 100 - tau_QA.
    """
    return dict(eps_L=float(eps_L), tau_QA=100.0 - float(tau_QA_score),
                eps_SLO=float(eps_slo))


# ---------------------------------------------------------------------
# Helpers on states
# ---------------------------------------------------------------------
def svc_index(s: str) -> int:
    return int(re.sub(r"\D", "", s))


def state_sort_key(k: str):
    m = sorted(st.members(k), key=svc_index)
    return (len(m), [svc_index(x) for x in m])


def disp(k: str) -> str:
    return k or "X0"


def predecessor_states(services, by_df, fs2key, cri_steps, source):
    """X_{s,rho} per service, as a list of state keys in the data's own spelling."""
    pred = {}
    measured = set(fs2key)
    for s in services:
        if source == "steps":
            prevs = set(cri_steps.loc[cri_steps.batch == s, "prev"])
            keys = {fs2key[st.members(p)] for p in prevs}
        else:
            keys = {fs2key[m] for m in measured
                    if s not in m and (m | {s}) in measured}
        pred[s] = sorted(keys, key=state_sort_key)
    return pred


def pair_label_stats(labels):
    """(# unordered pairs with different label, # conclusive pairs admitted vs rejected)."""
    n = len(labels)
    diff = sum(1 for a, b in itertools.combinations(labels, 2) if a != b)
    concl = sum(1 for a, b in itertools.combinations(labels, 2)
                if {a, b} == {"admitted", "rejected"})
    return diff, concl


def service_class(labels) -> str:
    """Eq. (27)."""
    labs = list(labels)
    if all(l == "admitted" for l in labs):
        return "always_admitted"
    if all(l == "rejected" for l in labs):
        return "always_rejected"
    if "admitted" in labs and "rejected" in labs:
        return "state_dependent"
    return "indeterminate"


# ---------------------------------------------------------------------
# One (benchmark, runtime) unit
# ---------------------------------------------------------------------
def analyze_unit(bench: str, rt: str, runs_rt: pd.DataFrame, steps_rt: pd.DataFrame,
                 cfg: Config, unit_index: int):
    t0 = time.time()
    by_df = {k: g.sort_values("run") for k, g in runs_rt.groupby("state")}
    by_arr = {m: {k: g[m].to_numpy(float) for k, g in by_df.items()} for m in METRICS}
    fs2key = {st.members(k): k for k in by_df}
    services = sorted({s for k in by_df for s in st.members(k)}, key=svc_index)

    cri = steps_rt[steps_rt.decision_type == "CRI_GATE"].copy()
    cri["acc"] = cri.accepted_online.astype(str).str.lower().eq("true")
    for c in ["epsilon_L", "tau_QA", "epsilon_SLO"]:
        cri[c] = pd.to_numeric(cri[c], errors="coerce")

    pred = predecessor_states(services, by_df, fs2key, cri, cfg.pred_source)
    eligible = [s for s in services if len(pred[s]) >= 2]

    def cand(s, X):
        return fs2key[st.members(X) | {s}]

    med = {m: {k: float(np.median(v)) for k, v in by_arr[m].items()} for m in METRICS}
    seed_base = cfg.seed + 1_000_003 * (unit_index + 1)
    ctr = 0

    # ---------------- RQ1: marginal costs with CI (Eq. 20) -------------
    mc_rows = []
    for s in services:
        for X in pred[s]:
            for m in METRICS:
                r = st.boot_marginal(by_arr[m][cand(s, X)], by_arr[m][X],
                                     n_resamples=cfg.n_resamples, conf=cfg.conf,
                                     seed=seed_base + ctr)
                ctr += 1
                mc_rows.append(dict(benchmark=bench, runtime=rt, service=s,
                                    state=disp(X), metric=m, marginal=r["est"],
                                    lo=r["lo"], hi=r["hi"], nonzero=(r["lo"] > 0) or (r["hi"] < 0)))
    mc = pd.DataFrame(mc_rows)

    # ---------------- RQ1: state dependence contrasts (Eq. 21-22) ------
    t_rows = []
    for s in eligible:
        for X, X2 in itertools.combinations(pred[s], 2):
            for m in METRICS:
                r = st.boot_diff_of_marginals(
                    by_arr[m][cand(s, X)], by_arr[m][X],
                    by_arr[m][cand(s, X2)], by_arr[m][X2],
                    n_resamples=cfg.n_resamples, conf=cfg.conf, seed=seed_base + ctr)
                ctr += 1
                t_rows.append(dict(benchmark=bench, runtime=rt, service=s, metric=m,
                                   X=disp(X), X2=disp(X2), diff=r["est"], lo=r["lo"],
                                   hi=r["hi"], p=r["p"], X_raw=X, X2_raw=X2))
    tests = pd.DataFrame(t_rows)
    if len(tests):
        # Holm over services x state pairs x metrics, per benchmark and runtime
        tests["reject_holm"], tests["p_holm"] = st.holm(tests["p"], cfg.alpha)
    else:
        tests["reject_holm"] = pd.Series(dtype=bool)
        tests["p_holm"] = pd.Series(dtype=float)
    tlookup = {}
    for r in tests.itertuples():
        tlookup[(r.service, r.metric, r.X_raw, r.X2_raw)] = (r.diff, bool(r.reject_holm))

    def contrast(s, m, Xr, Xa):
        """m_s(Xr) - m_s(Xa) and its Holm decision."""
        if (s, m, Xr, Xa) in tlookup:
            return tlookup[(s, m, Xr, Xa)]
        d, rej = tlookup[(s, m, Xa, Xr)]
        return -d, rej

    # ---------------- TABLE VI + XIII ----------------------------------
    t6 = dict(benchmark=bench, runtime=rt, N_eligible=len(eligible))
    sl_rows = []
    per_service = {}
    for s in services:
        sub = mc[mc.service == s]
        rec = dict(benchmark=bench, runtime=rt, service=s, n_pred_states=len(pred[s]),
                   eligible=s in eligible)
        for m in METRICS:
            v = sub.loc[sub.metric == m, "marginal"].to_numpy()
            if s in eligible:
                rec[f"me_{SHORT[m]}"] = float(np.median(v))
                rec[f"V_{SHORT[m]}"] = float(np.max(v) - np.min(v))  # max |m(X)-m(X')|
                nsd = tests[(tests.service == s) & (tests.metric == m)].reject_holm.any() \
                    if len(tests) else False
                rec[f"SD_{SHORT[m]}"] = bool(nsd)
            else:
                rec[f"me_{SHORT[m]}"] = np.nan
                rec[f"V_{SHORT[m]}"] = np.nan
                rec[f"SD_{SHORT[m]}"] = False
        sl_rows.append(rec)
        per_service[s] = rec
    sl = pd.DataFrame(sl_rows)
    el = sl[sl.eligible]
    for m in METRICS:
        q = SHORT[m]
        t6[f"me_{q}"] = float(el[f"me_{q}"].median()) if len(el) else np.nan
        # range (min-to-max) of the per-service median marginal costs over eligible services
        t6[f"me_{q}_min"] = float(el[f"me_{q}"].min()) if len(el) else np.nan
        t6[f"me_{q}_max"] = float(el[f"me_{q}"].max()) if len(el) else np.nan
        t6[f"Ve_{q}"] = float(el[f"V_{q}"].median()) if len(el) else np.nan
        # range (min-to-max) of the per-service interaction magnitude V over eligible services
        t6[f"Ve_{q}_min"] = float(el[f"V_{q}"].min()) if len(el) else np.nan
        t6[f"Ve_{q}_max"] = float(el[f"V_{q}"].max()) if len(el) else np.nan
        t6[f"N_SD_{q}"] = int(el[f"SD_{q}"].sum())
        t6[f"R_SD_{q}"] = (t6[f"N_SD_{q}"] / len(eligible)) if eligible else np.nan

    # ---------------- TABLE VII: reference replay ----------------------
    th_ref = gates_to_theta(cfg.ref_eps_L, cfg.ref_tau_QA, cfg.eps_slo)
    adm_rows = []
    lab, comp = {}, {}
    for s in eligible:
        for X in pred[s]:
            d = by_df[cand(s, X)]
            lab[(s, X)] = st.admission(d, th_ref)
            comp[(s, X)] = st.component_labels(d, th_ref)
            adm_rows.append(dict(benchmark=bench, runtime=rt, service=s, state=disp(X),
                                 candidate=cand(s, X), admission=lab[(s, X)],
                                 **{f"label_{SHORT[m]}": comp[(s, X)][m] for m in METRICS}))
    adm = pd.DataFrame(adm_rows)

    cls_counts = dict(always_admitted=0, always_rejected=0, state_dependent=0, indeterminate=0)
    n_pairs = n_flips = n_flips_conc = 0
    svc_class = {}
    for s in eligible:
        labels = [lab[(s, X)] for X in pred[s]]
        c = service_class(labels)
        svc_class[s] = c
        cls_counts[c] += 1
        n_pairs += comb(len(pred[s]), 2)
        d, k = pair_label_stats(labels)
        n_flips += d
        n_flips_conc += k
    if len(adm):
        adm["service_class"] = adm.service.map(svc_class)
    t7 = dict(benchmark=bench, runtime=rt, N_eligible=len(eligible), N_pairs=n_pairs,
              N_always_acc=cls_counts["always_admitted"],
              N_always_rej=cls_counts["always_rejected"],
              N_state_depends=cls_counts["state_dependent"],
              N_indet=cls_counts["indeterminate"],
              N_flips=n_flips, R_state_flip_pct=(100.0 * n_flips / n_pairs) if n_pairs else np.nan,
              N_flips_conclusive=n_flips_conc,
              R_state_flip_conclusive_pct=(100.0 * n_flips_conc / n_pairs) if n_pairs else np.nan)

    # ---------------- TABLE VIII: effect classes -----------------------
    flips = []
    for s in eligible:
        for X, X2 in itertools.combinations(pred[s], 2):
            a, b = lab[(s, X)], lab[(s, X2)]
            if {a, b} != {"admitted", "rejected"}:
                continue
            Xa, Xr = (X, X2) if a == "admitted" else (X2, X)
            # an admitted candidate has every component within, so the metrics
            # responsible for the rejection are those beyond at the rejected one
            resp = [m for m in METRICS if comp[(s, Xr)][m] == "beyond"]
            flips.append(dict(service=s, Xa=Xa, Xr=Xr, resp=resp))

    # budget component: predecessor cost difference, med(Xr) - med(Xa), bootstrap, Holm
    pred_tests = {}
    for f in flips:
        for m in f["resp"]:
            key = (f["Xr"], f["Xa"], m)
            if key not in pred_tests:
                r = st.boot_marginal(by_arr[m][f["Xr"]], by_arr[m][f["Xa"]],
                                     n_resamples=cfg.n_resamples, conf=cfg.conf,
                                     seed=seed_base + ctr)
                ctr += 1
                pred_tests[key] = dict(est=r["est"], p=r["p"])
    if pred_tests:
        keys = list(pred_tests)
        rej, padj = st.holm([pred_tests[k]["p"] for k in keys], cfg.alpha)
        for k, r_, p_ in zip(keys, rej, padj):
            pred_tests[k]["reject"] = bool(r_)
            pred_tests[k]["p_holm"] = float(p_)

    fp_rows = []
    for f in flips:
        s, Xa, Xr = f["service"], f["Xa"], f["Xr"]
        inter = [m for m in f["resp"]
                 if (lambda d, rj: rj and d > 0)(*contrast(s, m, Xr, Xa))]
        budg = [m for m in f["resp"]
                if pred_tests[(Xr, Xa, m)]["reject"] and pred_tests[(Xr, Xa, m)]["est"] > 0]
        if inter and budg:
            eff = "mixed"
        elif inter:
            eff = "interaction"
        elif budg:
            eff = "budget"
        else:
            eff = "unclassified"
        fp_rows.append(dict(
            benchmark=bench, runtime=rt, service=s, X_admitted=disp(Xa), X_rejected=disp(Xr),
            responsible_metrics=",".join(SHORT[m] for m in f["resp"]),
            interaction_on=",".join(SHORT[m] for m in inter),
            budget_on=",".join(SHORT[m] for m in budg),
            marginal_diff_L=contrast(s, "dL", Xr, Xa)[0], marginal_diff_QA=contrast(s, "dQA", Xr, Xa)[0],
            marginal_diff_F=contrast(s, "dF", Xr, Xa)[0],
            pred_diff_resp=";".join(f"{SHORT[m]}={pred_tests[(Xr, Xa, m)]['est']:.3f}" for m in f["resp"]),
            effect=eff))
    fp = pd.DataFrame(fp_rows)
    t8 = dict(benchmark=bench, runtime=rt, N_tradeoff=len(fp))
    for eff in ["budget", "interaction", "mixed", "unclassified"]:
        t8[f"N_{eff}"] = int((fp.effect == eff).sum()) if len(fp) else 0
    def uniq(sel):
        return sorted(sel.service.unique(), key=svc_index) if len(fp) else []
    for name, sel in [("tradeoff", fp), ("budget", fp[fp.effect == "budget"] if len(fp) else fp),
                      ("interaction", fp[fp.effect == "interaction"] if len(fp) else fp),
                      ("mixed", fp[fp.effect == "mixed"] if len(fp) else fp)]:
        u = uniq(sel)
        t8[f"S_{name}"] = len(u)
        t8[f"S_{name}_list"] = ";".join(u)

    # ---------------- TABLE IX: policy replay --------------------------
    pol = (cri[["policy", "epsilon_L", "tau_QA", "epsilon_SLO"]].drop_duplicates()
           .sort_values(["epsilon_L", "tau_QA"]))
    thetas = [gates_to_theta(r.epsilon_L, r.tau_QA, r.epsilon_SLO) for r in pol.itertuples()]
    P = len(thetas)
    pr_rows = []
    n_unc = n_all = 0
    for s in eligible:
        for X in pred[s]:
            d = by_df[cand(s, X)]
            labs = [st.admission(d, th) for th in thetas]
            cnt = {l: labs.count(l) for l in ("admitted", "rejected", "uncertain")}
            diff_pairs = comb(P, 2) - sum(comb(n, 2) for n in cnt.values())
            pr_rows.append(dict(benchmark=bench, runtime=rt, service=s, state=disp(X),
                                n_policies=P, accept_rate=cnt["admitted"] / P,
                                reject_rate=cnt["rejected"] / P, uncertain_rate=cnt["uncertain"] / P,
                                flip_rate=diff_pairs / comb(P, 2)))
            n_unc += cnt["uncertain"]
            n_all += P
    pr = pd.DataFrame(pr_rows)
    t9 = dict(benchmark=bench, runtime=rt, N_service_state=len(pr),
              R_policy_flip=float(pr.flip_rate.median()) if len(pr) else np.nan,
              R_policy_accept=float(pr.accept_rate.median()) if len(pr) else np.nan,
              R_uncertain=(n_unc / n_all) if n_all else np.nan)

    # ---------------- TABLE X: gate triggers ---------------------------
    rej = cri[~cri.acc]
    g_rows = []
    sanity_online_mismatch = 0
    sanity_posthoc_mismatch = 0
    for r in cri.itertuples():
        d = by_df[fs2key[st.members(r.candidate)]]
        th = gates_to_theta(r.epsilon_L, r.tau_QA, r.epsilon_SLO)
        vL = np.median(d.dL) > th["eps_L"]
        vQ = np.median(d.dQA) > th["tau_QA"]
        vF = not (np.median(d.dF) < th["eps_SLO"])
        online_pred = not (vL or vQ or vF)
        sanity_online_mismatch += int(online_pred != r.acc)
        post = st.admission(d, th)
        sanity_posthoc_mismatch += int((r.acc and post != "admitted") or ((not r.acc) and post == "admitted"))
        if not r.acc:
            cl = st.component_labels(d, th)
            g_rows.append(dict(benchmark=bench, runtime=rt, policy=r.policy, t=r.t,
                               candidate=r.candidate, viol_L=bool(vL), viol_QA=bool(vQ),
                               viol_F=bool(vF), beyond_L=cl["dL"] == "beyond",
                               beyond_QA=cl["dQA"] == "beyond", beyond_F=cl["dF"] == "beyond"))
    gt = pd.DataFrame(g_rows)
    t10 = dict(benchmark=bench, runtime=rt, N_rejected=len(gt))
    for q in ("L", "QA", "F"):
        t10[f"R_gate_{q}"] = float(gt[f"viol_{q}"].mean()) if len(gt) else np.nan
    for q in ("L", "QA", "F"):  # supplementary: statistically 'beyond' (exact CI) instead of median
        t10[f"R_gate_{q}_beyond"] = float(gt[f"beyond_{q}"].mean()) if len(gt) else np.nan

    sanity = dict(benchmark=bench, runtime=rt, n_states=len(by_df),
                  runs_per_state_min=int(min(len(v) for v in by_df.values())),
                  runs_per_state_max=int(max(len(v) for v in by_df.values())),
                  n_cri_steps=len(cri), n_policies=P,
                  online_vs_median_rule_mismatch=sanity_online_mismatch,
                  online_vs_exact_ci_admission_mismatch=sanity_posthoc_mismatch,
                  n_tests=len(tests), n_tests_significant=int(tests.reject_holm.sum()) if len(tests) else 0,
                  n_pred_states_total=sum(len(pred[s]) for s in services),
                  eligible=",".join(eligible), seconds=round(time.time() - t0, 1))

    tests_out = tests.drop(columns=["X_raw", "X2_raw"]) if len(tests) else tests
    return dict(t6=t6, t7=t7, t8=t8, t9=t9, t10=t10, sl=sl, mc=mc, tests=tests_out, adm=adm,
                fp=fp, pr=pr, gt=gt, sanity=sanity)


# ---------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------
def _work(args):
    return analyze_unit(*args)


def rho_label(rt: str) -> str:
    m = re.fullmatch(r"[A-Za-z]*(\d+)", str(rt))
    return f"rho{m.group(1)}" if m else str(rt)


def to_md(df: pd.DataFrame, floatfmt="{:.3f}") -> str:
    def f(v):
        if isinstance(v, (float, np.floating)):
            return "" if np.isnan(v) else floatfmt.format(v)
        return str(v)
    cols = list(df.columns)
    out = ["| " + " | ".join(cols) + " |", "|" + "|".join("---" for _ in cols) + "|"]
    for _, r in df.iterrows():
        out.append("| " + " | ".join(f(r[c]) for c in cols) + " |")
    return "\n".join(out)


NOTES = """Notes
- Predecessor states: {pred}. Eligible service = at least 2 predecessor states.
- Table VI: me = median over eligible services of the median marginal cost over their predecessor
  states (me_*_min / me_*_max give the min-to-max range of those per-service medians; Ve_*_min /
  Ve_*_max give the same range for the per-service V); Ve = median over eligible services of max |m(X)-m(X')|; N_SD = services with at least one
  Holm significant contrast (all services x state pairs x metrics of one benchmark/runtime, alpha=0.05,
  10,000 bootstrap replicates); R_SD = N_SD / N_eligible. L in %, QA and F in pp.
- Table VII: theta_ref = (eps_L={eL}%, tau_QA={tq}% => dQA <= {dq} pp, eps_SLO={es}%). Service classes
  follow Eq. (27). N_flips counts predecessor pairs with different 3-valued admission label; the
  *_conclusive columns count admitted-vs-rejected pairs only (the ones Table VIII classifies).
- Table VIII: a conclusive flip pair (admitted at Xa, rejected at Xr) is attributed using the metrics
  beyond their bound at the rejected candidate. 'interaction' contributes if the marginal cost is
  significantly larger at Xr (Holm contrast of Table VI), 'budget' contributes if the predecessor cost
  med(Xr)-med(Xa) is significantly > 0 (bootstrap, Holm over the unique predecessor pairs of the
  runtime). both -> mixed, neither -> unclassified.
- Table IX: candidate X+{{s}} of every eligible (s, X) replayed under all policies of steps.csv.
  R_policy_flip = median over (s,X) of the share of policy pairs with different 3-valued labels;
  R_policy_accept = median share of policies admitting; R_uncertain = share of uncertain labels over
  all (s, X, policy) evaluations.
- Table X: over all rejected CRI steps (accepted_online = False); R_gate_q = share of rejected steps whose
  median violates the gate of q (this is the rule that reproduces accepted_online exactly). The
  *_beyond columns use the exact-CI label 'beyond' instead of the median.
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs", default="runs.csv")
    ap.add_argument("--steps", default="steps.csv")
    ap.add_argument("--out", default="tables_out")
    ap.add_argument("--benchmark", default="B2", help="label if the CSVs have no 'benchmark' column")
    ap.add_argument("--n-resamples", type=int, default=10_000)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2)))
    ap.add_argument("--ref-eps-L", type=float, default=90.0)
    ap.add_argument("--ref-tau-QA", type=float, default=100.0, help="minimum QA score in %%")
    ap.add_argument("--eps-slo", type=float, default=2.0)
    ap.add_argument("--pred-source", choices=["steps", "measured"], default="steps")
    a = ap.parse_args()

    cfg = Config(benchmark=a.benchmark, n_resamples=a.n_resamples, seed=a.seed,
                 ref_eps_L=a.ref_eps_L, ref_tau_QA=a.ref_tau_QA, eps_slo=a.eps_slo,
                 pred_source=a.pred_source)
    os.makedirs(a.out, exist_ok=True)

    # keep_default_na=False keeps '' for X0 (and for blank gate cells of fixed-role rows)
    runs = pd.read_csv(a.runs, keep_default_na=False)
    steps = pd.read_csv(a.steps, keep_default_na=False)
    for df in (runs, steps):
        if "benchmark" not in df.columns:
            df.insert(0, "benchmark", cfg.benchmark)
    runs["state"] = runs["state"].astype(str)
    for c in ("prev", "candidate", "batch"):
        steps[c] = steps[c].astype(str)

    units = []
    for (b, rt), g in runs.groupby(["benchmark", "runtime"], sort=True):
        s = steps[(steps.benchmark == b) & (steps.runtime == rt)]
        units.append((b, rt, g, s, cfg, len(units)))
    print(f"{len(runs)} run rows, {len(steps)} step rows, {len(units)} benchmark x runtime units; "
          f"{cfg.n_resamples} bootstrap replicates, predecessor states from '{cfg.pred_source}'", flush=True)

    t0 = time.time()
    results = []
    if a.workers > 1 and len(units) > 1:
        with ProcessPoolExecutor(max_workers=min(a.workers, len(units))) as ex:
            for r in ex.map(_work, units):
                results.append(r)
                print(f"  done {r['t6']['benchmark']} {r['t6']['runtime']} ({r['sanity']['seconds']} s)", flush=True)
    else:
        for u in units:
            r = analyze_unit(*u)
            results.append(r)
            print(f"  done {r['t6']['benchmark']} {r['t6']['runtime']} ({r['sanity']['seconds']} s)", flush=True)
    print(f"total {time.time() - t0:.0f} s")

    def cat(key):
        frames = [r[key] if isinstance(r[key], pd.DataFrame) else pd.DataFrame([r[key]]) for r in results]
        frames = [f for f in frames if len(f)]
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    T = {k: cat(k) for k in ["t6", "t7", "t8", "t9", "t10", "sl", "mc", "tests", "adm", "fp", "pr", "gt", "sanity"]}
    for k in ("t6", "t7", "t8", "t9", "t10"):
        T[k].insert(2, "rho", T[k]["runtime"].map(rho_label))

    names = {"t6": "table_VI", "t7": "table_VII", "t8": "table_VIII", "t9": "table_IX",
             "t10": "table_X", "sl": "table_XIII_service_level", "mc": "detail_marginal_costs",
             "tests": "detail_state_dependence_tests", "adm": "detail_admission_reference",
             "fp": "detail_flip_pairs", "pr": "detail_policy_replay", "gt": "detail_gate_triggers",
             "sanity": "sanity_checks"}
    for k, n in names.items():
        T[k].to_csv(os.path.join(a.out, n + ".csv"), index=False, float_format="%.6g")

    th = gates_to_theta(cfg.ref_eps_L, cfg.ref_tau_QA, cfg.eps_slo)
    notes = NOTES.format(pred="steps.csv prev states (paper Eq. 18)" if cfg.pred_source == "steps"
                         else "every measured X with X+{s} measured (run.py)",
                         eL=cfg.ref_eps_L, tq=cfg.ref_tau_QA, dq=th["tau_QA"], es=cfg.eps_slo)
    titles = {"t6": "TABLE VI  (RQ1) state-conditioned marginal costs",
              "t7": "TABLE VII (RQ2) state-induced admission classes and flips under theta_ref",
              "t8": "TABLE VIII (RQ2) trade-off point effect classes",
              "t9": "TABLE IX  (RQ2) policy-induced admission sensitivity",
              "t10": "TABLE X   (RQ2) rejection trigger rates of the CRI trajectories"}
    txt, md = [], []
    for k, title in titles.items():
        df = T[k].drop(columns=[c for c in T[k].columns if c.endswith("_list")], errors="ignore")
        txt.append(title + "\n" + df.to_string(index=False, float_format=lambda v: f"{v:.3f}") + "\n")
        md.append(f"### {title}\n\n" + to_md(df) + "\n")
        if k == "t8":
            lists = T[k][["benchmark", "runtime"] + [c for c in T[k].columns if c.endswith("_list")]]
            txt.append("  unique services per effect:\n" + lists.to_string(index=False) + "\n")
            md.append("Unique services per effect:\n\n" + to_md(lists) + "\n")
    txt.append(notes)
    md.append("```\n" + notes + "\n```")
    open(os.path.join(a.out, "tables_summary.txt"), "w").write("\n".join(txt))
    open(os.path.join(a.out, "tables_summary.md"), "w").write("\n".join(md))

    print()
    print("\n".join(txt))
    sc = T["sanity"]
    print("Sanity:", f"online decision vs median rule mismatches = {int(sc.online_vs_median_rule_mismatch.sum())}",
          f"| online vs exact-CI admission mismatches = {int(sc.online_vs_exact_ci_admission_mismatch.sum())}",
          f"of {int(sc.n_cri_steps.sum())} CRI steps",
          f"| runs per state {int(sc.runs_per_state_min.min())}-{int(sc.runs_per_state_max.max())}")
    print(f"Wrote results to {os.path.abspath(a.out)}")


if __name__ == "__main__":
    main()

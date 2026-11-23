"""
statetools.py

Statistical helpers for the state dependence analysis.
Implements the estimation rules of the methodology (Subsection A).

    exact median interval    -> admission and governed set membership (RQ2, RQ3)
    bootstrap of differences -> marginal cost and state dependence (RQ1, RQ2)
    Holm correction          -> familywise error across many tests

Dependencies: numpy, pandas, scipy, statsmodels
"""
from __future__ import annotations

import itertools
import numpy as np
import pandas as pd
from scipy import stats
from statsmodels.stats.multitest import multipletests

METRICS = ["dL", "dQA", "dF"]


# ---------------------------------------------------------------------
# State keys
# ---------------------------------------------------------------------
def key(services) -> str:
    """Canonical string for a set of agentified services, '' for X0."""
    return "+".join(sorted(services))


def members(state_key: str) -> frozenset:
    return frozenset(s for s in state_key.split("+") if s)


# ---------------------------------------------------------------------
# 1. Exact interval for a single median (order statistics)
# ---------------------------------------------------------------------
def median_ci_exact(x, conf: float = 0.95):
    """Distribution free interval for the median of one sample.

    Each run falls below the true median with probability 1/2, so the
    number of runs below it is Binomial(R, 0.5). The interval between the
    j-th and (R-j+1)-th sorted values covers the median with probability
    P(j <= K <= R-j). We return the narrowest such interval whose
    coverage is at least `conf`.

    Returns (median, lo, hi, achieved_coverage, j).
    """
    x = np.sort(np.asarray(x, dtype=float))
    R = len(x)
    best = None
    for j in range(1, R // 2 + 1):
        cov = stats.binom.cdf(R - j, R, 0.5) - stats.binom.cdf(j - 1, R, 0.5)
        if cov >= conf:
            best = (j, cov)
        else:
            break
    if best is None:
        raise ValueError(f"R={R} runs are too few for {conf:.0%} coverage")
    j, cov = best
    return float(np.median(x)), float(x[j - 1]), float(x[R - j]), float(cov), j


def bound_label(x, bound: float, strict: bool = False, conf: float = 0.95) -> str:
    """'within', 'beyond' or 'uncertain' for the median of x against a bound.

    strict=False tests median <= bound, strict=True tests median < bound.
    """
    _, lo, hi, _, _ = median_ci_exact(x, conf)
    if (hi < bound) if strict else (hi <= bound):
        return "within"
    if (lo >= bound) if strict else (lo > bound):
        return "beyond"
    return "uncertain"


def admission(runs_of_state: pd.DataFrame, theta: dict, conf: float = 0.95) -> str:
    """Admission of one state under gates theta = {'eps_L', 'tau_QA', 'eps_SLO'}.

    'admitted'  every component within its bound
    'rejected'  some component beyond its bound
    'uncertain' otherwise
    """
    labels = [
        bound_label(runs_of_state["dL"], theta["eps_L"], conf=conf),
        bound_label(runs_of_state["dQA"], theta["tau_QA"], conf=conf),
        bound_label(runs_of_state["dF"], theta["eps_SLO"], strict=True, conf=conf),
    ]
    if "beyond" in labels:
        return "rejected"
    if all(l == "within" for l in labels):
        return "admitted"
    return "uncertain"


# ---------------------------------------------------------------------
# 2. Percentile bootstrap for differences of medians
# ---------------------------------------------------------------------
def _med(a, axis=-1):
    return np.median(a, axis=axis)


def boot_marginal(after, before, n_resamples=10_000, conf=0.95, seed=0):
    """Marginal cost median(after) - median(before), with percentile CI.

    The two samples are resampled separately (paired=False), because
    their runs come from independent executions.
    Returns dict(est, lo, hi, p, dist).
    """
    after, before = np.asarray(after, float), np.asarray(before, float)
    res = stats.bootstrap(
        (after, before),
        lambda a, b, axis=-1: _med(a, axis) - _med(b, axis),
        n_resamples=n_resamples, confidence_level=conf,
        method="percentile", paired=False, vectorized=True,
        random_state=np.random.default_rng(seed),
    )
    est = float(np.median(after) - np.median(before))
    return _pack(est, res)


def boot_diff_of_marginals(a1, b1, a2, b2, n_resamples=10_000, conf=0.95, seed=0):
    """[median(a1)-median(b1)] - [median(a2)-median(b2)] with percentile CI.

    Tests whether one service has the same marginal cost in two states.
    a1, b1 = runs of X cup {s}, X ; a2, b2 = runs of X' cup {s}, X'.
    """
    samples = tuple(np.asarray(v, float) for v in (a1, b1, a2, b2))
    res = stats.bootstrap(
        samples,
        lambda w, x, y, z, axis=-1: (_med(w, axis) - _med(x, axis))
                                    - (_med(y, axis) - _med(z, axis)),
        n_resamples=n_resamples, confidence_level=conf,
        method="percentile", paired=False, vectorized=True,
        random_state=np.random.default_rng(seed),
    )
    est = float((np.median(samples[0]) - np.median(samples[1]))
                - (np.median(samples[2]) - np.median(samples[3])))
    return _pack(est, res)


def _pack(est, res):
    dist = res.bootstrap_distribution
    # two sided bootstrap p value for H0 "difference = 0"
    p = 2 * min((dist <= 0).mean(), (dist >= 0).mean())
    p = float(min(1.0, max(p, 1 / len(dist))))
    return dict(est=est,
                lo=float(res.confidence_interval.low),
                hi=float(res.confidence_interval.high),
                p=p, dist=dist)


# ---------------------------------------------------------------------
# 3. Holm correction
# ---------------------------------------------------------------------
def holm(pvalues, alpha=0.05):
    """Holm adjusted p values and reject flags (familywise error alpha)."""
    reject, p_adj, _, _ = multipletests(pvalues, alpha=alpha, method="holm")
    return reject, p_adj


# ---------------------------------------------------------------------
# 4. Measured states of a service and the full analysis
# ---------------------------------------------------------------------
def measured_states_of(service: str, measured: set) -> list[str]:
    """X_s, the measured states X with s not in X and X cup {s} measured."""
    out = []
    for X in measured:
        m = members(X)
        if service not in m and key(m | {service}) in measured:
            out.append(X)
    return sorted(out, key=lambda k: (len(members(k)), k))


def state_dependence_tests(runs: pd.DataFrame, services, metric="dL",
                           n_resamples=10_000, alpha=0.05, seed=0):
    """All pairwise tests of Definition 3 for one metric, Holm corrected.

    `runs` has columns state, run, dL, dQA, dF for one benchmark and one
    runtime condition. Returns one row per (service, X, X').
    """
    by_state = {k: g[metric].to_numpy() for k, g in runs.groupby("state")}
    measured = set(by_state)
    rows = []
    for s in services:
        Xs = measured_states_of(s, measured)
        for i, (X, X2) in enumerate(itertools.combinations(Xs, 2)):
            r = boot_diff_of_marginals(
                by_state[key(members(X) | {s})], by_state[X],
                by_state[key(members(X2) | {s})], by_state[X2],
                n_resamples=n_resamples, seed=seed + i)
            rows.append(dict(service=s, X=X or "X0", X2=X2 or "X0",
                             diff=r["est"], lo=r["lo"], hi=r["hi"], p=r["p"]))
    out = pd.DataFrame(rows)
    out["metric"] = metric
    if len(out):
        out["reject_holm"], out["p_holm"] = holm(out["p"], alpha)
    return out


def component_labels(runs_of_state: pd.DataFrame, theta: dict, conf=0.95) -> dict:
    """Per metric label ('within', 'beyond', 'uncertain') for one state."""
    return {
        "dL": bound_label(runs_of_state["dL"], theta["eps_L"], conf=conf),
        "dQA": bound_label(runs_of_state["dQA"], theta["tau_QA"], conf=conf),
        "dF": bound_label(runs_of_state["dF"], theta["eps_SLO"], strict=True, conf=conf),
    }


def classify_services(runs: pd.DataFrame, services, theta: dict,
                      dep_tests: pd.DataFrame | None = None):
    """Service classes under one reference theta.

    cls        always / never / depends / undetermined
    flip_on    metrics whose gate label changes across the states of s
    mechanism  for 'depends' only. 'interaction' if the marginal cost of s
               differs between two states (Holm corrected) on a metric in
               flip_on, otherwise 'budget'

    dep_tests must hold the output of state_dependence_tests for every
    metric, concatenated, with a column 'metric'.
    """
    groups = {k: g for k, g in runs.groupby("state")}
    measured = set(groups)
    rows = []
    for s in services:
        Xs = measured_states_of(s, measured)
        cand = {X: groups[key(members(X) | {s})] for X in Xs}
        labels = {X: admission(cand[X], theta) for X in Xs}
        comps = {X: component_labels(cand[X], theta) for X in Xs}
        decided = [l for l in labels.values() if l != "uncertain"]
        if len(Xs) < 2:
            cls = "undetermined"
        elif decided and all(l == "admitted" for l in decided):
            cls = "always"
        elif decided and all(l == "rejected" for l in decided):
            cls = "never"
        elif "admitted" in decided and "rejected" in decided:
            cls = "depends"
        else:
            cls = "undetermined"
        flip_on = [m for m in METRICS
                   if {"within", "beyond"} <= {comps[X][m] for X in Xs}]
        mech = ""
        if cls == "depends" and dep_tests is not None and len(dep_tests):
            hit = dep_tests[(dep_tests.service == s) & dep_tests.reject_holm
                            & dep_tests.metric.isin(flip_on)]
            mech = "interaction" if len(hit) else "budget"
        rows.append(dict(service=s, n_states=len(Xs), cls=cls,
                         flip_on=",".join(flip_on), mechanism=mech,
                         admission=labels))
    return pd.DataFrame(rows)


def holm_joint(tests: pd.DataFrame, alpha=0.05) -> pd.DataFrame:
    """Reapply Holm across all rows, for example across all metrics of one
    benchmark and runtime condition, instead of within each metric."""
    out = tests.copy().reset_index(drop=True)
    out["reject_holm"], out["p_holm"] = holm(out["p"], alpha)
    return out

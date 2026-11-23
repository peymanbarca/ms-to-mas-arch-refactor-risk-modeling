#!/usr/bin/env python3
"""
plot_table_VI.py

Plots TABLE VI (RQ1: aggregated state-conditioned marginal costs across eligible
services) for one or two benchmarks in the same figure, from the table_VI.csv
files written by tables_analysis.py (one CSV per benchmark).

Figure: 3 rows (quality attributes: latency L, QA, failure rate F)
        x 2 columns (left m, right V); x axis = runtime configuration.
In every panel, each configuration has one bar per benchmark (B1 blue, B2 red):

    m  (circle)   median over eligible services of the per-service median
                  marginal cost, bar = min-to-max range over those services
    V  (diamond)  median over eligible services of the interaction magnitude
                  V = max_X m_s(X) - min_X m_s(X), bar = min-to-max range
    vertical label beside each bar: N_SD (services with a Holm-significant
                  state dependence) and R_SD = N_SD / N_eligible

m and V have the same unit per attribute but very different sizes (latency V is a
few pp, m up to ~90 %), so each panel has its own y range instead of one shared axis.

Usage
-----
    python3 plot_table_VI.py --csv B1_simulated_interactions/tables_out/table_VI.csv B2_simulated_interactions/tables_out/table_VI.csv
    python3 plot_table_VI.py --csv B1_simulated_interactions/tables_out/table_VI.csv B2_simulated_interactions/tables_out/table_VI.csv --labels B1 B2 --out figs
    python3 plot_table_VI.py --csv B1_simulated_interactions/tables_out/table_VI.csv                  # a single benchmark also works
    python3 plot_table_VI.py --csv ... --dark --formats png pdf

The benchmark label of each CSV is taken from --labels (default B1, B2, in the order
of --csv); the 'benchmark' column inside the files is ignored, so it does not matter
if both files were produced with the same default label.
Configurations are aligned by runtime (C1..C8); a configuration missing in one file
is simply left empty for that benchmark.

Needs: pandas, matplotlib. Columns used: runtime, rho, N_eligible, me_*, me_*_min,
me_*_max, Ve_*, Ve_*_min, Ve_*_max, N_SD_*, R_SD_*  (* = L, QA, F).
"""
from __future__ import annotations

import argparse
import os

import matplotlib.pyplot as plt
import matplotlib.transforms as mtransforms
import pandas as pd
from matplotlib.lines import Line2D

ATTRS = [  # (suffix in CSV, panel title, y label)
    ("L", "Latency", "latency inflation (%)"),
    ("QA", "QA", "quality deviation (pp)"),
    ("F", "Failure rate", "failure rate increase (pp)"),
]

# bench: first benchmark blue, second red (palette slots 1 and 8, light / dark steps)
THEMES = {
    "light": dict(surface="#fcfcfb", ink="#0b0b0b", ink2="#52514e", grid="#e4e3df",
                  axis="#b9b8b2", bench=["#2a78d6", "#e34948"]),
    "dark": dict(surface="#1a1a19", ink="#f0efec", ink2="#c3c2b7", grid="#34332f",
                 axis="#5a5954", bench=["#3987e5", "#e66767"]),
}


def rt_key(s: str) -> int:
    """C1 -> 1, C12 -> 12 (numeric order of runtime configurations)."""
    return int("".join(ch for ch in str(s) if ch.isdigit()))


def load(csv: str) -> pd.DataFrame:
    df = pd.read_csv(csv)
    df["_k"] = df["runtime"].map(rt_key)
    return df.sort_values("_k").set_index("runtime", drop=False)


def sd_label(n_sd: int, r_sd: float) -> str:
    return r"$N_{SD}$" + f"={n_sd}, " + r"$R_{SD}$" + f"={r_sd:.2f}"


def plot_benchmarks(data: dict, th: dict, out_base: str, formats, dpi: int):
    labels = list(data)
    k = len(labels)
    colors = th["bench"][:k]

    # x axis = union of runtime configurations, numeric order
    configs = sorted({rt for d in data.values() for rt in d.index}, key=rt_key)
    xpos = {rt: i for i, rt in enumerate(configs)}
    x_all = list(range(len(configs)))

    def rho_of(rt):
        for d in data.values():
            if rt in d.index:
                return str(d.loc[rt, "rho"]).replace("rho", "ρ")
        return rt

    def n_elig(rt):
        parts = []
        for lab, d in data.items():
            parts.append(f"{lab}={int(d.loc[rt, 'N_eligible'])}" if rt in d.index else f"{lab}=-")
        return "\n".join(parts)          # one benchmark per line, keeps the tick labels narrow

    xlabels = [f"{rho_of(rt)}\n" + r"$N_{eligible}$" + f"\n{n_elig(rt)}" for rt in configs]

    dx = 0.19          # bar offset from the configuration centre
    dtext = 0.13       # distance of the vertical N_SD / R_SD label from its bar
    offsets = [(bi - (k - 1) / 2) * 2 * dx for bi in range(k)]
    sides = [(-1 if (bi == 0 and k > 1) else +1) for bi in range(k)]  # label left of 1st bar, right of 2nd

    plt.rcParams.update({"font.family": "DejaVu Sans", "text.color": th["ink"],
                         "axes.labelcolor": th["ink2"], "xtick.color": th["ink2"],
                         "ytick.color": th["ink2"]})
    # rows = quality attributes, columns = m | V. Same units in both columns, but each
    # panel has its own y range (no dual axis): V is far smaller than m for latency.
    fig, axes = plt.subplots(len(ATTRS), 2, figsize=(13.5, 8.8), sharex="col",
                             facecolor=th["surface"])

    cols = [("me", "o", r"$\widetilde m^q$: median marginal cost of a service across its predecessor states"),
            ("Ve", "o", r"$V^q$: marginal cost variation magnitude of a service across its predecessor states")]
    for ci, (key, marker, ctitle) in enumerate(cols):
        print(ci)
        axes[0, ci].annotate(ctitle, xy=(0, 1), xycoords="axes fraction", xytext=(0, 36),
                             textcoords="offset points", fontsize=9, fontweight="bold",
                             ha="left", va="bottom", color=th["ink"])
        for ri, (q, title, ylab) in enumerate(ATTRS):
            ax = axes[ri, ci]
            ax.set_facecolor(th["surface"])
            tr = mtransforms.blended_transform_factory(ax.transData, ax.transAxes)
            tops = []
            for bi, (lab, d) in enumerate(data.items()):
                color = colors[bi]
                xs = [xpos[rt] + offsets[bi] for rt in d.index]
                med, lo, hi = d[f"{key}_{q}"], d[f"{key}_{q}_min"], d[f"{key}_{q}_max"]
                # min-to-max range over eligible services: thin bar with caps
                ax.vlines(xs, lo, hi, color=color, lw=2, zorder=2)
                ax.scatter(xs, lo, marker="_", s=50, color=color, lw=1.6, zorder=2)
                ax.scatter(xs, hi, marker="_", s=50, color=color, lw=1.6, zorder=2)
                # median marker with a surface ring so it stays readable on top of the bar
                ax.scatter(xs, med, marker=marker, s=20, color=color,
                           edgecolor=th["surface"], linewidth=1.1, zorder=3)
                tops.append(float(hi.max()))
                # N_SD / R_SD of this attribute: vertical text beside the bar

                if ci == 0:
                    for xb, r in zip(xs, d.itertuples()):
                        n_sd = int(getattr(r, f"N_SD_{q}"))
                        if n_sd > 0:
                            txt = sd_label(n_sd, float(getattr(r, f"R_SD_{q}")))
                            ax.text(xb + sides[bi] * dtext, 0.04, txt, transform=tr, rotation=90, ha="center", va="bottom", fontsize=5.8, color=th["ink2"])
            top = max(tops) if max(tops) > 0 else 1.0
            ax.set_ylim(-0.04 * top, 1.10 * top)
            ax.set_ylabel(ylab, fontsize=8.5)
            ax.yaxis.grid(True, color=th["grid"], lw=0.8, zorder=0)
            ax.set_axisbelow(True)
            for side in ("top", "right"):
                ax.spines[side].set_visible(False)
            for side in ("left", "bottom"):
                ax.spines[side].set_color(th["axis"])
            ax.tick_params(axis="both", labelsize=8, length=3)
            ax.set_xlim(-0.6, len(configs) - 0.4)
            if ci == 0:
                ax.set_title(title, loc="left", fontsize=10, fontweight="bold", pad=8)
                
            # vertical dashed dividers between runtime configurations
            for x_div in [i + 0.5 for i in range(len(configs) - 1)]:
                ax.axvline(x_div, color=th["grid"], linestyle="--", lw=0.9, zorder=0)
            
                           
        axes[-1, ci].set_xticks(x_all)
        axes[-1, ci].set_xticklabels(xlabels, fontsize=7.5)

    # legend 1: what marker and bar mean (neutral ink, colour belongs to the benchmark)
    handles = [Line2D([0], [0], color=th["ink2"], marker="o", lw=2, markersize=8,
                      markerfacecolor=th["ink2"], markeredgecolor=th["surface"],
                      label="median over eligible services"),
               Line2D([0], [0], color=th["ink2"], lw=2, marker="_", markersize=11,
                      label="min-to-max range over eligible services")]
    leg = fig.legend(handles=handles, loc="lower center", ncol=2, frameon=False, fontsize=8.5,
                     bbox_to_anchor=(0.5, 0.0))
    for t in leg.get_texts():
        t.set_color(th["ink2"])

    # legend 2: benchmark colours
    bench_handles = [Line2D([0], [0], color=c, marker="s", lw=0, markersize=7,
                            markerfacecolor=c, markeredgecolor="none", label=lab)
                     for lab, c in zip(labels, colors)]
    fig.legend(handles=bench_handles, loc="upper right", frameon=False, fontsize=8.5,
               bbox_to_anchor=(0.95, 0.9))
    for t in fig.legends[-1].get_texts():
        t.set_color(th["ink2"])

    fig.tight_layout(rect=(0, 0.05, 1, 0.97), h_pad=2.4, w_pad=2.5)

    paths = []
    for ext in formats:
        p = f"{out_base}.{ext}"
        fig.savefig(p, dpi=dpi, facecolor=th["surface"], bbox_inches="tight")
        paths.append(p)
    plt.close(fig)
    return paths


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", nargs="+", required=True, metavar="CSV",
                    help="table_VI.csv of each benchmark (one or two files)")
    ap.add_argument("--labels", nargs="+", default=None,
                    help="benchmark label of each CSV, default B1 B2")
    ap.add_argument("--out", default="figs")
    ap.add_argument("--formats", nargs="+", default=["png"])
    ap.add_argument("--dpi", type=int, default=400)
    ap.add_argument("--dark", action="store_true", help="dark surface instead of light")
    a = ap.parse_args()

    if len(a.csv) > 2:
        ap.error("at most two benchmarks are supported (B1 blue, B2 red)")
    labels = a.labels or [f"B{i + 1}" for i in range(len(a.csv))]
    if len(labels) != len(a.csv):
        ap.error("--labels needs one label per --csv")

    data = {lab: load(path) for lab, path in zip(labels, a.csv)}
    os.makedirs(a.out, exist_ok=True)
    th = THEMES["dark" if a.dark else "light"]
    for p in plot_benchmarks(data, th, os.path.join(a.out, "marginal_cost_analysis_state_dependency" + "_".join(labels)),
                             a.formats, a.dpi):
        print("wrote", p)


if __name__ == "__main__":
    main()

import json
import os
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch
from matplotlib import cm
from matplotlib.transforms import blended_transform_factory
import numpy as np

plt.rcParams.update({
    "text.usetex": False,
    "font.family": "serif",
    "font.serif": ["Times New Roman", "DejaVu Serif"],
    "mathtext.fontset": "cm",
})


json_path = os.path.join("results", "heatmap_contributions_data.json")
plot_path = os.path.join("generated_figs", "heatmap_contributions.png")

with open(json_path, "r", encoding="utf-8") as f:
    data = json.load(f)

row_titles = data["row_titles"]
row_desc = data["row_desc"]
benchmarks = data["benchmarks"]
metrics = [
    (
        item["name"],
        np.array(item["values"], dtype=float),
        np.array(item["errors"], dtype=float),
    )
    for item in data["metrics"]
]

# ==========================================================
# Plot settings
# ==========================================================

fig, ax = plt.subplots(figsize=(14, 3.8))

ax.set_xlim(0, len(metrics) * 3)
ax.set_ylim(0, len(row_titles))

ax.invert_yaxis()
ax.set_aspect("equal")

cmap = cm.Blues

# ==========================================================
# Draw blocks
# ==========================================================

for metric_id, (metric_name, values, errors) in enumerate(metrics):
    vmin = values.min()
    vmax = values.max()

    norm = (values - vmin) / (vmax - vmin + 1e-12)

    x0 = metric_id * 3

    ax.text(
        x0 + 1.5,
        -0.55,
        metric_name,
        ha="center",
        va="bottom",
        fontsize=16,
    )

    for j, b in enumerate(benchmarks):
        ax.text(
            x0 + j + 0.5,
            -0.18,
            b,
            ha="center",
            va="bottom",
            fontsize=13,
            color="black",
            fontweight="bold",
        )

    for i in range(values.shape[0]):
        for j in range(values.shape[1]):
            color = cmap(0.2 + 0.65 * norm[i, j])

            box_w = 0.8
            box_h = 0.8

            left = x0 + j + (1 - box_w) / 2
            bottom = i + (1 - box_h) / 2

            rect = FancyBboxPatch(
                (left, bottom),
                box_w,
                box_h,
                boxstyle="round,pad=0.02,rounding_size=0.05",
                linewidth=0,
                facecolor=color,
            )

            ax.add_patch(rect)

            txt_color = "white" if norm[i, j] > 0.55 else "#12345a"

            mean = values[i, j]
            ci = errors[i, j]

            main_text = f"{mean:.2f}".rstrip("0").rstrip(".")
            ci_text = f"±{ci:.2f}".rstrip("0").rstrip(".") if ci > 0 else ""

            ax.text(
                x0 + j + 0.5,
                i + 0.43,
                main_text,
                ha="center",
                va="center",
                fontsize=13,
                color=txt_color,
                fontweight="bold",
            )

            ax.text(
                x0 + j + 0.5,
                i + 0.67,
                ci_text,
                ha="center",
                va="center",
                fontsize=10,
                color=txt_color,
            )

# ==========================================================
# Row labels
# ==========================================================

for i, (title, desc) in enumerate(zip(row_titles, row_desc)):
    y = i + 0.35

    ax.text(
        -0.35,
        y,
        title,
        fontsize=13,
        fontweight="bold",
        ha="right",
        va="center",
    )

    ax.text(
        -0.35,
        y + 0.15,
        desc,
        fontsize=10,
        fontweight="bold",
        color="dimgray",
        ha="right",
        va="top",
    )

# ==========================================================
# Cosmetics
# ==========================================================

ax.set_xticks([])
ax.set_yticks([])

for s in ax.spines.values():
    s.set_visible(False)

transform = blended_transform_factory(ax.transData, ax.transAxes)

for x in range(3, len(metrics) * 3, 3):
    ax.plot(
        [x, x],
        [0.0, 1.15],
        transform=transform,
        color="gray",
        linestyle="--",
        linewidth=1.4,
        alpha=0.9,
        zorder=0,
        clip_on=False,
    )

plt.tight_layout()
plt.savefig(plot_path, dpi=600, bbox_inches="tight")
plt.show()
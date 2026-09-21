import numpy as np
import matplotlib.pyplot as plt


# ============================================================
# Configuration
# ============================================================

R = 5000

# OTL block size = 20% of total workload
b = int(0.20 * R)          # 1000 requests

# Sustained violation threshold = 30% of block
g_mid = int(0.30 * b)      # 300 requests

n_blocks = int(np.ceil(R / b))

predicate_names = ["QA", "Latency", "Failure"]


def make_execution_sample(R, pattern):
    """
    Generate binary predicate-violation timelines.

    1 = predicate violated
    0 = predicate satisfied
    """

    qa = np.zeros(R, dtype=int)
    latency = np.zeros(R, dtype=int)
    failure = np.zeros(R, dtype=int)

    if pattern == "warmup":
        # ----------------------------------------
        # Benign transient warm-up violation
        # ----------------------------------------
        # ~83-request violation near the beginning

        # Latency also temporarily violates
        latency[20:103] = 1

        # Small failure episode during warm-up
        failure[35:55] = 1

    elif pattern == "repeating":
        # ----------------------------------------
        # Short repeated violations throughout
        # ----------------------------------------
        # Repeated short episodes
        episodes = [
            (300, 340),
            (900, 945),
            (1500, 1540),
            (2150, 2195),
            (2850, 2890),
            (3500, 3545),
            (4200, 4240),
            (4700, 4745),
        ]

        # for start, end in episodes:
        #     qa[start:end] = 1

        # Latency episodes slightly different
        latency_episodes = [
            (250, 380),
            (950, 1000),
            (1600, 1720),
            (2300, 2350),
            (2800, 3040),
            (3650, 3960),
            (4350, 4490),
            (4800, 4840),
        ]

        for start, end in latency_episodes:
            latency[start:end] = 1

        # Failure episodes are shorter
        failure_episodes = [
            (320, 330),
            (1620, 1670),
            (2860, 2870),
            (3720, 3830),
        ]

        for start, end in failure_episodes:
            failure[start:end] = 1

    elif pattern == "sustained":
        # ----------------------------------------
        # Almost continuously violated
        # ----------------------------------------
        qa[85:130] = 1
        qa[475:610] = 1
        qa[2875:2910] = 1

        latency[120:] = 1

        failure[420:] = 1

        # Small recovery gaps to make it realistic
        latency[2200:2320] = 0
        latency[4400:4620] = 0

        failure[2700:2820] = 0
        failure[4500:4520] = 0

    elif pattern == "middle":
        # ----------------------------------------
        # Short transient violation in the middle
        # ----------------------------------------
        # qa[2350:2420] = 1
        latency[2370:2630] = 1
        failure[2490:2510] = 1

    return {
        "QA": qa,
        "Latency": latency,
        "Failure": failure
    }
    
# ============================================================
# Temporal violation feature extraction
# ============================================================

def longest_violation_run(binary_series):

    x = np.asarray(binary_series)

    starts = np.where(
        (x == 1) & (np.r_[0, x[:-1]] == 0)
    )[0]

    ends = np.where(
        (x == 1) & (np.r_[x[1:], 0] == 0)
    )[0]

    if len(starts) == 0:
        return 0

    durations = ends - starts + 1

    return durations.max()


def violation_features(binary_series):

    x = np.asarray(binary_series)

    violation_indices = np.where(x == 1)[0]

    if len(violation_indices) == 0:
        return {
            "first_violation": None,
            "longest_run": 0,
            "episodes": 0,
            "recurs": False
        }

    first_violation = violation_indices[0] + 1

    starts = np.where(
        (x == 1) & (np.r_[0, x[:-1]] == 0)
    )[0]

    ends = np.where(
        (x == 1) & (np.r_[x[1:], 0] == 0)
    )[0]

    durations = ends - starts + 1

    return {
        "first_violation": first_violation,
        "longest_run": durations.max(),
        "episodes": len(starts),
        "recurs": len(starts) > 1
    }


# ============================================================
# OTL block evaluation
# ============================================================

def evaluate_otl(execution, R, b, g_mid):

    results = []

    interrupt_block = None

    for block_id, start in enumerate(
        range(0, R, b),
        start=1
    ):

        end = min(start + b, R)

        block_result = {
            "block": block_id,
            "start": start + 1,
            "end": end,
            "predicates": {},
            "decision": "continue" + '✔'
        }

        # --------------------------------------------
        # Evaluate each predicate inside this block
        # --------------------------------------------

        for predicate in predicate_names:

            block_data = execution[predicate][start:end]

            max_run = longest_violation_run(block_data)

            sustained = max_run >= g_mid

            block_result["predicates"][predicate] = {
                "max_run": max_run,
                "sustained": sustained
            }

            # First sustained violation causes interruption
            if sustained:
                block_result["decision"] = "interrupt" + '✘'

        results.append(block_result)

        # --------------------------------------------
        # OTL interrupts at first unsafe block
        # --------------------------------------------

        if block_result["decision"] == "interrupt":

            interrupt_block = block_id

            break

    return results, interrupt_block


executions = {
    "Transient warm-up , Product Catalog (B2)": make_execution_sample(R, "warmup"),
    "Repeating \n violations, Text Parser (B3)": make_execution_sample(R, "repeating"),
    "Sustained violation, Order (B2)": make_execution_sample(R, "sustained"),
    "Transient middle, Shipment (B2)": make_execution_sample(R, "middle")
}


otl_results = {}

for execution_name, execution in executions.items():

    results, interrupt_block = evaluate_otl(
        execution,
        R,
        b,
        g_mid
    )

    otl_results[execution_name] = {
        "results": results,
        "interrupt_block": interrupt_block
    }

    # print(
    #     f"{execution_name}: "
    #     f"{'INTERRUPT at B' + str(interrupt_block)" \ 
    #     if interrupt_block else 'CONTINUE through all blocks'}"
    # )
    
fig, axes = plt.subplots(
    4,
    1,
    figsize=(15, 11),
    sharex=True
)

for ax, (execution_name, execution) in zip(
    axes,
    executions.items()
):

    execution_name_original = execution_name.split(",")[0]  # Remove benchmark info for title
    example_benchmark_service = execution_name.split(",")[1] if "," in execution_name else ""
    # ========================================================
    # Plot request-level predicate violations
    # ========================================================

    for i, predicate in enumerate(predicate_names):

        violation_indices = np.where(
            execution[predicate] == 1
        )[0]

        ax.scatter(
            violation_indices,
            np.full_like(
                violation_indices,
                i
            ),
            s=7,
            marker="s",
            zorder=3
        )

    # ========================================================
    # OTL evaluation
    # ========================================================

    results, interrupt_block = evaluate_otl(
        execution,
        R,
        b,
        g_mid
    )

    # ========================================================
    # Draw OTL block boundaries
    # ========================================================

    for block_id in range(1, n_blocks):

        boundary = block_id * b - 0.5

        ax.axvline(
            boundary,
            linestyle="--",
            linewidth=1,
            alpha=0.5
        )

    # ========================================================
    # Annotate each evaluated OTL block
    # ========================================================

    for result in results:

        block_id = result["block"]

        block_start = result["start"]
        block_end = result["end"]

        center = (
            (block_start + block_end) / 2
        ) - 1

        decision = result["decision"]

        # Maximum run across predicates
        max_runs = [
            result["predicates"][p]["max_run"]
            for p in predicate_names
        ]

        block_max_run = max(max_runs)

        # ----------------------------------------------------
        # Block annotation
        # ----------------------------------------------------

        annotation = (
            f"$B_{{{block_id}}}$\n"
            f"max consecutive violation = {block_max_run}\n"
            f"{decision.upper()}"
        )

        ax.text(
            center,
            2.55,
            annotation,
            ha="center",
            va="bottom",
            fontsize=8,
            fontweight="bold"
        )


        benchmark_service_text =  f"{example_benchmark_service.strip()} "


        # ------------------------------------------------
        # Add benchmark box
        # ------------------------------------------------
        ax.text(
            0.995,
            0.05,
            benchmark_service_text,
            transform=ax.transAxes,
            ha="right",
            va="bottom",
            fontsize=8,
            bbox=dict(
                boxstyle="round,pad=0.4",
                facecolor="white",
                edgecolor="gray",
                alpha=0.9
            )
        )
        # ----------------------------------------------------
        # Highlight interrupted block
        # ----------------------------------------------------

        if decision == "interrupt" + '✘':

            ax.axvspan(
                block_start - 1,
                block_end - 1,
                alpha=0.08,
                zorder=0
            )

            # Stop showing future OTL blocks
            # because execution terminates here.
            break

    # ========================================================
    # Plot formatting
    # ========================================================

    ax.set_yticks([0, 1, 2])
    ax.set_yticklabels(predicate_names)

    ax.set_ylim(-0.5, 3.15)

    ax.set_title(
        execution_name_original,
        fontweight="bold",
        loc="left"
    )

    ax.grid(
        axis="x",
        linestyle=":",
        alpha=0.3
    )


axes[-1].set_xlabel(
    "Request index",
    fontweight="bold"
)

fig.suptitle(
    "On-the-Loop (OTL) Runtime Checkpoints",
    fontsize=14,
    fontweight="bold"
)

fig.text(
    0.99,
    0.01,
    (
        f"Block size $b={b}$ requests | "
        f"Sustained-violation threshold "
        f"$g_{{mid}}={g_mid}$ requests"
    ),
    ha="right",
    fontsize=9
)

plt.tight_layout(
    rect=[0, 0.03, 1, 0.97]
)
plt.savefig("mid_execution_temporal_violation_patterns_otl.png", dpi=400, bbox_inches="tight")
plt.show()
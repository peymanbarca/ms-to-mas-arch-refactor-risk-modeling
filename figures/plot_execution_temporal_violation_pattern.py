import numpy as np
import matplotlib.pyplot as plt

R = 5000
rng = np.random.default_rng(42)


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


executions = {
    "Transient warm-up , Product Catalog (B2)": make_execution_sample(R, "warmup"),
    "Repeating violations, Text Parser (B3)": make_execution_sample(R, "repeating"),
    "Sustained violation, Order (B2)": make_execution_sample(R, "sustained"),
    "Transient middle, Shipment (B2)": make_execution_sample(R, "middle")
}

def violation_features(binary_series):

    x = np.asarray(binary_series)

    violation_indices = np.where(x == 1)[0]

    # No violations
    if len(violation_indices) == 0:
        return {
            "first_violation": None,
            "longest_run": 0,
            "episodes": 0,
            "recurs": False
        }

    # First violation
    first_violation = violation_indices[0] + 1

    # Identify episode boundaries
    starts = np.where(
        (x == 1) & (np.r_[0, x[:-1]] == 0)
    )[0]

    ends = np.where(
        (x == 1) & (np.r_[x[1:], 0] == 0)
    )[0]

    durations = ends - starts + 1

    longest_run = durations.max()
    episodes = len(starts)

    # Recurrence means:
    # at least one violation episode occurs after
    # a recovery period.
    recurs = episodes > 1

    return {
        "first_violation": first_violation,
        "longest_run": longest_run,
        "episodes": episodes,
        "recurs": recurs
    }
    

predicate_names = ["QA", "Latency", "Failure"]

for execution_name, execution in executions.items():

    print("\n", execution_name)

    for predicate in predicate_names:

        features = violation_features(
            execution[predicate]
        )

        print(
            f"{predicate:8s} | "
            f"first={features['first_violation']} | "
            f"longest={features['longest_run']} | "
            f"episodes={features['episodes']} | "
            f"recurs={features['recurs']}"
        )
            
# ----------------------------- Plotting -----------------------------

fig, axes = plt.subplots(
    4, 1,
    figsize=(14, 9),
    sharex=True
)

predicate_names = ["QA", "Latency", "Failure"]

for ax, (execution_name, execution) in zip(axes, executions.items()):

    execution_name_original = execution_name.split(",")[0]  # Remove benchmark info for title
    example_benchmark_service = execution_name.split(",")[1] if "," in execution_name else ""
    
    # Plot violations
    for i, predicate in enumerate(predicate_names):

        violation_indices = np.where(
            execution[predicate] == 1
        )[0]

        ax.scatter(
            violation_indices,
            np.full_like(violation_indices, i),
            s=8,
            marker="s",
            color="red" if predicate == "Latency" else ("orange" if predicate == "Failure" else "black")
        )

    # ------------------------------------------------
    # Calculate temporal features
    # ------------------------------------------------
    features = {
        predicate: violation_features(execution[predicate])
        for predicate in predicate_names
    }

    # ------------------------------------------------
    # Build statistics text
    # ------------------------------------------------
    stats_text = ""

    for predicate in predicate_names:
        f = features[predicate]

        first = f["first_violation"]
        longest = f["longest_run"]
        episodes = f["episodes"]
        recurs = "Yes" if f["recurs"] else "No"

        stats_text += (
            f"{example_benchmark_service.strip()} | "
            f"{predicate}: "
            f"First={first}, "
            f"MaxRun={longest}, "
            f"Episodes={episodes}, "
            f"Recurs={recurs}\n"
        )

    # ------------------------------------------------
    # Add statistics box
    # ------------------------------------------------
    ax.text(
        0.995,
        0.05,
        stats_text,
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

    # Formatting
    ax.set_yticks([0, 1, 2])
    ax.set_yticklabels(predicate_names)
    ax.set_ylim(-0.5, 2.5)


    ax.set_title(
        execution_name_original,
        fontweight="bold",
        loc="left"
    )

    ax.grid(
        axis="x",
        linestyle="--",
        alpha=0.3
    )


axes[-1].set_xlabel(
    "Request index",
    fontweight="bold"
)

fig.suptitle(
    "Temporal Patterns of Predicate Violations",
    fontsize=14,
    fontweight="bold"
)

plt.tight_layout()
plt.savefig("execution_temporal_violation_patterns.png", dpi=400, bbox_inches="tight")
plt.show()
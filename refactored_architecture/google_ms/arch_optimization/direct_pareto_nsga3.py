from pymoo.algorithms.moo.nsga3 import NSGA3
from pymoo.optimize import minimize
from pymoo.operators.sampling.rnd import BinaryRandomSampling
from pymoo.operators.crossover.pntx import TwoPointCrossover
from pymoo.operators.mutation.bitflip import BitflipMutation
from pymoo.util.ref_dirs import get_reference_directions

import random
import numpy as np

from pymoo.core.problem import ElementwiseProblem
import matplotlib.pyplot as plt



FAILURE_SLO_THRESHOLD = 0.02 

SERVICE_NAMES = [
    "ad_service:5057",
    "cart_service:5054",
    "checkout_service:5050",
    "currency_service:5053",
    "email_service:5056",
    "payment_service:5052",
    "product_catalog_service:5055",
    "recommendation_service:5058",
    "shipping_service:5051",
]

N_SERVICES = len(SERVICE_NAMES)

'''
Example architecture representation:
X = [1, 0, 1, 0, 1, 0, 1, 0, 1]
     │  │  │  │  │  │  │  │  │
     │  │  │  │  │  │  │  │  └─ shipping
     │  │  │  │  │  │  │  └──── recommendation
     │  │  │  │  │  │  └─────── product_catalog
     │  │  │  │  │  └────────── payment
     │  │  │  │  └───────────── email
     │  │  │  └──────────────── currency
     │  │  └─────────────────── checkout
     │  └────────────────────── cart
     └───────────────────────── ad
'''


# ============================================================
#  Convert binary chromosome -> architecture
# ============================================================

def chromosome_to_architecture(X):

    agents = []
    services = []

    for i, bit in enumerate(X):

        if int(bit) == 1:
            agents.append(SERVICE_NAMES[i])
        else:
            services.append(SERVICE_NAMES[i])

    return agents, services


# ============================================================
#  Architecture evaluation
# ============================================================

architecture_cache = {}


def evaluate_architecture_cached(X):

    # Make chromosome hashable
    key = tuple(int(x) for x in X)

    # Avoid evaluating exactly the same architecture twice
    if key in architecture_cache:
        return architecture_cache[key]

    agents, services = chromosome_to_architecture(X)

    print("\nEvaluating architecture:")
    print("X       =", key)
    print("Agents  =", agents)
    print("Services=", services)

    # --------------------------------------------------------
    # YOUR EXISTING EXPERIMENTAL PIPELINE GOES HERE
    # --------------------------------------------------------
    #
    # result = run_architecture_experiment(
    #     agents=agents,
    #     services=services
    # )
    #
    # It should return something like:
    #
    # {
    #     "delta_qa": ...,
    #     "delta_l": ...,
    #     "delta_f": ...
    # }
    #
    # --------------------------------------------------------
    
    delta_qa = random.uniform(0, 0.05)
    delta_l = random.uniform(0, 1.05)
    delta_f = random.uniform(0, 0.05)
    migration_coverage = len(agents) / N_SERVICES

    # todo: run real architecture experiment
    # Build architecture
    # Deploy selected services as agents
    # Run benchmark and experiment trial runner
    # Calculate QA, latency, failure
    # ...
    

    migration_coverage = len(agents) / N_SERVICES

    result = {"delta_qa": delta_qa, "delta_l": delta_l, "delta_f": delta_f}
    result["migration_coverage"] = migration_coverage
    result["X"] = list(key)
    result["agents"] = agents
    result["services"] = services

    architecture_cache[key] = result

    return result


# ============================================================
#  NSGA-III optimization problem
# ============================================================

class DirectArchitectureProblem(ElementwiseProblem):

    def __init__(self):

        super().__init__(
            n_var=N_SERVICES,
            n_obj=3,
            n_ieq_constr=1,

            # Binary variables
            xl=0,
            xu=1
        )

    def _evaluate(self, X, out, *args, **kwargs):

        result = evaluate_architecture_cached(X)

        delta_qa = result["delta_qa"]
        delta_l = result["delta_l"]
        delta_f = result["delta_f"]
        migration_coverage = result["migration_coverage"]

        # Three optimization objectives
        out["F"] = np.array([
            delta_qa,
            delta_l,
            1 - migration_coverage
        ])

        # Constraint:
        # delta_f <= FAILURE_SLO_THRESHOLD
        #
        # pymoo expects g(X) <= 0 for a feasible solution.
        out["G"] = np.array([
            delta_f - FAILURE_SLO_THRESHOLD
        ])

# ============================================================
#  Reference directions
# ============================================================

ref_dirs = get_reference_directions(
    "das-dennis",
    3,                  # 3 objectives
    n_partitions=12
)


# ============================================================
#  NSGA-III
# ============================================================

algorithm = NSGA3(

    ref_dirs=ref_dirs,

    pop_size=len(ref_dirs),

    sampling=BinaryRandomSampling(),

    crossover=TwoPointCrossover(),

    mutation=BitflipMutation(),

    eliminate_duplicates=True
)
# NSGA-III uses these directions to maintain diversity across the objective space.



# ============================================================
# Run optimization
# ============================================================

problem = DirectArchitectureProblem()

res = minimize(
    problem,
    algorithm,

    termination=("n_gen", 10),

    seed=42,

    verbose=True
)
# res.X contains the chromosomes and res.F contains the corresponding objective values


# ============================================================
# Results
# ============================================================

print("\n========================================")
print("NSGA-III FINISHED")
print("========================================")

n_evaluated = res.algorithm.evaluator.n_eval
print("Number of evaluations:", res.algorithm.evaluator.n_eval)

print("\nTotal Pareto architectures chromosome:", len(res.X))

for X, F in zip(res.X, res.F):

    agents, services = chromosome_to_architecture(X)

    print("\nX =", X.astype(int))
    print("Agents:", agents)
    print("Services:", services)

    print("Objective values:")
    print("  ΔQA       =", F[0])
    print("  ΔLatency  =", F[1])
    # print("  ΔFailure  =", F[2])
    print("  Coverage  =", 1-F[2])


n_evaluated = res.algorithm.evaluator.n_eval
print("NSGA-III Total Number of Evaluations:", res.algorithm.evaluator.n_eval)

n_total_architectures = 2 ** len(SERVICE_NAMES)

print("Total architecture space:", n_total_architectures)
n_unique_evaluated = len(architecture_cache)
print("NSGA-III Total Number of Unique Architecture Evaluations:", res.algorithm.evaluator.n_eval)

evaluation_reduction = (
    1 - n_unique_evaluated / n_total_architectures
) * 100

print(f"Total possible architectures: {n_total_architectures}")
print(f"Unique architectures evaluated: {n_unique_evaluated}")
print(f"Evaluation reduction: {evaluation_reduction:.2f}%")


# ============================================================
# Plot Results
# ============================================================


X_pareto = np.asarray(res.X, dtype=int)
F_pareto = np.asarray(res.F)

delta_qa = F_pareto[:, 0]
delta_l = F_pareto[:, 1]
# retrieve failure rate from your cached architecture results
delta_f = np.array([
    architecture_cache[tuple(map(int, X))]["delta_f"]
    for X in res.X
])

# Because the 4th objective was defined as -migration_coverage
migration_coverage = 1 - F_pareto[:, 2]

print("Maximum failure-rate inflation:",
      delta_f.max())

print("SLO threshold:",
      FAILURE_SLO_THRESHOLD)

plt.figure(figsize=(11, 8))

sizes = 100 + 800 * migration_coverage

plt.scatter(
    delta_qa,
    delta_l,
    s=sizes,
    alpha=0.75,
    edgecolors="black",
    linewidths=0.7
)

for i, X in enumerate(X_pareto):
    label = "".join(map(str, X)) +"\n" + "C=" + f"{migration_coverage[i]:.0%}"

    plt.annotate(
        label,
        (delta_qa[i], delta_l[i]),
        xytext=(6, 6),
        textcoords="offset points",
        fontsize=8
    )

plt.xlabel(r"$\Delta QA$", fontsize=13)
plt.ylabel(r"$\Delta L_{p95}$", fontsize=13)
plt.title("NSGA-III Pareto Set", fontsize=15)

plt.grid(True, alpha=0.25)
plt.tight_layout()
plt.show()
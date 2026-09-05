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
print("--------------------------------------------------")
print("Total Pareto architectures chromosome:", len(res.X))
print("--------------------------------------------------")


X_nsga = np.asarray(res.X)
F_nsga = np.asarray(res.F)

delta_qa = F_nsga[:, 0]
delta_l = F_nsga[:, 1]
coverage = 1 - F_nsga[:, 2]

# confirm feasible NSGA-III solutions (failure rate SLO constraint)
print('\nf\nailure rates: ', res.G[:,0],'\n\n')
assert np.all(res.G[:, 0] <= 0), \
    "NSGA-III result contains infeasible solutions."




def minmax_normalize(Z):

    z_min = Z.min(axis=0)
    z_max = Z.max(axis=0)

    denominator = z_max - z_min

    # Avoid division by zero for an objective
    # that has identical values for all Pareto points.
    denominator[denominator == 0] = 1.0

    Z_norm = (Z - z_min) / denominator

    return Z_norm


# Construct the 10 Das-Dennis reference directions
## The directions are: (1,0,0),(0,1,0),(0,0,1), (2/3,1/3,0),(2/3,0,1/3),(1/3,2/3,0), (1/3,1/3,1/3).

def generate_reference_directions(n_obj=3, n_partitions=3):

    directions = []

    def generate_recursive(
        remaining,
        dimensions_left,
        current
    ):
        if dimensions_left == 1:
            directions.append(
                current + [remaining]
            )
            return

        for value in range(remaining + 1):
            generate_recursive(
                remaining - value,
                dimensions_left - 1,
                current + [value]
            )

    generate_recursive(
        n_partitions,
        n_obj,
        []
    )

    return np.asarray(directions, dtype=float) / n_partitions 


# Calculate perpendicular distance to each reference line
def perpendicular_distance(point, direction):

    projection = (
        np.dot(point, direction)
        / np.dot(direction, direction)
    ) * direction

    return np.linalg.norm(
        point - projection
    )
    
# Select the closest NSGA-III Pareto point for every direction

def reference_line_selection(
    X_pareto,
    F_pareto
):

    # ---------------------------------------------
    # Convert objectives to:
    # [ΔL, ΔQA, 1-Coverage]
    # ---------------------------------------------

    delta_qa = F_pareto[:, 0]
    delta_l = F_pareto[:, 1]
    coverage = 1 - F_pareto[:, 2]

    Z = np.column_stack([
        delta_l,
        delta_qa,
        1.0 - coverage
    ])

    # ---------------------------------------------
    # Normalize objective space
    # ---------------------------------------------

    Z_norm = minmax_normalize(Z)

    # ---------------------------------------------
    # Generate 10 reference directions
    # ---------------------------------------------

    reference_directions = generate_reference_directions(
        n_obj=3,
        n_partitions=3
    )
    
    print('reference lines:\n ', reference_directions)
    print("Number of directions:", len(reference_directions))

    selected_indices = []
    selected_distances = []

    # ---------------------------------------------
    # Find nearest Pareto point for each direction
    # ---------------------------------------------

    for direction in reference_directions:

        distances = np.array([
            perpendicular_distance(
                point,
                direction
            )
            for point in Z_norm
        ])

        idx = np.argmin(distances)

        selected_indices.append(idx)
        selected_distances.append(
            distances[idx]
        )

    # ---------------------------------------------
    # Return selected architectures
    # ---------------------------------------------

    selected_indices = np.asarray(
        selected_indices,
        dtype=int
    )

    selected_distances = np.asarray(
        selected_distances
    )

    selected_X = X_pareto[selected_indices]
    selected_F = F_pareto[selected_indices]

    selected_Z = Z[selected_indices]
    selected_Z_norm = Z_norm[selected_indices]

    return {
        "reference_directions": reference_directions,
        "selected_indices": selected_indices,
        "selected_distances": selected_distances,
        "selected_X": selected_X,
        "selected_F": selected_F,
        "selected_Z": selected_Z,
        "selected_Z_norm": selected_Z_norm,
    }
    
def reference_line_selection_unique(
    X_pareto,
    F_pareto
):

    delta_qa = F_pareto[:, 0]
    delta_l = F_pareto[:, 1]
    coverage = 1 - F_pareto[:, 2]

    Z = np.column_stack([
        delta_l,
        delta_qa,
        1.0 - coverage
    ])

    Z_norm = minmax_normalize(Z)

    reference_directions = generate_reference_directions(
        n_obj=3,
        n_partitions=3
    )
    print("Number of directions:", len(reference_directions))


    selected_indices = []
    selected_distances = []

    # Calculate complete distance matrix
    distance_matrix = np.zeros(
        (
            len(reference_directions),
            len(Z_norm)
        )
    )

    for i, direction in enumerate(reference_directions):

        for j, point in enumerate(Z_norm):

            distance_matrix[i, j] = (
                perpendicular_distance(
                    point,
                    direction
                )
            )

    # ---------------------------------------------
    # Greedy unique assignment
    # ---------------------------------------------

    available_points = set(range(len(Z_norm)))

    for i in range(len(reference_directions)):

        candidates = list(available_points)

        if not candidates:
            break

        best_idx = min(
            candidates,
            key=lambda j: distance_matrix[i, j]
        )

        selected_indices.append(best_idx)
        selected_distances.append(
            distance_matrix[i, best_idx]
        )

        available_points.remove(best_idx)

    selected_indices = np.asarray(
        selected_indices,
        dtype=int
    )

    return {
        "reference_directions": reference_directions,
        "selected_indices": selected_indices,
        "selected_distances": np.asarray(
            selected_distances
        ),
        "selected_X": X_pareto[selected_indices],
        "selected_F": F_pareto[selected_indices],
        "selected_Z": Z[selected_indices],
        "selected_Z_norm": Z_norm[selected_indices],
    }

# ============================================================
# Results
# ============================================================

rl_result = reference_line_selection_unique(
    X_pareto=res.X,
    F_pareto=res.F
)

# Convert the selected results back to your architecture records

# selected_architectures = []

# for X in rl_result["selected_X"]:

#     key = tuple(int(x) for x in X)

#     architecture = architecture_cache[key]

#     selected_architectures.append(
#         architecture
#     )
    
    
selected = rl_result

reference_architectures = []

for i, idx in enumerate(
    selected["selected_indices"]
):

    architecture = dict(
        architecture_cache[
            tuple(
                int(x)
                for x in selected["selected_X"][i]
            )
        ]
    )

    architecture["reference_direction"] = (
        selected["reference_directions"][i].tolist()
    )

    architecture["reference_distance"] = float(
        selected["selected_distances"][i]
    )

    reference_architectures.append(
        architecture
    )
    
print("--------------------------------------------------")    
print("Selected architectures for each reference direction:")
print("--------------------------------------------------")
for i, architecture in enumerate(
    reference_architectures
):

    print(f"\nReference Direction {i+1}:")
    print("Reference Direction:", architecture["reference_direction"])
    print("Reference Distance:", architecture["reference_distance"])
    print("ΔQA:", architecture["delta_qa"])
    print("ΔL:", architecture["delta_l"])
    print("ΔF:", architecture["delta_f"])
    print("Coverage:", architecture["migration_coverage"])
    print("Agents:", architecture["agents"])
    print("Services:", architecture["services"])
    

# ============================================================
# Plot Results
# ============================================================    

F_pareto = np.asarray(res.F)

pareto_qa = F_pareto[:, 0]
pareto_l = F_pareto[:, 1]
pareto_coverage = 1 - F_pareto[:, 2]

F_selected = rl_result["selected_F"]

selected_qa = F_selected[:, 0]
selected_l = F_selected[:, 1]
selected_coverage = 1 - F_selected[:, 2]

plt.figure(figsize=(10, 7))

# NSGA-III Pareto set
plt.scatter(
    pareto_qa,
    pareto_l,
    s=100 + 600 * pareto_coverage,
    alpha=0.30,
    label="NSGA-III Pareto Set"
)

# Reference-line selected points
plt.scatter(
    selected_qa,
    selected_l,
    s=250 + 900 * selected_coverage,
    marker="*",
    edgecolors="black",
    linewidths=1.2,
    alpha=0.95,
    label="Reference-Line Selected"
)

# Label each selected architecture
for i in range(len(selected_qa)):

    r = rl_result["reference_directions"][i]

    label = (
        f"R{i+1} "
        f"({r[0]:.2f},{r[1]:.2f},{r[2]:.2f})"
    )

    plt.annotate(
        label,
        (
            selected_qa[i],
            selected_l[i]
        ),
        xytext=(7, 7),
        textcoords="offset points",
        fontsize=8
    )

plt.xlabel(r"$\Delta QA$")
plt.ylabel(r"$\Delta L_{p95}$")

plt.title(
    "Reference-Line Selection from NSGA-III Pareto Set"
)

plt.legend()
plt.grid(True, alpha=0.25)
plt.tight_layout()
plt.show()
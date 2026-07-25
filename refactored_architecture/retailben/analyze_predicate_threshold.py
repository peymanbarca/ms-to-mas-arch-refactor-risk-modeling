from pymoo.core.problem import ElementwiseProblem
from pymoo.algorithms.moo.nsga2 import NSGA2
from pymoo.optimize import minimize
import random
import pandas as pd
import matplotlib.pyplot as plt

def run_migration_policy(tau_qa, eps_l):
    """
    Runs the migration process using these acceptance thresholds.
    Returns architecture metrics.
    """

    return {
        "delta_qa": tau_qa-random.uniform(0, 10),
        "delta_latency": eps_l-random.uniform(0, 20),
        "delta_failure": eps_l/10,
        "u_max": 100 * ( eps_l/100)
    }
    

# Your optimization variables are:

# τ QA ∈[90,100]
# ϵL ∈[30,90]
    
# x = [tau_qa, epsilon_l]

# F = [
#     delta_qa,
#     delta_latency,
#     delta_failure,
#     -u_max
# ]


class ThresholdProblem(ElementwiseProblem):

    def __init__(self):

        super().__init__(
            n_var=2,
            n_obj=3, # 3 or 4 objectives
            xl=[90, 30],
            xu=[100, 90]
        )

    def _evaluate(self, x, out, *args, **kwargs):

        tau_qa = x[0]
        eps_l  = x[1]

        result = run_migration_policy(
            tau_qa,
            eps_l
        )

        out["F"] = [
            result["delta_qa"],
            result["delta_latency"],
            result["delta_failure"],
            -result["u_max"]
        ]
        

        out["F"] = [
            result["delta_qa"],
            result["delta_latency"],
            result["delta_failure"],
            # -result["u_max"]
        ]

# Run NSGA-II

problem = ThresholdProblem()

algorithm = NSGA2(
    pop_size=100
)

res = minimize(
    problem,
    algorithm,
    ('n_gen', 100),
    seed=1,
    verbose=True
)

thresholds = res.X # Pareto-optimal thresholds
objectives = res.F # Corresponding objective values

tau = thresholds[:,0]
eps = thresholds[:,1]

qa = objectives[:,0]
lat = objectives[:,1]

pareto_df = pd.DataFrame({
    "tau_QA": thresholds[:, 0],
    "epsilon_L": thresholds[:, 1],
    "Delta_QA": objectives[:, 0],
    "Delta_L": objectives[:, 1],
    "Delta_F": objectives[:, 2],
})

print(pareto_df.sort_values(["Delta_QA", "Delta_L"]).head())


# plt.figure(figsize=(6,5))
# plt.scatter(objectives[:,0], objectives[:,1], c=thresholds[:,1], s=60)
# plt.xlabel("ΔQA")
# plt.ylabel("ΔL")
# plt.colorbar(label="Latency Threshold (ϵL)")
# plt.title("Pareto Front: QA vs Latency")
# plt.show()


plt.figure(figsize=(8,6))

plt.scatter(qa, lat, s=70)

for i in range(len(qa)):
    plt.annotate(
        f"({tau[i]:.0f}, {eps[i]:.0f})",
        (qa[i], lat[i]),
        fontsize=8,
        xytext=(4,4),
        textcoords="offset points"
    )

plt.xlabel("QA degradation ΔQA (%)")
plt.ylabel("Latency deviation ΔL (%)")
plt.title("Pareto Frontier of Predicate Thresholds")

plt.grid(True)
plt.show()
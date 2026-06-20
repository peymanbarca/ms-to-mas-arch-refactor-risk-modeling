import json
import os
from collections import defaultdict


pwd = os.getcwd()


all_results = []

for ranking in ['Ranked', 'Reverse_Ranked', 'Random', 'Complexity_Based', 'Dependency_Based']:

    with open(pwd + '/results/final_results_' + ranking + '.json', 'r', encoding='utf-8') as f:  # Ranked, Reverse_Ranked, Random, Complexity_Based, Dependency_Based
        data = json.load(f)
        all_results.extend(data)
        
print(len(all_results))


all_results_runtime = []

for item in all_results:
    if item['LLM'] == 'llama3.2:3b':  # "qwen3:14b", 'llama3.2:3b'
        if item['temperature'] == "0.0": # 0.0, 0.8
             if item["concurrency"] == "5": # 5, 25
                all_results_runtime.append(item)

print(len(all_results_runtime))



def composite_score(item):
    return (
        item["sigma_delta_l"]
        + item["sigma_delta_qa"] * 100
        + item["sigma_delta_f"] * 100
    )
    
TOP_N = 5

sorted_items = sorted(
    all_results_runtime,
    key=composite_score
)

print("\n=== TOP 5 BEST EXPERIMENTS ===")
for i, item in enumerate(sorted_items[:TOP_N], 1):
    print(
        f"{i}. score={composite_score(item):.4f}, "
        f"LLM={item['LLM']}, "
        f"T={item['temperature']}, "
        f"U={item['concurrency']}, "
        f"MO={item['migration_order']}, "
        f"PM={item['predicate_mode']}, "
        f"GM={item['governance_mode']}, "
        f"TPOP={item['TPOP_enabled']}, "
        f"sigma_delta_l={item['sigma_delta_l']}, "
        f"sigma_delta_qa={item['sigma_delta_qa']}, "
        f"sigma_delta_f={item['sigma_delta_f']}, "
        f"total_rollback={item['total_rollback']}, "
        f"gov_requirement={item['gov_requirement']}, "
        f"f1_score={item['f1_score']}"
    )
    
print("\n=== MIDDLE 5 EXPERIMENTS ===")
l = len(sorted_items)
for i, item in enumerate(reversed(sorted_items[int(l/2)-TOP_N:int(l/2)]), 1):
    print(
        f"{i}. score={composite_score(item):.4f}, "
        f"LLM={item['LLM']}, "
        f"T={item['temperature']}, "
        f"U={item['concurrency']}, "
        f"MO={item['migration_order']}, "
        f"PM={item['predicate_mode']}, "
        f"GM={item['governance_mode']}, "
        f"TPOP={item['TPOP_enabled']}, "
        f"sigma_delta_l={item['sigma_delta_l']}, "
        f"sigma_delta_qa={item['sigma_delta_qa']}, "
        f"sigma_delta_f={item['sigma_delta_f']}, "
        f"total_rollback={item['total_rollback']}, "
        f"gov_requirement={item['gov_requirement']}, "
        f"f1_score={item['f1_score']}"
    )

print("\n=== TOP 5 WORST EXPERIMENTS ===")
for i, item in enumerate(reversed(sorted_items[-TOP_N:]), 1):
    print(
        f"{i}. score={composite_score(item):.4f}, "
        f"LLM={item['LLM']}, "
        f"T={item['temperature']}, "
        f"U={item['concurrency']}, "
        f"MO={item['migration_order']}, "
        f"PM={item['predicate_mode']}, "
        f"GM={item['governance_mode']}, "
        f"TPOP={item['TPOP_enabled']}, "
        f"sigma_delta_l={item['sigma_delta_l']}, "
        f"sigma_delta_qa={item['sigma_delta_qa']}, "
        f"sigma_delta_f={item['sigma_delta_f']}, "
        f"total_rollback={item['total_rollback']}, "
        f"gov_requirement={item['gov_requirement']}, "
        f"f1_score={item['f1_score']}"
    )

# print("\n\n Group data by different ranking, predicate, governance \n\n")


# groups = defaultdict(lambda: {
#     "sigma_delta_l": 0.0,
#     "sigma_delta_qa": 0.0,
#     "sigma_delta_f": 0.0,
#     "total_rollback": 0,
# })

# for item in all_results_runtime:
#     key = (
#         item["migration_order"],
#         item["predicate_mode"],
#         item["governance_mode"],
#         item["TPOP_enabled"]
#     )

#     groups[key]["sigma_delta_l"] += item["sigma_delta_l"]
#     groups[key]["sigma_delta_qa"] += item["sigma_delta_qa"]
#     groups[key]["sigma_delta_f"] += item["sigma_delta_f"]
#     groups[key]["total_rollback"] += item["total_rollback"]

# # Sort by your composite score
# sorted_groups = sorted(
#     groups.items(),
#     key=lambda x:
#         x[1]["sigma_delta_l"]
#         + x[1]["sigma_delta_qa"] * 100
#         + x[1]["sigma_delta_f"] * 100
#         + x[1]["total_rollback"],
#     reverse=False
# )


# for (migration_order, predicate_mode, governance_mode, tpop_enabled), metrics in sorted_groups:

#     composite_score = (
#         metrics["sigma_delta_l"]
#         + metrics["sigma_delta_qa"] * 100
#         + metrics["sigma_delta_f"] * 100
#         + metrics["total_rollback"]
#     )

#     print(
#         f"migration_order={migration_order}, "
#         f"predicate_mode={predicate_mode}, "
#         f"governance_mode={governance_mode}, "
#         f"TPOP_enabled={tpop_enabled}, "
#         f"sigma_delta_l={metrics['sigma_delta_l']:.4f}, "
#         f"sigma_delta_qa={metrics['sigma_delta_qa']:.4f}, "
#         f"sigma_delta_f={metrics['sigma_delta_f']:.4f}, "
#         f"total_rollback={metrics['total_rollback']}, "
#         f"composite_score={composite_score:.4f}"
#     )
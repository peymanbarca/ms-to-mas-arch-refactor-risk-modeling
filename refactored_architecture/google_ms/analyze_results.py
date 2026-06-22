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


all_results_runtime = all_results
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


def total_best_avg_compare():

    best_item = min(all_results_runtime, key=composite_score)

    others = [x for x in all_results_runtime if x is not best_item]

    avg_sigma_l = sum(x["sigma_delta_l"] for x in others) / len(others)
    avg_sigma_qa = sum(x["sigma_delta_qa"] for x in others) / len(others)
    avg_sigma_f = sum(x["sigma_delta_f"] for x in others) / len(others)
    avg_rollback = sum(x["total_rollback"] for x in others) / len(others)
    
    
    gain_sigma_l = 100 * (avg_sigma_l - best_item["sigma_delta_l"]) / avg_sigma_l
    gain_sigma_qa = 100 * (avg_sigma_qa - best_item["sigma_delta_qa"]) / avg_sigma_qa
    gain_sigma_f = 100 * (avg_sigma_f - best_item["sigma_delta_f"]) / avg_sigma_f
    if avg_rollback > 0:
        gain_rollback = int((avg_rollback - best_item["total_rollback"]) / avg_rollback)
    else:
        gain_rollback = 0
        
    print("Best configuration:")
    print(
        f"MO={best_item['migration_order']}, "
        f"PM={best_item['predicate_mode']}, "
        f"GM={best_item['governance_mode']}, "
        f"TPOP={best_item['TPOP_enabled']}, "
        f"sigma_delta_l={best_item['sigma_delta_l']}, "
        f"sigma_delta_qa={best_item['sigma_delta_qa']}, "
        f"sigma_delta_f={best_item['sigma_delta_f']}, "
        f"total_rollback={best_item['total_rollback']}, "
    )
    
    print(len(others), f"avg_rollback = {avg_rollback}, avg_qa={avg_sigma_qa}")

    print("\nImprovement over average of all other configurations:")
    print(f"σΔL improvement:  {gain_sigma_l:.2f}%")
    print(f"σΔQA improvement: {gain_sigma_qa:.2f}%")
    print(f"σΔF improvement:  {gain_sigma_f:.2f}%")
    print(f"Rollback reduction: {gain_rollback:.2f}%")


def total_best_avg_same_baseline_compare():
    
    best_item = min(all_results_runtime, key=composite_score)

    others = [x for x in all_results_runtime if x is not best_item \
              and x['migration_order'] != 'Ranked' \
              and x['predicate_mode'] == best_item['predicate_mode'] \
              and x['governance_mode'] == best_item['governance_mode']
              ]

    avg_sigma_l = sum(x["sigma_delta_l"] for x in others) / len(others)
    avg_sigma_qa = sum(x["sigma_delta_qa"] for x in others) / len(others)
    avg_sigma_f = sum(x["sigma_delta_f"] for x in others) / len(others)
    avg_rollback = sum(x["total_rollback"] for x in others) / len(others)
    
    
    gain_sigma_l = 100 * (avg_sigma_l - best_item["sigma_delta_l"]) / avg_sigma_l
    gain_sigma_qa = 100 * (avg_sigma_qa - best_item["sigma_delta_qa"]) / avg_sigma_qa
    gain_sigma_f = 100 * (avg_sigma_f - best_item["sigma_delta_f"]) / avg_sigma_f
    if avg_rollback > 0:
        gain_rollback = int((avg_rollback - best_item["total_rollback"]) / avg_rollback)
    else:
        gain_rollback = 0

    print("Best configuration:")
    print(
        f"MO={best_item['migration_order']}, "
        f"PM={best_item['predicate_mode']}, "
        f"GM={best_item['governance_mode']}, "
        f"TPOP={best_item['TPOP_enabled']}, "
        f"sigma_delta_l={best_item['sigma_delta_l']}, "
        f"sigma_delta_qa={best_item['sigma_delta_qa']}, "
        f"sigma_delta_f={best_item['sigma_delta_f']}, "
        f"total_rollback={best_item['total_rollback']}, "
    )
    
    print(len(others), f"avg_rollback = {avg_rollback}, avg_qa={avg_sigma_qa}")

    print("\nImprovement over average of all other configurations:")
    print(f"σΔL improvement:  {gain_sigma_l:.2f}%")
    print(f"σΔQA improvement: {gain_sigma_qa:.2f}%")
    print(f"σΔF improvement:  {gain_sigma_f:.2f}%")
    print(f"Rollback reduction: {gain_rollback:.2f}%")


def most_representative_results_for_each_runtime_cond():
    
    for LLM in ["llama3.2:3b", "qwen3:14b"]:
        for T in [0, 0.8]:
            for C in [5, 25]:
                all_results_runtime = []

                for item in all_results:
                    if item['LLM'] == LLM:
                        if item['temperature'] == str(T):
                            if item["concurrency"] == str(C):
                                all_results_runtime.append(item)
                print('===============================')
                print(f'LLM: {LLM}, T: {T}, C: {C}')
                print('total: ', len(all_results_runtime), '\n')
                
                best_item = min(all_results_runtime, key=composite_score)
                worst_item = max(all_results_runtime, key=composite_score)
                
                all_results_runtime_random = [x for x in all_results_runtime if x['migration_order'] == 'Random']
                print('total Random: ', len(all_results_runtime_random), '\n')
                best_random_item = min(all_results_runtime_random, key=composite_score)

                all_results_runtime_static = [x for x in all_results_runtime 
                                              if x['migration_order'] in ['Complexity_Based', 'Dependency_Based']
                                                or (x['migration_order'] == 'Ranked' and x['TPOP_enabled'] == "False")
                                              ]
                print('total Static: ', len(all_results_runtime_static), '\n')
                best_static_item = min(all_results_runtime_static, key=composite_score)
                

                
                print(f"\nBest Item: "
                    f"MO={best_item['migration_order']}, "
                    f"PM={best_item['predicate_mode']}, "
                    f"GM={best_item['governance_mode']}, "
                    f"TPOP={best_item['TPOP_enabled']}, "
                    f"sigma_delta_l={best_item['sigma_delta_l']}, "
                    f"sigma_delta_qa={best_item['sigma_delta_qa']}, "
                    f"sigma_delta_f={best_item['sigma_delta_f']}, "
                    f"total_rollback={best_item['total_rollback']}, "
                    f"gov_requirement={best_item['gov_requirement']}, "
                    f"f1_score={best_item['f1_score']}")

                print(f"\nWorst Item: "
                    f"MO={worst_item['migration_order']}, "
                    f"PM={worst_item['predicate_mode']}, "
                    f"GM={worst_item['governance_mode']}, "
                    f"TPOP={worst_item['TPOP_enabled']}, "
                    f"sigma_delta_l={worst_item['sigma_delta_l']}, "
                    f"sigma_delta_qa={worst_item['sigma_delta_qa']}, "
                    f"sigma_delta_f={worst_item['sigma_delta_f']}, "
                    f"total_rollback={worst_item['total_rollback']}, "
                    f"gov_requirement={worst_item['gov_requirement']}, "
                    f"f1_score={worst_item['f1_score']}")
                
                print(f"\nBest Random Item: "
                    f"MO={best_random_item['migration_order']}, "
                    f"PM={best_random_item['predicate_mode']}, "
                    f"GM={best_random_item['governance_mode']}, "
                    f"TPOP={best_random_item['TPOP_enabled']}, "
                    f"sigma_delta_l={best_random_item['sigma_delta_l']}, "
                    f"sigma_delta_qa={best_random_item['sigma_delta_qa']}, "
                    f"sigma_delta_f={best_random_item['sigma_delta_f']}, "
                    f"total_rollback={best_random_item['total_rollback']}, "
                    f"gov_requirement={best_random_item['gov_requirement']}, "
                    f"f1_score={best_random_item['f1_score']}")


                print(f"\nBest Static Item: "
                    f"MO={best_static_item['migration_order']}, "
                    f"PM={best_static_item['predicate_mode']}, "
                    f"GM={best_static_item['governance_mode']}, "
                    f"TPOP={best_static_item['TPOP_enabled']}, "
                    f"sigma_delta_l={best_static_item['sigma_delta_l']}, "
                    f"sigma_delta_qa={best_static_item['sigma_delta_qa']}, "
                    f"sigma_delta_f={best_static_item['sigma_delta_f']}, "
                    f"total_rollback={best_static_item['total_rollback']}, "
                    f"gov_requirement={best_static_item['gov_requirement']}, "
                    f"f1_score={best_static_item['f1_score']}")
                
                
                
def top_best_last_mid_analysis():
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



def group_analysis_by_ranking_predicate_gov():
    print("\n\n Group data by different ranking, predicate, governance \n\n")


    groups = defaultdict(lambda: {
        "sigma_delta_l": 0.0,
        "sigma_delta_qa": 0.0,
        "sigma_delta_f": 0.0,
        "total_rollback": 0,
    })

    for item in all_results_runtime:
        key = (
            item["migration_order"],
            item["predicate_mode"],
            item["governance_mode"],
            item["TPOP_enabled"]
        )

        groups[key]["sigma_delta_l"] += item["sigma_delta_l"]
        groups[key]["sigma_delta_qa"] += item["sigma_delta_qa"]
        groups[key]["sigma_delta_f"] += item["sigma_delta_f"]
        groups[key]["total_rollback"] += item["total_rollback"]

    # Sort by your composite score
    sorted_groups = sorted(
        groups.items(),
        key=lambda x:
            x[1]["sigma_delta_l"]
            + x[1]["sigma_delta_qa"] * 100
            + x[1]["sigma_delta_f"] * 100
            + x[1]["total_rollback"],
        reverse=False
    )


    for (migration_order, predicate_mode, governance_mode, tpop_enabled), metrics in sorted_groups:

        composite_score = (
            metrics["sigma_delta_l"]
            + metrics["sigma_delta_qa"] * 100
            + metrics["sigma_delta_f"] * 100
            + metrics["total_rollback"]
        )

        print(
            f"migration_order={migration_order}, "
            f"predicate_mode={predicate_mode}, "
            f"governance_mode={governance_mode}, "
            f"TPOP_enabled={tpop_enabled}, "
            f"sigma_delta_l={metrics['sigma_delta_l']:.4f}, "
            f"sigma_delta_qa={metrics['sigma_delta_qa']:.4f}, "
            f"sigma_delta_f={metrics['sigma_delta_f']:.4f}, "
            f"total_rollback={metrics['total_rollback']}, "
            f"composite_score={composite_score:.4f}"
        )
        
        
if __name__ == '__main__':
    most_representative_results_for_each_runtime_cond()
import json
import os





def analyze_results(results_file):
    with open(results_file, 'r') as f:
        step_results = f.read().split('------------')
        llm = results_file.split('LLM_')[1].split('_T_')[0]
        T = results_file.split('_T_')[1].split('_U_')[0]
        U = results_file.split('_U_')[1].split('_MO_')[0]
        MO = results_file.split('_MO_')[1].split('_PRED_')[0]
        PRED = results_file.split('_PRED_')[1].split('_GM_')[0]
        GM = results_file.split('_GM_')[1].split('_TPOP_')[0]
        TPOP = results_file.split('_TPOP_')[1].split('.json')[0]
        total_accepted = 0
        sigma_delta_qa = 0
        sigma_delta_l = 0
        sigma_delta_f = 0
        overrides_steps = 0
        steps_needed_override = 0
        interrupted_steps = 0
        total_predicate_true_accepted = 0
        total_predicate_true_rejected = 0
        total_predicate_false_accepted = 0
        total_predicate_false_rejected = 0
        cnt = 0
        for step in step_results: 
            try:
                step = json.loads(step.strip())
                decision_type = step.get('decision_type', 'N/A')
                is_accepted = step.get('is_accepted', 'N/A')
                target_service = step.get('target_service', 'N/A')
                delta_qa = float(step['evidence_summary']['metrics']['quality_assurance']['actual']) - 0 
                delta_l = float(str(step['evidence_summary']['metrics']['latency_p95']['actual']).replace('s','')) - 1 
                delta_f = float(step['evidence_summary']['metrics']['failure_rate']['actual']) - 0 
                total_accepted += 1 if is_accepted else 0
                sigma_delta_qa += delta_qa
                sigma_delta_l += delta_l
                sigma_delta_f += delta_f
                if decision_type.__contains__('override'):
                    overrides_steps += 1
                if decision_type.__contains__('interruption'):
                    interrupted_steps += 1
                if decision_type.__contains__('false_rejection_override') or decision_type.__contains__('but_false_rejection'):
                    total_predicate_false_rejected += 1
                    steps_needed_override += 1
                if decision_type.__contains__('false_acceptance_override_') or decision_type.__contains__('but_false_acceptance'):
                    total_predicate_false_accepted += 1
                    steps_needed_override += 1
                if decision_type.__contains__('true_accepted_by_predicate'):
                    total_predicate_true_accepted += 1
                if decision_type.__contains__('true_rejected_by_predicate'):
                    total_predicate_true_rejected += 1
                cnt +=1

            except json.JSONDecodeError:
                continue
        results = {
            'LLM': llm,
            'temperature': T,
            'concurrency': U,
            'migration_order': MO,
            'predicate_mode': PRED,
            'governance_mode': GM,
            'TPOP_enabled': TPOP,
            'total_steps': cnt,
            'total_accepted': total_accepted,
            'total_rollback': cnt - total_accepted,
            'sigma_delta_qa': sigma_delta_qa,
            'sigma_delta_l': sigma_delta_l,
            'sigma_delta_f': sigma_delta_f,
            'overrides_steps': overrides_steps,
            'steps_needed_override': steps_needed_override,
            'gov_requirement': (overrides_steps + steps_needed_override) / cnt * 100 if cnt > 0 else 0,
            'total_predicate_true_accepted': total_predicate_true_accepted,
            'total_predicate_true_rejected': total_predicate_true_rejected,
            'total_predicate_false_accepted': total_predicate_false_accepted,
            'total_predicate_false_rejected': total_predicate_false_rejected,
            'f1_score': (2 * total_predicate_true_accepted) / (2 * total_predicate_true_accepted + total_predicate_false_accepted + total_predicate_false_rejected) if (2 * total_predicate_true_accepted + total_predicate_false_accepted + total_predicate_false_rejected) > 0 else 0
        }
    return results


pwd = os.getcwd()


final_results = []
for file in os.listdir(pwd + '/results/Dependency_Based'):   # Ranked, Reverse_Ranked, Random, Complexity_Based, Dependency_Based
    if file.endswith('.json'):
        results_file = os.path.join(pwd + '/results/Dependency_Based', file)
        results = analyze_results(results_file)
        # print(json.dumps(results, indent=4))
        final_results.append(results)

print(len(final_results))
with open(pwd + '/results/final_results_' + 'Dependency_Based' + '.json', 'w') as f:
    json.dump(final_results, f, indent=4)
"""
Unified Section 4 Experiment Pipeline
- Runs Ablation Study (ranking orders x acceptance predicates x governance policies x runtime conditions)
- Runs Weight Sensitivity (Dirichlet samples + Kendall's tau) and Weight Tuning (grid search)
- Reuses ./deploy-local.sh, ./shutdown-local.sh, PostActionAdjudicator, and exp_runner_auto
- Persists all records into a SINGLE JSON file.
"""

import argparse
import itertools
import json
import os
import random
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import tqdm
from scipy.stats import kendalltau, spearmanr

from post_action_adjudication import (
    AdjudicationCriteria,
    AdjudicationMode,
    PostActionAdjudicator,
    create_execution_metrics_from_step_result,
)
from step_result import simulate_step_result

# ---------------------------------------------------------------------------
# Benchmark Configurations (Table 1 & Table 5; B1 ports/metrics from scripts)
# ---------------------------------------------------------------------------
BENCHMARK_CONFIGS: Dict[str, Dict[str, Any]] = {
    "B2": {
        "name": "RetailBen",
        "runner_module": "refactored_architecture.retailben.exp_runner_auto",
        "baseline_latency_p95": 1.0, # enter from experiment results of microservice architecture (baseline)
        "fanout": [
            ["pricing_service:8002", 0],
            ["product_catalog_service:8008", 2],
            ["inventory_service:8001", 1],
            ["shopping_cart_service:8003", 0],
            ["payment_service:8007", 1],
            ["order_service:8000", 7],
            ["subscription_service:8010", 1],
            ["procurement_service:8009", 1],
            ["shipment_service:8006", 1],
            # ["notification_service:8011", 1],
        ],
        "bc": [
            ["pricing_service:8002", 0],
            ["product_catalog_service:8008", 0.05],
            ["inventory_service:8001", 0.22],
            ["shopping_cart_service:8003", 0],
            ["payment_service:8007", 0],
            ["order_service:8000", 0.7],
            ["subscription_service:8010", 0.016],
            ["procurement_service:8009", 0],
            ["shipment_service:8006", 0],
            # ["notification_service:8011", 0],
        ],
        "c_cyc": [
            ["pricing_service:8002", 10],
            ["product_catalog_service:8008", 22],
            ["inventory_service:8001", 10],
            ["shopping_cart_service:8003", 18],
            ["payment_service:8007", 10],
            ["order_service:8000", 16],
            ["subscription_service:8010", 12],
            ["procurement_service:8009", 36],
            ["shipment_service:8006", 33],
            # ["notification_service:8011", 11],
        ],
        "c_cog": [
            ["pricing_service:8002", 8],
            ["product_catalog_service:8008", 18],
            ["inventory_service:8001", 14],
            ["shopping_cart_service:8003", 14],
            ["payment_service:8007", 6],
            ["order_service:8000", 28],
            ["subscription_service:8010", 10],
            ["procurement_service:8009", 26],
            ["shipment_service:8006", 30],
            # ["notification_service:8011", 4],
        ],
        "service_to_agent": {
            "inventory_service:8001": "inventory_agent:8001",
            "order_service:8000": "order_agent:8000",
            "payment_service:8007": "payment_agent:8007",
            "shipment_service:8006": "shipment_agent:8006",
            "shopping_cart_service:8003": "shopping_cart_agent:8003",
            "product_catalog_service:8008": "product_catalog_agent:8008",
            "pricing_service:8002": "pricing_agent:8002",
            "subscription_service:8010": "subscription_agent:8010",
            "procurement_service:8009": "procurement_agent:8009",
            # "notification_service:8011": "notification_agent:8011",
        },
        "tprop_weights": {
            "inventory_service->order_service": 1.0,
            "inventory_service->procurement_service": 1.0,
            "procurement_service->inventory_service": 1.0,
            "pricing_service->order_service": 1.0,
            "product_catalog_service->order_service": 1.0,
            "shopping_cart_service->order_service": 1.0,
            "payment_service->order_service": 1.0,
            "subscription_service->order_service": 1.0,
            "shipment_service->order_service": 1.0,
        },
    },
}

ACCEPTANCE_PREDICATE_MODES = ["Full", "Latency-Only", "Failure-Only", "QA-Only"]
GOVERNANCE_POLICIES = ["No", "Post-Audit-Selective-Only", "Post-Audit-Comprehensive"]
ORDERING_STRATEGIES = [
    "Ranked",
    "Reverse_Ranked",
    "Random",
    "Dependency_Based",
    "Complexity_Based",
]

DEFAULT_MODELS = ["llama3.2:3b", "llama3:8b"]
DEFAULT_TEMPERATURES = [0.0, 0.8]
DEFAULT_CONCURRENCIES = [20, 100]

GOVERNANCE_MODE_MAP = {
    "No": AdjudicationMode.NO_GOVERNANCE,
    "Post-Audit-Selective-Only": AdjudicationMode.SELECTIVE,
    "Post-Audit-Comprehensive": AdjudicationMode.COMPREHENSIVE,
}


# ---------------------------------------------------------------------------
# Single JSON Store Helper
# ---------------------------------------------------------------------------
class SingleJsonStore:
    """Maintains all Section 4 ablation, sensitivity, and tuning runs in one JSON file."""

    def __init__(self, output_path: Path):
        self.output_path = output_path
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.data = self._load_or_init()

    def _load_or_init(self) -> Dict[str, Any]:
        if self.output_path.exists():
            with self.output_path.open("r", encoding="utf-8") as f:
                return json.load(f)
        return {
            "metadata": {
                "created_at": datetime.now(timezone.utc).isoformat(),
                "schema_version": "1.0",
                "paper_section": "Section 4 - Controlled Evaluation of Agentification Strategies",
            },
            "ablation_runs": [],
            "weight_sensitivity_runs": [],
            "weight_tuning_runs": [],
        }

    def save(self) -> None:
        self.data["metadata"]["updated_at"] = datetime.now(timezone.utc).isoformat()
        tmp_path = self.output_path.with_suffix(".json.tmp")
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2)
        os.replace(tmp_path, self.output_path)

    def append_ablation_run(self, record: Dict[str, Any]) -> None:
        self.data["ablation_runs"].append(record)
        self.save()

    def append_weight_sensitivity_run(self, record: Dict[str, Any]) -> None:
        self.data["weight_sensitivity_runs"].append(record)
        self.save()

    def append_weight_tuning_run(self, record: Dict[str, Any]) -> None:
        self.data["weight_tuning_runs"].append(record)
        self.save()


# ---------------------------------------------------------------------------
# Ranking & Weight Helpers
# ---------------------------------------------------------------------------
def build_normalized_metrics(cfg: Dict[str, Any]) -> Tuple[List[str], Dict[str, Dict[str, float]]]:
    services = [x[0] for x in cfg["fanout"]]

    def _norm(items: List[List[Any]]) -> Dict[str, float]:
        total = sum(float(v[1]) for v in items)
        return {x[0]: (float(x[1]) / total if total > 0 else 0.0) for x in items}

    norm_map = {
        "fanout": _norm(cfg["fanout"]),
        "bc": _norm(cfg["bc"]),
        "c_cyc": _norm(cfg["c_cyc"]),
        "c_cog": _norm(cfg["c_cog"]),
        "t_prop": {s: 0.0 for s in services},
    }
    return services, norm_map


def compute_scores_for_weights(
    services: List[str],
    norm_map: Dict[str, Dict[str, float]],
    weights: Tuple[float, float, float, float, float],
) -> List[List[Any]]:
    wf, wb, wcyc, wcog, wt = weights
    scored = []
    for svc in services:
        score = (
            wf * norm_map["fanout"][svc]
            + wb * norm_map["bc"][svc]
            + wcyc * norm_map["c_cyc"][svc]
            + wcog * norm_map["c_cog"][svc]
            + wt * norm_map["t_prop"][svc]
        )
        scored.append([svc, float(score)])
    return scored


def get_ordering_candidates(
    cfg: Dict[str, Any],
    strategy: str,
    seed: int = 42,
) -> List[List[Any]]:
    services, norm_map = build_normalized_metrics(cfg)
    static_scores = compute_scores_for_weights(services, norm_map, (0.2, 0.2, 0.2, 0.2, 0.2))
    ascending = sorted(static_scores, key=lambda x: x[1], reverse=False)

    if strategy == "Ranked":
        return ascending
    if strategy == "Reverse_Ranked":
        return list(reversed(ascending))
    if strategy == "Random":
        rng = random.Random(seed)
        shuffled = ascending.copy()
        rng.shuffle(shuffled)
        return shuffled
    if strategy == "Dependency_Based":
        dep_scores = compute_scores_for_weights(services, norm_map, (0.5, 0.5, 0.0, 0.0, 0.0))
        return sorted(dep_scores, key=lambda x: x[1], reverse=False)
    if strategy == "Complexity_Based":
        comp_scores = compute_scores_for_weights(services, norm_map, (0.0, 0.0, 0.5, 0.5, 0.0))
        return sorted(comp_scores, key=lambda x: x[1], reverse=False)
    raise ValueError(f"Unsupported ordering strategy: {strategy}")


def ranking_similarity(
    services: List[str],
    baseline_ranking: List[List[Any]],
    candidate_ranking: List[List[Any]],
) -> Tuple[float, float]:
    base_names = [x[0] if isinstance(x, list) else x for x in baseline_ranking]
    cand_names = [x[0] if isinstance(x, list) else x for x in candidate_ranking]

    base_pos = {svc: i for i, svc in enumerate(base_names)}
    cand_pos = {svc: i for i, svc in enumerate(cand_names)}

    x_vals = [base_pos[s] for s in services]
    y_vals = [cand_pos[s] for s in services]

    rho, _ = spearmanr(x_vals, y_vals)
    tau, _ = kendalltau(x_vals, y_vals)
    return float(rho if rho is not None else 0.0), float(tau if tau is not None else 0.0)


def generate_dirichlet_weights(n_samples: int = 100, seed: int = 42) -> List[List[float]]:
    rng = np.random.default_rng(seed)
    samples = rng.dirichlet(alpha=np.ones(5), size=n_samples)
    return [[float(x) for x in row] for row in samples]


def generate_grid_weights(step: float = 0.2) -> List[List[float]]:
    vals = np.arange(0.0, 1.0 + step / 2.0, step)
    vectors: List[List[float]] = []
    for wf in vals:
        for wb in vals:
            for wcyc in vals:
                for wcog in vals:
                    wt = 1.0 - float(wf + wb + wcyc + wcog)
                    if -1e-9 <= wt <= 1.0 + 1e-9:
                        wt_clamped = max(0.0, min(1.0, round(wt, 6)))
                        vectors.append([
                            round(float(wf), 6),
                            round(float(wb), 6),
                            round(float(wcyc), 6),
                            round(float(wcog), 6),
                            wt_clamped,
                        ])
    return vectors


# ---------------------------------------------------------------------------
# Deployment & Step Execution Helpers (Preserving Local Scripts)
# ---------------------------------------------------------------------------
def _extract_name(item: Any) -> str:
    val = item[0] if isinstance(item, list) else item
    return str(val).split(":")[0]


def _extract_port(item: Any) -> int:
    val = item[0] if isinstance(item, list) else item
    return int(str(val).split(":")[1])


def build_deploy_args(
    services: List[Any],
    agents: List[Any],
    model: Optional[str] = None,
    temperature: Optional[float] = None,
) -> List[str]:
    svc_pairs = [f"{_extract_name(s)}:{_extract_port(s)}" for s in services]
    agent_pairs = [f"{_extract_name(a)}:{_extract_port(a)}" for a in agents]
    args = [
        f"services={','.join(svc_pairs)}",
        f"agents={','.join(agent_pairs)}",
    ]
    if model is not None:
        args.append(f"model={model}")
    if temperature is not None:
        args.append(f"temperature={temperature}")
    return args


def deploy_local(
    services: List[Any],
    agents: List[Any],
    model: Optional[str] = None,
    temperature: Optional[float] = None,
    dry_run: bool = False,
) -> None:
    cmd = ["./deploy-local.sh"] + build_deploy_args(services, agents, model, temperature)
    if dry_run:
        print(f"[DRY-RUN] {' '.join(cmd)}")
        return
    subprocess.run(cmd, check=True)


def shutdown_local(services: List[Any], agents: List[Any], dry_run: bool = False) -> None:
    cmd = ["./shutdown-local.sh"] + build_deploy_args(services, agents)
    if dry_run:
        print(f"[DRY-RUN] {' '.join(cmd)}")
        return
    subprocess.run(cmd, check=True)


def run_step_subprocess(
    cfg: Dict[str, Any],
    migration_order: str,
    step_num: int,
    predicate_mode: str,
    governance_policy: str,
    services: List[str],
    agents: List[str],
    target_service: str,
    temporal_propagation_enabled: bool,
    previous_step_acceptance_type: str,
    ranking_snapshot: Any,
    temperature: float,
    model: str,
    concurrency: int,
    adjudicator: PostActionAdjudicator,
    dry_run: bool = False,
) -> Dict[str, Any]:
    base_lat = float(cfg.get("baseline_latency_p95", 1.0))
    epsilon_l = base_lat * 1.9
    epsilon_qa = 0.0
    epsilon_f = 0.02

    if predicate_mode == "QA-Only":
        epsilon_l = -1.0
        epsilon_f = -1.0
    elif predicate_mode == "Latency-Only":
        epsilon_qa = -1.0
        epsilon_f = -1.0
    elif predicate_mode == "Failure-Only":
        epsilon_l = -1.0
        epsilon_qa = -1.0

    if dry_run:
        # parsed = {
        #     "result": True,
        #     "step_self_temporal_propagation": 0.0,
        #     "details": {
        #         "qa_inconsistency_rate": 0.0,
        #         "p95_latency": base_lat * 1.1,
        #         "failure_rate": 0.005,
        #         "reference_rejected": False,
        #         "otl_interrupted": False,
        #         "itl_override_type": None,
        #     },
        # }
        parsed = simulate_step_result(
            cfg=cfg,
            migration_order=migration_order,
            step_num=step_num,
            predicate_mode=predicate_mode,
            governance_policy=governance_policy,
            services=services,
            agents=agents,
            target_service=target_service,
            temporal_propagation_enabled=temporal_propagation_enabled,
            temperature=temperature,
            model=model,
            concurrency=concurrency,
        )
    else:
        proc = subprocess.run(
            [
                "python3",
                "-m",
                cfg["runner_module"],
                migration_order,
                predicate_mode,
                str(step_num),
                ",".join(services),
                ",".join(agents),
                str(epsilon_l),
                str(epsilon_qa),
                str(epsilon_f),
                str(governance_policy),
                str(target_service),
                str(previous_step_acceptance_type),
                str(temporal_propagation_enabled),
                str(ranking_snapshot),
                str(temperature),
                str(model),
                str(concurrency),
            ],
            cwd="../..",
            capture_output=True,
            text=True,
            check=True,
        )
        if not proc.stdout.strip():
            raise RuntimeError(f"Empty output from {cfg['runner_module']} at step {step_num}")
        parsed = json.loads(proc.stdout.strip())

    upstream_effect = any(
        dep.split("->")[1] == target_service
        for dep in cfg.get("tprop_weights", {})
    )
    step_tprop = float(parsed.get("step_self_temporal_propagation", 0.0)) if upstream_effect else 0.0
    parsed["step_self_temporal_propagation"] = step_tprop

    adj_mode = GOVERNANCE_MODE_MAP.get(governance_policy, AdjudicationMode.NO_GOVERNANCE)
    metrics = create_execution_metrics_from_step_result(
        step_result=parsed,
        step_number=step_num,
        target_service=target_service,
        total_trials=5000,
    )
    final_decision, decision_type, evidence_summary, prediction_category = adjudicator.adjudicate_step(
        metrics=metrics,
        mode=adj_mode,
        evidence_context={
            "previous_step_type": previous_step_acceptance_type,
            "temporal_propagation_enabled": temporal_propagation_enabled,
        },
    )

    details = parsed.get("details", {})
    qa_inc = float(details.get("qa_inconsistency_rate", 0.0))
    p95_lat = float(details.get("p95_latency", base_lat))
    fail_rate = float(details.get("failure_rate", 0.0))

    lat_dev_pct = max(0.0, ((p95_lat - base_lat) / base_lat) * 100.0) if base_lat > 0 else 0.0
    qa_dev_pp = max(0.0, qa_inc * 100.0)
    fail_dev_pp = max(0.0, fail_rate * 100.0)

    return {
        "step": step_num,
        "target_service": target_service,
        "services": list(services),
        "agents": list(agents),
        "is_accepted": bool(final_decision),
        "decision_type": decision_type,
        "prediction_category": prediction_category,
        "evidence_summary": evidence_summary,
        "step_self_temporal_propagation": step_tprop,
        "metrics": {
            "qa_inconsistency_rate": qa_inc,
            "p95_latency": p95_lat,
            "failure_rate": fail_rate,
            "qa_deviation_pp": qa_dev_pp,
            "latency_deviation_pct": lat_dev_pct,
            "failure_deviation_pp": fail_dev_pp,
            "reference_rejected": bool(details.get("reference_rejected", qa_inc > 0.0 or fail_rate > 0.025)),
            "otl_interrupted": bool(details.get("otl_interrupted", "INTERRUPT" in str(decision_type).upper())),
            "itl_override_type": details.get("itl_override_type"),
        },
    }


def execute_evolution_trajectory(
    benchmark_id: str,
    cfg: Dict[str, Any],
    order_label: str,
    initial_ranking: List[List[Any]],
    predicate_mode: str,
    governance_policy: str,
    model: str,
    temperature: float,
    concurrency: int,
    tprop_enabled: bool,
    adjudicator: PostActionAdjudicator,
    dry_run: bool = False,
) -> Dict[str, Any]:
    if not dry_run:
        subprocess.run("rm -f *.log", shell=True, cwd=".", check=True)

    current_services = [x[0] for x in initial_ranking]
    current_agents: List[str] = []
    ranking_state = [list(x) for x in initial_ranking]
    prev_acceptance_types = ["N/A"]
    observed_tprops: List[float] = []
    step_records: List[Dict[str, Any]] = []

    rollbacks = 0
    cum_qa_pp = 0.0
    cum_lat_pct = 0.0
    cum_fail_pp = 0.0
    otl_interrupts = 0
    itl_overrides = 0

    try:
        for step_idx in range(1, len(ranking_state) + 1):
            svc_entry = ranking_state[step_idx - 1]
            svc = svc_entry[0]
            agent = cfg["service_to_agent"][svc]
            target_svc = svc.split(":")[0]

            candidate_services = [s for s in current_services if s != svc]
            candidate_agents = current_agents + [agent]

            deploy_local(candidate_services, candidate_agents, model=model, temperature=temperature, dry_run=dry_run)

            step_res = run_step_subprocess(
                cfg=cfg,
                migration_order=order_label,
                step_num=step_idx,
                predicate_mode=predicate_mode,
                governance_policy=governance_policy,
                services=candidate_services,
                agents=candidate_agents,
                target_service=target_svc,
                temporal_propagation_enabled=tprop_enabled,
                previous_step_acceptance_type=prev_acceptance_types[-1],
                ranking_snapshot=ranking_state,
                temperature=temperature,
                model=model,
                concurrency=concurrency,
                adjudicator=adjudicator,
                dry_run=dry_run,
            )
            step_records.append(step_res)
            prev_acceptance_types.append(step_res["decision_type"])

            m = step_res["metrics"]
            cum_qa_pp += m["qa_deviation_pp"]
            cum_lat_pct += m["latency_deviation_pct"]
            cum_fail_pp += m["failure_deviation_pp"]
            if m["otl_interrupted"]:
                otl_interrupts += 1
            if m["itl_override_type"]:
                itl_overrides += 1

            accepted = step_res["is_accepted"]
            step_tprop = step_res["step_self_temporal_propagation"]

            if accepted:
                current_services = candidate_services
                current_agents = candidate_agents

                if tprop_enabled and step_tprop > 0.0:
                    observed_tprops.append(step_tprop)
                    norm_tprop = step_tprop / max(observed_tprops)
                    for dep, weight in cfg.get("tprop_weights", {}).items():
                        downstream, upstream = dep.split("->")
                        if target_svc == downstream:
                            for i, (s_name, s_score) in enumerate(ranking_state):
                                if s_name.split(":")[0] == upstream:
                                    ranking_state[i] = [s_name, float(s_score) + norm_tprop * float(weight)]
                    ranking_state.sort(key=lambda x: x[1], reverse=False)
            else:
                rollbacks += 1

        shutdown_local(current_services, current_agents, dry_run=dry_run)
    except Exception:
        shutdown_local(current_services, current_agents, dry_run=dry_run)
        raise

    n_steps = max(1, len(step_records))
    gov_int_pct = 100.0 * (otl_interrupts + itl_overrides) / n_steps

    return {
        "benchmark": benchmark_id,
        "order_strategy": order_label,
        "predicate_mode": predicate_mode,
        "governance_policy": governance_policy,
        "model": model,
        "temperature": temperature,
        "concurrency": concurrency,
        "temporal_propagation_enabled": tprop_enabled,
        "summary": {
            "cum_qa_deviation_pp": cum_qa_pp,
            "cum_latency_deviation_pct": cum_lat_pct,
            "cum_failure_deviation_pp": cum_fail_pp,
            "rollbacks": rollbacks,
            "otl_interrupts": otl_interrupts,
            "itl_overrides": itl_overrides,
            "gov_intervention_pct": gov_int_pct,
            "final_agent_count": len(current_agents),
            "total_services": len(initial_ranking),
            "final_coverage": len(current_agents) / max(1, len(initial_ranking)),
        },
        "steps": step_records,
    }


# ---------------------------------------------------------------------------
# Main Pipeline Orchestrator
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="Unified Section 4 Experiment Pipeline")
    parser.add_argument("--benchmark", type=str, default="B1", choices=["B1", "B2", "B3"])
    parser.add_argument("--output", type=str, default="results/ablation_study_weight_analysis_pipeline_results.json")
    parser.add_argument("--orders", nargs="+", default=ORDERING_STRATEGIES)
    parser.add_argument("--predicates", nargs="+", default=ACCEPTANCE_PREDICATE_MODES)
    parser.add_argument("--governance", nargs="+", default=GOVERNANCE_POLICIES)
    parser.add_argument("--models", nargs="+", default=DEFAULT_MODELS)
    parser.add_argument("--temps", nargs="+", type=float, default=DEFAULT_TEMPERATURES)
    parser.add_argument("--concurrencies", nargs="+", type=int, default=DEFAULT_CONCURRENCIES)
    parser.add_argument("--dirichlet-samples", type=int, default=100)
    parser.add_argument("--grid-step", type=float, default=0.2)
    parser.add_argument("--skip-ablation", action="store_true")
    parser.add_argument("--skip-weight-study", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    cfg = BENCHMARK_CONFIGS[args.benchmark]
    if not cfg["fanout"] and not args.dry_run:
        raise ValueError(f"Benchmark {args.benchmark} service metrics are not yet populated in BENCHMARK_CONFIGS.")

    store = SingleJsonStore(Path(args.output))
    adjudicator = PostActionAdjudicator(
        AdjudicationCriteria(
            delta_qa=0.0,
            delta_latency=0.1,
            delta_failure=0.005,
            delta_temporal_prop=0.1,
            grace_window_fraction=0.3333,
        )
    )

    # 1) Ablation Study
    if not args.skip_ablation:
        ablation_combos = list(
            itertools.product(args.orders, args.predicates, args.governance, args.models, args.temps, args.concurrencies)
        )
        for order_strat, pred_mode, gov_pol, model, temp, conc in tqdm.tqdm(ablation_combos, desc="Ablation"):
            tprop_flag = order_strat == "Ranked"
            init_ranking = get_ordering_candidates(cfg, order_strat)
            run_rec = execute_evolution_trajectory(
                benchmark_id=args.benchmark,
                cfg=cfg,
                order_label=order_strat,
                initial_ranking=init_ranking,
                predicate_mode=pred_mode,
                governance_policy=gov_pol,
                model=model,
                temperature=temp,
                concurrency=conc,
                tprop_enabled=tprop_flag,
                adjudicator=adjudicator,
                dry_run=args.dry_run,
            )
            store.append_ablation_run(run_rec)

    # 2) Weight Sensitivity & Tuning (challenging condition: small model, T=0.8, u=100)
    if not args.skip_weight_study and cfg["fanout"]:
        services, norm_map = build_normalized_metrics(cfg)
        equal_weights = (0.2, 0.2, 0.2, 0.2, 0.2)
        equal_ranking = sorted(
            compute_scores_for_weights(services, norm_map, equal_weights),
            key=lambda x: x[1],
            reverse=False,
        )

        # Dirichlet sensitivity (100 samples)
        dirichlet_vecs = generate_dirichlet_weights(n_samples=args.dirichlet_samples)
        for idx, w_vec in enumerate(tqdm.tqdm(dirichlet_vecs, desc="Dirichlet Sensitivity")):
            cand_ranking = sorted(
                compute_scores_for_weights(services, norm_map, tuple(w_vec)),
                key=lambda x: x[1],
                reverse=False,
            )
            rho, tau = ranking_similarity(services, equal_ranking, cand_ranking)
            traj = execute_evolution_trajectory(
                benchmark_id=args.benchmark,
                cfg=cfg,
                order_label=f"Dirichlet_{idx}",
                initial_ranking=cand_ranking,
                predicate_mode="Full",
                governance_policy="Post-Audit-Comprehensive",
                model="llama3.2:3b",
                temperature=0.8,
                concurrency=100,
                tprop_enabled=True,
                adjudicator=adjudicator,
                dry_run=args.dry_run,
            )
            traj["weights"] = w_vec
            traj["spearman_rho"] = rho
            traj["kendall_tau"] = tau
            store.append_weight_sensitivity_run(traj)

        # Grid search tuning
        grid_vecs = generate_grid_weights(step=args.grid_step)
        for idx, w_vec in enumerate(tqdm.tqdm(grid_vecs, desc="Weight Grid Tuning")):
            cand_ranking = sorted(
                compute_scores_for_weights(services, norm_map, tuple(w_vec)),
                key=lambda x: x[1],
                reverse=False,
            )
            rho, tau = ranking_similarity(services, equal_ranking, cand_ranking)
            traj = execute_evolution_trajectory(
                benchmark_id=args.benchmark,
                cfg=cfg,
                order_label=f"Grid_{idx}",
                initial_ranking=cand_ranking,
                predicate_mode="Full",
                governance_policy="Post-Audit-Comprehensive",
                model="llama3.2:3b",
                temperature=0.8,
                concurrency=100,
                tprop_enabled=True,
                adjudicator=adjudicator,
                dry_run=args.dry_run,
            )
            traj["weights"] = w_vec
            traj["spearman_rho"] = rho
            traj["kendall_tau"] = tau
            store.append_weight_tuning_run(traj)

    print(f"\nSaved unified Section 4 results to: {store.output_path}")


if __name__ == "__main__":
    main()
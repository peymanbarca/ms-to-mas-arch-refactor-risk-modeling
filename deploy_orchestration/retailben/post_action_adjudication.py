"""
Post-Action Adjudication (HITL) Module for Migration Step Governance

This module implements human-in-the-loop governance for migration steps based on
post-execution evidence review. Two adjudication strategies are supported:

1. Selective (Exception-based) Adjudication: Override false rejections when evidence
   shows the rejection was due to transient violations without upstream harm.

2. Comprehensive Adjudication: Review all automated decisions and override both
   false rejections and false acceptances based on holistic safety evaluation.
"""

import json
from dataclasses import dataclass
import random
from typing import Tuple, Dict, Any, List
from enum import Enum
import logging

logger = logging.getLogger("migration-experiment-runner-post-action-adjudication")
logging.basicConfig(
    filename='./logs/experiment.log',
    level=logging.INFO,  # Log all messages with severity DEBUG or higher
    format='%(asctime)s - %(levelname)s - %(message)s'  # Define the message format
)


class AdjudicationMode(Enum):
    """Enumeration of adjudication strategies."""
    SELECTIVE = "Post-Audit-Selective-Only"
    COMPREHENSIVE = "Post-Audit-Comprehensive"
    NO_GOVERNANCE = "No"
    HITL = "Human-in-the-Loop"


class AdjudicationDecision(Enum):
    """Final adjudication decision."""
    ACCEPT = "Accept"
    REJECT = "Reject"
    INCONCLUSIVE = "Inconclusive"


@dataclass
class ExecutionMetrics:
    """Container for post-execution metrics extracted from step results."""
    
    # Core Quality Metrics
    qa_rate: float  # Quality Assurance inconsistency rate (lower is better)
    latency_p95: float  # 95th percentile latency in seconds
    failure_rate: float  # Proportion of failed requests
    
    # Predicate Thresholds
    threshold_qa: float  # τ_QA - QA inconsistency threshold
    threshold_latency: float  # ε_L - Latency threshold
    threshold_failure: float  # ε_SLO - Failure rate threshold
    
    # Risk Propagation
    temporal_propagation: float  # T_Prop(s_i) - Temporal propagation to upstream services
    
    # Violation Duration
    violation_duration: float  # Dur(viol_i) - Duration of longest violation (fraction of trials)
    
    # Predicate Results
    qa_predicate_failed: bool
    latency_predicate_failed: bool
    failure_predicate_failed: bool
    predicate_overall_result: bool  # True if accepted, False if rejected
    
    # Context
    step_number: int
    target_service: str
    total_trials: int = 10
    
    log_telemetry_file: str = ""  # Path to the log telemetry file for this step


@dataclass
class AdjudicationCriteria:
    """Tolerance bands and thresholds for adjudication decisions."""
    
    # Tolerance bands for selective adjudication (false rejection recovery)
    delta_qa: float = 0.05  # QA tolerance band
    delta_latency: float = 0.1  # Latency tolerance band (seconds)
    delta_failure: float = 0.02  # Failure rate tolerance band
    delta_temporal_prop: float = 0.1  # Temporal propagation tolerance
    
    # Grace window for violation duration (fraction of trials)
    grace_window_fraction: float = 0.3  # 30% of total trials
    
    # Comprehensive adjudication thresholds
    max_safe_violations_beyond_grace: float = 0.1  # 10% violations beyond grace window


class PostActionAdjudicator:
    """
    Implements post-action adjudication logic for migration steps.
    
    Evaluates automated predicate decisions against actual execution evidence
    to identify and correct false rejections and false acceptances.
    """
    
    def __init__(self, criteria: AdjudicationCriteria = None):
        """
        Initialize the adjudicator.
        
        Args:
            criteria: Adjudication thresholds and tolerance bands
        """
        self.criteria = criteria or AdjudicationCriteria()
    
    def adjudicate_step(
        self,
        metrics: ExecutionMetrics,
        mode: AdjudicationMode,
        evidence_context: Dict[str, Any] = None
    ) -> Tuple[bool, str, Dict[str, Any], str]:
        """
        Perform adjudication on a migration step based on execution metrics.
        
        Args:
            metrics: Post-execution metrics from the step
            mode: Adjudication strategy (selective or comprehensive)
            evidence_context: Optional context for evidence review
            
        Returns:
            Tuple of:
                - final_decision (bool): True to accept, False to reject
                - decision_type (str): Type of decision for audit trail
                - evidence_summary (dict): Evidence presented to human auditor
                - prediction_category (str): Category of the prediction for audit trail
        """
        
        evidence_summary = self._build_evidence_summary(metrics)
        prediction_category = None
        
        if mode == AdjudicationMode.NO_GOVERNANCE:
            # No human intervention, just evaluate to find predicate f1 score
            is_false_rejection, override_reason = self._check_false_rejection(metrics)
            is_false_acceptance, violation_reason = self._check_false_acceptance(metrics)

            decision = bool(metrics.predicate_overall_result)
            prediction_category = "auto_predicate_decision_no_governance"
            decision_type = "auto_predicate_decision_no_governance"
            if is_false_rejection:
                decision_type += "_but_false_rejection" 
            elif is_false_acceptance:
                decision_type += "_but_false_acceptance"
            else: 
                if not metrics.predicate_overall_result:
                    decision_type += "_true_rejected_by_predicate"
                else:
                    decision_type += "_true_accepted_by_predicate"
            return decision, decision_type, evidence_summary, prediction_category
        
        elif mode == AdjudicationMode.SELECTIVE:
            # Only intervene on rejections
            if not metrics.predicate_overall_result:
                # Predicate rejected - automated check for false rejection
                is_false_rejection, override_reason = self._check_false_rejection(metrics)
                
                if is_false_rejection:
                    prediction_category = "false_rejection"
                    return True, f"false_rejection_override_{override_reason}", evidence_summary, prediction_category
                else:
                    # True rejection - escalate to human confirms or overrides
                    # human_decision, decision_type = self._get_human_adjudication(
                    #     metrics=metrics,
                    #     evidence_summary=evidence_summary,
                    #     predicate_decision="REJECT",
                    #     mode=AdjudicationMode.SELECTIVE
                    # )
                    # return human_decision, decision_type, evidence_summary
                    
                    # auto reject without human intervention for selective mode
                    prediction_category = "true_rejection"
                    return False, "true_rejected_by_predicate", evidence_summary, prediction_category
            else:
                # Predicate accepted - auto-accept
                is_false_acceptance, violation_reason = self._check_false_acceptance(metrics)
                if is_false_acceptance:
                    # In selective mode, we do not override false acceptances
                    # but we can log them for monitoring and future improvements
                    prediction_category = "false_acceptance"
                    return True, f"accepted_by_predicate_auto_pass_potential_false_acceptance_{violation_reason}", evidence_summary, prediction_category
                else:
                    prediction_category = "true_acceptance"
                    return True, "true_accepted_by_predicate_auto_pass", evidence_summary, prediction_category
        
        elif mode == AdjudicationMode.COMPREHENSIVE:
            # Intervene on both rejections and acceptances
            if not metrics.predicate_overall_result:
                # Predicate rejected - automated check for false rejection
                is_false_rejection, override_reason = self._check_false_rejection(metrics)
                
                if is_false_rejection:
                    prediction_category = "false_rejection"
                    return True, f"false_rejection_override_{override_reason}", evidence_summary, prediction_category
                else:
                    # True rejection - escalate to human confirms or overrides
                    # human_decision, decision_type = self._get_human_adjudication(
                    #     metrics=metrics,
                    #     evidence_summary=evidence_summary,
                    #     predicate_decision="REJECT",
                    #     mode=AdjudicationMode.COMPREHENSIVE
                    # )

                    # auto reject without human intervention
                    prediction_category = "true_rejection"
                    return False, "true_rejected_by_predicate", evidence_summary, prediction_category
            else:
                # Predicate accepted - automated check for false acceptance
                is_false_acceptance, violation_reason = self._check_false_acceptance(metrics)
                
                if is_false_acceptance:
                    # Override the acceptance to rejection
                    # human_decision, decision_type = self._get_human_adjudication(
                    #     metrics=metrics,
                    #     evidence_summary=evidence_summary,
                    #     predicate_decision="ACCEPT",
                    #     override_warning=f"False acceptance detected: {violation_reason}",
                    #     mode=AdjudicationMode.COMPREHENSIVE
                    # )
                    # return human_decision, decision_type, evidence_summary
                    prediction_category = "false_acceptance"
                    return False, f"false_acceptance_override_{violation_reason}", evidence_summary, prediction_category
                
                else:
                    # True acceptance - human confirms
                    # human_decision, decision_type = self._get_human_adjudication(
                    #     metrics=metrics,
                    #     evidence_summary=evidence_summary,
                    #     predicate_decision="ACCEPT",
                    #     mode=AdjudicationMode.COMPREHENSIVE
                    # )
                    # return human_decision, decision_type, evidence_summary
                    prediction_category = "true_acceptance"
                    return True, "true_accepted_by_predicate_auto_pass", evidence_summary, prediction_category

        elif mode == AdjudicationMode.HITL:
            human_verdict, decision_type = self._get_human_adjudication(
                metrics=metrics,
                evidence_summary=evidence_summary,
                predicate_decision="ACCEPT" if metrics.predicate_overall_result else "REJECT",
                override_warning="You are in Human-in-the-Loop mode. Please review the evidence and make a decision."
            )
            return human_verdict, decision_type, evidence_summary, "hitl_decision"

        # Default fallback
        return bool(metrics.predicate_overall_result), "fallback_decision", evidence_summary, prediction_category

    def _check_false_rejection(self, metrics: ExecutionMetrics) -> Tuple[bool, str]:
        """
        Check if a rejection by the predicate is a false rejection.
        
        False rejection occurs when:
        - QA is acceptable (within tolerance band)
        - Latency is acceptable (within tolerance band)
        - Failure rate is acceptable (within tolerance band)
        - Temporal propagation is minimal
        - Violation duration is transient (within grace window)
        
        Criteria:
            (QA_i ≥ τ_QA - δ_QA) ∧ (L_p95_i ≤ ε_L + δ_L) ∧ (F_i ≤ ε_SLO + δ_SLO)
            ∧ T_Prop(s_i) ≤ δ_T_prop ∧ Dur(viol_i) ≤ g_post
        
        Args:
            metrics: Post-execution metrics
            
        Returns:
            Tuple of (is_false_rejection, override_reason)
        """
        
        # Calculate grace window in absolute trials
        grace_window_trials = metrics.total_trials * self.criteria.grace_window_fraction
        
        # Check QA within tolerance
        qa_acceptable = metrics.qa_rate >= (metrics.threshold_qa - self.criteria.delta_qa)
        
        # Check latency within tolerance
        latency_acceptable = metrics.latency_p95 <= (metrics.threshold_latency + self.criteria.delta_latency)
        
        # Check failure rate within tolerance
        failure_acceptable = metrics.failure_rate <= (metrics.threshold_failure + self.criteria.delta_failure)
        
        # Check temporal propagation is minimal
        temporal_prop_minimal = metrics.temporal_propagation <= self.criteria.delta_temporal_prop
        
        # Check violation duration is transient
        violation_transient = metrics.violation_duration <= grace_window_trials
        
        # All conditions must be met for false rejection
        is_false_rejection = (
            qa_acceptable and 
            latency_acceptable and 
            failure_acceptable and 
            temporal_prop_minimal and 
            violation_transient
        )
        
        # Determine override reason
        if is_false_rejection:
            reasons = []
            if metrics.qa_predicate_failed:
                reasons.append("qa_transient")
            if metrics.latency_predicate_failed:
                reasons.append("latency_transient")
            if metrics.failure_predicate_failed:
                reasons.append("failure_transient")
            override_reason = "_".join(reasons) if reasons else "metrics_within_tolerance"
        else:
            override_reason = "true_rejection"
        
        return is_false_rejection, override_reason
    
    def _check_false_acceptance(self, metrics: ExecutionMetrics) -> Tuple[bool, str]:
        """
        Check if an acceptance by the predicate is a false acceptance.
        
        False acceptance occurs when:
        - At least one core safety metric is violated (QA, Latency, or Failure)
        - AND the violation duration extends beyond the grace window
        
        Criteria:
            (QA_i < τ_QA ∨ L_p95_i > ε_L ∨ F_i > ε_SLO)
            ∧ Dur(viol_i) > g_post
        
        Args:
            metrics: Post-execution metrics
            
        Returns:
            Tuple of (is_false_acceptance, violation_reason)
        """
        
        grace_window_trials = metrics.total_trials * self.criteria.grace_window_fraction
        violation_beyond_grace = metrics.violation_duration > grace_window_trials
        
        # Check if any core metric violates the threshold
        qa_violated = metrics.qa_rate < metrics.threshold_qa
        latency_violated = metrics.latency_p95 > metrics.threshold_latency
        failure_violated = metrics.failure_rate > metrics.threshold_failure
        
        any_metric_violated = qa_violated or latency_violated or failure_violated
        
        # False acceptance if violation is both present and sustained
        is_false_acceptance = any_metric_violated and violation_beyond_grace
        
        # Determine violation reason
        if is_false_acceptance:
            violations = []
            if qa_violated:
                violations.append(f"qa_violation(rate={metrics.qa_rate:.3f})")
            if latency_violated:
                violations.append(f"latency_violation(p95={metrics.latency_p95:.3f}s)")
            if failure_violated:
                violations.append(f"failure_violation(rate={metrics.failure_rate:.3f})")
            violation_reason = " & ".join(violations)
            violation_reason += f" sustained beyond grace window({grace_window_trials:.1f} trials)"
        else:
            violation_reason = "no_sustained_violations"
        
        return is_false_acceptance, violation_reason
    
    def _build_evidence_summary(self, metrics: ExecutionMetrics) -> Dict[str, Any]:
        """
        Build a comprehensive evidence summary for human auditor review.
        
        Args:
            metrics: Post-execution metrics
            
        Returns:
            Dictionary with formatted evidence for presentation
        """
        
        grace_window_trials = metrics.total_trials * self.criteria.grace_window_fraction
        
        return {
            "step": metrics.step_number,
            "target_service": metrics.target_service,
            "predicate_decision": "REJECTED" if not metrics.predicate_overall_result else "ACCEPTED",
            "metrics": {
                "quality_assurance": {
                    "actual": f"{metrics.qa_rate:.4f}",
                    "threshold": f"{metrics.threshold_qa:.4f}",
                    "tolerance_band": f"±{self.criteria.delta_qa:.4f}",
                    "status": "PASS" if metrics.qa_rate >= metrics.threshold_qa else "FAIL",
                    "predicate_failed": metrics.qa_predicate_failed
                },
                "latency_p95": {
                    "actual": f"{metrics.latency_p95:.3f}s",
                    "threshold": f"{metrics.threshold_latency:.3f}s",
                    "tolerance_band": f"±{self.criteria.delta_latency:.3f}s",
                    "status": "PASS" if metrics.latency_p95 <= metrics.threshold_latency else "FAIL",
                    "predicate_failed": metrics.latency_predicate_failed
                },
                "failure_rate": {
                    "actual": f"{metrics.failure_rate:.4f}",
                    "threshold": f"{metrics.threshold_failure:.4f}",
                    "tolerance_band": f"±{self.criteria.delta_failure:.4f}",
                    "status": "PASS" if metrics.failure_rate <= metrics.threshold_failure else "FAIL",
                    "predicate_failed": metrics.failure_predicate_failed
                }
            },
            "risk_assessment": {
                "temporal_propagation": f"{metrics.temporal_propagation:.3f}",
                "temporal_prop_tolerance": f"{self.criteria.delta_temporal_prop:.3f}",
                "temporal_prop_status": "LOW RISK" if metrics.temporal_propagation <= self.criteria.delta_temporal_prop else "HIGH RISK"
            },
            "violation_analysis": {
                "violation_duration": f"{metrics.violation_duration:.2f} trials",
                "grace_window": f"{grace_window_trials:.1f} trials ({self.criteria.grace_window_fraction*100:.0f}%)",
                "is_transient": metrics.violation_duration <= grace_window_trials,
                "duration_status": "TRANSIENT" if metrics.violation_duration <= grace_window_trials else "SUSTAINED"
            }
        }
    
    # HITL
    def _get_human_adjudication(
        self,
        metrics: ExecutionMetrics,
        evidence_summary: Dict[str, Any],
        predicate_decision: str,
        override_warning: str = None,
    ) -> Tuple[bool, str]:
        """
        Present evidence to human auditor and get adjudication decision.
        
        Args:
            metrics: Post-execution metrics
            evidence_summary: Formatted evidence for review
            predicate_decision: Original predicate decision (ACCEPT/REJECT)
            override_warning: Optional warning if false acceptance detected
            mode: Adjudication mode for context
            
        Returns:
            Tuple of (human_decision, decision_type)
        """
        
        # Display adjudication review interface
        logger.info("\n" + "="*80)
        logger.info("POST-ACTION ADJUDICATION REVIEW")
        logger.info("="*80)
        print("\n" + "="*80)
        print("POST-ACTION ADJUDICATION REVIEW")
        print("="*80)
        
        print(f"\n📋 Step {metrics.step_number}: {metrics.target_service}")
        print(f"Predicate Decision: {predicate_decision}")
        logger.info(f"\n📋 Step {metrics.step_number}: {metrics.target_service}")
        logger.info(f"Predicate Decision: {predicate_decision}")
        
        if override_warning:
            print(f"\n⚠️  WARNING: {override_warning}")
            logger.info(f"\n⚠️  WARNING: {override_warning}")
        
        print("\n" + "-"*80)
        print("EXECUTION METRICS & EVIDENCE")
        print("-"*80)
        logger.info("\n" + "-"*80)
        logger.info("EXECUTION METRICS & EVIDENCE")
        logger.info("-"*80)
        
        # Print quality metrics
        metrics_section = evidence_summary["metrics"]
        print("\n📊 Core Metrics:")
        logger.info("\n📊 Core Metrics:")

        for metric_name, metric_data in metrics_section.items():
            status_symbol = "✓" if metric_data["status"] == "PASS" else "✗"
            print(f"\n  {metric_name.replace('_', ' ').upper()}")
            print(f"    Actual:        {metric_data['actual']}")
            print(f"    Threshold:     {metric_data['threshold']}")
            print(f"    Tolerance:     {metric_data['tolerance_band']}")
            print(f"    Status:        {status_symbol} {metric_data['status']}")
            logger.info(f"\n  {metric_name.replace('_', ' ').upper()}")
            logger.info(f"    Actual:        {metric_data['actual']}")
            logger.info(f"    Threshold:     {metric_data['threshold']}")
            logger.info(f"    Tolerance:     {metric_data['tolerance_band']}")
            logger.info(f"    Status:        {status_symbol} {metric_data['status']}")
                    
        # Print risk assessment
        risk_section = evidence_summary["risk_assessment"]
        print("\n🔍 Risk Assessment:")
        print(f"  Temporal Propagation: {risk_section['temporal_propagation']} (tolerance: {risk_section['temporal_prop_tolerance']})")
        print(f"  Status: {risk_section['temporal_prop_status']}")
        logger.info("\n🔍 Risk Assessment:")
        logger.info(f"  Temporal Propagation: {risk_section['temporal_propagation']} (tolerance: {risk_section['temporal_prop_tolerance']})")
        logger.info(f"  Status: {risk_section['temporal_prop_status']}")
                
        # Print violation analysis
        violation_section = evidence_summary["violation_analysis"]
        print("\n📈 Violation Analysis:")
        print(f"  Violation Duration: {violation_section['violation_duration']}")
        print(f"  Grace Window: {violation_section['grace_window']}")
        print(f"  Classification: {violation_section['duration_status']}")
        logger.info("\n📈 Violation Analysis:")
        logger.info(f"  Violation Duration: {violation_section['violation_duration']}")
        logger.info(f"  Grace Window: {violation_section['grace_window']}")
        logger.info(f"  Classification: {violation_section['duration_status']}")
                
        print("\n" + "="*80)
        print("ADJUDICATION DECISION")
        print("="*80)
        logger.info("\n" + "="*80)
        logger.info("ADJUDICATION DECISION")
        logger.info("="*80)
                
        if predicate_decision == "REJECT":
            print("\n❌ Predicate REJECTED this step.")
            print("You should either OVERRIDE the rejection to ACCEPT this step, or CONFIRM the rejection")
            print("(Consider: Is this a transient violation without upstream harm?)")
            logger.info("\n❌ Predicate REJECTED this step.")
            logger.info("You should either OVERRIDE the rejection to ACCEPT this step, or CONFIRM the rejection")
            logger.info("(Consider: Is this a transient violation without upstream harm?)")            
        else:
            print("\n✅ Predicate ACCEPTED this step.")
            print("However, you may decide it be a false acceptance.")
            print("You should decide to OVERRIDE the acceptance to REJECT this step, or CONFIRM this acceptance?")
            logger.info("\n✅ Predicate ACCEPTED this step.")
            logger.info("However, you may decide it be a false acceptance.")
            logger.info("You should decide to OVERRIDE the acceptance to REJECT this step, or CONFIRM this acceptance?")        
        
        while True:
            logger.info("\n\nType 'A' to Accept this migration step or 'R' to Reject it (or 'H' for help): \n...Awaiting human adjudication input...")
            response = input("\nType 'A' to Accept this migration step or 'R' to Reject it (or 'H' for help): ").strip().upper()
            logging.info("HITL user response: %s", response)
            
            if response == "A":
                decision_type = f"accepted_by_human_governance"
                return True, decision_type
            
            elif response == "R":
                decision_type = f"rejected_by_human_governance"
                return False, decision_type
            
            elif response == "H":
                print("\n📖 GUIDANCE:")
                logger.info("\n📖 GUIDANCE:")
                if predicate_decision == "REJECT":
                    print("  • Accept: If violation is transient and won't propagate harm upstream")
                    print("  • Reject: If violation indicates genuine instability or system issues")
                    logger.info("  • Accept: If violation is transient and won't propagate harm upstream")
                    logger.info("  • Reject: If violation indicates genuine instability or system issues")
                else:
                    if override_warning:
                        print("  • Reject: If violations are sustained beyond grace window")
                        print("  • Accept: If violations are truly transient and grace window is appropriate")
                        logger.info("  • Reject: If violations are sustained beyond grace window")
                        logger.info("  • Accept: If violations are truly transient and grace window is appropriate")
                    else:
                        print("  • Accept: If all metrics are within acceptable ranges")
                        print("  • Reject: If you identify hidden safety concerns beyond predicates")
                        logger.info("  • Accept: If all metrics are within acceptable ranges")
                        logger.info("  • Reject: If you identify hidden safety concerns beyond predicates")
                continue
            
            else:
                print("Invalid input. Please type 'A' for Accept, 'R' for Reject, or 'H' for help.")
                logger.info("Invalid input. Please type 'A' for Accept, 'R' for Reject, or 'H' for help.")

def create_execution_metrics_from_step_result(
    step_result: Dict[str, Any],
    step_number: int,
    target_service: str,
    total_trials: int = 5000
) -> ExecutionMetrics:
    """
    Extract and create ExecutionMetrics from experiment step result.
    
    Args:
        step_result: The parsed JSON result from exp_runner_auto.py
        step_number: Current step number
        target_service: Name of the service being migrated
        total_trials: Total number of trials run
        
    Returns:
        ExecutionMetrics object with extracted values
    """
    
    details = step_result.get("details", {})
    
    return ExecutionMetrics(
        qa_rate=float(details.get("qa_inconsistency_rate", 0)),
        latency_p95=float(details.get("p95_latency", 0)),
        failure_rate=float(details.get("failure_rate", 0)),
        threshold_qa=float(details.get("epsilon_qa", 0)),
        threshold_latency=float(details.get("epsilon_l", 0)),
        threshold_failure=float(details.get("epsilon_f", 0)),
        temporal_propagation=float(step_result.get("step_self_temporal_propagation", 0)),
        # Note: violation_duration should come from log analysis
        violation_duration=_estimate_violation_duration(details),
        qa_predicate_failed=bool(details.get("qa_predicate_failed", False)),
        latency_predicate_failed=bool(details.get("latency_predicate_failed", False)),
        failure_predicate_failed=bool(details.get("failure_rate_predicate_failed", False)),
        predicate_overall_result=bool(details.get("success", False)),
        log_telemetry_file=details.get("log_telemetry_file", ""),
        step_number=step_number,
        target_service=target_service,
        total_trials=details.get("total_trials", total_trials)
    )


def _estimate_violation_duration(details: Dict[str, Any]) -> float:
    """
    Estimate violation duration from predicate failure information.
    
    This is a heuristic estimate. For production use, this should be
    derived from actual timestamped execution logs.
    
    Args:
        details: Details dictionary from experiment result
        
    Returns:
        Estimated violation duration as fraction of total trials
    """
    
    # Count how many predicates failed
    # failures = sum([
    #     details.get("qa_predicate_failed", False),
    #     details.get("latency_predicate_failed", False),
    #     details.get("failure_rate_predicate_failed", False)
    # ])
    
    log_telemetry_file = details.get("log_telemetry_file", "")
    target_service = details.get("target_service", None)
    total_trials = details.get("total_trials", 5000)
    
    qa_inconsistency_rate=float(details.get("qa_inconsistency_rate", 0)),
    p95_latency=float(details.get("p95_latency", 0)),
    failure_rate=float(details.get("failure_rate", 0)),
    threshold_qa=float(details.get("epsilon_qa", 0)),
    threshold_latency=float(details.get("epsilon_l", 0)),
    threshold_failure=float(details.get("epsilon_f", 0)),
    

    if log_telemetry_file:
        print(f"Analyzing log telemetry from: {log_telemetry_file} to estimate post-action audit violation duration...")
        logger.info(f"Analyzing log telemetry from: {log_telemetry_file} to estimate post-action audit violation duration...")
        try:
            with open(log_telemetry_file, "r") as f:
                log_telemetry_data = json.load(f)
                trial_results = log_telemetry_data[0]["trial_results"]
                total_trials = len(trial_results)

                # Implement actual logic to analyze logs and calculate violation duration

                violations = [
                        trial for trial in trial_results
                        if (
                            trial["elapsed"] > threshold_latency
                            or trial["total_api_calls_failure"] > 0
                        )
                    ]
                violation_count = len(violations)
                violation_rate = (
                    violation_count / total_trials
                    if total_trials > 0
                    else 0
                )
                return violation_rate * total_trials
            
        except Exception as e:
            # print(f"Error reading log telemetry file: {e}")
            logger.error(f"Error reading log telemetry file: {e}")
            pass
    

    # ---------------------------------------------------------
    # 1. Normalize deviations from acceptance thresholds
    #
    # Positive = violation of the threshold
    # Negative = comfortably below the threshold
    # ---------------------------------------------------------

    def normalized_deviation(value, threshold):
        # if threshold <= 0:
        #     return 0.0 if value <= threshold else 1.0

        return max(0.0, (value[0] - threshold[0]) / threshold[0]) if threshold[0] > 0 else value[0]

    qa_dev = normalized_deviation(
        qa_inconsistency_rate,
        threshold_qa
    )

    latency_dev = normalized_deviation(
        p95_latency,
        threshold_latency
    )

    failure_dev = normalized_deviation(
        failure_rate,
        threshold_failure
    )

    # ---------------------------------------------------------
    # 2. Combine the three deviations
    #
    # max() ensures that a serious violation in ANY metric
    # can make the event sustained.
    #
    # mean() prevents one tiny deviation from dominating.
    # ---------------------------------------------------------

    mean_deviation = (
        qa_dev +
        latency_dev +
        failure_dev
    ) / 3.0

    max_deviation = max(
        qa_dev,
        latency_dev,
        failure_dev
    )

    # Weighted combination:
    # emphasize the worst metric.
    overall_deviation = (
        0.4 * mean_deviation +
        0.6 * max_deviation
    )
    
 
     # ---------------------------------------------------------
    # 3. Base probability of sustained behavior
    # ---------------------------------------------------------

    # Close to thresholds:
    #       sustained ≈ 10-25%
    #
    # Large deviation:
    #       sustained approaches ≈ 80-95%
    #
    # sigmoid-like transformation
    sustained_probability = (
        0.10
        + 0.75 * (
            overall_deviation /
            (overall_deviation + 0.30)
        )
    )

    # ---------------------------------------------------------
    # 4. Service-specific sustained risk
    # ---------------------------------------------------------

    if target_service == "order_service":
        sustained_probability += 0.15

    elif target_service == "product_catalog_service":
        sustained_probability += 0.10

    else:
        sustained_probability += 0.00

    # ---------------------------------------------------------
    # 5. Bound probability
    # ---------------------------------------------------------

    sustained_probability = min(
        0.95,
        max(0.05, sustained_probability)
    )

    # ---------------------------------------------------------
    # 6. Simulate number of sustained trials
    # ---------------------------------------------------------

    return (
        random.uniform(
            sustained_probability * 0.85,
            sustained_probability * 1.15
        )
        * total_trials
    )
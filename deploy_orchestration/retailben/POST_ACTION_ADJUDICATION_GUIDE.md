# Post-Action Adjudication HITL (Human-In-The-Loop) Governance

## Overview

This document describes the Post-Action Adjudication mechanism for migration step governance. It implements human-in-the-loop (HITL) decision-making for microservice-to-agent architecture refactoring, where human experts perform audition and final adjudication for predicate-based acceptability results after step execution completes.

## Architecture

### Components

1. **ExecutionMetrics**: Container for post-execution metrics extracted from experiment results
2. **AdjudicationCriteria**: Tolerance bands and thresholds for adjudication decisions
3. **PostActionAdjudicator**: Core logic engine for adjudication
4. **Integration Point**: Updated orchestrator calls adjudicator for each step

### Workflow

```
Experiment Execution (step i)
    ↓
Predicate-based Automatic Decision (ACCEPTED/REJECTED)
    ↓
Post-Action Adjudication Analysis
    ├─ Extract Execution Metrics (QA, Latency, Failure Rate, etc.)
    ├─ Analyze Violation Duration & Temporal Propagation
    ├─ Apply Adjudication Logic (Selective or Comprehensive)
    └─ Determine if Human Review Needed
    ↓
Human Auditor Review (if applicable)
    ├─ Review Evidence Summary
    ├─ Analyze Metrics vs Thresholds
    ├─ Consider Risk Propagation
    └─ Make Final Adjudication Decision
    ↓
Final Decision (ACCEPT/REJECT)
    ↓
Update Migration State & Continue
```

## Adjudication Modes

### Mode 1: No Governance (`No`)

- Automatic predicate decision is final
- No human intervention
- Fast execution path

### Mode 2: Selective (Exception-based) Adjudication (`Post-Audit-Selective-Only`)

**Triggered Only On Rejections**: Human auditor reviews only when predicates REJECT a step.

**Objective**: Correct false rejections by identifying transient violations that don't indicate genuine instability.

**False Rejection Criteria**:

A rejection is considered FALSE when ALL of the following hold:

```
(QA_i ≥ τ_QA − δ_QA) ∧ (L_p95_i ≤ ε_L + δ_L) ∧ (F_i ≤ ε_SLO + δ_SLO)
∧ T_Prop(s_i) ≤ δ_T_prop ∧ Dur(viol_i) ≤ g_post
```

Where:
- **QA_i**: Quality assurance inconsistency rate (actual)
- **τ_QA**: QA threshold
- **δ_QA**: QA tolerance band (0.05 = 5%)
- **L_p95_i**: 95th percentile latency (actual)
- **ε_L**: Latency threshold
- **δ_L**: Latency tolerance band (0.1s)
- **F_i**: Failure rate (actual)
- **ε_SLO**: Failure rate threshold
- **δ_SLO**: Failure rate tolerance band (0.02 = 2%)
- **T_Prop(s_i)**: Temporal propagation to upstream services (≤ 0.1)
- **Dur(viol_i)**: Violation duration as fraction of total trials
- **g_post**: Grace window (30% of trials by default)

**Decision Rule**:
```
IF (Predicate REJECTED) AND (False Rejection Criteria Met) THEN
    → Automatic Override to ACCEPT
    → No human review needed
ELSE IF (Predicate REJECTED) THEN
    → Present evidence to human auditor
    → Human decides: ACCEPT (override) or REJECT (confirm)
ELSE
    → Auto-accept (predicate passed)
```

**Use Case**: When you trust the predicate thresholds but want to catch obvious false rejections due to temporary issues.

### Mode 3: Comprehensive Adjudication (`Post-Audit-Comprehensive`)

**Triggered On All Decisions**: Human auditor reviews every step decision (both acceptances and rejections).

**Objectives**:
1. Correct false rejections (same as selective mode)
2. Detect and override false acceptances (predicate blindness to safety dimensions)

**False Acceptance Criteria**:

An acceptance is considered FALSE when:

```
(QA_i < τ_QA ∨ L_p95_i > ε_L ∨ F_i > ε_SLO) ∧ Dur(viol_i) > g_post
```

At least one core metric is violated AND violation is sustained beyond grace window.

**Decision Rule**:
```
IF (Predicate REJECTED) THEN
    Check for false rejection (same as selective mode)
    If false rejection detected → Override to ACCEPT
    Else → Human reviews and decides
ELSE IF (Predicate ACCEPTED) THEN
    Check for false acceptance
    If false acceptance detected → Present warning to human
    Human reviews and decides: ACCEPT (confirm) or REJECT (override)
```

**Use Case**: When predicate thresholds may be incomplete (e.g., missing safety dimensions) or when comprehensive safety evaluation is required.

## Execution Metrics

The adjudicator analyzes these metrics extracted from experiment execution:

### Core Quality Metrics

| Metric | Source | Unit | Interpretation |
|--------|--------|------|-----------------|
| **QA Inconsistency Rate** | Experiment telemetry | [0, 1] | Lower = better; inconsistent results |
| **Latency p95** | Request timing | seconds | 95th percentile response time |
| **Failure Rate** | Request outcomes | [0, 1] | Proportion of failed requests |

### Predicate Thresholds

Set before experiment execution based on predicate mode:

- **threshold_qa** (τ_QA): Acceptable QA inconsistency
- **threshold_latency** (ε_L): Maximum acceptable p95 latency
- **threshold_failure** (ε_SLO): Maximum acceptable failure rate

### Risk Propagation Metrics

| Metric | Source | Meaning |
|--------|--------|---------|
| **Temporal Propagation** | Previous step decisions | Risk propagation to dependent services |
| **Violation Duration** | Log analysis | How long violation persisted (fraction of trials) |

### Violation Analysis

- **Transient Violations**: Duration ≤ grace window (30% of trials)
  - Examples: Warm-up hiccups, temporary resource contention, brief network issues
  - **Action**: May be overridable if metrics are within tolerance

- **Sustained Violations**: Duration > grace window
  - Examples: Systematic performance degradation, persistent failures
  - **Action**: Indicates real instability; requires careful evaluation

## Tolerance Bands & Thresholds

Default adjudication criteria (configurable):

```python
AdjudicationCriteria(
    delta_qa=0.05,                    # 5% tolerance on QA rate
    delta_latency=0.1,                # 0.1s tolerance on p95 latency
    delta_failure=0.02,               # 2% tolerance on failure rate
    delta_temporal_prop=0.1,          # 0.1 tolerance on temporal propagation
    grace_window_fraction=0.3         # 30% of trials as grace window
)
```

### Tuning Guidelines

- **Increase tolerance bands** → More conservative (fewer false rejections)
- **Decrease tolerance bands** → More strict (catch more problems)
- **Increase grace window** → More lenient on transient issues
- **Decrease grace window** → More conservative on sustained issues

## Human Auditor Interface

When a step requires human adjudication, the auditor receives:

### 1. Step Context

```
Step 5: inventory_service
Adjudication Mode: Selective
Predicate Decision: REJECTED
```

### 2. Execution Metrics

Three sections with actual vs. threshold comparisons:

**Quality Assurance**:
```
Actual:        0.0123
Threshold:     0.0000
Tolerance:     ±0.0500
Status:        FAIL (predicate failed)
```

**Latency p95**:
```
Actual:        2.456s
Threshold:     1.900s
Tolerance:     ±0.100s
Status:        FAIL (predicate failed)
```

**Failure Rate**:
```
Actual:        0.0300
Threshold:     0.0200
Tolerance:     ±0.0200
Status:        FAIL (predicate failed)
```

### 3. Risk Assessment

```
Temporal Propagation: 0.050 (tolerance: 0.100)
Status: LOW RISK
```

### 4. Violation Analysis

```
Violation Duration:   2.50 trials
Grace Window:         3.0 trials (30%)
Classification:       TRANSIENT
```

### 5. Decision Guidance

For rejections in selective mode:
```
❌ Predicate REJECTED this step.
Do you wish to OVERRIDE the rejection and ACCEPT the refactoring?
(Consider: Is this a transient violation without upstream harm?)

Type 'A' to Accept or 'R' to Reject (or 'H' for help)
```

For acceptances in comprehensive mode with false acceptance warning:
```
⚠️  WARNING: False acceptance detected: 
    latency_violation(p95=2.456s) sustained beyond grace window(3.0 trials)

✅ Predicate ACCEPTED this step.
However, evidence suggests this may be a false acceptance.
Do you wish to OVERRIDE the acceptance and REJECT the refactoring?

Type 'A' to Accept or 'R' to Reject (or 'H' for help)
```

## Decision Audit Trail

Each adjudication decision is logged with a decision type string for audit tracing:

### Selective Mode Decision Types

- `auto_decision_no_governance`: No governance applied
- `accepted_by_predicate_auto_pass`: Predicate accepted, auto-accepted
- `false_rejection_override_qa_transient`: QA violation was transient, overridden to accept
- `false_rejection_override_latency_transient`: Latency violation was transient, overridden
- `false_rejection_override_failure_transient`: Failure rate violation was transient
- `false_rejection_override_qa_latency_transient`: Multiple violations but all transient
- `accepted_by_governance_selective`: Human reviewed and accepted rejection
- `rejected_by_governance_selective`: Human reviewed and confirmed rejection

### Comprehensive Mode Decision Types

- Same as selective, plus:
- `rejected_by_governance_comprehensive`: Human overrode acceptance to reject
- `accepted_by_governance_comprehensive`: Human reviewed and confirmed acceptance

## Integration with Orchestrator

The orchestrator has been updated to use post-action adjudication:

```python
# Configuration
governance_policy = "Post-Audit-Selective-Only"  # or "Post-Audit-Comprehensive" or "No"

# Initialization
adjudication_criteria = AdjudicationCriteria(...)
post_action_adjudicator = PostActionAdjudicator(adjudication_criteria)

# For each step
execution_metrics = create_execution_metrics_from_step_result(
    step_result_parsed,
    step_number=step_num,
    target_service=target_service
)

final_decision, decision_type = post_action_adjudicator.adjudicate_step(
    metrics=execution_metrics,
    mode=adjudication_mode
)
```

## Configuration

### Switching Modes

Edit `progressive_refactor_orchestrator.py`:

```python
governance_policy = "Post-Audit-Selective-Only"  # Change this
```

Options:
- `"No"`: No governance, auto-decide
- `"Post-Audit-Selective-Only"`: Override false rejections only
- `"Post-Audit-Comprehensive"`: Review all decisions

### Customizing Criteria

```python
adjudication_criteria = AdjudicationCriteria(
    delta_qa=0.05,              # Tune QA tolerance
    delta_latency=0.1,          # Tune latency tolerance
    delta_failure=0.02,         # Tune failure rate tolerance
    delta_temporal_prop=0.1,    # Tune propagation threshold
    grace_window_fraction=0.3   # Tune transient vs sustained window
)
```

## Best Practices

### For Auditors

1. **Understand Context**: Review temporal propagation and dependency impacts
2. **Check Duration**: Distinguish transient (warm-up) from sustained (real problem)
3. **Look at Multiple Metrics**: Don't focus on one dimension; consider holistic health
4. **Document Reasoning**: When overriding, be aware of the decision audit trail
5. **Be Consistent**: Apply similar criteria across similar violation patterns

### For Configuration

1. **Start Conservative**: Use default tolerances, adjust based on results
2. **Monitor False Rates**: Track how often automatic decisions are overridden
3. **Periodic Review**: Check if grace window (30%) matches your typical convergence time
4. **Document Decisions**: Keep audit logs for post-hoc analysis
5. **A/B Test Modes**: Compare selective vs. comprehensive results

## Examples

### Example 1: False Rejection Recovery (Selective Mode)

```
Step 5: inventory_service
Predicate: REJECTED (Latency exceeded)

Evidence:
  - Latency p95: 2.0s (threshold 1.9s) [+0.1s violation]
  - QA rate: Good
  - Failure rate: Good
  - Violation duration: 2.5 trials (within 30% grace window)
  - Temporal propagation: 0.05 (low)

Analysis:
  → All conditions for false rejection met
  → System recovered within grace window
  → No downstream harm

Decision: ✅ AUTOMATICALLY OVERRIDE TO ACCEPT
  (No human review needed)
```

### Example 2: Confirm Rejection (Selective Mode)

```
Step 5: inventory_service
Predicate: REJECTED (Latency exceeded)

Evidence:
  - Latency p95: 3.5s (threshold 1.9s) [-1.6s violation]
  - Violation duration: 8.0 trials (beyond 30% grace window)
  - Temporal propagation: 0.25 (significant)

Analysis:
  → Violation is sustained, not transient
  → Significant propagation to upstream
  → Indicates real performance problem

Human Decision:
  Auditor: "This is a real latency issue, not transient. The service
           cannot handle the load. Confirm REJECTION."
  → REJECT
```

### Example 3: Catch False Acceptance (Comprehensive Mode)

```
Step 5: inventory_service
Predicate: ACCEPTED

⚠️ WARNING: False acceptance detected:
   latency_violation(p95=2.1s) sustained beyond grace window(3.0 trials)

Evidence:
  - Latency p95: 2.1s (threshold 1.9s) [slight violation]
  - But sustained for 4.5 trials (beyond grace window)
  - Multiple requests affected, not just warm-up

Human Decision:
  Auditor: "While individual metrics look acceptable, the sustained
           latency pattern suggests the system is struggling. The
           threshold was too lenient. Override to REJECT for safety."
  → REJECT
```

## Troubleshooting

### Issue: Too Many False Rejections Overridden

**Diagnosis**: Predicate thresholds too strict; tolerance bands too wide

**Solutions**:
1. Increase predicate thresholds (epsilon values)
2. Decrease tolerance bands (delta values)
3. Reduce grace window fraction
4. Switch to Comprehensive mode for additional review

### Issue: Too Many False Acceptances Not Detected

**Diagnosis**: Predicate thresholds too lenient; missing safety dimensions

**Solutions**:
1. Decrease predicate thresholds
2. Consider which safety dimension is missing
3. Switch to Comprehensive mode for human review
4. Add checks for that dimension

### Issue: Auditor Spending Too Much Time Reviewing

**Diagnosis**: Comprehensive mode reviews everything; many borderline cases

**Solutions**:
1. Switch to Selective mode (only review rejections)
2. Widen tolerance bands to reduce false rejections
3. Increase grace window for transient issues
4. Adjust thresholds based on audit feedback

### Issue: System Keeps Rejecting Steps Despite Seeming OK

**Diagnosis**: High temporal propagation affecting next steps

**Solutions**:
1. Check temporal_propagation in evidence
2. Review dependency weights
3. Consider if this step should be skipped in this migration order
4. Adjust migration ranking to avoid high-propagation steps

## Metrics Export & Analysis

The orchestrator generates audit logs in:

```
refactored_architecture/retailben/results/
  refactored_arch_results_llm_{LLM}_T_{T}_...json
```

Each step record includes:
- `acceptance_result`: Full metrics
- Predicate thresholds (epsilon values)
- Temporal propagation
- Previous step acceptance type
- Post-execution metadata

Use these for:
- Analyzing patterns in human decisions
- Tuning threshold criteria
- Comparing adjudication modes
- Post-hoc validation

## References

### Research Concepts

The implementation follows the formal definitions from the research:

- **Predicate-based Acceptability**: Binary decision based on QA, latency, failure rate
- **Post-Context Evidence**: Execution logs, traces, telemetry beyond predicates
- **Violation Durability**: Longest contiguous request sequence with violation
- **Temporal Propagation**: Risk propagation to dependent services
- **Grace Windows**: Transient vs. sustained violation classification

### Related Configurations

- `predicate_mode`: QA-Only, Latency-Only, Failure-Only, Full
- `temporal_propagation_enabled`: Enable/disable temporal propagation tracking
- `temporal_propagation_dependency_influence_weight`: Service dependency weights

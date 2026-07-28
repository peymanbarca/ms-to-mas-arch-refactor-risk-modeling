# Post-Action Adjudication - Quick Reference for Auditors

## Before You Start

- You'll be reviewing migration steps after they execute
- Your role: Verify if automatic predicate decisions are correct based on actual evidence
- **Trust the data**: Review metrics, logs, and violation patterns

## Your Review Checklist

### ✓ For Each Step, Check:

```
[ ] Step Number & Target Service        → Understand what's being migrated
[ ] Predicate Decision                  → What did the automatic system decide?
[ ] QA Rate                             → Is quality acceptable?
[ ] Latency p95                         → Is response time acceptable?
[ ] Failure Rate                        → Are requests succeeding?
[ ] Violation Duration                  → Is this a warm-up blip or real problem?
[ ] Temporal Propagation                → Will this break dependent services?
[ ] Grace Window Status                 → Is violation transient or sustained?
```

## Decision Quick Guide

### Scenario 1: Predicate REJECTED (Any Mode)

```
ASK YOURSELF:
├─ Is the violation REAL (sustained) or TEMPORARY (transient)?
│  └─ If TEMPORARY: Consider ACCEPT (false rejection)
│  └─ If SUSTAINED: Confirm REJECT
├─ Will this harm upstream services?
│  └─ High propagation → REJECT
│  └─ Low propagation → Can be overridden
└─ Is the metric that failed a critical safety dimension?
   └─ Critical (latency, failures) → Be cautious before overriding
   └─ Non-critical → Easier to override
```

### Scenario 2: Predicate ACCEPTED - Selective Mode

```
You see "ACCEPTED" → Usually OK, no action needed
Exception: If you notice violations in the evidence,
          you can manually override (see comprehensive mode below)
```

### Scenario 3: Predicate ACCEPTED - Comprehensive Mode (⚠️ Warning)

```
ASK YOURSELF:
├─ Is the predicate missing a safety dimension?
│  └─ Yes → REJECT (override the acceptance)
│  └─ No → ACCEPT (confirm)
├─ Are violations sustained beyond grace window?
│  └─ Yes, with metrics violated → REJECT
│  └─ No, just warm-up issues → ACCEPT
└─ Is the service critical to system health?
   └─ Critical → Be conservative, tend to REJECT warnings
   └─ Non-critical → Can be more lenient
```

## Quick Decision Flowchart

```
┌──────────────────────────────────────────────────┐
│ STEP EXECUTION COMPLETE                          │
│ Review Evidence Summary                          │
└──────────────────────────────────────────────────┘
                      ↓
              ┌──────────────┐
              │  REJECTED?   │
              └──────────────┘
                  ↙       ↘
                YES       NO
                 ↓         ↓
         ┌─────────────┐  [ACCEPTED]
         │ Check if    │     ↓
         │ FALSE       │  Is it Comprehensive Mode?
         │ REJECTION?  │     ├─ NO → ACCEPT
         └─────────────┘     └─ YES → Check for false
                ↓                  acceptance warning
        ┌────────────────┐
        │ All conditions │
        │ met? (metric   │
        │ within         │
        │ tolerance,     │
        │ transient,     │
        │ low propagation)
        └────────────────┘
            ↙       ↘
          YES       NO
           ↓         ↓
    ┌──────────┐ ┌───────────┐
    │ AUTO OK  │ │ HUMAN     │
    │ ACCEPT   │ │ REVIEW    │
    │ (rare)   │ │ REQUIRED  │
    └──────────┘ └─────────────┘
                      ↓
              ┌──────────────────┐
              │ Read Evidence    │
              │ Make Decision    │
              │ A = Accept       │
              │ R = Reject       │
              └──────────────────┘
```

## What Each Metric Tells You

| Metric | Good | Bad | Action |
|--------|------|-----|--------|
| **QA Rate** | Near 0 | >0.05 | Check if transient |
| **Latency p95** | <threshold | >threshold | Major red flag if sustained |
| **Failure Rate** | Near 0 | >0.02 | Critical - indicates broken functionality |
| **Violation Duration** | <30% trials | >30% trials | If >30%, it's real, not warm-up |
| **Temporal Propagation** | <0.1 | >0.1 | If high, downstream services at risk |

## Common Patterns & Guidance

### Pattern 1: All Metrics Bad, Duration Transient
```
→ Likely warm-up issue during initial requests
→ Decision: ACCEPT (if propagation is low)
→ Confidence: HIGH
```

### Pattern 2: One Metric Bad, Duration Sustained, High Propagation
```
→ Real performance problem affecting dependencies
→ Decision: REJECT
→ Confidence: HIGH
```

### Pattern 3: Metrics Slightly Beyond Threshold, But Transient
```
→ False rejection - system recovered quickly
→ Decision: ACCEPT (override)
→ Confidence: MEDIUM-HIGH (depending on critical metric)
```

### Pattern 4: Metrics Good, No Violations Shown, Predicate Rejected
```
→ Predicate may have been too strict or borderline case
→ Decision: Review tolerance bands, likely ACCEPT
→ Confidence: MEDIUM (may indicate threshold miscalibration)
```

### Pattern 5: ⚠️ COMPREHENSIVE MODE: Metrics Good but Violation Duration Long
```
→ Predicate may be blind to some dimension
→ Decision: REJECT (be conservative)
→ Confidence: MEDIUM (may indicate missing safety dimension)
```

## Red Flags ⚠️

- **Failure Rate > 5%**: Systematic failures, high risk → REJECT
- **Latency p95 > 2x threshold**: Severe degradation → REJECT
- **Temporal Propagation > 0.5**: Major risk to upstream → REJECT
- **Multiple metrics violated + sustained**: System unhealthy → REJECT
- **No grace window but rejection**: Predicate was right → REJECT

## Green Lights ✅

- **All metrics within tolerance bands**: System healthy
- **Violations exist but all transient**: Warm-up artifacts
- **Low temporal propagation**: Safe to proceed
- **Single metric borderline + transient**: Likely false rejection
- **Pattern consistent with previous steps**: Confidence boost

## If You're Unsure

```
Type 'H' for help guidance in the interface
Review the evidence section again, especially:
  1. Check if violation duration is transient (≤30%)
  2. Check temporal propagation (≤0.1 is safe)
  3. Check if this is a safety-critical metric
```

## Common Questions

**Q: Should I accept a high latency violation if it's transient?**
A: Depends on downstream services. If propagation is low and it recovered quickly, yes. If this affects order processing or payments, be conservative.

**Q: What if metrics look good but the service "feels" unstable?**
A: That's what comprehensive mode is for. The predicate may be missing something. Err on the side of caution.

**Q: How do I know if 30% grace window is right?**
A: Look at when violations clear. If 80% of issues clear within 3 requests (typical), grace window is good. If they persist longer, it's real.

**Q: Can I override the automatic override to false rejection?**
A: Yes, if evidence shows it's actually a real problem. Trust your judgment as a human expert.

**Q: What if previous step had high propagation - does that affect this step?**
A: Yes, look for that in the context. High propagation from step 4 makes step 5 riskier.

## Decision Template

When making your decision, think about:

```
This service [name] shows:
  • QA: [metric value] (threshold: [threshold]) = [status]
  • Latency: [metric value] (threshold: [threshold]) = [status]
  • Failure: [metric value] (threshold: [threshold]) = [status]
  • Duration: [metric value] trials (grace: [grace]) = [transient/sustained]
  • Propagation: [value] (tolerance: 0.1) = [low/high risk]

Key observations:
  1. [Observation about what's happening]
  2. [Observation about risk]
  3. [Observation about pattern]

My decision: [ACCEPT/REJECT] because:
  - [Reason 1]
  - [Reason 2]
  - [Reason 3]
```

## Tips for Success

1. **Be systematic**: Go through the checklist every time
2. **Document your reasoning**: You may review these decisions later
3. **Look for patterns**: Are failures happening at the same stage?
4. **Trust transience**: If it recovered, it recovered. Don't overthink.
5. **Be bold on safety**: If unsure and it's safety-critical, REJECT
6. **Be pragmatic on non-critical**: If service is non-critical and looks OK, ACCEPT
7. **Learn from outcomes**: After steps are deployed, see if your decisions were right

## Support

- See `POST_ACTION_ADJUDICATION_GUIDE.md` for full documentation
- Check evidence summary for context on specific metrics
- Use 'H' (help) in the interface for guidance during review

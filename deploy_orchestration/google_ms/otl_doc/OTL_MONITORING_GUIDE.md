# OTL (On-The-Loop) Monitoring & Interruption Module

## Overview

The **On-The-Loop (OTL)** monitoring system enables automatic detection and interruption of experiment runs when key performance metrics exceed predefined thresholds. It divides trials into non-overlapping blocks and performs health checks at block boundaries.

## Key Features

- **Block-based monitoring**: Divides total trials into non-overlapping blocks of size `b`
- **Multi-metric evaluation**: Checks p95 latency, failure rate, and latency inconsistency
- **Automatic interruption**: Stops experiment execution when thresholds are exceeded
- **Toggle-friendly**: Can be activated or deactivated via configuration
- **Comprehensive reporting**: Generates detailed OTL metrics report in results

## Architecture

### Components

#### 1. `OTLConfig` (Dataclass)
Configuration object that defines OTL behavior:

```python
@dataclass
class OTLConfig:
    enabled: bool = True
    block_size: int = 10
    p95_latency_threshold_s: float = 5.0
    failure_rate_threshold_pct: float = 10.0
    inconsistency_threshold_pct: float = 50.0
```

#### 2. `OTLMetrics` (Dataclass)
Computed metrics for a single block:

```python
@dataclass
class OTLMetrics:
    block_num: int
    trials_in_block: int
    p95_latency_s: float
    avg_failure_rate_pct: float
    inconsistency_pct: float  # Coefficient of Variation
    threshold_violations: List[str]
```

#### 3. `OTLMonitor` (Class)
Main monitoring engine that:
- Computes metrics for each block
- Checks against thresholds
- Sets interrupt flag on violations
- Generates comprehensive reports

## Metrics Explained

### 1. **P95 Latency** (`p95_latency_s`)
- **Definition**: 95th percentile of trial latencies in the block
- **Use case**: Detects performance degradation
- **Default threshold**: 5.0 seconds
- **Example**: If p95 latency is 6.5s and threshold is 5.0s → VIOLATION

### 2. **Average Failure Rate** (`avg_failure_rate_pct`)
- **Definition**: Percentage of failed trials in the block
- **Use case**: Detects reliability issues
- **Default threshold**: 10.0%
- **Example**: If 3 out of 20 trials fail → 15% > 10% → VIOLATION

### 3. **Invariant Violation Rate** (`invariant_violation_pct`)
- **Definition**: Percentage indicating database state inconsistencies
- **Checks**: 
  - Pending orders inconsistency (should decrease with successful trials)
  - Completed orders correctness (should match trial successes)
  - Payment success consistency (should correlate with trial successes)
  - Shipment booking correctness (should follow completed orders)
  - Overall DB state validity (should not be in error state with successful trials)
- **Use case**: Detects data consistency issues in microservice architecture
- **Default threshold**: 5.0%
- **Example**: If 2+ invariant checks fail and threshold is 5% → VIOLATION

## Configuration

### Method 1: Environment Variables

```bash
# Activate OTL
OTL_ENABLED=true

# Set block size (trials per block)
OTL_BLOCK_SIZE=20

# Set thresholds
OTL_P95_LATENCY_THRESHOLD_S=3.5
OTL_FAILURE_RATE_THRESHOLD_PCT=5.0
OTL_INVARIANT_VIOLATION_THRESHOLD_PCT=3.0

# Run experiment
python -m simulation.run_trial
```

### Method 2: Command-Line Arguments

```bash
python exp_runner.py \
  --trials 100 \
  --concurrency 8 \
  --otl-enabled \
  --otl-block-size 20 \
  --otl-p95-threshold 3.5 \
  --otl-failure-threshold 5.0 \
  --otl-invariant-threshold 3.0
```

### Method 3: Programmatic Configuration

```python
from exp_runner_with_otl import OTLConfig, OTLMonitor, full_trials_runner

# Create OTL configuration
otl_config = OTLConfig(
    enabled=True,
    block_size=15,
    p95_latency_threshold_s=4.0,
    failure_rate_threshold_pct=8.0,
    invariant_violation_threshold_pct=3.0
)

# Run experiment with OTL monitoring
run_results = full_trials_runner(
    LLM='llama3:8b',
    T=0,
    CONCURRENCY_RATE=8,
    R=100,
    otl_config=otl_config
)
```

## Usage Examples

### Example 1: Enable OTL with Defaults

```bash
# Run 100 trials with OTL monitoring using default thresholds
OTL_ENABLED=true OTL_BLOCK_SIZE=10 python -m simulation.run_trial
```

**Output:**
```
======================================================================
RUN 1 / 1
  N_TRIALS=100  MAX_WORKERS=1  DELAY=0s  DROP_RATE=0%
  OTL_ENABLED=true  BLOCK_SIZE=10
======================================================================

DB clean. Starting trials...

  ✓ Trial   1 | 1.234s | llm_calls=  5 | in_tok= 1200 | out_tok=  350
  ...
  [OTL] Block 1 (10 trials): p95=2.340s, failure_rate=0.0%, inconsistency=15.2%

  ✓ Trial  11 | 1.456s | llm_calls=  5 | in_tok= 1200 | out_tok=  350
  ...
  [OTL] Block 2 (10 trials): p95=2.456s, failure_rate=0.0%, inconsistency=18.5%
```

### Example 2: Strict Thresholds (Early Interruption)

```bash
# Strict thresholds for early problem detection
OTL_ENABLED=true \
OTL_BLOCK_SIZE=5 \
OTL_P95_LATENCY_THRESHOLD_S=2.0 \
OTL_FAILURE_RATE_THRESHOLD_PCT=2.0 \
OTL_INCONSISTENCY_THRESHOLD_PCT=30.0 \
python -m simulation.run_trial --trials 100
```

This will detect issues earlier (every 5 trials instead of 10).

### Example 3: Disable OTL

```bash
# Run without monitoring (default behavior)
python exp_runner.py --trials 100 --concurrency 8
# or explicitly:
OTL_ENABLED=false python -m simulation.run_trial
```

## Integration with Trial Execution Loop

The OTL monitoring is integrated into the main trial execution loop:

```python
# After each trial completes...
for future in as_completed(futures):
    trial_result = future.result()
    results.append(trial_result)
    trials_completed += 1
    
    # Check OTL after each block
    if otl_monitor:
        block_num = (trials_completed - 1) // otl_monitor.config.block_size + 1
        within_block_idx = (trials_completed - 1) % otl_monitor.config.block_size
        
        # If we've completed a full block, check metrics
        if within_block_idx == otl_monitor.config.block_size - 1 or trials_completed == R:
            block_results = results[block_start:block_end]
            metrics = otl_monitor.compute_block_metrics(block_num, block_results)
            
            # If violations detected, set interrupt flag
            if metrics.threshold_violations:
                run_interrupted = True
                break
```

## OTL Report Output

The OTL monitoring results are included in the final JSON output under `otl_report`:

```json
{
  "otl_report": {
    "otl_enabled": true,
    "block_size": 10,
    "total_blocks_processed": 3,
    "should_interrupt": false,
    "interrupt_reason": "",
    "thresholds": {
      "p95_latency_threshold_s": 5.0,
      "failure_rate_threshold_pct": 10.0,
      "invariant_violation_threshold_pct": 5.0
    },
    "block_metrics": [
      {
        "block_num": 1,
        "trials_in_block": 10,
        "p95_latency_s": 2.340,
        "avg_failure_rate_pct": 0.0,
        "invariant_violation_pct": 0.0,
        "db_state_before": {
          "total_completed_orders": 100,
          "total_pending_orders": 5,
          "total_success_payments": 98,
          "total_shipment_bookings": 95,
          "final_ec_state": "clean"
        },
        "db_state_after": {
          "total_completed_orders": 110,
          "total_pending_orders": 2,
          "total_success_payments": 108,
          "total_shipment_bookings": 105,
          "final_ec_state": "clean"
        },
        "violations": []
      },
      {
        "block_num": 2,
        "trials_in_block": 10,
        "p95_latency_s": 6.234,
        "avg_failure_rate_pct": 0.0,
        "invariant_violation_pct": 40.0,
        "db_state_before": {
          "total_completed_orders": 110,
          "total_pending_orders": 2,
          "total_success_payments": 108,
          "total_shipment_bookings": 105,
          "final_ec_state": "clean"
        },
        "db_state_after": {
          "total_completed_orders": 110,
          "total_pending_orders": 12,
          "total_success_payments": 108,
          "total_shipment_bookings": 105,
          "final_ec_state": "clean"
        },
        "violations": [
          "p95_latency (6.234s) exceeds threshold (5.0s)",
          "invariant_violations (40.0%) exceeds threshold (5.0%)"
        ]
      }
    ]
  }
}
```

## Workflow Diagram

```
Start Trial Execution
         |
         v
    [Trial N executed]
         |
         v
   Add to results
   Trials_completed++
         |
         v
   Block complete? 
   /            \
  NO          YES
  |             |
  |             v
  |      Compute Block Metrics:
  |      - p95 latency
  |      - failure rate %
  |      - inconsistency %
  |             |
  |             v
  |      Check Thresholds
  |             |
  |        /    |    \
  |       /     |     \
  |    PASS   FAIL   PASS
  |       |     |      |
  |       |     v      |
  |       |   Set      |
  |       |  Interrupt |
  |       |   Flag     |
  |       |     |      |
  |       \     |     /
  |         \   |   /
  |           \ | /
  |            v
  +----> All trials done?
            |
       /    |    \
      NO  YES  INTERRUPT
      |    |      |
      |    v      v
      |   Done   Cleanup
      |           |
      |           v
      |      Generate Report
      v      (with OTL metrics)
```

## Performance Considerations

### Block Size Impact

| Block Size | Pros | Cons |
|-----------|------|------|
| **Small (5-10)** | Early detection, quick feedback | More frequent checks, higher overhead |
| **Medium (20-50)** | Balanced, reasonable overhead | Delayed detection |
| **Large (100+)** | Minimal overhead | Late detection, many wasted trials |

**Recommendation**: For most use cases, `block_size=20` offers good balance.

### Threshold Tuning

| Scenario | Recommended Settings |
|----------|----------------------|
| **Strict (Microservice Validation)** | p95=2s, failure=2%, invariant_violations=2% |
| **Standard** (Default) | p95=5s, failure=10%, invariant_violations=5% |
| **Lenient (R&D)** | p95=10s, failure=20%, invariant_violations=10% |

## Troubleshooting

### Issue: Experiment always interrupts on first block

**Possible causes:**
- Thresholds too strict
- System still warming up (cache warm, JIT compilation)
- DB state checks are finding inconsistencies

**Solutions:**
```bash
# Increase thresholds
OTL_P95_LATENCY_THRESHOLD_S=10.0
OTL_FAILURE_RATE_THRESHOLD_PCT=20.0
OTL_INVARIANT_VIOLATION_THRESHOLD_PCT=10.0

# Or increase block size for more stable estimates
OTL_BLOCK_SIZE=30
```

### Issue: No violations detected but DB state is inconsistent

**Possible causes:**
- Thresholds too lenient
- DB checks not catching specific inconsistency type

**Solutions:**
```bash
# Lower thresholds
OTL_P95_LATENCY_THRESHOLD_S=2.0
OTL_INVARIANT_VIOLATION_THRESHOLD_PCT=2.0

# Reduce block size for earlier detection
OTL_BLOCK_SIZE=10
```

### Issue: Invariant violation metric always triggered even with successful trials

**Possible causes:**
- DB state not properly reflecting trial outcomes
- `get_final_state()` implementation doesn't match trial expectations

**Solutions:**
- Review `get_final_state()` logic to ensure it captures:
  - Orders in the correct state
  - Payments matching order completion
  - Shipments matching completed orders
- Check that the threshold comparison logic in `_compute_invariant_violations()` matches your DB schema
- Increase threshold temporarily while debugging DB state logic

## Advanced: Custom OTL Extensions

You can extend the OTL system for custom metrics:

```python
class CustomOTLMonitor(OTLMonitor):
    def compute_block_metrics(self, block_num, block_results):
        metrics = super().compute_block_metrics(block_num, block_results)
        
        # Add custom metric: memory usage
        memory_usage = self._compute_memory_usage(block_results)
        if memory_usage > MEMORY_THRESHOLD:
            metrics.threshold_violations.append(
                f"Memory usage ({memory_usage}MB) exceeds threshold"
            )
        
        return metrics
    
    def _compute_memory_usage(self, results):
        # Custom logic
        pass
```

## Integration with CI/CD

```yaml
# GitHub Actions example
name: Run Experiment with OTL

on: [push, pull_request]

jobs:
  experiment:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v2
      - name: Run experiment with OTL
        run: |
          OTL_ENABLED=true \
          OTL_BLOCK_SIZE=20 \
          python exp_runner.py --trials 200 --concurrency 8
      - name: Check OTL report
        run: |
          python -c "
          import json
          with open('results/log_telemetry.json') as f:
              results = json.load(f)
          if results[0]['summary']['otl_report']['should_interrupt']:
              exit(1)
          "
```

## Summary

The OTL monitoring module provides:

✅ **Automatic health checks** during experiment execution  
✅ **Block-based metrics** for stable estimates  
✅ **Configurable thresholds** for different scenarios  
✅ **Early interruption** to save time and resources  
✅ **Comprehensive reporting** for analysis  
✅ **Production-ready** with proper error handling  

Use it to catch performance regressions early and build more robust experiments!

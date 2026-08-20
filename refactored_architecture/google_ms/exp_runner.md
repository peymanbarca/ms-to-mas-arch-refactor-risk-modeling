

## Sample run end-to-end experiment runner script

```bash
    python3 -m refactored_architecture.google_ms.exp_runner --trials=20 --concurrency=10
```

### Sample run the automatic end-to-end experiment runner (used in ablation study runner experiment shell scripts)

```bash
    python3 python3 -m refactored_architecture.google_ms.exp_runner_auto ranked_sample full 1 all_services some_agent 0.9 0 0.02 full_governance order_service _ true Ranked 0 llama3.2:3b 10 20
```
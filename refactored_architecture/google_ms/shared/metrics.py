"""
Utility module for LLM metrics collection and reporting.

All agentic services use this to track and report token consumption.
"""

from __future__ import annotations

import datetime
from typing import Any, Dict

from . import demo_pb2


def build_llm_metrics(
    total_input_tokens: int,
    total_output_tokens: int,
    total_llm_calls: int,
) -> demo_pb2.LLMMetrics:
    """
    Build an LLMMetrics protobuf message with token tracking.
    
    Args:
        total_input_tokens: Total input tokens consumed across all LLM calls
        total_output_tokens: Total output tokens consumed across all LLM calls
        total_llm_calls: Total number of LLM invocations
        
    Returns:
        Populated LLMMetrics protobuf message
    """
    now = datetime.datetime.now(tz=datetime.timezone.utc)
    timestamp = now.isoformat()
    
    return demo_pb2.LLMMetrics(
        total_input_tokens=total_input_tokens,
        total_output_tokens=total_output_tokens,
        total_llm_calls=total_llm_calls,
        timestamp=timestamp,
    )


def metrics_to_dict(metrics: demo_pb2.LLMMetrics) -> Dict[str, Any]:
    """Convert LLMMetrics protobuf to dict (for JSON responses)."""
    if not metrics:
        return None
    return {
        "total_input_tokens": metrics.total_input_tokens,
        "total_output_tokens": metrics.total_output_tokens,
        "total_llm_calls": metrics.total_llm_calls,
        "timestamp": metrics.timestamp,
    }


def metrics_summary(metrics: demo_pb2.LLMMetrics) -> str:
    """Return a human-readable summary of metrics."""
    if not metrics:
        return "no metrics"
    total_tokens = metrics.total_input_tokens + metrics.total_output_tokens
    return (
        f"LLM calls={metrics.total_llm_calls} "
        f"in={metrics.total_input_tokens} "
        f"out={metrics.total_output_tokens} "
        f"total={total_tokens}"
    )

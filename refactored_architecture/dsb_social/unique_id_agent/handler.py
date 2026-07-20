"""
UniqueIdHandler (Agent version) — Thrift interface UNCHANGED.

The only difference from the original handler.py is that ComposeUniqueId
now drives the LangGraph UniqueIdAgent instead of calling
SnowflakeGenerator.next_id() directly.

Flow per request:
  1. Thrift call arrives → ComposeUniqueId()
  2. Build initial agent state from (req_id, post_type, machine_id, ts, seq)
     by calling generator.next_inputs()
  3. Run the LangGraph graph (gather_inputs → reason_unique_id → validate_output)
  4. Return state["unique_id"] to the Thrift caller
  5. Log LLM token metrics

The Thrift interface, error codes, and OpenTracing span are identical to
the original handler.
"""

import asyncio
import logging
import time

import opentracing
from opentracing.ext import tags as ot_tags
from opentracing.propagation import Format

from ms_baseline.dsb_social.gen_py.social_network import UniqueIdService
from ms_baseline.dsb_social.gen_py.social_network.ttypes import ServiceException, ErrorCode
from .snowflake_agent import AgentSnowflakeGenerator
from .agent import build_unique_id_agent

logger = logging.getLogger("unique-id-agent.handler")


class UniqueIdHandler(UniqueIdService.Iface):
    """
    Thrift handler — same interface as the original.

    Parameters
    ----------
    generator : AgentSnowflakeGenerator
    tracer    : opentracing.Tracer
    """

    def __init__(self, generator: AgentSnowflakeGenerator, tracer: opentracing.Tracer):
        self._gen    = generator
        self._tracer = tracer
        self._graph  = build_unique_id_agent()

    # ------------------------------------------------------------------
    # Thrift interface — unchanged signature
    # ------------------------------------------------------------------

    def ComposeUniqueId(self, req_id: int, post_type, carrier: dict) -> int:
        """Generate and return a unique Snowflake ID via the LangGraph agent."""

        # ---- OpenTracing span (identical to original) ----
        parent_ctx = None
        try:
            parent_ctx = self._tracer.extract(Format.TEXT_MAP, carrier)
        except (opentracing.InvalidCarrierException,
                opentracing.SpanContextCorruptedException):
            pass

        with self._tracer.start_active_span(
            "ComposeUniqueId",
            child_of=parent_ctx,
            tags={
                ot_tags.SPAN_KIND: ot_tags.SPAN_KIND_RPC_SERVER,
                "req_id":    req_id,
                "post_type": str(post_type),
            },
        ) as scope:
            span = scope.span
            t1 = time.time()
            try:
                # ---- Step 1: get raw inputs from the generator ----
                timestamp_ms, machine_id, sequence = self._gen.next_inputs()

                # ---- Step 2: build agent initial state ----
                initial_state = {
                    "req_id":          req_id,
                    "post_type":       post_type,
                    "machine_id":      machine_id,
                    "timestamp_ms":    timestamp_ms,
                    "sequence":        sequence,
                    "unique_id":       None,
                    "total_input_tokens":  0,
                    "total_output_tokens": 0,
                    "total_llm_calls":     0,
                    "fallback_used":       False,
                }

                # ---- Step 3: run the LangGraph agent ----
                # The Thrift server is TThreadedServer (thread-per-request).
                # We run the async graph in a fresh event loop per thread.
                out = asyncio.run(self._graph.ainvoke(initial_state))

                unique_id = out["unique_id"]

                # ---- Step 4: log LLM metrics ----
                logger.info(
                    "ComposeUniqueId req_id=%d post_type=%s -> %d  "
                    "llm_calls=%d in_tokens=%d out_tokens=%d fallback=%s",
                    req_id, post_type, unique_id,
                    out["total_llm_calls"],
                    out["total_input_tokens"],
                    out["total_output_tokens"],
                    out["fallback_used"],
                )
                t2 = time.time()
                logger.info(
                    f"[handler] req_id={req_id} unique_id={unique_id} "
                    f"llm_calls={out['total_llm_calls']} "
                    f"in_tokens={out['total_input_tokens']} "
                    f"out_tokens={out['total_output_tokens']} "
                    f"fallback={out['fallback_used']} "
                    f"took={t2 - t1:.3f}s"
                )

                span.set_tag("unique_id",      unique_id)
                span.set_tag("llm_calls",      out["total_llm_calls"])
                span.set_tag("fallback_used",  out["fallback_used"])

                return unique_id

            except RuntimeError as exc:
                logger.error("Clock skew in UniqueId agent: %s", exc)
                span.set_tag("error", True)
                raise ServiceException(
                    errorCode=ErrorCode.SE_THRIFT_HANDLER_ERROR,
                    message=f"UniqueId generation failed: {exc}",
                )
            except Exception as exc:
                logger.exception("Unexpected error in ComposeUniqueId agent")
                span.set_tag("error", True)
                raise ServiceException(
                    errorCode=ErrorCode.SE_THRIFT_HANDLER_ERROR,
                    message=str(exc),
                )

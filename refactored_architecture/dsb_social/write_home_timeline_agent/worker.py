"""
worker.py — WriteHomeTimeline message consumer worker.

This version uses the LangGraph agent for:
  decode_message -> reason_validate_message -> validate_decision -> forward_to_home_timeline

The worker no longer calls HomeTimelineService directly.
The graph performs the downstream service call.
"""

import asyncio
import logging
import time

import opentracing
from opentracing.propagation import Format

from ms_baseline.dsb_social.gen_py.social_network.ttypes import ServiceException
from .message import decode, WriteHomeTimelineMessage
from .agent import build_write_home_timeline_agent, WriteHomeTimelineAgentState
from .thrift_pool import ThriftClientPool

logger = logging.getLogger("write-home-timeline-agent.worker")


class MessageWorker:
    """
    Processes a single WriteHomeTimeline AMQP delivery through the agent graph.

    Parameters
    ----------
    home_timeline_pool : ThriftClientPool for HomeTimelineService
    tracer             : opentracing.Tracer
    """

    def __init__(
        self,
        home_timeline_pool: ThriftClientPool,
        tracer: opentracing.Tracer,
    ):
        self._pool = home_timeline_pool
        self._tracer = tracer
        self._graph = build_write_home_timeline_agent(home_timeline_pool)

    def process(self, body: bytes) -> bool:
        """
        Execute the graph for one AMQP delivery.

        Returns
        -------
        bool
            True  -> approved and forwarded successfully
            False -> rejected by validation (intentional skip, not an error)

        Raises
        ------
        ServiceException / Exception
            For downstream failures or unexpected runtime errors.
        """
        # Decode once here only for tracing context and logging.
        # The graph will decode again as part of its own deterministic node.
        try:
            preview = decode(body)
        except Exception:
            preview = None

        parent_ctx = None
        if preview is not None:
            try:
                parent_ctx = self._tracer.extract(Format.TEXT_MAP, preview.carrier)
            except Exception:
                parent_ctx = None

        with self._tracer.start_active_span(
            "WriteHomeTimeline-consumer",
            child_of=parent_ctx,
        ) as scope:
            span = scope.span
            t1 = time.time()
            if preview is not None:
                span.set_tag("req_id", preview.req_id)
                span.set_tag("post_id", preview.post_id)
                span.set_tag("user_id", preview.user_id)

            out_carrier = {}
            try:
                self._tracer.inject(span.context, Format.TEXT_MAP, out_carrier)
            except Exception:
                out_carrier = {}
            
            logger.info(
                "WriteHomeTimeline consumer starting for msg: req_id=%s post_id=%s user_id=%s",
                preview.req_id if preview else None,
                preview.post_id if preview else None,
                preview.user_id if preview else None,
            )

            initial: WriteHomeTimelineAgentState = {
                "body": body,

                "decode_ok": False,
                "decode_error": None,
                "req_id": None,
                "post_id": None,
                "user_id": None,
                "timestamp": None,
                "user_mentions_id": None,
                "carrier": None,

                "llm_approved": None,
                "llm_reason": None,
                "llm_cleaned_mentions": None,

                "approved": None,
                "validated_mentions": None,

                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "total_llm_calls": 0,
                "fallback_used": False,
            }

            try:
                out = asyncio.run(self._graph.ainvoke(initial))
            except ServiceException:
                span.set_tag("error", True)
                raise
            except Exception as exc:
                logger.error("WriteHomeTimeline graph failed: %s", exc)
                span.set_tag("error", True)
                span.log_kv({"event": "graph_error", "message": str(exc)})
                raise

            approved = bool(out.get("approved", False))

            logger.info(
                "WriteHomeTimeline consumer graph done req_id=%s post_id=%s approved=%s",
                out.get("req_id"),
                out.get("post_id"),
                approved,
            )

            span.set_tag("approved", approved)
            span.set_tag("fallback", out.get("fallback_used", False))

            t2 = time.time()
            logger.info(
                "WriteHomeTimeline consumer req_id=%s post_id=%s completed in %.3f seconds",
                out.get("req_id"),
                out.get("post_id"),
                t2 - t1,
            )
            
            return approved

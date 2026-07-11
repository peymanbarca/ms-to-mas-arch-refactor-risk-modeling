"""
HomeTimelineHandler — Python port of HomeTimelineHandler.h

Implements the Thrift HomeTimelineService.Iface interface.

Graph-backed methods
--------------------
WriteHomeTimeline(req_id, post_id, user_id, timestamp, user_mentions_id, carrier)
ReadHomeTimeline(req_id, user_id, start, stop, carrier) -> list<Post>

Storage
-------
Redis sorted sets only (no MongoDB):
  Key   = "home-timeline:<user_id>"
  Member = str(post_id)
  Score  = timestamp (milliseconds)

Downstream dependencies
-----------------------
  SocialGraphService  — used by the write graph to fetch followers
  PostStorageService  — used by the read graph to hydrate post_ids
"""

import logging
from typing import Any
import asyncio

import opentracing
from opentracing.ext import tags as ot_tags
from opentracing.propagation import Format
import redis

from ms_baseline.dsb_social.gen_py.social_network import HomeTimelineService
from ms_baseline.dsb_social.gen_py.social_network.ttypes import (
    ServiceException,
    ErrorCode,
)
from .thrift_pool import ThriftClientPool
from .agent import (
    build_write_agent,
    build_read_agent,
    WriteHomeTimelineState,
    ReadHomeTimelineState,
)

logger = logging.getLogger("home-timeline-service")

_REDIS_KEY_PREFIX = "home-timeline:"   # home-timeline:<user_id>


class HomeTimelineHandler(HomeTimelineService.Iface):
    """
    Parameters
    ----------
    redis_client        : redis.Redis
    post_storage_pool   : ThriftClientPool for PostStorageService
    social_graph_pool   : ThriftClientPool for SocialGraphService
    tracer              : opentracing.Tracer
    num_workers         : retained for compatibility; graphs run synchronously
    """

    def __init__(
        self,
        redis_client: redis.Redis,
        post_storage_pool: ThriftClientPool,
        social_graph_pool: ThriftClientPool,
        tracer: opentracing.Tracer,
        num_workers: int = 16,
    ):
        self._redis = redis_client
        self._post_pool = post_storage_pool
        self._graph_pool = social_graph_pool
        self._tracer = tracer
        self._num_workers = num_workers

        # Compile graphs once.
        self._write_graph = build_write_agent(self._redis, self._graph_pool)
        self._read_graph = build_read_agent(self._redis, self._post_pool)

    # ------------------------------------------------------------------
    # WriteHomeTimeline
    # ------------------------------------------------------------------

    def WriteHomeTimeline(
        self,
        req_id: int,
        post_id: int,
        user_id: int,
        timestamp: int,
        user_mentions_id: list,
        carrier: dict,
    ) -> None:
        """
        Fan out the post to followers + mentioned users via the write graph.
        """
        parent_ctx = self._extract_ctx(carrier)

        with self._tracer.start_active_span(
            "WriteHomeTimeline",
            child_of=parent_ctx,
            tags={
                ot_tags.SPAN_KIND: ot_tags.SPAN_KIND_RPC_SERVER,
                "req_id": req_id,
                "user_id": user_id,
                "post_id": post_id,
                "timestamp": timestamp,
            },
        ) as scope:
            span = scope.span

            initial: WriteHomeTimelineState = {
                "req_id": req_id,
                "post_id": post_id,
                "user_id": user_id,
                "timestamp": timestamp,
                "user_mentions_id": list(user_mentions_id or []),
                "followers": [],
                "llm_targets": None,
                "llm_excluded_author": None,
                "final_targets": [],
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "total_llm_calls": 0,
                "fallback_used": False,
            }

            out = self._run_graph(
                self._write_graph,
                initial,
                span,
                op_name="WriteHomeTimeline",
                req_id=req_id,
            )

            if not out.get("final_targets") and not out.get("fallback_used", False):
                # Empty target set is not an error; the graph may legitimately
                # produce no recipients.
                logger.info(
                    "WriteHomeTimeline req_id=%d post_id=%d -> no targets",
                    req_id, post_id,
                )

            self._log_metrics("WriteHomeTimeline", req_id, out, span)

            # Hard fail if the graph rejected the write.
            # This keeps service semantics explicit for callers.
            if out.get("final_targets") is None:
                span.set_tag("error", True)
                raise ServiceException(
                    errorCode=ErrorCode.SE_THRIFT_HANDLER_ERROR,
                    message="WriteHomeTimeline agent failed to produce targets",
                )

            logger.debug(
                "WriteHomeTimeline req_id=%d post_id=%d user_id=%d targets=%d",
                req_id,
                post_id,
                user_id,
                len(out.get("final_targets") or []),
            )

    # ------------------------------------------------------------------
    # ReadHomeTimeline
    # ------------------------------------------------------------------

    def ReadHomeTimeline(
        self,
        req_id: int,
        user_id: int,
        start: int,
        stop: int,
        carrier: dict,
    ) -> list:
        """
        Return posts [start, stop) from the user's home timeline using the read graph.
        """
        parent_ctx = self._extract_ctx(carrier)

        with self._tracer.start_active_span(
            "ReadHomeTimeline",
            child_of=parent_ctx,
            tags={
                ot_tags.SPAN_KIND: ot_tags.SPAN_KIND_RPC_SERVER,
                "req_id": req_id,
                "user_id": user_id,
                "start": start,
                "stop": stop,
            },
        ) as scope:
            span = scope.span

            initial: ReadHomeTimelineState = {
                "req_id": req_id,
                "user_id": user_id,
                "start": start,
                "stop": stop,
                "all_post_ids": [],
                "all_timestamps": [],
                "llm_page_ids": None,
                "final_page_ids": [],
                "posts": [],
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "total_llm_calls": 0,
                "fallback_used": False,
            }

            out = self._run_graph(
                self._read_graph,
                initial,
                span,
                op_name="ReadHomeTimeline",
                req_id=req_id,
            )

            posts = out.get("posts") or []
            span.set_tag("post_count", len(posts))

            self._log_metrics("ReadHomeTimeline", req_id, out, span)
            logger.debug(
                "ReadHomeTimeline req_id=%d user_id=%d [%d:%d] -> %d posts",
                req_id, user_id, start, stop, len(posts),
            )
            return posts

    # ==================================================================
    # Private — graph invocation
    # ==================================================================

    # def _run_graph(self, graph: Any, initial: dict, span, op_name: str, req_id: int) -> dict:
    #     """Invoke a compiled LangGraph and translate failures into ServiceException."""
    #     try:
    #         return graph.invoke(initial)
    #     except ServiceException:
    #         span.set_tag("error", True)
    #         raise
    #     except Exception as exc:
    #         logger.exception("%s graph failed req_id=%d", op_name, req_id)
    #         span.set_tag("error", True)
    #         raise ServiceException(
    #             errorCode=ErrorCode.SE_THRIFT_HANDLER_ERROR,
    #             message=f"{op_name} agent failed: {exc}",
    #         )

    def _run_graph(
        self,
        graph: Any,
        initial: dict,
        span,
        op_name: str,
        req_id: int,
    ) -> dict:
        """Invoke async LangGraph from synchronous Thrift handler."""
        try:
            return asyncio.run(graph.ainvoke(initial))
        except ServiceException:
            span.set_tag("error", True)
            raise
        except Exception as exc:
            logger.exception("%s graph failed req_id=%d", op_name, req_id)
            span.set_tag("error", True)
            raise ServiceException(
                errorCode=ErrorCode.SE_THRIFT_HANDLER_ERROR,
                message=f"{op_name} agent failed: {exc}",
            )
            
    # ==================================================================
    # Private — logging + tracing helpers
    # ==================================================================

    def _log_metrics(self, op: str, req_id: int, out: dict, span) -> None:
        in_tok = out.get("total_input_tokens", 0)
        out_tok = out.get("total_output_tokens", 0)
        calls = out.get("total_llm_calls", 0)
        fallback = out.get("fallback_used", False)

        logger.info(
            "%s req_id=%d llm_calls=%d in=%d out=%d fallback=%s",
            op, req_id, calls, in_tok, out_tok, fallback,
        )
        print(
            f"[handler:{op}] req_id={req_id} llm_calls={calls} "
            f"in_tokens={in_tok} out_tokens={out_tok} fallback={fallback}"
        )
        span.set_tag("llm_calls", calls)
        span.set_tag("fallback", fallback)

    def _extract_ctx(self, carrier: dict):
        try:
            return self._tracer.extract(Format.TEXT_MAP, carrier)
        except Exception:
            return None
"""
UserTimelineHandler — Python port of UserTimelineHandler.h

Implements the Thrift UserTimelineService.Iface interface.

Graph-backed methods
---------------------
WriteUserTimeline(req_id, post_id, user_id, timestamp, carrier)
ReadUserTimeline(req_id, user_id, start, stop, carrier) -> list<Post>

Storage
-------
Redis sorted set:
  Key   = "user-timeline:<user_id>"
  Member = str(post_id)
  Score  = timestamp (milliseconds)

MongoDB (db="user-timeline", collection="user-timeline"):
  One document per user:
  {
    "user_id": i64,
    "posts": [
      {"post_id": i64, "timestamp": i64},
      ...
    ]
  }

Downstream dependency:
  PostStorageService (client pool) — called by the read graph to hydrate
  post_ids into full Post structs.
"""

import asyncio
import logging
from typing import Any

import opentracing
from opentracing.ext import tags as ot_tags
from opentracing.propagation import Format
from pymongo import MongoClient
import redis

from ms_baseline.dsb_social.gen_py.social_network import UserTimelineService
from ms_baseline.dsb_social.gen_py.social_network.ttypes import (
    ServiceException,
    ErrorCode,
)
from .thrift_pool import ThriftClientPool
from .agent import (
    build_write_timeline_agent,
    build_read_timeline_agent,
    WriteTimelineState,
    ReadTimelineState,
)

logger = logging.getLogger("user-timeline-service")

_REDIS_KEY_PREFIX = "user-timeline:"   # user-timeline:<user_id>


class UserTimelineHandler(UserTimelineService.Iface):
    """
    Parameters
    ----------
    mongo_client       : pymongo.MongoClient
    mongo_db           : str   e.g. "user-timeline"
    mongo_col          : str   e.g. "user-timeline"
    redis_client       : redis.Redis
    post_storage_pool  : ThriftClientPool for PostStorageService
    tracer             : opentracing.Tracer
    num_workers        : kept for compatibility; graph execution is synchronous
    """

    def __init__(
        self,
        mongo_client: MongoClient,
        mongo_db: str,
        mongo_col: str,
        redis_client: redis.Redis,
        post_storage_pool: ThriftClientPool,
        tracer: opentracing.Tracer,
        num_workers: int = 8,
    ):
        self._col = mongo_client[mongo_db][mongo_col]
        self._redis = redis_client
        self._post_pool = post_storage_pool
        self._tracer = tracer

        # Compile graphs once.
        self._write_graph = build_write_timeline_agent(self._redis, self._col)
        self._read_graph = build_read_timeline_agent(
            self._redis,
            self._col,
            self._post_pool,
        )

        # Unique index on user_id — mirrors C++ MongoDB setup
        self._col.create_index("user_id", unique=True, background=True)

    # ------------------------------------------------------------------
    # WriteUserTimeline
    # ------------------------------------------------------------------

    def WriteUserTimeline(
        self,
        req_id: int,
        post_id: int,
        user_id: int,
        timestamp: int,
        carrier: dict,
    ) -> None:
        """
        Record a new post in the user's personal timeline using the write graph.

        The graph performs:
          fetch_existing -> reason_write -> validate_write -> apply_write
        """
        parent_ctx = self._extract_ctx(carrier)

        with self._tracer.start_active_span(
            "WriteUserTimeline",
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

            initial: WriteTimelineState = {
                "req_id": req_id,
                "post_id": post_id,
                "user_id": user_id,
                "timestamp": timestamp,
                "already_exists": False,
                "timeline_size": 0,
                "llm_approved": None,
                "llm_reason": None,
                "approved": None,
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "total_llm_calls": 0,
                "fallback_used": False,
            }

            out = self._run_graph(
                self._write_graph,
                initial,
                span,
                op_name="WriteUserTimeline",
                req_id=req_id,
            )

            if not out.get("approved", False):
                span.set_tag("error", True)
                raise ServiceException(
                    errorCode=ErrorCode.SE_THRIFT_HANDLER_ERROR,
                    message=out.get("llm_reason") or "WriteUserTimeline request rejected",
                )

            self._log_metrics("WriteUserTimeline", req_id, out, span)

            logger.debug(
                "WriteUserTimeline req_id=%d user_id=%d post_id=%d",
                req_id, user_id, post_id,
            )

    # ------------------------------------------------------------------
    # ReadUserTimeline
    # ------------------------------------------------------------------

    def ReadUserTimeline(
        self,
        req_id: int,
        user_id: int,
        start: int,
        stop: int,
        carrier: dict,
    ) -> list:
        """
        Return posts [start, stop) from the user's personal timeline,
        most-recent first, using the read graph.

        The graph performs:
          fetch_post_ids -> reason_paginate -> validate_paginate -> hydrate_posts
        """
        parent_ctx = self._extract_ctx(carrier)

        with self._tracer.start_active_span(
            "ReadUserTimeline",
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

            initial: ReadTimelineState = {
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
                op_name="ReadUserTimeline",
                req_id=req_id,
            )

            posts = out.get("posts") or []
            span.set_tag("post_count", len(posts))
            self._log_metrics("ReadUserTimeline", req_id, out, span)

            logger.debug(
                "ReadUserTimeline req_id=%d user_id=%d start=%d stop=%d -> %d posts",
                req_id, user_id, start, stop, len(posts),
            )
            return posts

    # ==================================================================
    # Private — graph invocation
    # ==================================================================

    def _run_graph(self, graph: Any, initial: dict, span, op_name: str, req_id: int) -> dict:
        """
        Invoke a compiled LangGraph and translate failures into ServiceException.
        """
        try:
            out = asyncio.run(graph.ainvoke(initial))
            return out
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

    def _inject_ctx(self, span) -> dict:
        carrier = {}
        try:
            self._tracer.inject(span.context, Format.TEXT_MAP, carrier)
        except Exception:
            pass
        return carrier
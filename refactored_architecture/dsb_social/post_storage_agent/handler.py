"""
PostStorageHandler — Python port of PostStorageHandler.h

Implements the Thrift PostStorageService.Iface interface.

Graph-backed methods
--------------------
StorePost(req_id, post, carrier)  -> uses store_agent
ReadPost(req_id, post_id, carrier) -> uses read_agent
ReadPosts(req_id, post_ids, carrier) -> uses read_batch_agent

Storage
-------
MongoDB:
  db="post", collection="post"
  Schema: { post_id, creator, req_id, text, user_mentions, media, urls,
            timestamp, post_type }
  Index: unique on post_id

Redis:
  key   = str(post_id)
  value = JSON string of the post document
"""

import asyncio
import logging
from typing import Any

import opentracing
from opentracing.ext import tags as ot_tags
from opentracing.propagation import Format
from pymongo import MongoClient
import redis

from ms_baseline.dsb_social.gen_py.social_network import PostStorageService
from ms_baseline.dsb_social.gen_py.social_network.ttypes import (
    Post,
    ServiceException,
    ErrorCode,
)
from .post_serializer import post_to_dict, dict_to_post, post_to_json, json_to_post
from .agent import (
    build_store_agent,
    build_read_agent,
    build_read_batch_agent,
    StorePostState,
    ReadPostState,
    ReadBatchState,
)

logger = logging.getLogger("post-storage-service")

_CACHE_TTL = 0   # no expiry — match original behaviour


class PostStorageHandler(PostStorageService.Iface):
    """
    Parameters
    ----------
    mongo_client  : pymongo.MongoClient
    mongo_db      : str   e.g. "post"
    mongo_col     : str   e.g. "post"
    redis_client  : redis.Redis
    tracer        : opentracing.Tracer
    num_workers   : retained for compatibility; graph invocation is synchronous
    """

    def __init__(
        self,
        mongo_client: MongoClient,
        mongo_db: str,
        mongo_col: str,
        redis_client: redis.Redis,
        tracer: opentracing.Tracer,
        num_workers: int = 8,
    ):
        self._col = mongo_client[mongo_db][mongo_col]
        self._redis = redis_client
        self._tracer = tracer

        # Unique index on post_id — mirrors C++ MongoDB setup
        self._col.create_index("post_id", unique=True, background=True)

        # Compile graphs once
        self._store_graph = build_store_agent(self._redis, self._col)
        self._read_graph = build_read_agent(self._redis, self._col)
        self._read_batch_graph = build_read_batch_agent(self._redis, self._col)

    # ------------------------------------------------------------------
    # StorePost
    # ------------------------------------------------------------------

    def StorePost(self, req_id: int, post: Post, carrier: dict) -> None:
        """
        Persist a post using the store graph:
          reason_validate_post -> validate_store -> persist_post
        """
        parent_ctx = self._extract_ctx(carrier)

        with self._tracer.start_active_span(
            "StorePost",
            child_of=parent_ctx,
            tags={
                ot_tags.SPAN_KIND: ot_tags.SPAN_KIND_RPC_SERVER,
                "req_id": req_id,
                "post_id": post.post_id,
            },
        ) as scope:
            span = scope.span

            initial: StorePostState = {
                "req_id": req_id,
                "post": post,
                "llm_valid": None,
                "llm_issues": None,
                "valid": None,
                "issues": [],
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "total_llm_calls": 0,
                "fallback_used": False,
            }

            out = self._run_graph(
                self._store_graph,
                initial,
                span,
                op_name="StorePost",
                req_id=req_id,
            )

            self._log_metrics("StorePost", req_id, out, span)
            logger.debug("StorePost req_id=%d post_id=%d stored", req_id, post.post_id)

    # ------------------------------------------------------------------
    # ReadPost
    # ------------------------------------------------------------------

    def ReadPost(self, req_id: int, post_id: int, carrier: dict) -> Post:
        """
        Return a single post by post_id using the read graph:
          check_cache -> reason_cache_decision -> validate_cache -> fetch_if_needed
        """
        parent_ctx = self._extract_ctx(carrier)

        with self._tracer.start_active_span(
            "ReadPost",
            child_of=parent_ctx,
            tags={
                ot_tags.SPAN_KIND: ot_tags.SPAN_KIND_RPC_SERVER,
                "req_id": req_id,
                "post_id": post_id,
            },
        ) as scope:
            span = scope.span

            initial: ReadPostState = {
                "req_id": req_id,
                "post_id": post_id,
                "cached_json": None,
                "cached_post": None,
                "llm_use_cache": None,
                "llm_reason": None,
                "use_cache": None,
                "post": None,
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "total_llm_calls": 0,
                "fallback_used": False,
            }

            out = self._run_graph(
                self._read_graph,
                initial,
                span,
                op_name="ReadPost",
                req_id=req_id,
            )

            post = out.get("post")
            if post is None:
                span.set_tag("error", True)
                raise ServiceException(
                    errorCode=ErrorCode.SE_THRIFT_HANDLER_ERROR,
                    message=f"Post not found: {post_id}",
                )

            self._log_metrics("ReadPost", req_id, out, span)
            return post

    # ------------------------------------------------------------------
    # ReadPosts
    # ------------------------------------------------------------------

    def ReadPosts(self, req_id: int, post_ids: list, carrier: dict) -> list:
        """
        Return a list of posts in the same order as input post_ids using:
          check_cache_batch -> reason_batch_complete -> validate_batch
          -> fetch_missing -> assemble_ordered
        """
        parent_ctx = self._extract_ctx(carrier)

        with self._tracer.start_active_span(
            "ReadPosts",
            child_of=parent_ctx,
            tags={
                ot_tags.SPAN_KIND: ot_tags.SPAN_KIND_RPC_SERVER,
                "req_id": req_id,
                "post_count": len(post_ids),
            },
        ) as scope:
            span = scope.span

            if not post_ids:
                return []

            initial: ReadBatchState = {
                "req_id": req_id,
                "post_ids": list(post_ids),
                "cache_hits": {},
                "missing_ids": [],
                "llm_ids_to_fetch": None,
                "ids_to_fetch": [],
                "mongo_hits": {},
                "posts": [],
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "total_llm_calls": 0,
                "fallback_used": False,
            }

            out = self._run_graph(
                self._read_batch_graph,
                initial,
                span,
                op_name="ReadPosts",
                req_id=req_id,
            )

            posts = out.get("posts") or []
            self._log_metrics("ReadPosts", req_id, out, span)
            logger.debug("ReadPosts req_id=%d returned %d posts", req_id, len(posts))
            return posts

    # ==================================================================
    # Private — graph invocation
    # ==================================================================

    def _run_graph(self, graph: Any, initial: dict, span, op_name: str, req_id: int) -> dict:
        """Invoke a compiled LangGraph and translate failures into ServiceException."""
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

    def _inject_ctx(self, span) -> dict:
        carrier = {}
        try:
            self._tracer.inject(span.context, Format.TEXT_MAP, carrier)
        except Exception:
            pass
        return carrier
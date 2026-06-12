"""
MediaHandler (Agent version) — Thrift interface UNCHANGED.

ComposeMedia drives the MediaAgent LangGraph graph.
The final list[Media] is assembled by merging:
  - cached_results  (from check_cache node)
  - validated_types (from validate_output node, persisted by persist node)
in the original input order.
"""

import asyncio
import logging

import opentracing
from opentracing.ext import tags as ot_tags
from opentracing.propagation import Format
from pymongo import MongoClient
import redis

from ms_baseline.dsb_social.gen_py.social_network import MediaService
from ms_baseline.dsb_social.gen_py.social_network.ttypes import Media, ServiceException, ErrorCode
from .agent import build_media_agent, MediaAgentState

logger = logging.getLogger("media-agent.handler")


class MediaHandler(MediaService.Iface):
    """
    Parameters
    ----------
    mongo_client : pymongo.MongoClient
    mongo_db     : str
    mongo_col    : str
    redis_client : redis.Redis
    tracer       : opentracing.Tracer
    """

    def __init__(
        self,
        mongo_client: MongoClient,
        mongo_db: str,
        mongo_col: str,
        redis_client: redis.Redis,
        tracer: opentracing.Tracer,
    ):
        self._col    = mongo_client[mongo_db][mongo_col]
        self._redis  = redis_client
        self._tracer = tracer

        # Unique index on media_id — unchanged from original
        self._col.create_index("media_id", unique=True, background=True)

        # Compile agent graph once (captures redis + mongo via closures)
        self._graph = build_media_agent(redis_client, self._col)

    # ------------------------------------------------------------------
    # Thrift interface — unchanged signature
    # ------------------------------------------------------------------

    def ComposeMedia(
        self,
        req_id: int,
        media_types: list,
        media_ids: list,
        carrier: dict,
    ) -> list:
        """
        Validate and persist media items via the LangGraph agent.

        Returns list[Media] in the same order as the input lists.
        Raises ServiceException on length mismatch (unchanged from original).
        """
        parent_ctx = self._extract_ctx(carrier)

        with self._tracer.start_active_span(
            "ComposeMedia",
            child_of=parent_ctx,
            tags={
                ot_tags.SPAN_KIND: ot_tags.SPAN_KIND_RPC_SERVER,
                "req_id":      req_id,
                "media_count": len(media_ids),
            },
        ) as scope:
            span = scope.span

            # ---- validation (identical to original) ----
            if len(media_types) != len(media_ids):
                msg = (
                    f"media_types length ({len(media_types)}) != "
                    f"media_ids length ({len(media_ids)})"
                )
                logger.error("ComposeMedia req_id=%d: %s", req_id, msg)
                span.set_tag("error", True)
                raise ServiceException(
                    errorCode=ErrorCode.SE_THRIFT_HANDLER_ERROR,
                    message=msg,
                )

            if not media_ids:
                return []

            # ---- build initial agent state ----
            initial: MediaAgentState = {
                "req_id":       req_id,
                "media_ids":    list(media_ids),
                "media_types":  list(media_types),
                "cached_results":        [],
                "uncached_ids":          [],
                "uncached_types":        [],
                "llm_validated_types":   None,
                "validated_types":       [],
                "total_input_tokens":    0,
                "total_output_tokens":   0,
                "total_llm_calls":       0,
                "fallback_used":         False,
            }

            # ---- run agent ----
            try:
                out = asyncio.run(self._graph.ainvoke(initial))
            except Exception as exc:
                logger.error(
                    "MediaAgent graph failed req_id=%d: %s", req_id, exc
                )
                span.set_tag("error", True)
                raise ServiceException(
                    errorCode=ErrorCode.SE_THRIFT_HANDLER_ERROR,
                    message=f"MediaAgent failed: {exc}",
                )

            # ---- assemble final list[Media] in original input order ----
            # Build lookup: media_id → final validated type
            # (from both cached and freshly processed items)
            type_by_id: dict[int, str] = {}

            for item in out["cached_results"]:
                type_by_id[item["media_id"]] = item["media_type"]

            for mid, vtype in zip(out["uncached_ids"], out["validated_types"]):
                type_by_id[mid] = vtype

            result = [
                Media(media_id=mid, media_type=type_by_id[mid])
                for mid in media_ids
            ]

            # ---- log metrics ----
            logger.info(
                "ComposeMedia req_id=%d count=%d llm_calls=%d "
                "in_tokens=%d out_tokens=%d fallback=%s",
                req_id,
                len(result),
                out["total_llm_calls"],
                out["total_input_tokens"],
                out["total_output_tokens"],
                out["fallback_used"],
            )
            print(
                f"[handler] req_id={req_id} count={len(result)} "
                f"llm_calls={out['total_llm_calls']} "
                f"in_tokens={out['total_input_tokens']} "
                f"out_tokens={out['total_output_tokens']} "
                f"fallback={out['fallback_used']}"
            )

            span.set_tag("llm_calls",   out["total_llm_calls"])
            span.set_tag("fallback",    out["fallback_used"])
            return result

    # ------------------------------------------------------------------
    # Tracing helper
    # ------------------------------------------------------------------

    def _extract_ctx(self, carrier: dict):
        try:
            return self._tracer.extract(Format.TEXT_MAP, carrier)
        except Exception:
            return None
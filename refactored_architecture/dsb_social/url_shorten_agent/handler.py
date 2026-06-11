"""
UrlShortenHandler (Agent version) — Thrift interface UNCHANGED.

ComposeUrls  → runs build_compose_url_agent graph per URL (sequential across
               the batch; LLM call is inside the graph)
GetExtendedUrls → runs build_expand_url_agent graph per URL (no LLM)

Token metrics are accumulated across all URLs in a ComposeUrls batch and
logged per-request with fallback statistics.
"""

import asyncio
import logging

import opentracing
from opentracing.ext import tags as ot_tags
from opentracing.propagation import Format
from pymongo import MongoClient
import redis

from ms_baseline.dsb_social.gen_py.social_network import UrlShortenService
from ms_baseline.dsb_social.gen_py.social_network.ttypes import Url, ServiceException, ErrorCode
from .agent import (
    build_compose_url_agent,
    build_expand_url_agent,
    ComposeUrlAgentState,
    ExpandUrlAgentState,
)

logger = logging.getLogger("url-shorten-agent.handler")

_CACHE_TTL = 0


class UrlShortenHandler(UrlShortenService.Iface):
    """
    Parameters
    ----------
    mongo_client : pymongo.MongoClient
    mongo_db     : str
    mongo_col    : str
    redis_client : redis.Redis
    hostname     : str   e.g. "http://short-url/"
    tracer       : opentracing.Tracer
    """

    def __init__(
        self,
        mongo_client: MongoClient,
        mongo_db: str,
        mongo_col: str,
        redis_client: redis.Redis,
        hostname: str,
        tracer: opentracing.Tracer,
    ):
        self._col     = mongo_client[mongo_db][mongo_col]
        self._redis   = redis_client
        self._host    = hostname
        self._tracer  = tracer

        # Unique indices — unchanged from original
        self._col.create_index("expanded_url",  unique=True, background=True)
        self._col.create_index("shortened_url", unique=True, background=True)

        # Compile both graphs once (they capture redis + mongo via closures)
        self._compose_graph = build_compose_url_agent(
            redis_client, self._col, hostname
        )
        self._expand_graph  = build_expand_url_agent(redis_client, self._col)

    # ------------------------------------------------------------------
    # ComposeUrls
    # ------------------------------------------------------------------

    def ComposeUrls(self, req_id: int, urls: list, carrier: dict) -> list:
        parent_ctx = self._extract_ctx(carrier)

        with self._tracer.start_active_span(
            "ComposeUrls",
            child_of=parent_ctx,
            tags={
                ot_tags.SPAN_KIND: ot_tags.SPAN_KIND_RPC_SERVER,
                "req_id": req_id, "url_count": len(urls),
            },
        ) as scope:
            span = scope.span

            total_in = total_out = total_calls = fallbacks = 0
            results = []

            for expanded_url in urls:
                initial: ComposeUrlAgentState = {
                    "req_id":        req_id,
                    "expanded_url":  expanded_url,
                    "hostname":      self._host,
                    "cache_hit":     False,
                    "cached_short":  None,
                    "llm_token":     None,
                    "short_token":   None,
                    "shortened_url": None,
                    "total_input_tokens":  0,
                    "total_output_tokens": 0,
                    "total_llm_calls":     0,
                    "fallback_used":       False,
                }

                try:
                    out = asyncio.run(self._compose_graph.ainvoke(initial))
                except Exception as exc:
                    logger.error(
                        "ComposeUrls graph failed req_id=%d url=%s: %s",
                        req_id, expanded_url, exc,
                    )
                    span.set_tag("error", True)
                    raise ServiceException(
                        errorCode=ErrorCode.SE_THRIFT_HANDLER_ERROR,
                        message=f"ComposeUrls failed for {expanded_url}: {exc}",
                    )

                total_in    += out["total_input_tokens"]
                total_out   += out["total_output_tokens"]
                total_calls += out["total_llm_calls"]
                if out["fallback_used"]:
                    fallbacks += 1

                results.append(Url(
                    shortened_url=out["shortened_url"],
                    expanded_url=expanded_url,
                ))

            logger.info(
                "ComposeUrls req_id=%d urls=%d llm_calls=%d "
                "in_tokens=%d out_tokens=%d fallbacks=%d",
                req_id, len(urls), total_calls, total_in, total_out, fallbacks,
            )
            span.set_tag("llm_calls",  total_calls)
            span.set_tag("fallbacks",  fallbacks)
            return results

    # ------------------------------------------------------------------
    # GetExtendedUrls
    # ------------------------------------------------------------------

    def GetExtendedUrls(
        self, req_id: int, shortened_urls: list, carrier: dict
    ) -> list:
        parent_ctx = self._extract_ctx(carrier)

        with self._tracer.start_active_span(
            "GetExtendedUrls",
            child_of=parent_ctx,
            tags={
                ot_tags.SPAN_KIND: ot_tags.SPAN_KIND_RPC_SERVER,
                "req_id": req_id, "url_count": len(shortened_urls),
            },
        ) as scope:
            span = scope.span
            results = []

            for shortened_url in shortened_urls:
                initial: ExpandUrlAgentState = {
                    "req_id":        req_id,
                    "shortened_url": shortened_url,
                    "cache_hit":     False,
                    "expanded_url":  None,
                }

                try:
                    out = asyncio.run(self._expand_graph.ainvoke(initial))
                except Exception as exc:
                    span.set_tag("error", True)
                    raise ServiceException(
                        errorCode=ErrorCode.SE_THRIFT_HANDLER_ERROR,
                        message=f"GetExtendedUrls failed for {shortened_url}: {exc}",
                    )

                if out["expanded_url"] is None:
                    span.set_tag("error", True)
                    raise ServiceException(
                        errorCode=ErrorCode.SE_THRIFT_HANDLER_ERROR,
                        message=f"shortened_url not found: {shortened_url}",
                    )

                results.append(out["expanded_url"])

            logger.info(
                "GetExtendedUrls req_id=%d urls=%d", req_id, len(shortened_urls)
            )
            return results

    # ------------------------------------------------------------------
    # Tracing helpers
    # ------------------------------------------------------------------

    def _extract_ctx(self, carrier: dict):
        try:
            return self._tracer.extract(Format.TEXT_MAP, carrier)
        except Exception:
            return None
"""
TextHandler (Agent version) — Thrift interface UNCHANGED.

ComposeText drives the TextAgent LangGraph graph.
The final TextServiceReturn is assembled from:
  - final_text          (LLM-modified or fallback-deterministic)
  - final_user_mentions (from LLM or fallback tool results)
  - final_urls          (from LLM or fallback tool results)
"""

import asyncio
import logging

import opentracing
from opentracing.ext import tags as ot_tags
from opentracing.propagation import Format

from ms_baseline.dsb_social.gen_py.social_network import TextService
from ms_baseline.dsb_social.gen_py.social_network.ttypes import (
    TextServiceReturn, UserMention, Url, ServiceException, ErrorCode,
)
from .thrift_pool import ThriftClientPool
from .agent import build_text_agent, TextAgentState

logger = logging.getLogger("text-agent.handler")


class TextHandler(TextService.Iface):
    """
    Parameters
    ----------
    url_pool     : ThriftClientPool for UrlShortenService
    mention_pool : ThriftClientPool for UserMentionService
    tracer       : opentracing.Tracer
    """

    def __init__(
        self,
        url_pool: ThriftClientPool,
        mention_pool: ThriftClientPool,
        tracer: opentracing.Tracer,
    ):
        self._url_pool     = url_pool
        self._mention_pool = mention_pool
        self._tracer       = tracer
        self._graph        = build_text_agent(url_pool, mention_pool)

    # ------------------------------------------------------------------
    # Thrift interface — unchanged signature
    # ------------------------------------------------------------------

    def ComposeText(self, req_id: int, text: str, carrier: dict) -> TextServiceReturn:
        """
        Process raw post text via the LangGraph agent.
        Returns TextServiceReturn(text, user_mentions, urls).
        """
        parent_ctx = self._extract_ctx(carrier)

        with self._tracer.start_active_span(
            "ComposeText",
            child_of=parent_ctx,
            tags={
                ot_tags.SPAN_KIND: ot_tags.SPAN_KIND_RPC_SERVER,
                "req_id":    req_id,
                "text_len":  len(text),
            },
        ) as scope:
            span = scope.span

            # Inject child carrier for tool calls
            child_carrier = {}
            try:
                self._tracer.inject(span.context, Format.TEXT_MAP, child_carrier)
            except Exception:
                child_carrier = dict(carrier)

            initial: TextAgentState = {
                "req_id":                req_id,
                "raw_text":              text,
                "carrier":               child_carrier,
                "llm_text":              None,
                "llm_user_mentions":     None,
                "llm_urls":              None,
                "final_text":            None,
                "final_user_mentions":   None,
                "final_urls":            None,
                "total_input_tokens":    0,
                "total_output_tokens":   0,
                "total_llm_calls":       0,
                "fallback_used":         False,
                "tool_url_results":      None,
                "tool_mention_results":  None,
            }

            try:
                out = asyncio.run(self._graph.ainvoke(initial))
            except Exception as exc:
                logger.error(
                    "TextAgent graph failed req_id=%d: %s", req_id, exc
                )
                span.set_tag("error", True)
                raise ServiceException(
                    errorCode=ErrorCode.SE_THRIFT_HANDLER_ERROR,
                    message=f"TextAgent failed: {exc}",
                )

            # ---- Assemble TextServiceReturn ----
            user_mentions = [
                UserMention(
                    user_id=int(m["user_id"]),
                    username=m["username"],
                )
                for m in (out["final_user_mentions"] or [])
            ]
            urls = [
                Url(
                    shortened_url=u["shortened_url"],
                    expanded_url=u["expanded_url"],
                )
                for u in (out["final_urls"] or [])
            ]

            logger.info(
                "ComposeText req_id=%d text_len=%d->%d "
                "urls=%d mentions=%d llm_calls=%d "
                "in_tokens=%d out_tokens=%d fallback=%s",
                req_id,
                len(text), len(out["final_text"] or ""),
                len(urls), len(user_mentions),
                out["total_llm_calls"],
                out["total_input_tokens"],
                out["total_output_tokens"],
                out["fallback_used"],
            )
            print(
                f"[handler] req_id={req_id} "
                f"llm_calls={out['total_llm_calls']} "
                f"in_tokens={out['total_input_tokens']} "
                f"out_tokens={out['total_output_tokens']} "
                f"fallback={out['fallback_used']}"
            )

            span.set_tag("llm_calls",  out["total_llm_calls"])
            span.set_tag("fallback",   out["fallback_used"])
            span.set_tag("url_count",  len(urls))
            span.set_tag("mention_count", len(user_mentions))

            return TextServiceReturn(
                text=out["final_text"] or text,
                user_mentions=user_mentions,
                urls=urls,
            )

    # ------------------------------------------------------------------
    # Tracing helper
    # ------------------------------------------------------------------

    def _extract_ctx(self, carrier: dict):
        try:
            return self._tracer.extract(Format.TEXT_MAP, carrier)
        except Exception:
            return None
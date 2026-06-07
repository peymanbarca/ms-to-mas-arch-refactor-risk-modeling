"""
TextHandler — Python port of TextHandler.h

Implements the Thrift TextService.Iface interface.

What the C++ original does
--------------------------

ComposeText(req_id, text, carrier) -> TextServiceReturn

1.  Parse `text` with two regexes:
      - URL pattern     → list of expanded URL strings
      - Mention pattern → list of @username strings (without '@')

2.  Fan out TWO parallel downstream RPC calls using std::future:
      - UrlShortenService.ComposeUrls(req_id, urls, carrier)
            → list<Url>  (shortened_url + expanded_url pairs)
      - UserMentionService.ComposeUserMentions(req_id, usernames, carrier)
            → list<UserMention>  (user_id + username pairs)

3.  Collect both futures.

4.  Build a url_map { expanded_url -> shortened_url } from the ComposeUrls result.

5.  Replace every expanded URL in the original text with its shortened form
    using std::regex_replace.

6.  Return TextServiceReturn:
      text          : modified text (URLs replaced with short forms)
      user_mentions : list<UserMention>
      urls          : list<Url>

Python parallelism
------------------
The C++ code uses std::async + std::future.  We use concurrent.futures.ThreadPoolExecutor
with two workers — one per downstream call — giving identical wall-clock
behaviour: both RPC calls proceed concurrently and we join them before
assembling the result.

No database, no cache — TextService is pure logic + two downstream calls.
"""

import concurrent.futures
import logging

import opentracing
from opentracing.ext import tags as ot_tags
from opentracing.propagation import Format

from ms_baseline.dsb_social.gen_py.social_network import TextService
from ms_baseline.dsb_social.gen_py.social_network.ttypes import (
    TextServiceReturn, ServiceException, ErrorCode,
)
from .text_parser import parse, replace_urls
from .thrift_pool import ThriftClientPool

logger = logging.getLogger("text-service")


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
        # Dedicated executor — 2 workers per request is sufficient; the pool
        # is shared across all in-flight requests (thread-safe).
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=16, thread_name_prefix="text-svc"
        )

    # ------------------------------------------------------------------
    # Thrift interface
    # ------------------------------------------------------------------

    def ComposeText(
        self,
        req_id: int,
        text: str,
        carrier: dict,
    ) -> TextServiceReturn:
        """
        Parse URLs and @mentions from text, fan out to downstream services
        in parallel, replace URLs in text, return TextServiceReturn.

        Parameters
        ----------
        req_id  : i64   — trace request ID
        text    : str   — raw post text
        carrier : dict  — OpenTracing propagation headers

        Returns
        -------
        TextServiceReturn(text, user_mentions, urls)

        Raises
        ------
        ServiceException propagated from either downstream service.
        """
        parent_ctx = self._extract_ctx(carrier)

        with self._tracer.start_active_span(
            "ComposeText",
            child_of=parent_ctx,
            tags={
                ot_tags.SPAN_KIND: ot_tags.SPAN_KIND_RPC_SERVER,
                "req_id": req_id,
            },
        ) as scope:
            span = scope.span

            # ---- 1. Parse text ----
            parsed = parse(text)
            logger.debug(
                "ComposeText req_id=%d found %d URLs, %d mentions",
                req_id, len(parsed.urls), len(parsed.usernames),
            )
            span.set_tag("url_count",     len(parsed.urls))
            span.set_tag("mention_count", len(parsed.usernames))

            # ---- 2. Fan out in parallel ----
            # Inject child span context into outgoing carriers
            url_carrier     = self._inject_ctx(span)
            mention_carrier = self._inject_ctx(span)

            url_future = self._executor.submit(
                self._call_url_shorten,
                req_id, parsed.urls, url_carrier,
            )
            mention_future = self._executor.submit(
                self._call_user_mention,
                req_id, parsed.usernames, mention_carrier,
            )

            # ---- 3. Collect results ----
            try:
                url_results     = url_future.result()
                mention_results = mention_future.result()
            except ServiceException:
                span.set_tag("error", True)
                raise
            except Exception as exc:
                logger.error("Downstream call failed req_id=%d: %s", req_id, exc)
                span.set_tag("error", True)
                span.log_kv({"event": "error", "message": str(exc)})
                raise ServiceException(
                    errorCode=ErrorCode.SE_THRIFT_HANDLER_ERROR,
                    message=f"Downstream call failed: {exc}",
                )

            # ---- 4. Build url_map and replace URLs in text ----
            url_map = {u.expanded_url: u.shortened_url for u in url_results}
            modified_text = replace_urls(text, url_map)

            logger.debug(
                "ComposeText req_id=%d completed text_len=%d",
                req_id, len(modified_text),
            )

            # ---- 5. Return ----
            return TextServiceReturn(
                text=modified_text,
                user_mentions=mention_results,
                urls=url_results,
            )

    # ------------------------------------------------------------------
    # Downstream call helpers
    # ------------------------------------------------------------------

    def _call_url_shorten(
        self, req_id: int, urls: list[str], carrier: dict
    ) -> list:
        """Call UrlShortenService.ComposeUrls. Returns [] if urls is empty."""
        if not urls:
            return []
        with self._url_pool.connection() as client:
            return client.ComposeUrls(req_id, urls, carrier)

    def _call_user_mention(
        self, req_id: int, usernames: list[str], carrier: dict
    ) -> list:
        """Call UserMentionService.ComposeUserMentions. Returns [] if empty."""
        if not usernames:
            return []
        with self._mention_pool.connection() as client:
            return client.ComposeUserMentions(req_id, usernames, carrier)

    # ------------------------------------------------------------------
    # Tracing helpers
    # ------------------------------------------------------------------

    def _extract_ctx(self, carrier: dict):
        try:
            return self._tracer.extract(Format.TEXT_MAP, carrier)
        except Exception:
            return None

    def _inject_ctx(self, span) -> dict:
        """Inject the current span context into a fresh dict carrier."""
        carrier = {}
        try:
            self._tracer.inject(span.context, Format.TEXT_MAP, carrier)
        except Exception:
            pass
        return carrier

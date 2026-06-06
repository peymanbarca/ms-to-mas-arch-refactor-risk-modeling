"""
UniqueIdHandler — Python port of socialNetwork/src/UniqueIdService/UniqueIdHandler.h

Implements the Thrift-generated UniqueIdService.Iface interface.

The C++ original:
  1. Starts an OpenTracing child span from the incoming carrier
  2. Calls the Snowflake generator (machine_id + timestamp + counter)
  3. Finishes the span and returns the i64 ID

No database, no cache — this service is pure CPU/clock logic.
"""

import logging

import opentracing
from opentracing.ext import tags as ot_tags
from opentracing.propagation import Format

from ms_baseline.dsb_social.gen_py.social_network import UniqueIdService
from ms_baseline.dsb_social.gen_py.social_network.ttypes import ServiceException, ErrorCode
from .snowflake import SnowflakeGenerator

logger = logging.getLogger("unique-id-service")


class UniqueIdHandler(UniqueIdService.Iface):
    """
    Handler for the UniqueIdService Thrift interface.

    Parameters
    ----------
    generator : SnowflakeGenerator
        Pre-constructed generator seeded with the configured machine_id.
    tracer : opentracing.Tracer
        Active Jaeger / OpenTracing tracer instance.
    """

    def __init__(self, generator: SnowflakeGenerator, tracer: opentracing.Tracer):
        self._gen    = generator
        self._tracer = tracer

    # ------------------------------------------------------------------
    # Thrift interface
    # ------------------------------------------------------------------

    def ComposeUniqueId(self, req_id: int, post_type, carrier: dict) -> int:
        """
        Generate and return a unique Snowflake ID.

        Parameters
        ----------
        req_id    : i64  — request trace ID propagated from the caller
        post_type : PostType enum value (POST / REPOST / REPLY / DM)
        carrier   : Thrift trace-context map (OpenTracing B3 / Jaeger headers)

        Returns
        -------
        i64 Snowflake ID

        Raises
        ------
        ServiceException with SE_THRIFT_HANDLER_ERROR on clock skew or
        any unexpected failure.
        """
        # ---- OpenTracing: extract parent context from carrier ----
        parent_ctx = None
        try:
            parent_ctx = self._tracer.extract(Format.TEXT_MAP, carrier)
        except (opentracing.InvalidCarrierException,
                opentracing.SpanContextCorruptedException):
            pass  # no parent span — that is fine

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
            try:
                unique_id = self._gen.next_id()
                logger.debug(
                    "ComposeUniqueId req_id=%d post_type=%s -> %d",
                    req_id, post_type, unique_id,
                )
                span.set_tag("unique_id", unique_id)
                return unique_id

            except RuntimeError as exc:
                # Clock moved backwards
                logger.error("UniqueId generation failed: %s", exc)
                span.set_tag("error", True)
                span.log_kv({"event": "error", "message": str(exc)})
                raise ServiceException(
                    errorCode=ErrorCode.SE_THRIFT_HANDLER_ERROR,
                    message=f"UniqueId generation failed: {exc}",
                )
            except Exception as exc:
                logger.exception("Unexpected error in ComposeUniqueId")
                span.set_tag("error", True)
                span.log_kv({"event": "error", "message": str(exc)})
                raise ServiceException(
                    errorCode=ErrorCode.SE_THRIFT_HANDLER_ERROR,
                    message=str(exc),
                )

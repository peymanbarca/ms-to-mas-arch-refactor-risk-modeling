"""
worker.py — WriteHomeTimeline message consumer (per-thread worker).

Faithful port of the C++ WriteHomeTimelineHandler::operator()() callback
that is registered as the AMQP consumer callback.

What the C++ original does per message
---------------------------------------
1. Decode the JSON payload from the AMQP delivery body.
2. Extract OpenTracing span context from the carrier field.
3. Start a child span "WriteHomeTimeline".
4. Call HomeTimelineService.WriteHomeTimeline(
       req_id, post_id, user_id, timestamp, user_mentions_id, carrier
   ) via a Thrift client from the pool.
5. ACK the AMQP message.
6. On any error: NACK (requeue) and log.

The C++ service creates one AMQP channel per worker thread and uses
basic_consume with a callback. We replicate this with a single blocking
pika connection per worker thread started from the main server loop.
"""

import logging
import time

import opentracing
from opentracing.propagation import Format

from ms_baseline.dsb_social.gen_py.social_network.ttypes import ServiceException
from .message import decode, WriteHomeTimelineMessage
from .thrift_pool import ThriftClientPool

logger = logging.getLogger("write-home-timeline-service.worker")


class MessageWorker:
    """
    Processes a single WriteHomeTimeline AMQP delivery.

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
        self._pool   = home_timeline_pool
        self._tracer = tracer

    def process(self, body: bytes) -> None:
        """
        Decode message body and call HomeTimelineService.WriteHomeTimeline.

        Raises on failure so the caller can NACK.

        Parameters
        ----------
        body : raw AMQP message body bytes
        """
        
        t1 = time.time()
        
        # ---- 1. Decode ----
        try:
            msg = decode(body)
        except Exception as exc:
            logger.error("Failed to decode message body: %s | body=%r", exc, body[:200])
            raise

        logger.debug(
            "Processing req_id=%d post_id=%d user_id=%d mentions=%s",
            msg.req_id, msg.post_id, msg.user_id, msg.user_mentions_id,
        )

        # ---- 2. Extract parent span from carrier ----
        parent_ctx = None
        try:
            parent_ctx = self._tracer.extract(Format.TEXT_MAP, msg.carrier)
        except Exception:
            pass

        # ---- 3. Start child span ----
        with self._tracer.start_active_span(
            "WriteHomeTimeline-consumer",
            child_of=parent_ctx,
        ) as scope:
            span = scope.span
            span.set_tag("req_id",  msg.req_id)
            span.set_tag("post_id", msg.post_id)
            span.set_tag("user_id", msg.user_id)

            # Inject updated carrier for downstream call
            out_carrier = {}
            try:
                self._tracer.inject(span.context, Format.TEXT_MAP, out_carrier)
            except Exception:
                out_carrier = msg.carrier

            # ---- 4. Call HomeTimelineService ----
            try:
                with self._pool.connection() as client:
                    client.WriteHomeTimeline(
                        msg.req_id,
                        msg.post_id,
                        msg.user_id,
                        msg.timestamp,
                        msg.user_mentions_id,
                        out_carrier,
                    )
                logger.debug(
                    "WriteHomeTimeline OK req_id=%d post_id=%d",
                    msg.req_id, msg.post_id,
                )
            except ServiceException as exc:
                logger.error(
                    "HomeTimelineService error req_id=%d: [%s] %s",
                    msg.req_id, exc.errorCode, exc.message,
                )
                span.set_tag("error", True)
                span.log_kv({"event": "service_error", "message": exc.message})
                raise
            except Exception as exc:
                logger.error(
                    "HomeTimelineService call failed req_id=%d: %s",
                    msg.req_id, exc,
                )
                span.set_tag("error", True)
                span.log_kv({"event": "error", "message": str(exc)})
                raise
        t2 = time.time()
        logger.info("WriteHomeTimeline consumer req_id=%d post_id=%d took %.3f sec", msg.req_id, msg.post_id, t2 - t1)
"""
ComposePostHandler — Python port of ComposePostHandler.h

Implements the Thrift ComposePostService.Iface interface.

What the C++ original does
--------------------------

ComposePost(req_id, username, user_id, text, media_ids, media_types,
            post_type, carrier)

Phase 1 — Parallel fan-out to 4 services (std::async × 4):
  ┌─ UniqueIdService.ComposeUniqueId   → post_id (i64)
  ├─ TextService.ComposeText           → TextServiceReturn
  │      which internally fans out to:
  │        UrlShortenService.ComposeUrls
  │        UserMentionService.ComposeUserMentions
  ├─ UserService.ComposeCreatorWithUserId → Creator
  └─ MediaService.ComposeMedia         → list<Media>

Phase 2 — Assemble Post struct from all results.

Phase 3 — 3 downstream writes (all initiated, mongo/redis first):
  1. PostStorageService.StorePost(post)          [sync Thrift RPC]
  2. UserTimelineService.WriteUserTimeline(...)   [sync Thrift RPC]
  3. Publish to RabbitMQ "write-home-timeline"    [async via pika]
     → consumed by WriteHomeTimelineService
       → fans out to HomeTimelineService.WriteHomeTimeline for each follower

Python parallelism
------------------
Phase 1 uses concurrent.futures.ThreadPoolExecutor with 4 workers submitted
simultaneously, mirroring the C++ std::async × 4 pattern exactly.
Phase 3 steps 1+2 are sequential (matching C++ which does them after futures
complete), then step 3 publishes the RabbitMQ message.

No storage
----------
ComposePostService has no own database. It is a pure orchestrator.
"""

import concurrent.futures
import logging
import time

import opentracing
from opentracing.ext import tags as ot_tags
from opentracing.propagation import Format

from ms_baseline.dsb_social.gen_py.social_network import ComposePostService
from ms_baseline.dsb_social.gen_py.social_network.ttypes import (
    Post, ServiceException, ErrorCode,
)
from .thrift_pool import ThriftClientPool

logger = logging.getLogger("compose-post-service")
logging.getLogger("pika").setLevel(logging.WARNING)

class ComposePostHandler(ComposePostService.Iface):
    """
    Parameters
    ----------
    unique_id_pool      : ThriftClientPool for UniqueIdService
    text_pool           : ThriftClientPool for TextService
    user_pool           : ThriftClientPool for UserService
    media_pool          : ThriftClientPool for MediaService
    post_storage_pool   : ThriftClientPool for PostStorageService
    user_timeline_pool  : ThriftClientPool for UserTimelineService
    publisher           : HomeTimelinePublisher (RabbitMQ)
    tracer              : opentracing.Tracer
    num_workers         : int  thread-pool size for Phase 1 fan-out
    """

    def __init__(
        self,
        unique_id_pool: ThriftClientPool,
        text_pool: ThriftClientPool,
        user_pool: ThriftClientPool,
        media_pool: ThriftClientPool,
        post_storage_pool: ThriftClientPool,
        user_timeline_pool: ThriftClientPool,
        publisher,
        tracer: opentracing.Tracer,
        num_workers: int = 8,
    ):
        self._unique_id_pool    = unique_id_pool
        self._text_pool         = text_pool
        self._user_pool         = user_pool
        self._media_pool        = media_pool
        self._post_storage_pool = post_storage_pool
        self._timeline_pool     = user_timeline_pool
        self._publisher         = publisher
        self._tracer            = tracer
        self._executor          = concurrent.futures.ThreadPoolExecutor(
            max_workers=num_workers,
            thread_name_prefix="compose-post",
        )

    # ------------------------------------------------------------------
    # Thrift interface
    # ------------------------------------------------------------------

    def ComposePost(
        self,
        req_id: int,
        username: str,
        user_id: int,
        text: str,
        media_ids: list,
        media_types: list,
        post_type,
        carrier: dict,
    ) -> None:
        """
        Orchestrate post creation across all downstream services.

        Parameters
        ----------
        req_id      : trace request ID
        username    : author's username
        user_id     : author's user_id (from JWT, already resolved)
        text        : raw post text (URLs + @mentions not yet processed)
        media_ids   : list of i64 media IDs (may be empty)
        media_types : list of media type strings matching media_ids
        post_type   : PostType enum (POST / REPOST / REPLY / DM)
        carrier     : OpenTracing propagation headers
        """
        parent_ctx = self._extract_ctx(carrier)

        with self._tracer.start_active_span(
            "ComposePost",
            child_of=parent_ctx,
            tags={
                ot_tags.SPAN_KIND: ot_tags.SPAN_KIND_RPC_SERVER,
                "req_id":    req_id,
                "username":  username,
                "user_id":   user_id,
                "post_type": str(post_type),
            },
        ) as scope:
            span = scope.span
            
            logger.info(
                "\n\n-------------------- ComposePost Orchestration new Request req_id=%d user_id=%d username=%s post_type=%s\n\n",
                req_id, user_id, username, post_type,
            )

            # ================================================================
            # Phase 1 — Parallel fan-out: 4 async RPC calls simultaneously
            # ================================================================
            t1 = time.time()

            uid_carrier    = self._inject_ctx(span)
            text_carrier   = self._inject_ctx(span)
            user_carrier   = self._inject_ctx(span)
            media_carrier  = self._inject_ctx(span)

            uid_future = self._executor.submit(
                self._call_unique_id, req_id, post_type, uid_carrier
            )
            text_future = self._executor.submit(
                self._call_text, req_id, text, text_carrier
            )
            creator_future = self._executor.submit(
                self._call_compose_creator, req_id, user_id, username, user_carrier
            )
            media_future = self._executor.submit(
                self._call_media, req_id, media_types, media_ids, media_carrier
            )

            # ---- Collect all 4 results ----
            try:
                post_id           = uid_future.result()
                text_result       = text_future.result()
                creator           = creator_future.result()
                media_list        = media_future.result()
                t2 = time.time()
                logger.info(
                    "Phase 1 fan-out completed, post_id=%d req_id=%d took %.3f sec",
                    post_id, req_id, t2 - t1,
                )
            except ServiceException:
                span.set_tag("error", True)
                raise
            except Exception as exc:
                logger.error(
                    "Phase 1 fan-out failed req_id=%d: %s", req_id, exc
                )
                span.set_tag("error", True)
                span.log_kv({"event": "fanout_error", "message": str(exc)})
                raise ServiceException(
                    errorCode=ErrorCode.SE_THRIFT_HANDLER_ERROR,
                    message=f"Phase 1 fan-out failed: {exc}",
                )

            # ================================================================
            # Phase 2 — Assemble Post struct
            # ================================================================
            timestamp = int(time.time() * 1000)   # milliseconds, same as C++

            post = Post(
                post_id=post_id,
                creator=creator,
                req_id=req_id,
                text=text_result.text,
                user_mentions=text_result.user_mentions,
                media=media_list,
                urls=text_result.urls,
                timestamp=timestamp,
                post_type=post_type,
            )

            span.set_tag("post_id",   post_id)
            span.set_tag("timestamp", timestamp)

            logger.debug(
                "ComposePost req_id=%d assembled post_id=%d",
                req_id, post_id,
            )

            # ================================================================
            # Phase 3 — Downstream writes
            # ================================================================

            # Step 1: PostStorageService.StorePost
            self._store_post(req_id, post, span)

            # Step 2: UserTimelineService.WriteUserTimeline
            self._write_user_timeline(req_id, post_id, user_id, timestamp, span)

            # Step 3: Publish to RabbitMQ → WriteHomeTimelineService fan-out
            mention_ids = [m.user_id for m in (text_result.user_mentions or [])]
            self._publish_home_timeline(
                req_id, post_id, user_id, timestamp, mention_ids, span
            )

            logger.debug(
                "ComposePost req_id=%d post_id=%d DONE", req_id, post_id
            )

    # ==================================================================
    # Private — Phase 1 downstream calls
    # ==================================================================

    def _call_unique_id(self, req_id: int, post_type, carrier: dict) -> int:
        with self._unique_id_pool.connection() as client:
            t1 = time.time()
            result = client.ComposeUniqueId(req_id, post_type, carrier)
            t2 = time.time()
            logger.info("Unique ID call req_id=%d post_id=%d took %.3f sec", req_id, result, t2 - t1)
            return result

    def _call_text(self, req_id: int, text: str, carrier: dict):
        with self._text_pool.connection() as client:
            t1 = time.time()
            result = client.ComposeText(req_id, text, carrier)
            t2 = time.time()
            logger.info("Text call req_id=%d took %.3f sec", req_id, t2 - t1)
            return result

    def _call_compose_creator(
        self, req_id: int, user_id: int, username: str, carrier: dict
    ):
        with self._user_pool.connection() as client:
            t1 = time.time()
            result = client.ComposeCreatorWithUserId(req_id, user_id, username, carrier)
            t2 = time.time()
            logger.info("Creator call req_id=%d took %.3f sec", req_id, t2 - t1)
            return result

    def _call_media(
        self,
        req_id: int,
        media_types: list,
        media_ids: list,
        carrier: dict,
    ) -> list:
        if not media_ids:
            return []
        with self._media_pool.connection() as client:
            t1 = time.time()
            result = client.ComposeMedia(req_id, media_types, media_ids, carrier)
            t2 = time.time()
            logger.info("Media call req_id=%d took %.3f sec", req_id, t2 - t1)
            return result

    # ==================================================================
    # Private — Phase 3 writes
    # ==================================================================

    def _store_post(self, req_id: int, post: Post, span) -> None:
        child_carrier = self._inject_ctx(span)
        try:
            with self._post_storage_pool.connection() as client:
                t1 = time.time()
                client.StorePost(req_id, post, child_carrier)
                t2 = time.time()
                logger.info("StorePost call req_id=%d took %.3f sec", req_id, t2 - t1)
        except ServiceException:
            raise
        except Exception as exc:
            logger.error("StorePost failed req_id=%d: %s", req_id, exc)
            span.set_tag("error", True)
            raise ServiceException(
                errorCode=ErrorCode.SE_THRIFT_HANDLER_ERROR,
                message=f"StorePost failed: {exc}",
            )

    def _write_user_timeline(
        self,
        req_id: int,
        post_id: int,
        user_id: int,
        timestamp: int,
        span,
    ) -> None:
        child_carrier = self._inject_ctx(span)
        try:
            with self._timeline_pool.connection() as client:
                t1 = time.time()
                client.WriteUserTimeline(req_id, post_id, user_id, timestamp,
                                         child_carrier)
                t2 = time.time()
                logger.info("WriteUserTimeline call req_id=%d took %.3f sec", req_id, t2 - t1)
        except ServiceException:
            raise
        except Exception as exc:
            logger.error("WriteUserTimeline failed req_id=%d: %s", req_id, exc)
            span.set_tag("error", True)
            raise ServiceException(
                errorCode=ErrorCode.SE_THRIFT_HANDLER_ERROR,
                message=f"WriteUserTimeline failed: {exc}",
            )

    def _publish_home_timeline(
        self,
        req_id: int,
        post_id: int,
        user_id: int,
        timestamp: int,
        mention_ids: list,
        span,
    ) -> None:
        out_carrier = self._inject_ctx(span)
        try:
            t1 = time.time()
            self._publisher.publish(
                req_id=req_id,
                post_id=post_id,
                user_id=user_id,
                timestamp=timestamp,
                user_mentions_id=mention_ids,
                carrier=out_carrier,
            )
            t2 = time.time()
            logger.info("RabbitMQ publish req_id=%d post_id=%d took %.3f sec", req_id, post_id, t2 - t1)
        except Exception as exc:
            # RabbitMQ publish failure is logged but not fatal — the post is
            # already stored and the user timeline is written. Home timeline
            # fan-out will be delayed until the message broker recovers.
            # This mirrors the C++ error handling which logs and continues.
            logger.warning(
                "RabbitMQ publish failed req_id=%d post_id=%d: %s",
                req_id, post_id, exc,
            )
            span.log_kv({"event": "rabbitmq_error", "message": str(exc)})

    # ==================================================================
    # Tracing helpers
    # ==================================================================

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
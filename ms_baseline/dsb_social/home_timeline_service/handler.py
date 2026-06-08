"""
HomeTimelineHandler — Python port of HomeTimelineHandler.h

Implements the Thrift HomeTimelineService.Iface interface.

What the C++ original does
--------------------------

WriteHomeTimeline(req_id, post_id, user_id, timestamp, user_mentions_id, carrier)

  The "fan-out on write" path. Called by ComposePostService after a post
  is stored, so every follower's home timeline gets the new post pushed.

  1. Call SocialGraphService.GetFollowers(user_id) to get the author's
     follower list. This call is made via std::async (parallel).
  2. Collect all target user_ids:
       - all followers of the author
       - all users mentioned in the post (user_mentions_id)
       Deduplicate the union.
  3. For each target user_id, ZADD home-timeline:<target_id>
       score=timestamp  member=post_id
     These ZADDs are batched via a Redis pipeline for efficiency.
  4. No MongoDB write — HomeTimeline is purely Redis-based (unlike
     UserTimeline which also writes to MongoDB).

ReadHomeTimeline(req_id, user_id, start, stop, carrier) -> list<Post>

  1. ZREVRANGE home-timeline:<user_id> start (stop-1)  [most-recent first]
  2. Call PostStorageService.ReadPosts(post_ids) to hydrate.
  3. Return list<Post>.
  Unlike UserTimeline there is NO MongoDB fallback. If the Redis key
  does not exist the feed is simply empty (cold start). The C++ handler
  does not have a MongoDB fallback either.

Storage
-------
Redis sorted sets only (no MongoDB):
  Key   = "home-timeline:<user_id>"
  Member = str(post_id)
  Score  = timestamp (milliseconds)

Downstream dependencies
-----------------------
  SocialGraphService  — GetFollowers on WriteHomeTimeline
  PostStorageService  — ReadPosts on ReadHomeTimeline

Parallelism
-----------
  WriteHomeTimeline calls GetFollowers via the thread pool (mirroring
  std::async in C++).  The subsequent fan-out ZADDs are pipelined in
  Redis for efficiency — one pipeline call covers all target users.
"""

import concurrent.futures
import logging

import opentracing
from opentracing.ext import tags as ot_tags
from opentracing.propagation import Format
import redis

from ms_baseline.dsb_social.gen_py.social_network import HomeTimelineService
from ms_baseline.dsb_social.gen_py.social_network.ttypes import ServiceException, ErrorCode
from .thrift_pool import ThriftClientPool

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
    num_workers         : int  thread-pool size (default 16)
    """

    def __init__(
        self,
        redis_client: redis.Redis,
        post_storage_pool: ThriftClientPool,
        social_graph_pool: ThriftClientPool,
        tracer: opentracing.Tracer,
        num_workers: int = 16,
    ):
        self._redis      = redis_client
        self._post_pool  = post_storage_pool
        self._graph_pool = social_graph_pool
        self._tracer     = tracer
        self._executor   = concurrent.futures.ThreadPoolExecutor(
            max_workers=num_workers,
            thread_name_prefix="home-timeline",
        )

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
        Fan out the new post to all followers' and mentioned users' home timelines.

        Parameters
        ----------
        req_id           : trace request ID
        post_id          : the new post's ID
        user_id          : the author's user_id
        timestamp        : post creation timestamp (milliseconds)
        user_mentions_id : list of user_ids @mentioned in the post
        carrier          : OpenTracing propagation headers
        """
        parent_ctx = self._extract_ctx(carrier)

        with self._tracer.start_active_span(
            "WriteHomeTimeline",
            child_of=parent_ctx,
            tags={
                ot_tags.SPAN_KIND: ot_tags.SPAN_KIND_RPC_SERVER,
                "req_id":    req_id,
                "user_id":   user_id,
                "post_id":   post_id,
                "timestamp": timestamp,
            },
        ) as scope:
            span = scope.span

            # ---- 1. Get followers from SocialGraphService (async) ----
            child_carrier = self._inject_ctx(span)
            followers_future = self._executor.submit(
                self._get_followers, user_id, req_id, child_carrier
            )

            try:
                followers = followers_future.result()
            except ServiceException:
                span.set_tag("error", True)
                raise
            except Exception as exc:
                logger.error(
                    "GetFollowers failed req_id=%d user_id=%d: %s",
                    req_id, user_id, exc,
                )
                span.set_tag("error", True)
                raise ServiceException(
                    errorCode=ErrorCode.SE_THRIFT_HANDLER_ERROR,
                    message=f"GetFollowers failed: {exc}",
                )

            # ---- 2. Build deduplicated target set ----
            # followers + mentioned users — each gets the post in their feed
            targets = set(followers) | set(user_mentions_id)
            span.set_tag("fanout_count", len(targets))

            logger.debug(
                "WriteHomeTimeline req_id=%d post_id=%d -> %d targets "
                "(%d followers + %d mentions)",
                req_id, post_id,
                len(targets), len(followers), len(user_mentions_id),
            )

            if not targets:
                return

            # ---- 3. Fan-out: ZADD into each target's home timeline ----
            self._redis_fanout(post_id, timestamp, targets, span)

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
        Return posts [start, stop) from the user's home timeline, most-recent first.

        No MongoDB fallback — home timeline is Redis-only. If the key
        doesn't exist (cold start / never followed anyone), returns [].

        Parameters
        ----------
        start : 0-based inclusive start index
        stop  : exclusive stop index

        Returns
        -------
        list<Post>  — hydrated Post structs, reverse-chronological order
        """
        parent_ctx = self._extract_ctx(carrier)

        with self._tracer.start_active_span(
            "ReadHomeTimeline",
            child_of=parent_ctx,
            tags={
                ot_tags.SPAN_KIND: ot_tags.SPAN_KIND_RPC_SERVER,
                "req_id":  req_id,
                "user_id": user_id,
                "start":   start,
                "stop":    stop,
            },
        ) as scope:
            span = scope.span

            # ---- 1. Get post_ids from Redis ----
            post_ids = self._redis_read(user_id, start, stop, span)
            if not post_ids:
                logger.debug(
                    "ReadHomeTimeline req_id=%d user_id=%d empty", req_id, user_id
                )
                return []

            # ---- 2. Hydrate via PostStorageService ----
            posts = self._fetch_posts(req_id, post_ids, span)

            span.set_tag("post_count", len(posts))
            logger.debug(
                "ReadHomeTimeline req_id=%d user_id=%d [%d:%d] -> %d posts",
                req_id, user_id, start, stop, len(posts),
            )
            return posts

    # ==================================================================
    # Private — Redis fan-out and read
    # ==================================================================

    def _redis_fanout(
        self,
        post_id: int,
        timestamp: int,
        targets: set,
        span,
    ) -> None:
        """
        Pipeline ZADD post_id (score=timestamp) into every target's sorted set.

        Using a pipeline batches all ZADDs into a single round-trip to Redis,
        matching the efficiency of the C++ implementation which iterates and
        calls the Redis client once per target but using pipelining.
        """
        try:
            pipe = self._redis.pipeline(transaction=False)
            for target_id in targets:
                key = _REDIS_KEY_PREFIX + str(target_id)
                pipe.zadd(key, {str(post_id): timestamp})
            pipe.execute()
            logger.debug(
                "_redis_fanout post_id=%d ts=%d -> %d keys",
                post_id, timestamp, len(targets),
            )
        except redis.RedisError as exc:
            logger.error("Redis pipeline fanout failed post_id=%d: %s", post_id, exc)
            span.set_tag("error", True)
            span.log_kv({"event": "redis_fanout_error", "error": str(exc)})
            raise ServiceException(
                errorCode=ErrorCode.SE_REDIS_ERROR,
                message=f"Redis fanout failed: {exc}",
            )

    def _redis_read(
        self,
        user_id: int,
        start: int,
        stop: int,
        span,
    ) -> list:
        """
        ZREVRANGE home-timeline:<user_id> start (stop-1) → list[int post_id].
        Returns [] if key doesn't exist or on error.
        stop-1 because Redis end is inclusive but our interface is exclusive.
        """
        key = _REDIS_KEY_PREFIX + str(user_id)
        try:
            members = self._redis.zrevrange(key, start, stop - 1)
            return [int(m) for m in members]
        except redis.RedisError as exc:
            logger.warning("Redis ZREVRANGE key=%s failed: %s", key, exc)
            span.log_kv({"event": "redis_read_error", "error": str(exc)})
            return []

    # ==================================================================
    # Private — downstream service calls
    # ==================================================================

    def _get_followers(
        self, user_id: int, req_id: int, carrier: dict
    ) -> list:
        """Call SocialGraphService.GetFollowers."""
        with self._graph_pool.connection() as client:
            return client.GetFollowers(req_id, user_id, carrier)

    def _fetch_posts(self, req_id: int, post_ids: list, span) -> list:
        """Call PostStorageService.ReadPosts to hydrate post_ids."""
        child_carrier = self._inject_ctx(span)
        try:
            with self._post_pool.connection() as client:
                return client.ReadPosts(req_id, post_ids, child_carrier)
        except ServiceException:
            raise
        except Exception as exc:
            logger.error(
                "PostStorageService.ReadPosts failed req_id=%d: %s", req_id, exc
            )
            span.set_tag("error", True)
            span.log_kv({"event": "post_storage_error", "error": str(exc)})
            raise ServiceException(
                errorCode=ErrorCode.SE_THRIFT_HANDLER_ERROR,
                message=f"PostStorageService call failed: {exc}",
            )

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
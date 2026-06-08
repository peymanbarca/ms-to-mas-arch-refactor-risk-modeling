"""
UserTimelineHandler — Python port of UserTimelineHandler.h

Implements the Thrift UserTimelineService.Iface interface.

What the C++ original does
--------------------------

WriteUserTimeline(req_id, post_id, user_id, timestamp, carrier)
  1. ZADD user-timeline:<user_id>  score=timestamp  member=post_id
     into Redis (the user's personal timeline sorted set).
  2. $push { post_id, timestamp } into MongoDB collection "user-timeline",
     document keyed by user_id.
  Both writes happen concurrently via std::async in the C++ original.
  We submit both to a ThreadPoolExecutor for identical parallelism.

ReadUserTimeline(req_id, user_id, start, stop, carrier) -> list<Post>
  1. Try Redis ZREVRANGE user-timeline:<user_id> start stop WITHSCORES
     to get the [start, stop) window of post_ids (most-recent-first).
  2. If Redis key missing: fall back to MongoDB, read the user's
     post_id list (sorted by timestamp desc), slice [start:stop],
     then seed Redis with all known entries.
  3. Call PostStorageService.ReadPosts(post_ids) to hydrate the
     post_id list into full Post structs.
  4. Return list<Post>.

Storage
-------
Redis sorted set:
  Key   = "user-timeline:<user_id>"
  Member = str(post_id)
  Score  = timestamp (milliseconds, same units as the C++ original)

MongoDB (db="user-timeline", collection="user-timeline"):
  One document per user:
  {
    "user_id": i64,
    "posts": [
      {"post_id": i64, "timestamp": i64},
      ...
    ]
  }
  Unique index on user_id.
  The posts array is NOT kept sorted in MongoDB — sorting is done on read.

Downstream dependency:
  PostStorageService (client pool) — called by ReadUserTimeline to hydrate
  post_ids into full Post structs.
"""

import concurrent.futures
import logging

import opentracing
from opentracing.ext import tags as ot_tags
from opentracing.propagation import Format
from pymongo import MongoClient
import redis

from ms_baseline.dsb_social.gen_py.social_network import UserTimelineService
from ms_baseline.dsb_social.gen_py.social_network.ttypes import ServiceException, ErrorCode
from .thrift_pool import ThriftClientPool

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
    num_workers        : int   thread-pool size for parallel write (default 8)
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
        self._col         = mongo_client[mongo_db][mongo_col]
        self._redis       = redis_client
        self._post_pool   = post_storage_pool
        self._tracer      = tracer
        self._executor    = concurrent.futures.ThreadPoolExecutor(
            max_workers=num_workers,
            thread_name_prefix="user-timeline",
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
        Record a new post in the user's personal timeline.

        Performs Redis ZADD and MongoDB $push concurrently (std::async port).
        """
        parent_ctx = self._extract_ctx(carrier)

        with self._tracer.start_active_span(
            "WriteUserTimeline",
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

            # Submit both writes concurrently — mirrors C++ std::async
            redis_future = self._executor.submit(
                self._redis_write, user_id, post_id, timestamp, span
            )
            mongo_future = self._executor.submit(
                self._mongo_write, user_id, post_id, timestamp, req_id, span
            )

            # Collect — propagate any exceptions
            redis_exc = None
            mongo_exc = None

            try:
                redis_future.result()
            except ServiceException as exc:
                redis_exc = exc
            except Exception as exc:
                logger.warning("Redis write failed: %s", exc)
                # Non-fatal — MongoDB write is the durable store

            try:
                mongo_future.result()
            except ServiceException as exc:
                mongo_exc = exc

            if mongo_exc is not None:
                span.set_tag("error", True)
                raise mongo_exc

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
        most-recent first.

        Parameters
        ----------
        start : 0-based inclusive start index
        stop  : exclusive stop index  (C++ uses [start, stop) semantics)

        Returns
        -------
        list<Post>  — hydrated Post structs in reverse-chronological order
        """
        parent_ctx = self._extract_ctx(carrier)

        with self._tracer.start_active_span(
            "ReadUserTimeline",
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

            # ---- 1. Get post_ids (Redis first, MongoDB fallback) ----
            post_ids = self._get_post_ids(user_id, start, stop, req_id, span)

            if not post_ids:
                return []

            # ---- 2. Hydrate post_ids → Post structs via PostStorageService ----
            posts = self._fetch_posts(req_id, post_ids, span)

            span.set_tag("post_count", len(posts))
            logger.debug(
                "ReadUserTimeline req_id=%d user_id=%d start=%d stop=%d -> %d posts",
                req_id, user_id, start, stop, len(posts),
            )
            return posts

    # ==================================================================
    # Private — post_id list retrieval
    # ==================================================================

    def _get_post_ids(
        self,
        user_id: int,
        start: int,
        stop: int,
        req_id: int,
        span,
    ) -> list:
        """
        Return post_ids for [start, stop) window, most-recent first.
        Tries Redis ZREVRANGE first; falls back to MongoDB.
        """
        redis_key = _REDIS_KEY_PREFIX + str(user_id)
        # stop - 1 because Redis ZREVRANGE end is inclusive
        redis_stop = stop - 1

        # 1. Redis ZREVRANGE (most-recent = highest score = first)
        cached_ids = self._redis_zrevrange(redis_key, start, redis_stop, span)
        if cached_ids is not None:
            logger.debug(
                "_get_post_ids user_id=%d [%d:%d] cache HIT count=%d",
                user_id, start, stop, len(cached_ids),
            )
            return cached_ids

        # 2. MongoDB fallback
        logger.debug(
            "_get_post_ids user_id=%d [%d:%d] cache MISS, querying MongoDB",
            user_id, start, stop,
        )
        doc = self._mongo_read(user_id, req_id, span)
        all_posts = (doc or {}).get("posts", [])

        # Sort by timestamp descending (most recent first) — mirrors C++ sort
        all_posts_sorted = sorted(
            all_posts, key=lambda p: p.get("timestamp", 0), reverse=True
        )

        # Seed Redis with all known post_ids so future reads hit cache
        if all_posts_sorted:
            self._redis_seed(redis_key, all_posts_sorted, span)

        # Slice the requested window
        window = all_posts_sorted[start:stop]
        return [int(p["post_id"]) for p in window]

    # ==================================================================
    # Private — PostStorageService call
    # ==================================================================

    def _fetch_posts(self, req_id: int, post_ids: list, span) -> list:
        """Call PostStorageService.ReadPosts to hydrate post_ids."""
        child_carrier = self._inject_ctx(span)
        try:
            with self._post_pool.connection() as client:
                return client.ReadPosts(req_id, post_ids, child_carrier)
        except ServiceException:
            raise
        except Exception as exc:
            logger.error("PostStorageService.ReadPosts failed: %s", exc)
            span.set_tag("error", True)
            span.log_kv({"event": "post_storage_error", "error": str(exc)})
            raise ServiceException(
                errorCode=ErrorCode.SE_THRIFT_HANDLER_ERROR,
                message=f"PostStorageService call failed: {exc}",
            )

    # ==================================================================
    # Private — Redis helpers
    # ==================================================================

    def _redis_write(
        self, user_id: int, post_id: int, timestamp: int, span
    ) -> None:
        """ZADD user-timeline:<user_id> score=timestamp member=post_id."""
        key = _REDIS_KEY_PREFIX + str(user_id)
        try:
            self._redis.zadd(key, {str(post_id): timestamp})
            logger.debug("Redis ZADD key=%s post_id=%d ts=%d", key, post_id, timestamp)
        except redis.RedisError as exc:
            logger.warning("Redis ZADD failed key=%s: %s", key, exc)
            span.log_kv({"event": "redis_error", "error": str(exc)})
            # Non-fatal — MongoDB is durable

    def _redis_zrevrange(
        self, key: str, start: int, stop: int, span
    ) -> list | None:
        """
        ZREVRANGE key start stop → list[int post_id] or None on miss/error.
        Returns None when key doesn't exist (cache miss), so callers
        fall through to MongoDB.
        """
        try:
            if not self._redis.exists(key):
                return None
            members = self._redis.zrevrange(key, start, stop)
            return [int(m) for m in members]
        except redis.RedisError as exc:
            logger.warning("Redis ZREVRANGE key=%s failed: %s", key, exc)
            span.log_kv({"event": "redis_error", "error": str(exc)})
            return None   # non-fatal — fall through to MongoDB

    def _redis_seed(self, key: str, posts: list, span) -> None:
        """
        Seed Redis sorted set from MongoDB data.
        posts: list of {"post_id": i64, "timestamp": i64}
        """
        try:
            mapping = {
                str(p["post_id"]): int(p.get("timestamp", 0))
                for p in posts
            }
            self._redis.zadd(key, mapping)
            logger.debug("Redis seed key=%s with %d entries", key, len(mapping))
        except redis.RedisError as exc:
            logger.warning("Redis seed key=%s failed: %s", key, exc)
            span.log_kv({"event": "redis_seed_error", "error": str(exc)})

    # ==================================================================
    # Private — MongoDB helpers
    # ==================================================================

    def _mongo_write(
        self,
        user_id: int,
        post_id: int,
        timestamp: int,
        req_id: int,
        span,
    ) -> None:
        """
        $push {post_id, timestamp} onto the user's posts array.
        Upsert creates the document if it doesn't exist.
        """
        try:
            self._col.update_one(
                {"user_id": user_id},
                {
                    "$push": {
                        "posts": {
                            "post_id":   post_id,
                            "timestamp": timestamp,
                        }
                    }
                },
                upsert=True,
            )
            logger.debug(
                "MongoDB push user_id=%d post_id=%d ts=%d",
                user_id, post_id, timestamp,
            )
        except Exception as exc:
            logger.error(
                "MongoDB push failed req_id=%d user_id=%d: %s",
                req_id, user_id, exc,
            )
            span.log_kv({"event": "mongo_error", "error": str(exc)})
            raise ServiceException(
                errorCode=ErrorCode.SE_MONGODB_ERROR,
                message=f"MongoDB write failed: {exc}",
            )

    def _mongo_read(self, user_id: int, req_id: int, span) -> dict | None:
        """Fetch the user-timeline document from MongoDB."""
        try:
            return self._col.find_one(
                {"user_id": user_id},
                {"posts": 1, "_id": 0},
            )
        except Exception as exc:
            logger.error(
                "MongoDB read failed req_id=%d user_id=%d: %s",
                req_id, user_id, exc,
            )
            span.log_kv({"event": "mongo_error", "error": str(exc)})
            raise ServiceException(
                errorCode=ErrorCode.SE_MONGODB_ERROR,
                message=f"MongoDB read failed: {exc}",
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

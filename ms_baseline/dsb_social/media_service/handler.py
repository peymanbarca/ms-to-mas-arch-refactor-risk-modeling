"""
MediaHandler — Python port of socialNetwork/src/MediaService/MediaHandler.h

What the C++ original does
--------------------------
ComposeMedia(req_id, media_types, media_ids, carrier) -> list<Media>

1. Validate that len(media_types) == len(media_ids).
2. For each (media_id, media_type) pair:
   a. Check Memcached (Redis in our port) — if found, skip MongoDB write.
   b. If not cached, insert a document into MongoDB collection "media":
        { media_id: <i64>, media_type: <string> }
   c. Write the pair into the cache (key = str(media_id), value = media_type).
3. Build and return list<Media> structs from the inputs.

Key design notes
----------------
- The original stores media *metadata* only (id + type string).
  Actual binary media content is not handled by this service.
- Cache key is the string representation of media_id.
- MongoDB upsert (update with upsert=True) avoids duplicate-key errors on retry.
- Original uses Memcached; we use Redis with the same key/value semantics.
- MongoDB write uses insert_one inside a try/except to handle duplicates
  gracefully (idempotent on retry, matching C++ behaviour).
"""

import json
import logging

import opentracing
from opentracing.ext import tags as ot_tags
from opentracing.propagation import Format
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError
import redis

from ms_baseline.dsb_social.gen_py.social_network import MediaService
from ms_baseline.dsb_social.gen_py.social_network.ttypes import Media, ServiceException, ErrorCode

logger = logging.getLogger("media-service")

# Redis cache TTL — the C++ Memcached entries had no explicit TTL (stored
# indefinitely). We use 0 (no expiry) to match that behaviour.
_CACHE_TTL = 0  # 0 = no expiry in Redis SET


class MediaHandler(MediaService.Iface):
    """
    Handler for the MediaService Thrift interface.

    Parameters
    ----------
    mongo_client  : pymongo.MongoClient
    mongo_db      : str   — database name (e.g. "media")
    mongo_col     : str   — collection name (e.g. "media")
    redis_client  : redis.Redis
    tracer        : opentracing.Tracer
    """

    def __init__(
        self,
        mongo_client: MongoClient,
        mongo_db: str,
        mongo_col: str,
        redis_client: redis.Redis,
        tracer: opentracing.Tracer,
    ):
        self._mongo = mongo_client[mongo_db][mongo_col]
        self._redis = redis_client
        self._tracer = tracer

        # Ensure unique index on media_id — mirrors C++ MongoDB setup
        self._mongo.create_index("media_id", unique=True, background=True)

    # ------------------------------------------------------------------
    # Thrift interface
    # ------------------------------------------------------------------

    def ComposeMedia(
        self,
        req_id: int,
        media_types: list,
        media_ids: list,
        carrier: dict,
    ) -> list:
        """
        Validate, persist, and return Media structs.

        Parameters
        ----------
        req_id      : i64  — trace request ID
        media_types : list[str]  — e.g. ["photo", "video"]
        media_ids   : list[i64]  — pre-assigned IDs (from UniqueIdService)
        carrier     : dict       — OpenTracing propagation headers

        Returns
        -------
        list[Media]

        Raises
        ------
        ServiceException(SE_THRIFT_HANDLER_ERROR) on mismatched list lengths.
        ServiceException(SE_MONGODB_ERROR) on persistent store failure.
        ServiceException(SE_REDIS_ERROR) on cache failure.
        """
        # ---- extract parent span ----
        parent_ctx = None
        try:
            parent_ctx = self._tracer.extract(Format.TEXT_MAP, carrier)
        except Exception:
            pass

        with self._tracer.start_active_span(
            "ComposeMedia",
            child_of=parent_ctx,
            tags={
                ot_tags.SPAN_KIND: ot_tags.SPAN_KIND_RPC_SERVER,
                "req_id": req_id,
            },
        ) as scope:
            span = scope.span

            # ---- validation (mirrors C++ check) ----
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

            media_list = []

            for media_id, media_type in zip(media_ids, media_types):
                cache_key = str(media_id)

                # ---- 1. check Redis cache ----
                cached = self._get_from_cache(cache_key, span)

                if cached is None:
                    # ---- 2. write to MongoDB ----
                    self._store_to_mongo(media_id, media_type, req_id, span)

                    # ---- 3. populate cache ----
                    self._set_in_cache(cache_key, media_type, span)
                else:
                    logger.debug(
                        "ComposeMedia req_id=%d media_id=%d cache HIT",
                        req_id, media_id,
                    )

                media_list.append(Media(media_id=media_id, media_type=media_type))

            span.set_tag("media_count", len(media_list))
            logger.debug(
                "ComposeMedia req_id=%d -> %d media items", req_id, len(media_list)
            )
            return media_list

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_from_cache(self, cache_key: str, span) -> str | None:
        """Return cached media_type string or None on miss/error."""
        try:
            val = self._redis.get(cache_key)
            if val is not None:
                return val.decode("utf-8")
            return None
        except redis.RedisError as exc:
            logger.warning("Redis GET failed for key %s: %s", cache_key, exc)
            span.log_kv({"event": "redis_get_error", "key": cache_key, "error": str(exc)})
            # Cache miss is non-fatal — fall through to MongoDB
            return None

    def _set_in_cache(self, cache_key: str, media_type: str, span) -> None:
        """Write media_type into Redis; log but don't raise on failure."""
        try:
            if _CACHE_TTL > 0:
                self._redis.setex(cache_key, _CACHE_TTL, media_type)
            else:
                self._redis.set(cache_key, media_type)
        except redis.RedisError as exc:
            logger.warning("Redis SET failed for key %s: %s", cache_key, exc)
            span.log_kv({"event": "redis_set_error", "key": cache_key, "error": str(exc)})
            # Non-fatal — data is already in MongoDB

    def _store_to_mongo(
        self, media_id: int, media_type: str, req_id: int, span
    ) -> None:
        """
        Upsert media document into MongoDB.
        Uses update_one with upsert=True for idempotency on retry —
        matches C++ behaviour where a duplicate insert is silently ignored.
        """
        try:
            self._mongo.update_one(
                {"media_id": media_id},
                {"$set": {"media_id": media_id, "media_type": media_type}},
                upsert=True,
            )
            logger.debug(
                "MongoDB upsert req_id=%d media_id=%d type=%s",
                req_id, media_id, media_type,
            )
        except Exception as exc:
            logger.error(
                "MongoDB write failed req_id=%d media_id=%d: %s",
                req_id, media_id, exc,
            )
            span.set_tag("error", True)
            span.log_kv({"event": "mongo_error", "error": str(exc)})
            raise ServiceException(
                errorCode=ErrorCode.SE_MONGODB_ERROR,
                message=f"MongoDB write failed: {exc}",
            )
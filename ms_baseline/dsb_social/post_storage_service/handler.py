"""
PostStorageHandler — Python port of PostStorageHandler.h

Implements the Thrift PostStorageService.Iface interface.

What the C++ original does
--------------------------

StorePost(req_id, post, carrier)
  1. Serialise Post to a BSON document.
  2. Insert into MongoDB collection "post" (unique index on post_id).
  3. Write the serialised post to Memcached (key = str(post_id)).
  Note: duplicate inserts (same post_id) silently succeed in the C++ via
  MongoDB duplicate-key handling; we use upsert for the same effect.

ReadPost(req_id, post_id, carrier) -> Post
  1. Check Memcached (Redis) by key = str(post_id).
  2. If hit: deserialise and return.
  3. If miss: query MongoDB, populate cache, return.
  4. If not found anywhere: raise ServiceException.

ReadPosts(req_id, post_ids, carrier) -> list<Post>
  The C++ implementation uses std::async to fan out the individual reads in
  parallel — one future per post_id. All futures are submitted immediately
  then collected in a second pass.

  Our Python port uses concurrent.futures.ThreadPoolExecutor for the same
  semantics: every post_id is submitted as a separate task, the tasks run
  concurrently, and results are gathered with Future.result().

  The returned list is in the SAME ORDER as the input post_ids.

Storage
-------
MongoDB:
  db="post", collection="post"
  Schema: { post_id, creator, req_id, text, user_mentions, media, urls,
            timestamp, post_type }
  Index: unique on post_id

Redis (replacing Memcached):
  key   = str(post_id)
  value = JSON string of the post document
"""

import concurrent.futures
import logging

import opentracing
from opentracing.ext import tags as ot_tags
from opentracing.propagation import Format
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError
import redis

from ms_baseline.dsb_social.gen_py.social_network import PostStorageService
from ms_baseline.dsb_social.gen_py.social_network.ttypes import Post, ServiceException, ErrorCode
from .post_serializer import post_to_dict, dict_to_post, post_to_json, json_to_post

logger = logging.getLogger("post-storage-service")

_CACHE_TTL = 0   # no expiry — match original Memcached behaviour


class PostStorageHandler(PostStorageService.Iface):
    """
    Parameters
    ----------
    mongo_client  : pymongo.MongoClient
    mongo_db      : str   e.g. "post"
    mongo_col     : str   e.g. "post"
    redis_client  : redis.Redis
    tracer        : opentracing.Tracer
    num_workers   : int   thread pool size for parallel ReadPosts (default 8)
    """

    def __init__(
        self,
        mongo_client: MongoClient,
        mongo_db: str,
        mongo_col: str,
        redis_client: redis.Redis,
        tracer: opentracing.Tracer,
        num_workers: int = 8,
    ):
        self._col     = mongo_client[mongo_db][mongo_col]
        self._redis   = redis_client
        self._tracer  = tracer

        # Unique index on post_id — mirrors C++ MongoDB setup
        self._col.create_index("post_id", unique=True, background=True)

        # Shared thread pool for parallel ReadPosts
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=num_workers,
            thread_name_prefix="post-storage",
        )

    # ------------------------------------------------------------------
    # StorePost
    # ------------------------------------------------------------------

    def StorePost(self, req_id: int, post: Post, carrier: dict) -> None:
        """
        Persist a post to MongoDB and prime the Redis cache.

        Idempotent: duplicate post_id is silently ignored (upsert).
        """
        parent_ctx = self._extract_ctx(carrier)

        with self._tracer.start_active_span(
            "StorePost",
            child_of=parent_ctx,
            tags={
                ot_tags.SPAN_KIND: ot_tags.SPAN_KIND_RPC_SERVER,
                "req_id":  req_id,
                "post_id": post.post_id,
            },
        ) as scope:
            span = scope.span

            doc = post_to_dict(post)

            # ---- 1. MongoDB upsert ----
            try:
                self._col.update_one(
                    {"post_id": post.post_id},
                    {"$set": doc},
                    upsert=True,
                )
            except Exception as exc:
                logger.error(
                    "StorePost MongoDB upsert failed req_id=%d post_id=%d: %s",
                    req_id, post.post_id, exc,
                )
                span.set_tag("error", True)
                span.log_kv({"event": "mongo_error", "error": str(exc)})
                raise ServiceException(
                    errorCode=ErrorCode.SE_MONGODB_ERROR,
                    message=f"MongoDB write failed: {exc}",
                )

            # ---- 2. Prime Redis cache ----
            self._cache_set(post.post_id, post, span)

            logger.debug(
                "StorePost req_id=%d post_id=%d stored", req_id, post.post_id
            )

    # ------------------------------------------------------------------
    # ReadPost
    # ------------------------------------------------------------------

    def ReadPost(self, req_id: int, post_id: int, carrier: dict) -> Post:
        """
        Return a single post by post_id (cache-first).

        Raises ServiceException if post_id is not found.
        """
        parent_ctx = self._extract_ctx(carrier)

        with self._tracer.start_active_span(
            "ReadPost",
            child_of=parent_ctx,
            tags={
                ot_tags.SPAN_KIND: ot_tags.SPAN_KIND_RPC_SERVER,
                "req_id":  req_id,
                "post_id": post_id,
            },
        ) as scope:
            span = scope.span
            post = self._read_one(post_id, req_id, span)
            if post is None:
                msg = f"Post not found: {post_id}"
                span.set_tag("error", True)
                raise ServiceException(
                    errorCode=ErrorCode.SE_THRIFT_HANDLER_ERROR,
                    message=msg,
                )
            return post

    # ------------------------------------------------------------------
    # ReadPosts
    # ------------------------------------------------------------------

    def ReadPosts(self, req_id: int, post_ids: list, carrier: dict) -> list:
        """
        Return a list of posts for the given post_ids, in the same order.

        Uses a thread pool to fan out individual cache/MongoDB reads in
        parallel — matching the C++ std::async + std::future pattern.

        Any post_id not found raises ServiceException (same as C++).
        """
        parent_ctx = self._extract_ctx(carrier)

        with self._tracer.start_active_span(
            "ReadPosts",
            child_of=parent_ctx,
            tags={
                ot_tags.SPAN_KIND: ot_tags.SPAN_KIND_RPC_SERVER,
                "req_id":    req_id,
                "post_count": len(post_ids),
            },
        ) as scope:
            span = scope.span

            if not post_ids:
                return []

            # Submit all reads in parallel
            futures = {
                post_id: self._executor.submit(
                    self._read_one, post_id, req_id, span
                )
                for post_id in post_ids
            }

            # Collect in input order (matching C++ result ordering)
            results = []
            for post_id in post_ids:
                try:
                    post = futures[post_id].result()
                except ServiceException:
                    span.set_tag("error", True)
                    raise
                except Exception as exc:
                    logger.error(
                        "ReadPosts failed post_id=%d: %s", post_id, exc
                    )
                    span.set_tag("error", True)
                    raise ServiceException(
                        errorCode=ErrorCode.SE_THRIFT_HANDLER_ERROR,
                        message=f"ReadPost failed for {post_id}: {exc}",
                    )

                if post is None:
                    msg = f"Post not found: {post_id}"
                    span.set_tag("error", True)
                    raise ServiceException(
                        errorCode=ErrorCode.SE_THRIFT_HANDLER_ERROR,
                        message=msg,
                    )
                results.append(post)

            logger.debug(
                "ReadPosts req_id=%d returned %d posts", req_id, len(results)
            )
            return results

    # ==================================================================
    # Internal — single post read (cache → MongoDB)
    # ==================================================================

    def _read_one(self, post_id: int, req_id: int, span) -> Post | None:
        """
        Fetch one post by post_id.
        Returns Post on success, None if not found.
        Raises ServiceException on storage errors.
        """
        # 1. Redis cache
        cached = self._cache_get(post_id, span)
        if cached is not None:
            logger.debug(
                "_read_one req_id=%d post_id=%d cache HIT", req_id, post_id
            )
            return cached

        # 2. MongoDB
        try:
            doc = self._col.find_one(
                {"post_id": post_id},
                {"_id": 0},   # exclude ObjectId
            )
        except Exception as exc:
            logger.error(
                "_read_one MongoDB find failed post_id=%d: %s", post_id, exc
            )
            span.log_kv({"event": "mongo_error", "error": str(exc)})
            raise ServiceException(
                errorCode=ErrorCode.SE_MONGODB_ERROR,
                message=f"MongoDB read failed: {exc}",
            )

        if doc is None:
            logger.debug(
                "_read_one req_id=%d post_id=%d not found", req_id, post_id
            )
            return None

        post = dict_to_post(doc)
        # Backfill cache
        self._cache_set(post_id, post, span)
        logger.debug(
            "_read_one req_id=%d post_id=%d MongoDB HIT", req_id, post_id
        )
        return post

    # ==================================================================
    # Redis helpers
    # ==================================================================

    def _cache_get(self, post_id: int, span) -> Post | None:
        """Return cached Post or None on miss/error."""
        try:
            val = self._redis.get(str(post_id))
            if val is not None:
                return json_to_post(val.decode("utf-8"))
            return None
        except Exception as exc:
            logger.warning("Redis GET post_id=%d failed: %s", post_id, exc)
            span.log_kv({"event": "redis_get_error", "error": str(exc)})
            return None   # non-fatal

    def _cache_set(self, post_id: int, post: Post, span) -> None:
        """Store post JSON in Redis. Non-fatal on error."""
        try:
            val = post_to_json(post)
            if _CACHE_TTL > 0:
                self._redis.setex(str(post_id), _CACHE_TTL, val)
            else:
                self._redis.set(str(post_id), val)
        except Exception as exc:
            logger.warning("Redis SET post_id=%d failed: %s", post_id, exc)
            span.log_kv({"event": "redis_set_error", "error": str(exc)})

    # ==================================================================
    # Tracing helper
    # ==================================================================

    def _extract_ctx(self, carrier: dict):
        try:
            return self._tracer.extract(Format.TEXT_MAP, carrier)
        except Exception:
            return None
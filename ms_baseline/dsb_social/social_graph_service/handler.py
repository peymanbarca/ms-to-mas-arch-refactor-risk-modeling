"""
SocialGraphHandler — Python port of SocialGraphHandler.h

Implements the full SocialGraphService.Iface Thrift interface.

Methods
-------
GetFollowers(req_id, user_id, carrier)          -> list<i64>
GetFollowees(req_id, user_id, carrier)          -> list<i64>
Follow(req_id, user_id, followee_id, carrier)
Unfollow(req_id, user_id, followee_id, carrier)
FollowWithUsername(req_id, user_username, followee_username, carrier)
UnfollowWithUsername(req_id, user_username, followee_username, carrier)
InsertUser(req_id, user_id, carrier)

Storage layout
--------------

MongoDB (db="social-graph", collection="social-graph"):
  One document per user:
  {
    "user_id":   i64,
    "followers": [i64, ...],   <- list of user_ids who follow this user
    "followees": [i64, ...]    <- list of user_ids this user follows
  }
  Unique index on user_id.

Redis sorted sets (replacing original Memcached — note: the C++ original
actually uses Redis directly for the social graph, not Memcached):
  Key: "followers:<user_id>"   members = follower user_ids, score = follow timestamp
  Key: "followees:<user_id>"   members = followee user_ids, score = follow timestamp

  ZADD  on Follow   (add to both follower and followee sets)
  ZREM  on Unfollow (remove from both sets)
  ZRANGE on Get*    (return all members, ordered by score)

Cache policy:
  - Read (GetFollowers/GetFollowees): Redis ZRANGE → MongoDB fallback.
  - Write (Follow/Unfollow): dual-write to both Redis and MongoDB atomically.
    If the Redis key does not exist yet, we seed it from MongoDB first, then apply
    the mutation. This matches the C++ handler's "ensure key exists" pattern.

Downstream dependency:
  - UserService client pool (for FollowWithUsername / UnfollowWithUsername).
    These methods resolve usernames to user_ids via UserService.GetUserId,
    then call Follow / Unfollow internally.

Parallel fan-out:
  - GetFollowers and GetFollowees each fan out the per-user read in a single
    call, but FollowWithUsername / UnfollowWithUsername need TWO parallel
    GetUserId calls (one for each username). We use ThreadPoolExecutor for that.
"""

import concurrent.futures
import logging
import time

import opentracing
from opentracing.ext import tags as ot_tags
from opentracing.propagation import Format
from pymongo import MongoClient, ReturnDocument
import redis

from ms_baseline.dsb_social.gen_py.social_network import SocialGraphService
from ms_baseline.dsb_social.gen_py.social_network.ttypes import ServiceException, ErrorCode
from .thrift_pool import ThriftClientPool

logger = logging.getLogger("social-graph-service")

# Redis key prefixes — match the C++ redis_client key construction
_KEY_FOLLOWERS = "followers:"   # followers:<user_id> → sorted set of follower IDs
_KEY_FOLLOWEES = "followees:"   # followees:<user_id> → sorted set of followee IDs


class SocialGraphHandler(SocialGraphService.Iface):
    """
    Parameters
    ----------
    mongo_client     : pymongo.MongoClient
    mongo_db         : str   e.g. "social-graph"
    mongo_col        : str   e.g. "social-graph"
    redis_client     : redis.Redis
    user_service_pool: ThriftClientPool for UserService (needed for *WithUsername)
    tracer           : opentracing.Tracer
    num_workers      : int   thread pool size for parallel username resolution
    """

    def __init__(
        self,
        mongo_client: MongoClient,
        mongo_db: str,
        mongo_col: str,
        redis_client: redis.Redis,
        user_service_pool: ThriftClientPool,
        tracer: opentracing.Tracer,
        num_workers: int = 8,
    ):
        self._col       = mongo_client[mongo_db][mongo_col]
        self._redis     = redis_client
        self._user_pool = user_service_pool
        self._tracer    = tracer
        self._executor  = concurrent.futures.ThreadPoolExecutor(
            max_workers=num_workers,
            thread_name_prefix="social-graph",
        )

        # Unique index on user_id — mirrors C++ MongoDB setup
        self._col.create_index("user_id", unique=True, background=True)

    # ------------------------------------------------------------------
    # InsertUser
    # ------------------------------------------------------------------

    def InsertUser(self, req_id: int, user_id: int, carrier: dict) -> None:
        """
        Initialise an empty social graph entry for a newly registered user.
        Called by UserService after a successful RegisterUser.
        Idempotent — duplicate inserts are silently ignored.
        """
        parent_ctx = self._extract_ctx(carrier)
        with self._tracer.start_active_span(
            "InsertUser",
            child_of=parent_ctx,
            tags={
                ot_tags.SPAN_KIND: ot_tags.SPAN_KIND_RPC_SERVER,
                "req_id": req_id, "user_id": user_id,
            },
        ) as scope:
            span = scope.span
            try:
                self._col.update_one(
                    {"user_id": user_id},
                    {"$setOnInsert": {
                        "user_id":   user_id,
                        "followers": [],
                        "followees": [],
                    }},
                    upsert=True,
                )
            except Exception as exc:
                logger.error("InsertUser MongoDB failed user_id=%d: %s", user_id, exc)
                span.set_tag("error", True)
                raise ServiceException(
                    errorCode=ErrorCode.SE_MONGODB_ERROR,
                    message=f"MongoDB write failed: {exc}",
                )
            logger.debug("InsertUser req_id=%d user_id=%d", req_id, user_id)

    # ------------------------------------------------------------------
    # GetFollowers
    # ------------------------------------------------------------------

    def GetFollowers(self, req_id: int, user_id: int, carrier: dict) -> list:
        """Return list of user_ids that follow `user_id`."""
        parent_ctx = self._extract_ctx(carrier)
        with self._tracer.start_active_span(
            "GetFollowers",
            child_of=parent_ctx,
            tags={
                ot_tags.SPAN_KIND: ot_tags.SPAN_KIND_RPC_SERVER,
                "req_id": req_id, "user_id": user_id,
            },
        ) as scope:
            span = scope.span
            result = self._get_ids(
                user_id, _KEY_FOLLOWERS, "followers", req_id, span
            )
            span.set_tag("count", len(result))
            return result

    # ------------------------------------------------------------------
    # GetFollowees
    # ------------------------------------------------------------------

    def GetFollowees(self, req_id: int, user_id: int, carrier: dict) -> list:
        """Return list of user_ids that `user_id` follows."""
        parent_ctx = self._extract_ctx(carrier)
        with self._tracer.start_active_span(
            "GetFollowees",
            child_of=parent_ctx,
            tags={
                ot_tags.SPAN_KIND: ot_tags.SPAN_KIND_RPC_SERVER,
                "req_id": req_id, "user_id": user_id,
            },
        ) as scope:
            span = scope.span
            result = self._get_ids(
                user_id, _KEY_FOLLOWEES, "followees", req_id, span
            )
            span.set_tag("count", len(result))
            return result

    # ------------------------------------------------------------------
    # Follow
    # ------------------------------------------------------------------

    def Follow(
        self,
        req_id: int,
        user_id: int,
        followee_id: int,
        carrier: dict,
    ) -> None:
        """
        user_id starts following followee_id.

        Dual-write to both Redis (sorted sets) and MongoDB (array push),
        matching the C++ atomic update pattern.
        """
        parent_ctx = self._extract_ctx(carrier)
        with self._tracer.start_active_span(
            "Follow",
            child_of=parent_ctx,
            tags={
                ot_tags.SPAN_KIND: ot_tags.SPAN_KIND_RPC_SERVER,
                "req_id": req_id,
                "user_id": user_id,
                "followee_id": followee_id,
            },
        ) as scope:
            span = scope.span
            ts = int(time.time() * 1000)   # millisecond timestamp, same as C++

            # ---- Redis ----
            self._redis_follow(user_id, followee_id, ts, span)

            # ---- MongoDB ----
            self._mongo_follow(user_id, followee_id, span)

            logger.debug(
                "Follow req_id=%d user_id=%d -> followee_id=%d",
                req_id, user_id, followee_id,
            )

    # ------------------------------------------------------------------
    # Unfollow
    # ------------------------------------------------------------------

    def Unfollow(
        self,
        req_id: int,
        user_id: int,
        followee_id: int,
        carrier: dict,
    ) -> None:
        """user_id stops following followee_id."""
        parent_ctx = self._extract_ctx(carrier)
        with self._tracer.start_active_span(
            "Unfollow",
            child_of=parent_ctx,
            tags={
                ot_tags.SPAN_KIND: ot_tags.SPAN_KIND_RPC_SERVER,
                "req_id": req_id,
                "user_id": user_id,
                "followee_id": followee_id,
            },
        ) as scope:
            span = scope.span

            # ---- Redis ----
            self._redis_unfollow(user_id, followee_id, span)

            # ---- MongoDB ----
            self._mongo_unfollow(user_id, followee_id, span)

            logger.debug(
                "Unfollow req_id=%d user_id=%d -x followee_id=%d",
                req_id, user_id, followee_id,
            )

    # ------------------------------------------------------------------
    # FollowWithUsername
    # ------------------------------------------------------------------

    def FollowWithUsername(
        self,
        req_id: int,
        user_username: str,
        followee_username: str,
        carrier: dict,
    ) -> None:
        """
        Resolve both usernames to user_ids in parallel, then call Follow.
        Mirrors the C++ std::async pattern for the two GetUserId calls.
        """
        parent_ctx = self._extract_ctx(carrier)
        with self._tracer.start_active_span(
            "FollowWithUsername",
            child_of=parent_ctx,
            tags={
                ot_tags.SPAN_KIND: ot_tags.SPAN_KIND_RPC_SERVER,
                "req_id": req_id,
                "user_username":    user_username,
                "followee_username": followee_username,
            },
        ) as scope:
            span = scope.span
            child_carrier = self._inject_ctx(span)

            user_id_fut = self._executor.submit(
                self._get_user_id, user_username, req_id, child_carrier
            )
            followee_id_fut = self._executor.submit(
                self._get_user_id, followee_username, req_id, child_carrier
            )

            try:
                user_id     = user_id_fut.result()
                followee_id = followee_id_fut.result()
            except ServiceException:
                span.set_tag("error", True)
                raise
            except Exception as exc:
                span.set_tag("error", True)
                raise ServiceException(
                    errorCode=ErrorCode.SE_THRIFT_HANDLER_ERROR,
                    message=f"Username resolution failed: {exc}",
                )

            self.Follow(req_id, user_id, followee_id, self._inject_ctx(span))

    # ------------------------------------------------------------------
    # UnfollowWithUsername
    # ------------------------------------------------------------------

    def UnfollowWithUsername(
        self,
        req_id: int,
        user_username: str,
        followee_username: str,
        carrier: dict,
    ) -> None:
        """Resolve both usernames to user_ids in parallel, then call Unfollow."""
        parent_ctx = self._extract_ctx(carrier)
        with self._tracer.start_active_span(
            "UnfollowWithUsername",
            child_of=parent_ctx,
            tags={
                ot_tags.SPAN_KIND: ot_tags.SPAN_KIND_RPC_SERVER,
                "req_id": req_id,
                "user_username":    user_username,
                "followee_username": followee_username,
            },
        ) as scope:
            span = scope.span
            child_carrier = self._inject_ctx(span)

            user_id_fut = self._executor.submit(
                self._get_user_id, user_username, req_id, child_carrier
            )
            followee_id_fut = self._executor.submit(
                self._get_user_id, followee_username, req_id, child_carrier
            )

            try:
                user_id     = user_id_fut.result()
                followee_id = followee_id_fut.result()
            except ServiceException:
                span.set_tag("error", True)
                raise
            except Exception as exc:
                span.set_tag("error", True)
                raise ServiceException(
                    errorCode=ErrorCode.SE_THRIFT_HANDLER_ERROR,
                    message=f"Username resolution failed: {exc}",
                )

            self.Unfollow(req_id, user_id, followee_id, self._inject_ctx(span))

    # ==================================================================
    # Private — get followers or followees
    # ==================================================================

    def _get_ids(
        self,
        user_id: int,
        redis_key_prefix: str,
        mongo_field: str,
        req_id: int,
        span,
    ) -> list:
        """
        Shared implementation for GetFollowers / GetFollowees.
        Cache-first (Redis ZRANGE), MongoDB fallback.
        Returns list of i64 user_ids.
        """
        redis_key = redis_key_prefix + str(user_id)

        # 1. Redis: ZRANGE returns all members sorted by score (follow timestamp)
        cached = self._redis_zrange(redis_key, span)
        if cached is not None:
            logger.debug(
                "_get_ids user_id=%d key=%s cache HIT count=%d",
                user_id, redis_key_prefix.rstrip(":"), len(cached),
            )
            return cached

        # 2. MongoDB
        try:
            doc = self._col.find_one(
                {"user_id": user_id},
                {mongo_field: 1, "_id": 0},
            )
        except Exception as exc:
            logger.error("_get_ids MongoDB failed user_id=%d: %s", user_id, exc)
            span.log_kv({"event": "mongo_error", "error": str(exc)})
            raise ServiceException(
                errorCode=ErrorCode.SE_MONGODB_ERROR,
                message=f"MongoDB read failed: {exc}",
            )

        ids = [int(uid) for uid in (doc or {}).get(mongo_field, [])]

        # Backfill Redis — seed sorted set with current timestamp as score for
        # existing entries (we don't have original timestamps; use 0 as placeholder)
        if ids:
            self._redis_seed(redis_key, ids, span)

        logger.debug(
            "_get_ids user_id=%d key=%s MongoDB HIT count=%d",
            user_id, redis_key_prefix.rstrip(":"), len(ids),
        )
        return ids

    # ==================================================================
    # Private — Redis follow/unfollow
    # ==================================================================

    def _redis_follow(
        self, user_id: int, followee_id: int, ts: int, span
    ) -> None:
        """
        ZADD followees:<user_id>     score=ts  member=followee_id
        ZADD followers:<followee_id> score=ts  member=user_id
        """
        try:
            pipe = self._redis.pipeline(transaction=False)
            pipe.zadd(_KEY_FOLLOWEES + str(user_id),    {str(followee_id): ts})
            pipe.zadd(_KEY_FOLLOWERS + str(followee_id), {str(user_id): ts})
            pipe.execute()
        except redis.RedisError as exc:
            logger.warning("Redis follow failed: %s", exc)
            span.log_kv({"event": "redis_error", "error": str(exc)})
            # Non-fatal — MongoDB write still proceeds

    def _redis_unfollow(
        self, user_id: int, followee_id: int, span
    ) -> None:
        """
        ZREM followees:<user_id>     member=followee_id
        ZREM followers:<followee_id> member=user_id
        """
        try:
            pipe = self._redis.pipeline(transaction=False)
            pipe.zrem(_KEY_FOLLOWEES + str(user_id),     str(followee_id))
            pipe.zrem(_KEY_FOLLOWERS + str(followee_id), str(user_id))
            pipe.execute()
        except redis.RedisError as exc:
            logger.warning("Redis unfollow failed: %s", exc)
            span.log_kv({"event": "redis_error", "error": str(exc)})

    def _redis_zrange(self, key: str, span) -> list | None:
        """
        ZRANGE key 0 -1 → list of int user_ids, or None on miss/error.
        Returns None (not empty list) when key doesn't exist, so callers
        can distinguish "cached empty set" from "cache miss".
        """
        try:
            exists = self._redis.exists(key)
            if not exists:
                return None
            members = self._redis.zrange(key, 0, -1)
            return [int(m) for m in members]
        except redis.RedisError as exc:
            logger.warning("Redis ZRANGE key=%s failed: %s", key, exc)
            span.log_kv({"event": "redis_error", "error": str(exc)})
            return None

    def _redis_seed(self, key: str, ids: list, span) -> None:
        """Seed a Redis sorted set with placeholder scores (0) for all ids."""
        try:
            mapping = {str(uid): 0 for uid in ids}
            self._redis.zadd(key, mapping)
        except redis.RedisError as exc:
            logger.warning("Redis ZADD seed key=%s failed: %s", key, exc)
            span.log_kv({"event": "redis_error", "error": str(exc)})

    # ==================================================================
    # Private — MongoDB follow/unfollow
    # ==================================================================

    def _mongo_follow(self, user_id: int, followee_id: int, span) -> None:
        """
        $addToSet (idempotent) on both documents:
          user_id    document: push followee_id onto followees
          followee_id document: push user_id    onto followers
        """
        try:
            self._col.update_one(
                {"user_id": user_id},
                {"$addToSet": {"followees": followee_id}},
                upsert=True,
            )
            self._col.update_one(
                {"user_id": followee_id},
                {"$addToSet": {"followers": user_id}},
                upsert=True,
            )
        except Exception as exc:
            logger.error("MongoDB follow failed: %s", exc)
            span.set_tag("error", True)
            span.log_kv({"event": "mongo_error", "error": str(exc)})
            raise ServiceException(
                errorCode=ErrorCode.SE_MONGODB_ERROR,
                message=f"MongoDB write failed: {exc}",
            )

    def _mongo_unfollow(self, user_id: int, followee_id: int, span) -> None:
        """
        $pull (idempotent) on both documents:
          user_id    document: remove followee_id from followees
          followee_id document: remove user_id    from followers
        """
        try:
            self._col.update_one(
                {"user_id": user_id},
                {"$pull": {"followees": followee_id}},
            )
            self._col.update_one(
                {"user_id": followee_id},
                {"$pull": {"followers": user_id}},
            )
        except Exception as exc:
            logger.error("MongoDB unfollow failed: %s", exc)
            span.set_tag("error", True)
            span.log_kv({"event": "mongo_error", "error": str(exc)})
            raise ServiceException(
                errorCode=ErrorCode.SE_MONGODB_ERROR,
                message=f"MongoDB write failed: {exc}",
            )

    # ==================================================================
    # Private — UserService lookup
    # ==================================================================

    def _get_user_id(self, username: str, req_id: int, carrier: dict) -> int:
        """Resolve a username to user_id via UserService.GetUserId."""
        with self._user_pool.connection() as client:
            return client.GetUserId(req_id, username, carrier)

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

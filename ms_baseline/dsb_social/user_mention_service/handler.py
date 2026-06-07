"""
UserMentionHandler — Python port of UserMentionHandler.h

Implements the Thrift UserMentionService.Iface.

What the C++ original does
--------------------------

ComposeUserMentions(req_id, usernames, carrier) -> list<UserMention>

For each username in `usernames`:
  1. Check Memcached (Redis in our port) for a cached user_id.
     Cache key = username,  value = str(user_id).
  2. If cached: use cached user_id.
  3. If not cached:
       a. Query MongoDB collection "user" for document where username == username.
       b. If found: extract user_id, populate cache.
       c. If not found: raise ServiceException(SE_THRIFT_HANDLER_ERROR).
  4. Build UserMention(user_id=<i64>, username=<str>) for each resolved entry.
  5. Return list<UserMention>.

Notes
-----
- UserMentionService shares the SAME MongoDB collection as UserService
  (db="user", collection="user"). It is a read-only consumer of that
  collection — it never writes users, it only looks them up.
- The original C++ uses Memcached for the username → user_id cache.
  We replace that with Redis (same key/value semantics, no TTL).
- Cache key  = username  (plain string)
  Cache value = str(user_id)   (decoded back to int on read)
- MongoDB document shape expected by this service:
    { "user_id": <i64>, "username": <str>, ... }
  (other fields such as password_hashed, salt, etc. are ignored here)
- MongoDB unique index on "username" is expected to already exist
  (created by UserService at startup). We do NOT create it here since
  this service is read-only.
"""

import logging

import opentracing
from opentracing.ext import tags as ot_tags
from opentracing.propagation import Format
from pymongo import MongoClient
import redis

from ms_baseline.dsb_social.gen_py.social_network import UserMentionService
from ms_baseline.dsb_social.gen_py.social_network.ttypes import UserMention, ServiceException, ErrorCode

logger = logging.getLogger("user-mention-service")

_CACHE_TTL = 0      # 0 = no expiry — matching original Memcached behaviour


class UserMentionHandler(UserMentionService.Iface):
    """
    Parameters
    ----------
    mongo_client : pymongo.MongoClient
    mongo_db     : str   — e.g. "user"
    mongo_col    : str   — e.g. "user"  (same collection as UserService)
    redis_client : redis.Redis
    tracer       : opentracing.Tracer
    """

    def __init__(
        self,
        mongo_client: MongoClient,
        mongo_db: str,
        mongo_col: str,
        redis_client: redis.Redis,
        tracer: opentracing.Tracer,
    ):
        self._col    = mongo_client[mongo_db][mongo_col]
        self._redis  = redis_client
        self._tracer = tracer

    # ------------------------------------------------------------------
    # Thrift interface
    # ------------------------------------------------------------------

    def ComposeUserMentions(
        self, req_id: int, usernames: list, carrier: dict
    ) -> list:
        """
        Resolve a list of @mention usernames to UserMention structs.

        Parameters
        ----------
        req_id    : i64        — trace request ID
        usernames : list[str]  — raw @mention usernames (without the '@')
        carrier   : dict       — OpenTracing propagation headers

        Returns
        -------
        list[UserMention]  — same order as input; each has .user_id + .username

        Raises
        ------
        ServiceException(SE_THRIFT_HANDLER_ERROR)
            If any username cannot be resolved.
        ServiceException(SE_MONGODB_ERROR)
            On MongoDB failure.
        ServiceException(SE_REDIS_ERROR)
            On a Redis failure that prevents resolution (should not happen in
            practice since Redis errors fall through to MongoDB).
        """
        parent_ctx = self._extract_ctx(carrier)

        with self._tracer.start_active_span(
            "ComposeUserMentions",
            child_of=parent_ctx,
            tags={
                ot_tags.SPAN_KIND: ot_tags.SPAN_KIND_RPC_SERVER,
                "req_id":         req_id,
                "username_count": len(usernames),
            },
        ) as scope:
            span = scope.span

            # De-duplicate while preserving order — the C++ implementation
            # processes each username once and builds the return list in the
            # same order as the input list.
            seen:   dict[str, UserMention] = {}   # username -> resolved UserMention
            result: list[UserMention]      = []

            for username in usernames:
                if username in seen:
                    # Already resolved earlier in this same request
                    result.append(seen[username])
                    continue

                user_id = self._resolve_username(username, req_id, span)
                mention = UserMention(user_id=user_id, username=username)
                seen[username] = mention
                result.append(mention)

            span.set_tag("resolved_count", len(seen))
            logger.debug(
                "ComposeUserMentions req_id=%d resolved %d usernames",
                req_id, len(seen),
            )
            return result

    # ------------------------------------------------------------------
    # Resolution pipeline: cache → MongoDB
    # ------------------------------------------------------------------

    def _resolve_username(self, username: str, req_id: int, span) -> int:
        """
        Return user_id for username via cache-first lookup.

        Raises ServiceException if not found anywhere.
        """
        # 1. Redis cache (username -> user_id)
        cached_id = self._cache_get(username, span)
        if cached_id is not None:
            logger.debug(
                "_resolve req_id=%d username=%r cache HIT user_id=%d",
                req_id, username, cached_id,
            )
            return cached_id

        # 2. MongoDB lookup
        doc = self._mongo_find(username, req_id, span)
        if doc is None:
            msg = f"User not found: {username!r}"
            logger.warning("ComposeUserMentions req_id=%d: %s", req_id, msg)
            span.set_tag("error", True)
            span.log_kv({"event": "user_not_found", "username": username})
            raise ServiceException(
                errorCode=ErrorCode.SE_THRIFT_HANDLER_ERROR,
                message=msg,
            )

        user_id = int(doc["user_id"])
        logger.debug(
            "_resolve req_id=%d username=%r MongoDB HIT user_id=%d",
            req_id, username, user_id,
        )

        # 3. Backfill cache
        self._cache_set(username, user_id, span)
        return user_id

    # ------------------------------------------------------------------
    # MongoDB helpers
    # ------------------------------------------------------------------

    def _mongo_find(self, username: str, req_id: int, span) -> dict | None:
        """Find a user document by username. Returns None if not found."""
        try:
            return self._col.find_one(
                {"username": username},
                # Project only the fields we need — avoids fetching
                # password hashes and salts over the wire unnecessarily.
                {"_id": 0, "user_id": 1, "username": 1},
            )
        except Exception as exc:
            logger.error(
                "MongoDB find failed req_id=%d username=%r: %s",
                req_id, username, exc,
            )
            span.set_tag("error", True)
            span.log_kv({"event": "mongo_error", "error": str(exc)})
            raise ServiceException(
                errorCode=ErrorCode.SE_MONGODB_ERROR,
                message=f"MongoDB read failed: {exc}",
            )

    # ------------------------------------------------------------------
    # Redis helpers
    # ------------------------------------------------------------------

    def _cache_get(self, username: str, span) -> int | None:
        """Return cached user_id (int) for username, or None on miss/error."""
        try:
            val = self._redis.get(username)
            if val is not None:
                return int(val)
            return None
        except redis.RedisError as exc:
            logger.warning("Redis GET username=%r failed: %s", username, exc)
            span.log_kv({"event": "redis_get_error", "key": username, "error": str(exc)})
            return None     # non-fatal — fall through to MongoDB

    def _cache_set(self, username: str, user_id: int, span) -> None:
        """Store username -> user_id mapping in Redis."""
        try:
            if _CACHE_TTL > 0:
                self._redis.setex(username, _CACHE_TTL, str(user_id))
            else:
                self._redis.set(username, str(user_id))
        except redis.RedisError as exc:
            logger.warning("Redis SET username=%r failed: %s", username, exc)
            span.log_kv({"event": "redis_set_error", "key": username, "error": str(exc)})
            # Non-fatal — data is still readable from MongoDB next time

    # ------------------------------------------------------------------
    # Tracing helper
    # ------------------------------------------------------------------

    def _extract_ctx(self, carrier: dict):
        try:
            return self._tracer.extract(Format.TEXT_MAP, carrier)
        except Exception:
            return None

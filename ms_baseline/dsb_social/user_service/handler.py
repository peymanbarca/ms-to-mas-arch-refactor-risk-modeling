"""
UserHandler — Python port of UserHandler.h

Implements the full UserService.Iface Thrift interface.

Methods
-------
RegisterUser            — create a new user with auto-generated user_id
RegisterUserWithId      — create a new user with a caller-supplied user_id
                          (used by init_social_graph.py seed script)
Login                   — verify password, return signed JWT
ComposeCreatorWithUserId   — build Creator struct from user_id + username
                             (no DB lookup needed — caller already has both)
ComposeCreatorWithUsername — resolve username → user_id, build Creator struct
GetUserId               — resolve username → user_id (i64)

Storage layout
--------------
MongoDB document (db="user", collection="user"):
  {
    "user_id":         i64,
    "first_name":      str,
    "last_name":       str,
    "username":        str,         <- unique index
    "password_hashed": str,         <- SHA-256 hex of (password + salt)
    "salt":            str          <- 64-char hex random salt
  }

Redis cache (replacing Memcached):
  key   = username
  value = str(user_id)

  key   = str(user_id)
  value = JSON-encoded full user document (for ComposeCreatorWithUserId)

User-id generation
------------------
The C++ handler keeps an in-process atomic counter initialised from
  max(existing user_id) + 1
on startup, and increments it per RegisterUser call. We replicate this
with a threading.Lock-protected counter seeded the same way.
RegisterUserWithId writes the supplied id directly and updates the counter
if the supplied id is larger than the current counter.
"""

import json
import logging
import threading

import opentracing
from opentracing.ext import tags as ot_tags
from opentracing.propagation import Format
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError
import redis

from ms_baseline.dsb_social.gen_py.social_network import UserService
from ms_baseline.dsb_social.gen_py.social_network.ttypes import (
    Creator, ServiceException, ErrorCode,
)
from .password import generate_salt, hash_password, verify_password
from .jwt_helper import generate_token

logger = logging.getLogger("user-service")

_CACHE_TTL = 0   # no expiry — match original Memcached behaviour

# Redis key prefixes
_KEY_USERNAME = "username:"   # username  -> user_id  (str)
_KEY_USER_ID  = "userid:"     # user_id   -> JSON user doc


class UserHandler(UserService.Iface):
    """
    Parameters
    ----------
    mongo_client   : pymongo.MongoClient
    mongo_db       : str   e.g. "user"
    mongo_col      : str   e.g. "user"
    redis_client   : redis.Redis
    secret         : str   JWT signing secret (from service-config.json)
    jwt_expiry     : int   JWT lifetime in seconds (default 3600)
    tracer         : opentracing.Tracer
    """

    def __init__(
        self,
        mongo_client: MongoClient,
        mongo_db: str,
        mongo_col: str,
        redis_client: redis.Redis,
        secret: str,
        jwt_expiry: int,
        tracer: opentracing.Tracer,
    ):
        self._col       = mongo_client[mongo_db][mongo_col]
        self._redis     = redis_client
        self._secret    = secret
        self._jwt_expiry = jwt_expiry
        self._tracer    = tracer

        # Unique index on username
        self._col.create_index("username", unique=True, background=True)
        # Index on user_id for fast lookups
        self._col.create_index("user_id", unique=True, background=True)

        # Seed the counter from the highest existing user_id
        self._counter_lock = threading.Lock()
        self._counter = self._seed_counter()
        logger.info("UserHandler ready, next user_id counter starts at %d", self._counter)

    # ------------------------------------------------------------------
    # Counter helpers
    # ------------------------------------------------------------------

    def _seed_counter(self) -> int:
        """Return max(user_id) + 1 from MongoDB, or 1 if collection is empty."""
        try:
            doc = self._col.find_one(
                {}, {"user_id": 1, "_id": 0}, sort=[("user_id", -1)]
            )
            return (doc["user_id"] + 1) if doc else 1
        except Exception as exc:
            logger.warning("Could not seed user_id counter: %s — starting at 1", exc)
            return 1

    def _next_user_id(self) -> int:
        with self._counter_lock:
            uid = self._counter
            self._counter += 1
            return uid

    def _update_counter_if_needed(self, user_id: int) -> None:
        """Ensure counter stays above any externally supplied user_id."""
        with self._counter_lock:
            if user_id >= self._counter:
                self._counter = user_id + 1

    # ------------------------------------------------------------------
    # RegisterUser
    # ------------------------------------------------------------------

    def RegisterUser(
        self,
        req_id: int,
        first_name: str,
        last_name: str,
        username: str,
        password: str,
        carrier: dict,
    ) -> None:
        """Register a new user with an auto-generated user_id."""
        parent_ctx = self._extract_ctx(carrier)
        with self._tracer.start_active_span(
            "RegisterUser",
            child_of=parent_ctx,
            tags={
                ot_tags.SPAN_KIND: ot_tags.SPAN_KIND_RPC_SERVER,
                "req_id": req_id, "username": username,
            },
        ) as scope:
            user_id = self._next_user_id()
            self._register(
                req_id, first_name, last_name, username,
                password, user_id, scope.span,
            )

    # ------------------------------------------------------------------
    # RegisterUserWithId
    # ------------------------------------------------------------------

    def RegisterUserWithId(
        self,
        req_id: int,
        first_name: str,
        last_name: str,
        username: str,
        password: str,
        user_id: int,
        carrier: dict,
    ) -> None:
        """
        Register a user with an explicit user_id.
        Used by the init_social_graph.py seed script which pre-assigns IDs.
        """
        parent_ctx = self._extract_ctx(carrier)
        with self._tracer.start_active_span(
            "RegisterUserWithId",
            child_of=parent_ctx,
            tags={
                ot_tags.SPAN_KIND: ot_tags.SPAN_KIND_RPC_SERVER,
                "req_id": req_id, "username": username, "user_id": user_id,
            },
        ) as scope:
            self._update_counter_if_needed(user_id)
            self._register(
                req_id, first_name, last_name, username,
                password, user_id, scope.span,
            )

    # ------------------------------------------------------------------
    # Login
    # ------------------------------------------------------------------

    def Login(
        self,
        req_id: int,
        username: str,
        password: str,
        carrier: dict,
    ) -> str:
        """
        Verify credentials and return a signed JWT on success.

        Returns
        -------
        str — signed JWT token

        Raises
        ------
        ServiceException(SE_UNAUTHORIZED)     — wrong password
        ServiceException(SE_THRIFT_HANDLER_ERROR) — user not found
        """
        parent_ctx = self._extract_ctx(carrier)
        with self._tracer.start_active_span(
            "Login",
            child_of=parent_ctx,
            tags={
                ot_tags.SPAN_KIND: ot_tags.SPAN_KIND_RPC_SERVER,
                "req_id": req_id, "username": username,
            },
        ) as scope:
            span = scope.span

            doc = self._fetch_user_by_username(username, req_id, span)
            if doc is None:
                span.set_tag("error", True)
                raise ServiceException(
                    errorCode=ErrorCode.SE_THRIFT_HANDLER_ERROR,
                    message=f"User not found: {username!r}",
                )

            if not verify_password(password, doc["salt"], doc["password_hashed"]):
                logger.warning("Login failed — wrong password req_id=%d username=%r",
                               req_id, username)
                span.set_tag("error", True)
                raise ServiceException(
                    errorCode=ErrorCode.SE_UNAUTHORIZED,
                    message="Invalid username or password",
                )

            token = generate_token(
                doc["user_id"], username, self._secret, self._jwt_expiry
            )
            logger.debug("Login OK req_id=%d username=%r user_id=%d",
                         req_id, username, doc["user_id"])
            return token

    # ------------------------------------------------------------------
    # ComposeCreatorWithUserId
    # ------------------------------------------------------------------

    def ComposeCreatorWithUserId(
        self,
        req_id: int,
        user_id: int,
        username: str,
        carrier: dict,
    ) -> Creator:
        """
        Build and return a Creator struct from user_id + username.

        The C++ implementation simply constructs the Creator struct directly
        from the supplied arguments — no DB lookup required because the
        caller (Nginx Lua / ComposePostService) already has both values from
        the JWT claims.
        """
        parent_ctx = self._extract_ctx(carrier)
        with self._tracer.start_active_span(
            "ComposeCreatorWithUserId",
            child_of=parent_ctx,
            tags={
                ot_tags.SPAN_KIND: ot_tags.SPAN_KIND_RPC_SERVER,
                "req_id": req_id, "user_id": user_id, "username": username,
            },
        ):
            logger.debug(
                "ComposeCreatorWithUserId req_id=%d user_id=%d username=%r",
                req_id, user_id, username,
            )
            return Creator(user_id=user_id, username=username)

    # ------------------------------------------------------------------
    # ComposeCreatorWithUsername
    # ------------------------------------------------------------------

    def ComposeCreatorWithUsername(
        self,
        req_id: int,
        username: str,
        carrier: dict,
    ) -> Creator:
        """
        Resolve username → user_id via cache/MongoDB, then return Creator.
        """
        parent_ctx = self._extract_ctx(carrier)
        with self._tracer.start_active_span(
            "ComposeCreatorWithUsername",
            child_of=parent_ctx,
            tags={
                ot_tags.SPAN_KIND: ot_tags.SPAN_KIND_RPC_SERVER,
                "req_id": req_id, "username": username,
            },
        ) as scope:
            span = scope.span
            user_id = self._resolve_username_to_id(username, req_id, span)
            return Creator(user_id=user_id, username=username)

    # ------------------------------------------------------------------
    # GetUserId
    # ------------------------------------------------------------------

    def GetUserId(
        self,
        req_id: int,
        username: str,
        carrier: dict,
    ) -> int:
        """Resolve username → user_id (i64)."""
        parent_ctx = self._extract_ctx(carrier)
        with self._tracer.start_active_span(
            "GetUserId",
            child_of=parent_ctx,
            tags={
                ot_tags.SPAN_KIND: ot_tags.SPAN_KIND_RPC_SERVER,
                "req_id": req_id, "username": username,
            },
        ) as scope:
            span = scope.span
            return self._resolve_username_to_id(username, req_id, span)

    # ==================================================================
    # Private — shared registration logic
    # ==================================================================

    def _register(
        self,
        req_id: int,
        first_name: str,
        last_name: str,
        username: str,
        password: str,
        user_id: int,
        span,
    ) -> None:
        """
        Hash password, build document, insert into MongoDB, populate cache.
        Raises ServiceException on duplicate username or MongoDB error.
        """
        salt            = generate_salt()
        password_hashed = hash_password(password, salt)

        doc = {
            "user_id":         user_id,
            "first_name":      first_name,
            "last_name":       last_name,
            "username":        username,
            "password_hashed": password_hashed,
            "salt":            salt,
        }

        try:
            self._col.insert_one(doc)
        except DuplicateKeyError:
            msg = f"Username already registered: {username!r}"
            logger.warning("RegisterUser req_id=%d: %s", req_id, msg)
            span.set_tag("error", True)
            raise ServiceException(
                errorCode=ErrorCode.SE_THRIFT_HANDLER_ERROR,
                message=msg,
            )
        except Exception as exc:
            logger.error("MongoDB insert failed req_id=%d: %s", req_id, exc)
            span.set_tag("error", True)
            raise ServiceException(
                errorCode=ErrorCode.SE_MONGODB_ERROR,
                message=f"MongoDB write failed: {exc}",
            )

        # Populate both cache directions
        self._cache_set_username(username, user_id, span)
        self._cache_set_user_doc(user_id, doc, span)

        logger.debug(
            "Registered user req_id=%d username=%r user_id=%d",
            req_id, username, user_id,
        )

    # ==================================================================
    # Private — lookup helpers
    # ==================================================================

    def _fetch_user_by_username(
        self, username: str, req_id: int, span
    ) -> dict | None:
        """
        Fetch full user document by username.
        Cache key: "username:<username>" -> user_id, then "userid:<user_id>" -> JSON doc.
        Falls back to MongoDB on cache miss.
        """
        # 1. Try username cache to get user_id
        cached_id = self._cache_get_username(username, span)
        if cached_id is not None:
            # 2. Try user_id cache for full doc
            cached_doc = self._cache_get_user_doc(cached_id, span)
            if cached_doc is not None:
                logger.debug("_fetch_user_by_username cache HIT username=%r", username)
                return cached_doc

        # 3. MongoDB
        try:
            doc = self._col.find_one({"username": username})
        except Exception as exc:
            logger.error("MongoDB find failed username=%r: %s", username, exc)
            span.log_kv({"event": "mongo_error", "error": str(exc)})
            raise ServiceException(
                errorCode=ErrorCode.SE_MONGODB_ERROR,
                message=f"MongoDB read failed: {exc}",
            )

        if doc is None:
            return None

        doc.pop("_id", None)
        self._cache_set_username(username, doc["user_id"], span)
        self._cache_set_user_doc(doc["user_id"], doc, span)
        return doc

    def _resolve_username_to_id(
        self, username: str, req_id: int, span
    ) -> int:
        """Return user_id for username. Raises ServiceException if not found."""
        # Fast path: username cache
        cached_id = self._cache_get_username(username, span)
        if cached_id is not None:
            return cached_id

        # MongoDB
        try:
            doc = self._col.find_one(
                {"username": username},
                {"user_id": 1, "username": 1, "_id": 0},
            )
        except Exception as exc:
            span.log_kv({"event": "mongo_error", "error": str(exc)})
            raise ServiceException(
                errorCode=ErrorCode.SE_MONGODB_ERROR,
                message=f"MongoDB read failed: {exc}",
            )

        if doc is None:
            span.set_tag("error", True)
            raise ServiceException(
                errorCode=ErrorCode.SE_THRIFT_HANDLER_ERROR,
                message=f"User not found: {username!r}",
            )

        self._cache_set_username(username, doc["user_id"], span)
        return int(doc["user_id"])

    # ==================================================================
    # Redis helpers
    # ==================================================================

    def _cache_get_username(self, username: str, span) -> int | None:
        """username -> user_id. Returns int or None."""
        try:
            val = self._redis.get(_KEY_USERNAME + username)
            return int(val) if val is not None else None
        except redis.RedisError as exc:
            span.log_kv({"event": "redis_get_error", "error": str(exc)})
            return None

    def _cache_set_username(self, username: str, user_id: int, span) -> None:
        try:
            self._redis.set(_KEY_USERNAME + username, str(user_id))
        except redis.RedisError as exc:
            span.log_kv({"event": "redis_set_error", "error": str(exc)})

    def _cache_get_user_doc(self, user_id: int, span) -> dict | None:
        """user_id -> full user document dict. Returns dict or None."""
        try:
            val = self._redis.get(_KEY_USER_ID + str(user_id))
            if val is not None:
                return json.loads(val)
            return None
        except redis.RedisError as exc:
            span.log_kv({"event": "redis_get_error", "error": str(exc)})
            return None

    def _cache_set_user_doc(self, user_id: int, doc: dict, span) -> None:
        """Store the sanitised user document in Redis (no _id field)."""
        try:
            safe_doc = {k: v for k, v in doc.items() if k != "_id"}
            if _CACHE_TTL > 0:
                self._redis.setex(
                    _KEY_USER_ID + str(user_id), _CACHE_TTL,
                    json.dumps(safe_doc),
                )
            else:
                self._redis.set(_KEY_USER_ID + str(user_id), json.dumps(safe_doc))
        except redis.RedisError as exc:
            span.log_kv({"event": "redis_set_error", "error": str(exc)})

    # ==================================================================
    # Tracing helper
    # ==================================================================

    def _extract_ctx(self, carrier: dict):
        try:
            return self._tracer.extract(Format.TEXT_MAP, carrier)
        except Exception:
            return None
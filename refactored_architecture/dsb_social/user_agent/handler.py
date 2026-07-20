"""
UserHandler (Agent version) — Thrift interface UNCHANGED.

All methods that contain static logic now drive LangGraph agents:

  RegisterUser / RegisterUserWithId  → self._register_graph
  Login                              → self._login_graph
  GetUserId                          → self._resolve_graph
  ComposeCreatorWithUsername         → self._resolve_graph
  ComposeCreatorWithUserId           → no graph (pure struct assembly, no logic)

The deterministic _register() helper is REMOVED.
verify_password() and generate_token() are no longer called directly here —
they live inside validate_verify and issue_token graph nodes as fallbacks.
"""

import asyncio
import json
import logging
import threading

import opentracing
from opentracing.ext import tags as ot_tags
from opentracing.propagation import Format
from pymongo import MongoClient
import redis
import time

from ms_baseline.dsb_social.gen_py.social_network import UserService
from ms_baseline.dsb_social.gen_py.social_network.ttypes import (
    Creator, ServiceException, ErrorCode,
)
from .agent import (
    build_register_agent,
    build_login_agent,
    RegisterAgentState,
    LoginAgentState,
    generate_salt
    )

logger = logging.getLogger("user-agent.handler")

_KEY_USERNAME = "username:"
_KEY_USER_ID  = "userid:"


class UserHandler(UserService.Iface):

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
        self._col        = mongo_client[mongo_db][mongo_col]
        self._redis      = redis_client
        self._secret     = secret
        self._jwt_expiry = jwt_expiry
        self._tracer     = tracer

        self._col.create_index("username", unique=True, background=True)
        self._col.create_index("user_id",  unique=True, background=True)

        self._counter_lock = threading.Lock()
        self._counter      = self._seed_counter()
        logger.info("UserHandler ready, counter starts at %d", self._counter)

        # ── Compile all three agent graphs once ──
        self._register_graph = build_register_agent(
            redis_client, self._col
        )
        self._login_graph    = build_login_agent(redis_client, self._col)
        # self._resolve_graph  = build_resolve_username_agent(
        #     redis_client, self._col
        # )

    # ------------------------------------------------------------------
    # Counter helpers
    # ------------------------------------------------------------------

    def _seed_counter(self) -> int:
        try:
            doc = self._col.find_one(
                {}, {"user_id": 1, "_id": 0}, sort=[("user_id", -1)]
            )
            return (doc["user_id"] + 1) if doc else 1
        except Exception as exc:
            logger.warning("Could not seed counter: %s — starting at 1", exc)
            return 1

    def _next_user_id(self) -> int:
        with self._counter_lock:
            uid = self._counter
            self._counter += 1
            return uid

    def _update_counter_if_needed(self, user_id: int) -> None:
        with self._counter_lock:
            if user_id >= self._counter:
                self._counter = user_id + 1

    # ------------------------------------------------------------------
    # RegisterUser  →  register_graph
    # ------------------------------------------------------------------

    def RegisterUser(self, req_id, first_name, last_name, username, password, carrier):
        parent_ctx = self._extract_ctx(carrier)
        with self._tracer.start_active_span(
            "RegisterUser", child_of=parent_ctx,
            tags={ot_tags.SPAN_KIND: ot_tags.SPAN_KIND_RPC_SERVER,
                  "req_id": req_id, "username": username},
        ) as scope:
            user_id = self._next_user_id()
            self._run_register(req_id, first_name, last_name,
                               username, password, user_id, scope.span)

    # ------------------------------------------------------------------
    # RegisterUserWithId  →  register_graph
    # ------------------------------------------------------------------

    def RegisterUserWithId(self, req_id, first_name, last_name,
                           username, password, user_id, carrier):
        parent_ctx = self._extract_ctx(carrier)
        with self._tracer.start_active_span(
            "RegisterUserWithId", child_of=parent_ctx,
            tags={ot_tags.SPAN_KIND: ot_tags.SPAN_KIND_RPC_SERVER,
                  "req_id": req_id, "username": username, "user_id": user_id},
        ) as scope:
            self._update_counter_if_needed(user_id)
            self._run_register(req_id, first_name, last_name,
                               username, password, user_id, scope.span)

    # ------------------------------------------------------------------
    # Login  →  login_graph
    # ------------------------------------------------------------------

    def Login(self, req_id, username, password, carrier) -> str:
        parent_ctx = self._extract_ctx(carrier)
        with self._tracer.start_active_span(
            "Login", child_of=parent_ctx,
            tags={ot_tags.SPAN_KIND: ot_tags.SPAN_KIND_RPC_SERVER,
                  "req_id": req_id, "username": username},
        ) as scope:
            span = scope.span
            t1 = time.time()

            initial: LoginAgentState = {
                "req_id": req_id,
                "username": username,
                "password": password,
                "secret": self._secret,
                "jwt_expiry": self._jwt_expiry,
                "user_doc": None,
                "llm_match": None,
                "verified": None,
                "token": None,
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "total_llm_calls": 0,
                "fallback_used": False,
            }

            try:
                out = asyncio.run(self._login_graph.ainvoke(initial))
            except ServiceException:
                span.set_tag("error", True)
                raise
            except Exception as exc:
                logger.exception("Login graph failed req_id=%d", req_id)
                span.set_tag("error", True)
                raise ServiceException(
                    errorCode=ErrorCode.SE_THRIFT_HANDLER_ERROR,
                    message=f"Login agent failed: {exc}",
                )

            self._log_metrics("Login", req_id, out, span)

            token = out.get("token")
            if not token:
                span.set_tag("error", True)
                raise ServiceException(
                    errorCode=ErrorCode.SE_THRIFT_HANDLER_ERROR,
                    message="Login agent did not produce a token",
                )
            t2 = time.time()
            logger.info("Login req_id=%d username=%r completed in %.3f seconds", req_id, initial["username"], t2 - t1)
            return token

    # ------------------------------------------------------------------
    # ComposeCreatorWithUserId  — no graph (pure struct, no logic)
    # ------------------------------------------------------------------

    def ComposeCreatorWithUserId(self, req_id, user_id, username, carrier):
        parent_ctx = self._extract_ctx(carrier)
        with self._tracer.start_active_span(
            "ComposeCreatorWithUserId", child_of=parent_ctx,
            tags={ot_tags.SPAN_KIND: ot_tags.SPAN_KIND_RPC_SERVER,
                  "req_id": req_id, "user_id": user_id, "username": username},
        ):
            return Creator(user_id=user_id, username=username)

    # ------------------------------------------------------------------
    # ComposeCreatorWithUsername  →  resolve_graph
    # ------------------------------------------------------------------

    def ComposeCreatorWithUsername(self, req_id, username, carrier):
        parent_ctx = self._extract_ctx(carrier)
        with self._tracer.start_active_span(
            "ComposeCreatorWithUsername", child_of=parent_ctx,
            tags={ot_tags.SPAN_KIND: ot_tags.SPAN_KIND_RPC_SERVER,
                  "req_id": req_id, "username": username},
        ) as scope:
            user_id = self._run_resolve(req_id, username, scope.span)
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
    # Private — graph invocation helpers
    # ==================================================================

    def _run_register(self, req_id, first_name, last_name,
                      username, password, user_id, span) -> None:
        """Invoke register_graph. Propagates ServiceException on failure."""
        t1 = time.time()
        initial: RegisterAgentState = {
            "req_id": req_id,
            "first_name": first_name,
            "last_name": last_name,
            "username": username,
            "password": password,
            "user_id": user_id,
            "salt": generate_salt(),
            "llm_password_hashed": None,
            "password_hashed": None,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "total_llm_calls": 0,
            "fallback_used": False,
        }
        try:
            out = asyncio.run(self._register_graph.ainvoke(initial))
        except ServiceException:
            span.set_tag("error", True)
            raise
        except Exception as exc:
            logger.exception("Register graph failed req_id=%d", req_id)
            span.set_tag("error", True)
            raise ServiceException(
                errorCode=ErrorCode.SE_THRIFT_HANDLER_ERROR,
                message=f"Register agent failed: {exc}",
            )
        self._log_metrics("Register", req_id, out, span)
        t2 = time.time()
        logger.info("Register req_id=%d user_name=%r completed in %.3f seconds", req_id, initial["username"], t2 - t1)


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
    
    # def _run_resolve(self, req_id: int, username: str, span) -> int:
    #     """Invoke resolve_graph. Returns user_id. Propagates ServiceException."""
    #     initial: ResolveUsernameState = {
    #         "req_id":              req_id,
    #         "username":            username,
    #         "cache_hit":           False,
    #         "cached_id":           None,
    #         "fetched_doc":         None,
    #         "llm_user_id":         None,
    #         "final_user_id":       None,
    #         "total_input_tokens":  0,
    #         "total_output_tokens": 0,
    #         "total_llm_calls":     0,
    #         "fallback_used":       False,
    #     }
    #     try:
    #         out = asyncio.run(self._resolve_graph.ainvoke(initial))
    #     except ServiceException:
    #         span.set_tag("error", True)
    #         raise
    #     except Exception as exc:
    #         logger.exception("Resolve graph failed req_id=%d username=%r",
    #                          req_id, username)
    #         span.set_tag("error", True)
    #         raise ServiceException(
    #             errorCode=ErrorCode.SE_THRIFT_HANDLER_ERROR,
    #             message=f"Resolve agent failed: {exc}",
    #         )

    #     # Backfill Redis on cache miss
    #     uid = out.get("final_user_id")
    #     if uid and not out.get("cache_hit"):
    #         try:
    #             self._redis.set(_KEY_USERNAME + username, str(uid))
    #         except Exception:
    #             pass

    #     self._log_metrics("Resolve", req_id, out, span)
    #     return out["final_user_id"]

    # ==================================================================
    # Private — logging + tracing
    # ==================================================================

    def _log_metrics(self, op: str, req_id: int, out: dict, span) -> None:
        in_tok   = out.get("total_input_tokens",  0)
        out_tok  = out.get("total_output_tokens", 0)
        calls    = out.get("total_llm_calls",     0)
        fallback = out.get("fallback_used",       False)
        logger.info(
            "%s req_id=%d llm_calls=%d in=%d out=%d fallback=%s",
            op, req_id, calls, in_tok, out_tok, fallback,
        )
        print(f"[handler:{op}] req_id={req_id} llm_calls={calls} "
              f"in_tokens={in_tok} out_tokens={out_tok} fallback={fallback}")
        span.set_tag("llm_calls", calls)
        span.set_tag("fallback",  fallback)

    def _extract_ctx(self, carrier: dict):
        try:
            return self._tracer.extract(Format.TEXT_MAP, carrier)
        except Exception:
            return None
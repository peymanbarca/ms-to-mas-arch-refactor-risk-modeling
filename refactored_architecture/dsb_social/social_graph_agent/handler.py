"""
SocialGraphHandler — Python port of SocialGraphHandler.h

Implements the full SocialGraphService.Iface Thrift interface.

Graph-backed methods
---------------------
GetFollowers(req_id, user_id, carrier)          -> list<i64>
GetFollowees(req_id, user_id, carrier)          -> list<i64>
Follow(req_id, user_id, followee_id, carrier)
Unfollow(req_id, user_id, followee_id, carrier)
FollowWithUsername(req_id, user_username, followee_username, carrier)
UnfollowWithUsername(req_id, user_username, followee_username, carrier)

Deterministic method
--------------------
InsertUser(req_id, user_id, carrier)

Notes
-----
- The read/mutation paths are now executed through LangGraph agents.
- InsertUser remains a direct MongoDB upsert, because the agent design keeps it deterministic.
- The handler preserves tracing and error translation to Thrift ServiceException.

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

import asyncio
import logging
import time
from typing import Any

import opentracing
from opentracing.ext import tags as ot_tags
from opentracing.propagation import Format
from pymongo import MongoClient
import redis

from ms_baseline.dsb_social.gen_py.social_network import SocialGraphService
from ms_baseline.dsb_social.gen_py.social_network.ttypes import (
    ServiceException,
    ErrorCode,
)
from .thrift_pool import ThriftClientPool
from .agent import (
    build_follow_agent,
    build_get_graph_agent,
    build_resolve_follow_agent,
    FollowAgentState,
    GetGraphAgentState,
    ResolveFollowAgentState,
)

logger = logging.getLogger("social-graph-service")

_KEY_FOLLOWERS = "followers:"
_KEY_FOLLOWEES = "followees:"


class SocialGraphHandler(SocialGraphService.Iface):
    """
    Parameters
    ----------
    mongo_client      : pymongo.MongoClient
    mongo_db          : str   e.g. "social-graph"
    mongo_col         : str   e.g. "social-graph"
    redis_client      : redis.Redis
    user_service_pool : ThriftClientPool for UserService (needed for username resolution)
    tracer            : opentracing.Tracer
    num_workers       : int   retained for compatibility; resolution is now inside the graph
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
        self._col = mongo_client[mongo_db][mongo_col]
        self._redis = redis_client
        self._user_pool = user_service_pool
        self._tracer = tracer

        # Compile graphs once.
        self._follow_graph = build_follow_agent(self._redis, self._col)
        self._get_graph = build_get_graph_agent(self._redis, self._col)
        self._resolve_follow_graph = build_resolve_follow_agent(
            self._redis,
            self._col,
            self._user_pool,
        )

        # Unique index on user_id — mirrors C++ MongoDB setup.
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
                "req_id": req_id,
                "user_id": user_id,
            },
        ) as scope:
            span = scope.span
            t1 = time.time()
            try:
                self._col.update_one(
                    {"user_id": user_id},
                    {
                        "$setOnInsert": {
                            "user_id": user_id,
                            "followers": [],
                            "followees": [],
                        }
                    },
                    upsert=True,
                )
            except Exception as exc:
                logger.error("InsertUser MongoDB failed user_id=%d: %s", user_id, exc)
                span.set_tag("error", True)
                raise ServiceException(
                    errorCode=ErrorCode.SE_MONGODB_ERROR,
                    message=f"MongoDB write failed: {exc}",
                )
            t2 = time.time()
            logger.info("InsertUser req_id=%d user_id=%d completed in %.3f seconds", req_id, user_id, t2 - t1)

    # ------------------------------------------------------------------
    # GetFollowers
    # ------------------------------------------------------------------

    def GetFollowers(self, req_id: int, user_id: int, carrier: dict) -> list:
        """Return list of user_ids that follow user_id."""
        parent_ctx = self._extract_ctx(carrier)
        with self._tracer.start_active_span(
            "GetFollowers",
            child_of=parent_ctx,
            tags={
                ot_tags.SPAN_KIND: ot_tags.SPAN_KIND_RPC_SERVER,
                "req_id": req_id,
                "user_id": user_id,
            },
        ) as scope:
            span = scope.span
            t1 = time.time()

            initial: GetGraphAgentState = {
                "req_id": req_id,
                "user_id": user_id,
                "direction": "followers",
                "raw_ids": [],
                "llm_valid": None,
                "llm_cleaned_ids": None,
                "final_ids": [],
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "total_llm_calls": 0,
                "fallback_used": False,
            }

            out = self._run_graph(
                self._get_graph,
                initial,
                span,
                op_name="GetFollowers",
                req_id=req_id,
            )

            result = list(out.get("final_ids") or [])
            span.set_tag("count", len(result))
            self._log_metrics("GetFollowers", req_id, out, span)
            t2 = time.time()
            logger.info("GetFollowers req_id=%d user_id=%d count=%d completed in %.3f seconds", req_id, user_id, len(result), t2 - t1)
            return result

    # ------------------------------------------------------------------
    # GetFollowees
    # ------------------------------------------------------------------

    def GetFollowees(self, req_id: int, user_id: int, carrier: dict) -> list:
        """Return list of user_ids that user_id follows."""
        parent_ctx = self._extract_ctx(carrier)
        with self._tracer.start_active_span(
            "GetFollowees",
            child_of=parent_ctx,
            tags={
                ot_tags.SPAN_KIND: ot_tags.SPAN_KIND_RPC_SERVER,
                "req_id": req_id,
                "user_id": user_id,
            },
        ) as scope:
            span = scope.span

            initial: GetGraphAgentState = {
                "req_id": req_id,
                "user_id": user_id,
                "direction": "followees",
                "raw_ids": [],
                "llm_valid": None,
                "llm_cleaned_ids": None,
                "final_ids": [],
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "total_llm_calls": 0,
                "fallback_used": False,
            }

            out = self._run_graph(
                self._get_graph,
                initial,
                span,
                op_name="GetFollowees",
                req_id=req_id,
            )

            result = list(out.get("final_ids") or [])
            span.set_tag("count", len(result))
            self._log_metrics("GetFollowees", req_id, out, span)
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
        Now driven by follow_graph.
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

            initial: FollowAgentState = {
                "req_id": req_id,
                "user_id": user_id,
                "followee_id": followee_id,
                "operation": "follow",
                "user_username": None,
                "followee_username": None,
                "user_followees": [],
                "followee_followers": [],
                "llm_approved": None,
                "llm_reason": None,
                "approved": None,
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "total_llm_calls": 0,
                "fallback_used": False,
            }

            out = self._run_graph(
                self._follow_graph,
                initial,
                span,
                op_name="Follow",
                req_id=req_id,
            )

            if not out.get("approved", False):
                span.set_tag("error", True)
                raise ServiceException(
                    errorCode=ErrorCode.SE_THRIFT_HANDLER_ERROR,
                    message=out.get("llm_reason") or "Follow request rejected",
                )

            self._log_metrics("Follow", req_id, out, span)
            logger.debug(
                "Follow req_id=%d user_id=%d -> followee_id=%d",
                req_id,
                user_id,
                followee_id,
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
        """user_id stops following followee_id. Now driven by follow_graph."""
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

            initial: FollowAgentState = {
                "req_id": req_id,
                "user_id": user_id,
                "followee_id": followee_id,
                "operation": "unfollow",
                "user_username": None,
                "followee_username": None,
                "user_followees": [],
                "followee_followers": [],
                "llm_approved": None,
                "llm_reason": None,
                "approved": None,
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "total_llm_calls": 0,
                "fallback_used": False,
            }

            out = self._run_graph(
                self._follow_graph,
                initial,
                span,
                op_name="Unfollow",
                req_id=req_id,
            )

            if not out.get("approved", False):
                span.set_tag("error", True)
                raise ServiceException(
                    errorCode=ErrorCode.SE_THRIFT_HANDLER_ERROR,
                    message=out.get("llm_reason") or "Unfollow request rejected",
                )

            self._log_metrics("Unfollow", req_id, out, span)
            logger.debug(
                "Unfollow req_id=%d user_id=%d -x followee_id=%d",
                req_id,
                user_id,
                followee_id,
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
        Resolve both usernames and then apply the follow graph.
        The graph itself performs resolution plus mutation.
        """
        parent_ctx = self._extract_ctx(carrier)
        with self._tracer.start_active_span(
            "FollowWithUsername",
            child_of=parent_ctx,
            tags={
                ot_tags.SPAN_KIND: ot_tags.SPAN_KIND_RPC_SERVER,
                "req_id": req_id,
                "user_username": user_username,
                "followee_username": followee_username,
            },
        ) as scope:
            span = scope.span

            initial: ResolveFollowAgentState = {
                "req_id": req_id,
                "user_username": user_username,
                "followee_username": followee_username,
                "operation": "follow",
                "user_id": None,
                "followee_id": None,
                "user_followees": [],
                "followee_followers": [],
                "llm_approved": None,
                "llm_reason": None,
                "approved": None,
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "total_llm_calls": 0,
                "fallback_used": False,
            }

            out = self._run_graph(
                self._resolve_follow_graph,
                initial,
                span,
                op_name="FollowWithUsername",
                req_id=req_id,
            )

            if not out.get("approved", False):
                span.set_tag("error", True)
                raise ServiceException(
                    errorCode=ErrorCode.SE_THRIFT_HANDLER_ERROR,
                    message=out.get("llm_reason") or "Follow request rejected",
                )

            self._log_metrics("FollowWithUsername", req_id, out, span)

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
        """
        Resolve both usernames and then apply the unfollow graph.
        The graph itself performs resolution plus mutation.
        """
        parent_ctx = self._extract_ctx(carrier)
        with self._tracer.start_active_span(
            "UnfollowWithUsername",
            child_of=parent_ctx,
            tags={
                ot_tags.SPAN_KIND: ot_tags.SPAN_KIND_RPC_SERVER,
                "req_id": req_id,
                "user_username": user_username,
                "followee_username": followee_username,
            },
        ) as scope:
            span = scope.span

            initial: ResolveFollowAgentState = {
                "req_id": req_id,
                "user_username": user_username,
                "followee_username": followee_username,
                "operation": "unfollow",
                "user_id": None,
                "followee_id": None,
                "user_followees": [],
                "followee_followers": [],
                "llm_approved": None,
                "llm_reason": None,
                "approved": None,
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "total_llm_calls": 0,
                "fallback_used": False,
            }

            out = self._run_graph(
                self._resolve_follow_graph,
                initial,
                span,
                op_name="UnfollowWithUsername",
                req_id=req_id,
            )

            if not out.get("approved", False):
                span.set_tag("error", True)
                raise ServiceException(
                    errorCode=ErrorCode.SE_THRIFT_HANDLER_ERROR,
                    message=out.get("llm_reason") or "Unfollow request rejected",
                )

            self._log_metrics("UnfollowWithUsername", req_id, out, span)

    # ==================================================================
    # Private — graph invocation
    # ==================================================================

    def _run_graph(self, graph: Any, initial: dict, span, op_name: str, req_id: int) -> dict:
        """Invoke a compiled LangGraph and translate failures into ServiceException."""
        try:
            out = asyncio.run(graph.ainvoke(initial))
        except ServiceException:
            span.set_tag("error", True)
            raise
        except Exception as exc:
            logger.exception("%s graph failed req_id=%d", op_name, req_id)
            span.set_tag("error", True)
            raise ServiceException(
                errorCode=ErrorCode.SE_THRIFT_HANDLER_ERROR,
                message=f"{op_name} agent failed: {exc}",
            )
        return out

    # ==================================================================
    # Private — logging + tracing helpers
    # ==================================================================

    def _log_metrics(self, op: str, req_id: int, out: dict, span) -> None:
        in_tok = out.get("total_input_tokens", 0)
        out_tok = out.get("total_output_tokens", 0)
        calls = out.get("total_llm_calls", 0)
        fallback = out.get("fallback_used", False)

        logger.info(
            "%s req_id=%d llm_calls=%d in=%d out=%d fallback=%s",
            op, req_id, calls, in_tok, out_tok, fallback,
        )
        print(
            f"[handler:{op}] req_id={req_id} llm_calls={calls} "
            f"in_tokens={in_tok} out_tokens={out_tok} fallback={fallback}"
        )
        span.set_tag("llm_calls", calls)
        span.set_tag("fallback", fallback)

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

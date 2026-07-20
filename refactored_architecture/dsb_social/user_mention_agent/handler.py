"""
UserMentionHandler (Agent version) — Thrift interface UNCHANGED.

ComposeUserMentions → runs build_resolve_username_agent graph per username
                       (sequential across the batch; LLM call is inside the graph)

Token metrics are accumulated across all usernames in a ComposeUserMentions batch
and logged per-request.
"""

import asyncio
import logging
import time

import opentracing
from opentracing.ext import tags as ot_tags
from opentracing.propagation import Format
from pymongo import MongoClient
import redis

from ms_baseline.dsb_social.gen_py.social_network import UserMentionService
from ms_baseline.dsb_social.gen_py.social_network.ttypes import UserMention, ServiceException, ErrorCode
from .agent import (
    build_resolve_username_agent,
    ResolveUsernameAgentState,
)

logger = logging.getLogger("user-mention-agent.handler")

_CACHE_TTL = 0


class UserMentionHandler(UserMentionService.Iface):
    """
    Parameters
    ----------
    mongo_client : pymongo.MongoClient
    mongo_db     : str
    mongo_col    : str
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

        # Compile the graph once (it captures redis + mongo via closures)
        self._resolve_graph = build_resolve_username_agent(redis_client, self._col)

    # ------------------------------------------------------------------
    # ComposeUserMentions
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
            On MongoDB failure (relayed from agent graph).
        ServiceException(SE_REDIS_ERROR)
            On a critical Redis failure (should not happen in practice).
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
            t1 = time.time()
            # De-duplicate while preserving order — process each unique username once
            seen:   dict[str, UserMention] = {}   # username -> resolved UserMention
            result: list[UserMention]      = []

            total_in = total_out = total_calls = 0

            for username in usernames:
                if username in seen:
                    # Already resolved earlier in this same request
                    result.append(seen[username])
                    continue

                initial: ResolveUsernameAgentState = {
                    "req_id":     req_id,
                    "username":   username,
                    "cache_hit":     False,
                    "cached_user_id": None,
                    "user_id":        None,
                    "total_input_tokens":  0,
                    "total_output_tokens": 0,
                    "total_llm_calls":     0,
                }

                try:
                    out = asyncio.run(self._resolve_graph.ainvoke(initial))
                except Exception as exc:
                    logger.error(
                        "ComposeUserMentions graph failed req_id=%d username=%r: %s",
                        req_id, username, exc,
                        exc_info=True,
                    )
                    span.set_tag("error", True)
                    raise ServiceException(
                        errorCode=ErrorCode.SE_THRIFT_HANDLER_ERROR,
                        message=f"ComposeUserMentions failed for {username}: {exc}",
                    )

                user_id = out["user_id"]
                if user_id is None:
                    msg = f"User not found: {username!r}"
                    logger.warning("ComposeUserMentions req_id=%d: %s", req_id, msg)
                    span.set_tag("error", True)
                    span.log_kv({"event": "user_not_found", "username": username})
                    raise ServiceException(
                        errorCode=ErrorCode.SE_THRIFT_HANDLER_ERROR,
                        message=msg,
                    )

                total_in    += out["total_input_tokens"]
                total_out   += out["total_output_tokens"]
                total_calls += out["total_llm_calls"]

                mention = UserMention(user_id=user_id, username=username)
                seen[username] = mention
                result.append(mention)

            t2 = time.time()
            logger.info(
                "ComposeUserMentions req_id=%d usernames=%d resolved=%d llm_calls=%d "
                "in_tokens=%d out_tokens=%d took=%.3fs",
                req_id, len(usernames), len(seen), total_calls, total_in, total_out, t2 - t1,
            )
            span.set_tag("resolved_count", len(seen))
            span.set_tag("llm_calls",      total_calls)
            return result

    # ------------------------------------------------------------------
    # Tracing helpers
    # ------------------------------------------------------------------

    def _extract_ctx(self, carrier: dict):
        try:
            return self._tracer.extract(Format.TEXT_MAP, carrier)
        except Exception:
            return None

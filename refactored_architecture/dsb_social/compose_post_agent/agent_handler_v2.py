"""
ComposePostHandler — Python port of ComposePostHandler.h

Implements the Thrift ComposePostService.Iface interface.

What the C++ original does
--------------------------

ComposePost(req_id, username, user_id, text, media_ids, media_types,
            post_type, carrier)

Phase 1 — Parallel fan-out to 4 services (std::async × 4):
  ┌─ UniqueIdService.ComposeUniqueId   → post_id (i64)
  ├─ TextService.ComposeText           → TextServiceReturn
  │      which internally fans out to:
  │        UrlShortenService.ComposeUrls
  │        UserMentionService.ComposeUserMentions
  ├─ UserService.ComposeCreatorWithUserId → Creator
  └─ MediaService.ComposeMedia         → list<Media>

Phase 2 — Assemble Post struct from all results.

Phase 3 — 3 downstream writes (all initiated, mongo/redis first):
  1. PostStorageService.StorePost(post)          [sync Thrift RPC]
  2. UserTimelineService.WriteUserTimeline(...)   [sync Thrift RPC]
  3. Publish to RabbitMQ "write-home-timeline"    [async via pika]
     → consumed by WriteHomeTimelineService
       → fans out to HomeTimelineService.WriteHomeTimeline for each follower

Python parallelism
------------------
Phase 1 uses concurrent.futures.ThreadPoolExecutor with 4 workers submitted
simultaneously, mirroring the C++ std::async × 4 pattern exactly.
Phase 3 steps 1+2 are sequential (matching C++ which does them after futures
complete), then step 3 publishes the RabbitMQ message.

No storage
----------
ComposePostService has no own database. It is a pure orchestrator.
"""
import concurrent.futures
import json
import logging
import re
import time
from typing import Any, TypedDict
import asyncio

import opentracing
from opentracing.ext import tags as ot_tags
from opentracing.propagation import Format

from langchain_core.messages import SystemMessage
from langchain_ollama import ChatOllama
from langgraph.graph import StateGraph, END

from ms_baseline.dsb_social.gen_py.social_network import ComposePostService
from ms_baseline.dsb_social.gen_py.social_network.ttypes import (
    Post,
    ServiceException,
    ErrorCode,
)

from .thrift_pool import ThriftClientPool

logger = logging.getLogger("compose-post-service")


def _attr(obj: Any, name: str, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


class ComposePostState(TypedDict, total=False):
    req_id: int
    username: str
    user_id: int
    text: str
    media_ids: list
    media_types: list
    post_type: Any
    carrier: dict

    post_id: int
    text_result: Any
    creator: Any
    media_list: list
    post: Post
    timestamp: int

    write_post_done: bool
    write_user_timeline_done: bool
    publish_home_timeline_done: bool

    next_action: str
    history: list[dict]
    done: bool
    
    total_input_tokens: int
    total_output_tokens: int
    total_llm_calls: int


class ComposePostHandler(ComposePostService.Iface):
    """
    AI-agent version of ComposePostHandler.

    Thrift interface stays the same.
    Downstream thrift calls and RabbitMQ payload stay the same.
    Only the orchestration logic is agentic.
    """

    def __init__(
        self,
        unique_id_pool: ThriftClientPool,
        text_pool: ThriftClientPool,
        user_pool: ThriftClientPool,
        media_pool: ThriftClientPool,
        post_storage_pool: ThriftClientPool,
        user_timeline_pool: ThriftClientPool,
        publisher,
        tracer: opentracing.Tracer,
        ollama_model: str = "llama3.2:3b",
        ollama_base_url: str = "http://localhost:11434",
        temperature: float = 0.0,
        num_workers: int = 8,
        max_steps: int = 20,
    ):
        self._unique_id_pool = unique_id_pool
        self._text_pool = text_pool
        self._user_pool = user_pool
        self._media_pool = media_pool
        self._post_storage_pool = post_storage_pool
        self._timeline_pool = user_timeline_pool
        self._publisher = publisher
        self._tracer = tracer
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=num_workers,
            thread_name_prefix="compose-post",
        )
        self._max_steps = max_steps

        self._model = ChatOllama(
            model=ollama_model,
            base_url=ollama_base_url,
            temperature=0.0,
            reasoning=False
        )

        self._graph = self._build_graph()

    # ------------------------------------------------------------------
    # Public Thrift method
    # ------------------------------------------------------------------

    def ComposePost(
        self,
        req_id: int,
        username: str,
        user_id: int,
        text: str,
        media_ids: list,
        media_types: list,
        post_type,
        carrier: dict,
    ) -> None:
        parent_ctx = self._extract_ctx(carrier)

        with self._tracer.start_active_span(
            "ComposePost",
            child_of=parent_ctx,
            tags={
                ot_tags.SPAN_KIND: ot_tags.SPAN_KIND_RPC_SERVER,
                "req_id": req_id,
                "username": username,
                "user_id": user_id,
                "post_type": str(post_type),
            },
        ) as scope:
            span = scope.span
            span.set_tag("agent_mode", True)

            # Same carrier used by all downstream services, like the original code.
            trace_carrier = self._inject_ctx(span)

            state: ComposePostState = {
                "req_id": req_id,
                "username": username,
                "user_id": user_id,
                "text": text,
                "media_ids": media_ids or [],
                "media_types": media_types or [],
                "post_type": post_type,
                "carrier": trace_carrier,
                "post_id": None,
                "text_result": None,
                "creator": None,
                "media_list": [] if not media_ids else None,
                "post": None,
                "timestamp": None,
                "write_post_done": False,
                "write_user_timeline_done": False,
                "publish_home_timeline_done": False,
                "history": [],
                "done": False,
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "total_llm_calls": 0,
            }

            try:
                final_state = self._graph.invoke(
                    state,
                    config={"recursion_limit": self._max_steps},
                )
            except ServiceException:
                span.set_tag("error", True)
                raise
            except Exception as exc:
                logger.exception("ComposePost agent failed req_id=%d", req_id)
                span.set_tag("error", True)
                raise ServiceException(
                    errorCode=ErrorCode.SE_THRIFT_HANDLER_ERROR,
                    message=f"ComposePost agent failed: {exc}",
                )

            if not final_state.get("done", False):
                span.set_tag("error", True)
                raise ServiceException(
                    errorCode=ErrorCode.SE_THRIFT_HANDLER_ERROR,
                    message="ComposePost agent stopped before completion",
                )

            span.set_tag("post_id", final_state.get("post_id"))
            span.set_tag("timestamp", final_state.get("timestamp"))
            logger.debug(
                "ComposePost req_id=%d completed post_id=%s, total_input_tokens=%d, total_output_tokens=%d, total_llm_calls=%d",
                req_id,
                final_state.get("post_id"),
                final_state.get("total_input_tokens", 0),
                final_state.get("total_output_tokens", 0),
                final_state.get("total_llm_calls", 0),
            )

    # ------------------------------------------------------------------
    # Graph construction
    # ------------------------------------------------------------------

    def _build_graph(self):
        builder = StateGraph(ComposePostState)

        builder.add_node("reason", self._reason)
        builder.add_node("act", self._act)

        builder.set_entry_point("reason")
        builder.add_edge("reason", "act")
        builder.add_conditional_edges(
            "act",
            self._route_after_act,
            {
                "reason": "reason",
                END: END,
            },
        )

        return builder.compile()

    def _route_after_act(self, state: ComposePostState):
        return END if state.get("done") else "reason"

    # ------------------------------------------------------------------
    # Reason node
    # ------------------------------------------------------------------

    def _reason(self, state: ComposePostState):
        allowed = self._allowed_actions(state)

        prompt = self._system_prompt(allowed, state)

        logger.info(
            "\n\n ------------------------- ComposePost reason req_id=%d prompt=%s \n\n ------------------------- ",
            state["req_id"],
            prompt,
        )

        # Async invocation (same pattern as the other agents)
        response = self._model.invoke(prompt)                

        raw = (response.text() or "").strip()

        usage = getattr(response, "usage_metadata", {}) or {}

        input_tokens = usage.get("input_tokens", 0)
        output_tokens = usage.get("output_tokens", 0)

        action = self._parse_action(raw, allowed=None)

        logger.info(
            "\n\n ------------------------- ComposePost reason req_id=%d raw=%s action=%s in_tokens=%d out_tokens=%d \n\n ------------------------- ",
            state["req_id"],
            raw,
            action,
            input_tokens,
            output_tokens,
        )

        history = list(state.get("history", []))
        history.append(
            {
                "stage": "reason",
                "allowed": allowed,
                "raw": raw,
                "chosen_action": action,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            }
        )

        return {
            "next_action": action,
            "history": history,
            "total_input_tokens": state.get("total_input_tokens", 0)
            + input_tokens,
            "total_output_tokens": state.get("total_output_tokens", 0)
            + output_tokens,
            "total_llm_calls": state.get("total_llm_calls", 0)
            + 1,
        }

    def _system_prompt(self, allowed: list[str], state: ComposePostState) -> str:
        state_view = {
            "req_id": state.get("req_id"),
            "user_id": state.get("user_id"),
            "media_ids": state.get("media_ids"),
            "media_types": state.get("media_types"),
            "has_post_id": state.get("post_id") is not None,
            "has_text_result": state.get("text_result") is not None,
            "has_creator": state.get("creator") is not None,
            "has_media_list": state.get("media_list") is not None,
            "has_post": state.get("post") is not None, # assemble_post
            "write_post_done": state.get("write_post_done", False),
            "write_user_timeline_done": state.get("write_user_timeline_done", False),
            "publish_home_timeline_done": state.get(
                "publish_home_timeline_done", False
            ),
        }
        
        workflow = [
            {
                "action": "compose_unique_id",
                "completed": state.get("post_id") is not None,
                "rank": 1,
            },
            {
                "action": "compose_text",
                "completed": state.get("text_result") is not None,
                "rank": 2,
            },
            {
                "action": "compose_creator",
                "completed": state.get("creator") is not None,
                "rank": 3,
            },
            {
                "action": "compose_media",
                "completed": state.get("media_list") is not None or len(state.get("media_ids", [])) == 0,
                "rank": 4,
            },
            {
                "action": "assemble_post",
                "completed": state.get("post") is not None,
                "rank": 5,
            },
            {
                "action": "store_post",
                "completed": state.get("write_post_done", False),
                "rank": 6,
            },
            {
                "action": "write_user_timeline",
                "completed": state.get("write_user_timeline_done", False),
                "rank": 7,
            },
            {
                "action": "publish_home_timeline",
                "completed": state.get("publish_home_timeline_done", False),
                "rank": 8,
            },
            # {
            #     "action": "finish",
            #     "completed": state.get("done", False),
            #     "rank": 9,
            # },
        ]
        
        actions = [x["action"] for x in workflow if x['completed'] is False] + ["finish"]
        all_done = all(stage.get("completed") is True for stage in workflow)  
        if all_done:
            workflow = []  # if all stages are completed, the workflow is empty 
            
        # workflow = [x for x in workflow if x["completed"] is False]  # filter out completed stages

        return f"""
            You are an orchestrator for a compose-post workflow.

            Your job is to choose exactly ONE next action from this list: {actions} based on decision policy and current workflow state.

            Decision policy:
            - If all stages in the workflow are completed or workflow is empty, choose "finish" as the next action.
            - Choose the first stage in the rank order from workflow which is not completed as next action.
            - Never skip a stage, choose any earlier stage or repeat a completed stage.
            
            
            Current workflow state:

            {json.dumps(workflow)}
            
            Return ONLY JSON in the following schema, without providing intermediate reasoning:

            {{"action":"<next action>"}}

            """.strip()

    def _parse_action(self, raw: str, allowed: list[str] | None) -> str:
        action = ""
        try:
            # Prefer a direct JSON object.
            match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
            payload = json.loads(match.group(0) if match else raw)
            action = str(payload.get("action", "")).strip()
        except Exception:
            action = ""

        if allowed is None: # raw stochastic output, no constraints
            return action
        else: # deterministic fallback to first allowed action if the model output is invalid
            if action not in allowed:
                action = allowed[0]
            return action

    # ------------------------------------------------------------------
    # Act node
    # ------------------------------------------------------------------

    def _act(self, state: ComposePostState):
        action = state["next_action"]
        updates = {}
        history = list(state.get("history", []))

        try:
            if action == "compose_unique_id":
                post_id = self._call_unique_id(
                    state["req_id"], state["post_type"], state["carrier"]
                )
                updates["post_id"] = post_id
                history.append(
                    {"stage": "act", "action": action, "result": {"post_id": post_id}}
                )

            elif action == "compose_text":
                text_result = self._call_text(
                    state["req_id"], state["text"], state["carrier"]
                )
                updates["text_result"] = text_result
                history.append(
                    {
                        "stage": "act",
                        "action": action,
                        "result": {"text_result": str(text_result)},
                    }
                )

            elif action == "compose_creator":
                creator = self._call_compose_creator(
                    state["req_id"],
                    state["user_id"],
                    state["username"],
                    state["carrier"],
                )
                updates["creator"] = creator
                history.append(
                    {
                        "stage": "act",
                        "action": action,
                        "result": {"creator": str(creator)},
                    }
                )

            elif action == "compose_media":
                media_list = self._call_media(
                    state["req_id"],
                    state["media_types"],
                    state["media_ids"],
                    state["carrier"],
                )
                updates["media_list"] = media_list
                history.append(
                    {
                        "stage": "act",
                        "action": action,
                        "result": {"media_count": len(media_list or [])},
                    }
                )

            elif action == "assemble_post":
                post = self._assemble_post(state)
                updates["post"] = post
                updates["timestamp"] = post.timestamp
                history.append(
                    {
                        "stage": "act",
                        "action": action,
                        "result": {
                            "post_id": post.post_id,
                            "timestamp": post.timestamp,
                        },
                    }
                )

            elif action == "store_post":
                self._store_post(state["req_id"], state["post"], state["carrier"])
                updates["write_post_done"] = True
                history.append({"stage": "act", "action": action, "result": "ok"})

            elif action == "write_user_timeline":
                self._write_user_timeline(
                    state["req_id"],
                    state["post"].post_id,
                    state["user_id"],
                    state["post"].timestamp,
                    state["carrier"],
                )
                updates["write_user_timeline_done"] = True
                history.append({"stage": "act", "action": action, "result": "ok"})

            elif action == "publish_home_timeline":
                mention_ids = [
                    m.user_id
                    for m in (_attr(state.get("text_result"), "user_mentions", []) or [])
                ]
                self._publish_home_timeline(
                    state["req_id"],
                    state["post"].post_id,
                    state["user_id"],
                    state["post"].timestamp,
                    mention_ids,
                    state["carrier"],
                )
                updates["publish_home_timeline_done"] = True
                history.append({"stage": "act", "action": action, "result": "ok"})

            elif action == "finish":
                updates["done"] = True
                history.append({"stage": "act", "action": action, "result": "done"})

            else:
                raise ServiceException(
                    errorCode=ErrorCode.SE_THRIFT_HANDLER_ERROR,
                    message=f"Unknown agent action: {action}",
                )

        except ServiceException:
            raise
        except Exception as exc:
            logger.exception("Action %s failed req_id=%d", action, state["req_id"])
            raise ServiceException(
                errorCode=ErrorCode.SE_THRIFT_HANDLER_ERROR,
                message=f"Action {action} failed: {exc}",
            )

        updates["history"] = history

        # Auto-finish once everything is complete and the final stage is done.
        if (
            updates.get("publish_home_timeline_done", state.get("publish_home_timeline_done"))
            and updates.get("write_user_timeline_done", state.get("write_user_timeline_done"))
            and updates.get("write_post_done", state.get("write_post_done"))
            and updates.get("post", state.get("post")) is not None
        ):
            # The model still gets to choose "finish"; this just avoids a dead-end.
            pass

        return updates

    def _allowed_actions(self, state: ComposePostState) -> list[str]:
        if state.get("post_id") is None:
            return ["compose_unique_id", "compose_text", "compose_creator"] + (
                ["compose_media"] if state.get("media_list") is None else []
            )

        if state.get("text_result") is None:
            return ["compose_text", "compose_creator"] + (
                ["compose_media"] if state.get("media_list") is None else []
            )

        if state.get("creator") is None:
            return ["compose_creator"] + (
                ["compose_media"] if state.get("media_list") is None else []
            )

        if state.get("media_list") is None:
            return ["compose_media"]

        if state.get("post") is None:
            return ["assemble_post"]

        if not state.get("write_post_done", False):
            return ["store_post"]

        if not state.get("write_user_timeline_done", False):
            return ["write_user_timeline"]

        if not state.get("publish_home_timeline_done", False):
            return ["publish_home_timeline"]

        return ["finish"]

    # ------------------------------------------------------------------
    # Existing downstream calls, unchanged interfaces
    # ------------------------------------------------------------------

    def _call_unique_id(self, req_id: int, post_type, carrier: dict) -> int:
        with self._unique_id_pool.connection() as client:
            return client.ComposeUniqueId(req_id, post_type, carrier)

    def _call_text(self, req_id: int, text: str, carrier: dict):
        with self._text_pool.connection() as client:
            return client.ComposeText(req_id, text, carrier)

    def _call_compose_creator(
        self, req_id: int, user_id: int, username: str, carrier: dict
    ):
        with self._user_pool.connection() as client:
            return client.ComposeCreatorWithUserId(req_id, user_id, username, carrier)

    def _call_media(
        self,
        req_id: int,
        media_types: list,
        media_ids: list,
        carrier: dict,
    ) -> list:
        if not media_ids:
            return []
        with self._media_pool.connection() as client:
            return client.ComposeMedia(req_id, media_types, media_ids, carrier)

    def _assemble_post(self, state: ComposePostState) -> Post:
        text_result = state["text_result"]
        creator = state["creator"]
        media_list = state["media_list"] or []

        timestamp = int(time.time() * 1000)

        return Post(
            post_id=state["post_id"],
            creator=creator,
            req_id=state["req_id"],
            text=_attr(text_result, "text"),
            user_mentions=_attr(text_result, "user_mentions", []),
            media=media_list,
            urls=_attr(text_result, "urls", []),
            timestamp=timestamp,
            post_type=state["post_type"],
        )

    def _store_post(self, req_id: int, post: Post, carrier: dict) -> None:
        try:
            with self._post_storage_pool.connection() as client:
                client.StorePost(req_id, post, carrier)
        except ServiceException:
            raise
        except Exception as exc:
            logger.error("StorePost failed req_id=%d: %s", req_id, exc)
            raise ServiceException(
                errorCode=ErrorCode.SE_THRIFT_HANDLER_ERROR,
                message=f"StorePost failed: {exc}",
            )

    def _write_user_timeline(
        self,
        req_id: int,
        post_id: int,
        user_id: int,
        timestamp: int,
        carrier: dict,
    ) -> None:
        try:
            with self._timeline_pool.connection() as client:
                client.WriteUserTimeline(req_id, post_id, user_id, timestamp, carrier)
        except ServiceException:
            raise
        except Exception as exc:
            logger.error("WriteUserTimeline failed req_id=%d: %s", req_id, exc)
            raise ServiceException(
                errorCode=ErrorCode.SE_THRIFT_HANDLER_ERROR,
                message=f"WriteUserTimeline failed: {exc}",
            )

    def _publish_home_timeline(
        self,
        req_id: int,
        post_id: int,
        user_id: int,
        timestamp: int,
        mention_ids: list,
        carrier: dict,
    ) -> None:
        try:
            self._publisher.publish(
                req_id=req_id,
                post_id=post_id,
                user_id=user_id,
                timestamp=timestamp,
                user_mentions_id=mention_ids,
                carrier=carrier,
            )
        except Exception as exc:
            logger.warning(
                "RabbitMQ publish failed req_id=%d post_id=%d: %s",
                req_id,
                post_id,
                exc,
            )

    # ------------------------------------------------------------------
    # Tracing helpers
    # ------------------------------------------------------------------

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
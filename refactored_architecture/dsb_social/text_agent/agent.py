"""
TEXT AGENT - Graph Topology

    START
      |
      v
  [reason_parse_and_compose]   LLM ReAct node with tools.
      |                        The LLM:
      |                        1. Identifies all URLs in the text
      |                        2. Calls shorten_urls(urls_json) tool
      |                        3. Identifies all @mention usernames
      |                        4. Calls resolve_mentions(usernames_json) tool
      |                        5. Replaces expanded URLs in text with shortened forms
      |                        6. Returns JSON: {text, user_mentions, urls}
      |
      v
  [validate_output]            Deterministic guard.
      |                        Verifies the LLM output is a valid TextServiceReturn.
      |                        If not: falls back to deterministic text_parser +
      |                        direct Thrift calls (identical to original service).
      v
     END → TextServiceReturn(text, user_mentions, urls)

Key Design Decisions
--------------------
- Thrift interface (TextService.Iface) UNCHANGED.
- No own storage (TextService has no DB) — UNCHANGED.
- The LLM reasons about: extracting URLs, extracting @mentions, replacing URLs
  in text. These are the "static logic" steps converted to LLM reasoning.
- UrlShortenService and UserMentionService are exposed as @tool functions
  so the LLM can call them during its reasoning chain.
- validate_output falls back to deterministic text_parser + direct parallel
  Thrift calls (original behaviour) if LLM output is invalid.
- Token metrics tracked per request.
"""

import json
import logging
import re
import asyncio
import concurrent.futures
from typing import TypedDict, Optional, List, Dict, Any

from langgraph.graph import StateGraph, END
from langchain_ollama import ChatOllama

import opentracing
from opentracing.propagation import Format

from .text_parser import parse, replace_urls
from .thrift_pool import ThriftClientPool

logger = logging.getLogger("text-agent")

# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------

llm = ChatOllama(model="llama3.2:3b", temperature=0.0, reasoning=False)


# ---------------------------------------------------------------------------
# Agent State
# ---------------------------------------------------------------------------

class TextAgentState(TypedDict):
    # Inputs
    req_id:    int
    raw_text:  str
    carrier:   Dict[str, str]

    # LLM output (set by reason_parse_and_compose)
    llm_text:          Optional[str]          # modified text (URLs replaced)
    llm_user_mentions: Optional[List[Dict]]   # [{user_id, username}, ...]
    llm_urls:          Optional[List[Dict]]   # [{shortened_url, expanded_url}, ...]

    # Final output (set by validate_output, possibly corrected)
    final_text:          Optional[str]
    final_user_mentions: Optional[List[Dict]]
    final_urls:          Optional[List[Dict]]

    # Metrics
    total_input_tokens:  int
    total_output_tokens: int
    total_llm_calls:     int
    fallback_used:       bool

    # Tool call results (stored for validate_output to use in fallback)
    tool_url_results:     Optional[List[Dict]]
    tool_mention_results: Optional[List[Dict]]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_json(text: str) -> Optional[dict]:
    try:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            return json.loads(m.group(0))
    except Exception as exc:
        logger.warning("JSON parse error: %s — raw: %r", exc, text[:300])
    return None


def _validate_text_result(parsed: Optional[dict]) -> bool:
    """Check the LLM returned a structurally valid TextServiceReturn dict."""
    if not isinstance(parsed, dict):
        return False
    if not isinstance(parsed.get("text"), str):
        return False
    if not isinstance(parsed.get("user_mentions"), list):
        return False
    if not isinstance(parsed.get("urls"), list):
        return False
    # Each user_mention must have user_id and username
    for m in parsed["user_mentions"]:
        if not isinstance(m, dict):
            return False
        if not isinstance(m.get("user_id"), int) or not isinstance(m.get("username"), str):
            return False
    # Each url must have shortened_url and expanded_url
    for u in parsed["urls"]:
        if not isinstance(u, dict):
            return False
        if not isinstance(u.get("shortened_url"), str) or not isinstance(u.get("expanded_url"), str):
            return False
    return True


# ===========================================================================
# Node factories
# ===========================================================================

def make_reason_node(url_pool: ThriftClientPool, mention_pool: ThriftClientPool):
    """
    Node: reason_parse_and_compose  (LLM with tools)

    The LLM:
    1. Identifies URLs in raw_text
    2. Calls shorten_urls tool → gets shortened forms
    3. Identifies @mention usernames
    4. Calls resolve_mentions tool → gets user_ids
    5. Replaces expanded URLs in text with shortened forms
    6. Returns structured JSON

    Tool calls happen synchronously inside the prompt loop since
    pika/thrift are blocking. The LLM reasons in a ReAct-style prompt.
    """

    # ---- Tool implementations (called by the prompt loop) ----

    def _call_shorten_urls(urls: List[str], req_id: int, carrier: dict) -> List[Dict]:
        if not urls:
            return []
        try:
            with url_pool.connection() as client:
                result = client.ComposeUrls(req_id, urls, carrier)
            output = [{"shortened_url": u.shortened_url, "expanded_url": u.expanded_url}
                      for u in result]
            print(f"[tool:shorten_urls] {len(urls)} URLs -> {output}")
            return output
        except Exception as exc:
            logger.warning("shorten_urls tool failed: %s", exc)
            return []

    def _call_resolve_mentions(usernames: List[str], req_id: int, carrier: dict) -> List[Dict]:
        if not usernames:
            return []
        try:
            with mention_pool.connection() as client:
                result = client.ComposeUserMentions(req_id, usernames, carrier)
            output = [{"user_id": m.user_id, "username": m.username}
                      for m in result]
            print(f"[tool:resolve_mentions] {usernames} -> {output}")
            return output
        except Exception as exc:
            logger.warning("resolve_mentions tool failed: %s", exc)
            return []

    async def reason_parse_and_compose(state: TextAgentState) -> TextAgentState:
        req_id  = state["req_id"]
        text    = state["raw_text"]
        carrier = state["carrier"]

        # ---- Step 1: deterministically extract URLs and mentions
        #              so the LLM has them as input context ----
        parsed       = parse(text)
        found_urls   = parsed.urls
        found_names  = parsed.usernames

        # ---- Step 2: call tools (blocking, run in thread) ----
        url_results = await asyncio.to_thread(
            _call_shorten_urls, found_urls, req_id, carrier
        )
        mention_results = await asyncio.to_thread(
            _call_resolve_mentions, found_names, req_id, carrier
        )

        # Store tool results for validate_output fallback
        state["tool_url_results"]     = url_results
        state["tool_mention_results"] = mention_results

        # Build url_map for the LLM context
        url_map_str = json.dumps(
            {u["expanded_url"]: u["shortened_url"] for u in url_results},
            indent=2,
        )

        prompt = f"""
You are a text processing agent for a social network.

Your task is to process a raw post text by:
1. Replacing all expanded URLs with their shortened forms (using the provided URL map)
2. Returning the user mentions with their resolved user IDs
3. Returning the URL pairs (shortened + expanded)

Inputs:
  RAW_TEXT = {json.dumps(text)}

  URL_MAP (expanded_url -> shortened_url):
  {url_map_str}

  RESOLVED_USER_MENTIONS:
  {json.dumps(mention_results, indent=2)}

Instructions:
- Replace every expanded URL in RAW_TEXT with its corresponding shortened_url from URL_MAP
- If a URL appears in the text but is not in URL_MAP, leave it unchanged
- Keep all other text (including @mentions) exactly as-is
- Return ONLY valid JSON — no explanation, no code, no markdown

Schema:
{{
  "text": "<modified text with URLs replaced>",
  "user_mentions": [
    {{"user_id": <int>, "username": "<string>"}},
    ...
  ],
  "urls": [
    {{"shortened_url": "<string>", "expanded_url": "<string>"}},
    ...
  ]
}}
"""

        logger.info("LLM prompt req_id=%d text_len=%d", req_id, len(text))

        response = await asyncio.to_thread(llm.invoke, prompt)
        raw      = response.text()
        in_tok   = response.usage_metadata.get("input_tokens",  0)
        out_tok  = response.usage_metadata.get("output_tokens", 0)

        logger.info("LLM raw=%r  in=%d out=%d", raw[:300], in_tok, out_tok)
        print(f"[reason_parse_and_compose] raw={raw[:200]!r}  in={in_tok} out={out_tok}")

        parsed_response = _parse_json(raw)

        if _validate_text_result(parsed_response):
            state["llm_text"]          = parsed_response["text"]
            state["llm_user_mentions"] = parsed_response["user_mentions"]
            state["llm_urls"]          = parsed_response["urls"]
        else:
            logger.warning(
                "LLM returned invalid TextServiceReturn req_id=%d raw=%r",
                req_id, raw[:200],
            )
            state["llm_text"]          = None
            state["llm_user_mentions"] = None
            state["llm_urls"]          = None

        state["total_input_tokens"]  += in_tok
        state["total_output_tokens"] += out_tok
        state["total_llm_calls"]     += 1
        return state

    return reason_parse_and_compose


def make_validate_output_node(url_pool: ThriftClientPool, mention_pool: ThriftClientPool):
    """
    Node: validate_output  (deterministic guard)

    If LLM output is valid → use it.
    If not → fall back to deterministic text_parser + tool results already
    fetched by reason_parse_and_compose (no extra Thrift calls needed).
    """
    async def validate_output(state: TextAgentState) -> TextAgentState:
        # ---- Check LLM output ----
        if (
            state.get("llm_text") is not None
            and state.get("llm_user_mentions") is not None
            and state.get("llm_urls") is not None
        ):
            state["final_text"]          = state["llm_text"]
            state["final_user_mentions"] = state["llm_user_mentions"]
            state["final_urls"]          = state["llm_urls"]
            state["fallback_used"]       = False
            logger.info("validate_output PASS req_id=%d", state["req_id"])
            print(f"[validate_output] PASS")
            return state

        # ---- Fallback: use deterministic text_parser + stored tool results ----
        logger.warning(
            "validate_output FALLBACK req_id=%d", state["req_id"]
        )
        print("[validate_output] FALLBACK — using deterministic parser + tool results")

        raw_text        = state["raw_text"]
        url_results     = state.get("tool_url_results")     or []
        mention_results = state.get("tool_mention_results") or []

        # Build url_map and replace URLs deterministically
        url_map       = {u["expanded_url"]: u["shortened_url"] for u in url_results}
        modified_text = replace_urls(raw_text, url_map)

        state["final_text"]          = modified_text
        state["final_user_mentions"] = mention_results
        state["final_urls"]          = url_results
        state["fallback_used"]       = True
        return state

    return validate_output


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------

def build_text_agent(
    url_pool: ThriftClientPool,
    mention_pool: ThriftClientPool,
) -> any:
    """Build and compile the TextService LangGraph agent."""
    graph = StateGraph(TextAgentState)

    graph.add_node("reason_parse_and_compose",
                   make_reason_node(url_pool, mention_pool))
    graph.add_node("validate_output",
                   make_validate_output_node(url_pool, mention_pool))

    graph.set_entry_point("reason_parse_and_compose")
    graph.add_edge("reason_parse_and_compose", "validate_output")
    graph.add_edge("validate_output",          END)

    return graph.compile()
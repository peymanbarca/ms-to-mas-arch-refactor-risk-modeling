"""

LangGraph recommendation agent — replaces random.sample() with LLM-driven
reasoning while keeping the identical gRPC ListRecommendations interface.

Graph topology:
─────────────────────────────────────────────────────────────────────────────
  ┌──────────────────┐
  │  fetch_catalog   │  (deterministic) ProductCatalogService.ListProducts
  └────────┬─────────┘
           │
  ┌────────▼─────────┐
  │  filter_products │  (deterministic) exclude request.product_ids; build
  │                  │  rich candidate list with name + categories for LLM
  └────────┬─────────┘
           │
     ┌─────▼──────┐  no candidates left?
     │   route    │────────────────────────────────────────┐
     └─────┬──────┘  candidates available                  │ empty
           │                                               │
  ┌────────▼───────────────┐                  ┌────────────▼──────────┐
  │ recommendation_reason  │  (LLM / llama3)   │  return_empty         │
  │                        │  ranks + selects   │  (no candidates)      │
  │                        │  up to MAX_RECS    └────────────┬──────────┘
  └────────┬───────────────┘                               │
           │                                               │
  ┌────────▼───────────────┐◄──────────────────────────────┘
  │  format_response        │  (deterministic) build final product_ids list
  └────────┬───────────────┘
           │
          END
─────────────────────────────────────────────────────────────────────────────

Why LLM instead of random.sample():
  random.sample() picks blindly from the filtered catalog.  The LLM receives
  the user's existing product IDs, the candidate products' names and categories,
  and reasons about:
    • Complementary categories  (sunglasses → hats, bags)
    • Avoiding redundancy       (don't recommend 3 identical items)
    • Diversity                 (mix categories for better discovery)
    • Recency / popularity cues (injected via product metadata if available)

  If the LLM call fails for any reason the agent gracefully falls back to
  random.sample() — identical to the original algorithm — so the gRPC
  response is always returned within the SLA.

The catalog stub is injected at construction time (same as the existing
RecommendationServicer pattern) so tests can swap in mocks without starting
real services.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from typing import Any, Dict, List, Optional

import grpc
from langchain_ollama import ChatOllama
from langgraph.graph import END, StateGraph
from typing_extensions import TypedDict

from ..shared import demo_pb2
from ..shared import demo_pb2_grpc

logger = logging.getLogger("recommendationagent")

# ── LLM — lower temperature for more consistent ranking ───────────────────────
llm = ChatOllama(model="llama3.2:3b", temperature=0.2, reasoning=False)

MAX_RECOMMENDATIONS: int = 5   # mirrors original MAX_RESPONSES


# ════════════════════════════════════════════════════════════════════════════
# Agent state
# ════════════════════════════════════════════════════════════════════════════

class RecommendationAgentState(TypedDict):
    """
    Shared state threaded through every node.

    Inputs (set before ainvoke):
        user_id         – current user identifier
        excluded_ids    – product IDs already in cart / recently viewed
        catalog_stub    – injected ProductCatalogServiceStub (not serialised)
        grpc_context    – injected gRPC ServicerContext for abort calls

    Intermediate (written by nodes):
        all_product_ids     – full catalog ID list from fetch_catalog
        all_products        – rich list of {id, name, categories} dicts
        candidate_products  – all_products minus excluded_ids
        recommended_ids     – final selected product IDs (written by LLM or fallback)
        llm_used            – True if LLM reasoning ran, False if fallback used
        error               – set on non-fatal LLM failure; None otherwise

    Metrics:
        total_input_tokens
        total_output_tokens
        total_llm_calls
    """
    # inputs
    user_id:      str
    excluded_ids: List[str]
    catalog_stub: Any           # demo_pb2_grpc.ProductCatalogServiceStub
    grpc_context: Any           # grpc.aio.ServicerContext | None

    # intermediate
    all_product_ids:    List[str]
    all_products:       List[Dict[str, Any]]   # [{id, name, categories}, ...]
    candidate_products: List[Dict[str, Any]]   # filtered subset

    # output
    recommended_ids: List[str]
    llm_used:        bool
    error:           Optional[str]

    # metrics
    total_input_tokens:  int
    total_output_tokens: int
    total_llm_calls:     int


# ════════════════════════════════════════════════════════════════════════════
# Node 1 – fetch_catalog  (deterministic)
# ════════════════════════════════════════════════════════════════════════════

async def fetch_catalog_node(state: RecommendationAgentState) -> RecommendationAgentState:
    """
    Deterministic tool node.

    Calls ProductCatalogService.ListProducts via the injected stub and builds
    a rich product list carrying name + categories for the LLM prompt.

    On gRPC failure it aborts the context (same behaviour as the original
    servicer) and returns an empty product list so downstream nodes can
    handle the empty case gracefully.
    """
    logger.info("[fetch_catalog] calling ProductCatalogService.ListProducts")

    try:
        resp: demo_pb2.ListProductsResponse = await state["catalog_stub"].ListProducts(
            demo_pb2.Empty()
        )
    except grpc.aio.AioRpcError as exc:
        logger.error(
            "[fetch_catalog] ListProducts failed | code=%s details=%s",
            exc.code(), exc.details(),
        )
        ctx = state.get("grpc_context")
        if ctx:
            await ctx.abort(
                grpc.StatusCode.INTERNAL,
                f"upstream ProductCatalogService error: {exc.details()}",
            )
        return {
            **state,
            "all_product_ids":    [],
            "all_products":       [],
            "candidate_products": [],
            "error": f"catalog fetch failed: {exc.details()}",
        }

    # Build rich product dicts — name + categories give the LLM context to reason
    all_products = [
        {
            "id":         p.id,
            "name":       p.name,
            "categories": list(p.categories),
        }
        for p in resp.products
    ]
    all_ids = [p["id"] for p in all_products]

    logger.info("[fetch_catalog] fetched %d products from catalog", len(all_products))

    return {
        **state,
        "all_product_ids": all_ids,
        "all_products":    all_products,
        "error":           None,
    }


# ════════════════════════════════════════════════════════════════════════════
# Node 2 – filter_products  (deterministic)
# ════════════════════════════════════════════════════════════════════════════

async def filter_products_node(state: RecommendationAgentState) -> RecommendationAgentState:
    """
    Deterministic tool node.

    Mirrors the original algorithm:
        filtered_products = list(set(product_ids) - set(request.product_ids))

    Also enriches candidates with product details so the LLM can reason about
    category diversity and complementarity — something random.sample() cannot do.
    """
    excluded = set(state["excluded_ids"])
    candidates = [p for p in state["all_products"] if p["id"] not in excluded]

    logger.info(
        "[filter_products] catalog=%d excluded=%d candidates=%d",
        len(state["all_products"]),
        len(excluded),
        len(candidates),
    )

    return {**state, "candidate_products": candidates}


# ════════════════════════════════════════════════════════════════════════════
# Conditional router after filter_products
# ════════════════════════════════════════════════════════════════════════════

def route_after_filter(state: RecommendationAgentState) -> str:
    """
    If no candidates remain (all products already in cart) skip LLM.
    Otherwise proceed to LLM reasoning.
    """
    if not state["candidate_products"]:
        logger.info("[route] no candidates → return_empty")
        return "return_empty"
    logger.info("[route] %d candidates → recommendation_reasoning",
                len(state["candidate_products"]))
    return "recommendation_reasoning"


# ════════════════════════════════════════════════════════════════════════════
# Node 3a – return_empty  (deterministic shortcut)
# ════════════════════════════════════════════════════════════════════════════

async def return_empty_node(state: RecommendationAgentState) -> RecommendationAgentState:
    """
    Shortcut node — reached only when every catalog product is already excluded.
    Sets recommended_ids=[] and skips LLM entirely.
    """
    logger.info("[return_empty] all products excluded — returning empty list")
    return {**state, "recommended_ids": [], "llm_used": False}


# ════════════════════════════════════════════════════════════════════════════
# Node 3b – recommendation_reasoning  (LLM node)
# ════════════════════════════════════════════════════════════════════════════

def _parse_recommended_ids(text: str, valid_ids: set) -> list[str]:
    """
    Extract product IDs from LLM JSON response.

    Expected format:
        {"recommended_product_ids": ["ID1", "ID2", ...]}

    Falls back to scanning the raw text for known product IDs if JSON fails.
    """
    # ── Try JSON wrapper ──────────────────────────────────────────────────────
    try:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            data = json.loads(m.group(0))
            ids = data.get("recommended_product_ids", [])
            if isinstance(ids, list):
                # Keep only IDs that exist in the valid set
                valid = [i for i in ids if i in valid_ids]
                if valid:
                    return valid
    except Exception:
        pass

    # ── Fallback: scan raw text for known IDs ────────────────────────────────
    found = [pid for pid in valid_ids if pid in text]
    return found[:MAX_RECOMMENDATIONS]


async def recommendation_reasoning_node(
    state: RecommendationAgentState,
) -> RecommendationAgentState:
    """
    LLM node — the only non-deterministic step.

    Provides the LLM with:
      • The products the user already has (excluded_ids)
      • The candidate products with their names and categories
      • The target number of recommendations (MAX_RECOMMENDATIONS)

    The LLM selects up to MAX_RECOMMENDATIONS product IDs that:
      • Are NOT in the excluded list
      • Come from diverse categories (avoid recommending 5 identical items)
      • Complement the products the user already has

    Returns JSON:
        {"recommended_product_ids": ["ID1", "ID2", ...]}

    Falls back to random.sample() on any parsing or LLM failure — same as
    the original algorithm — so the gRPC response is always returned.
    """
    candidates    = state["candidate_products"]
    excluded_ids  = state["excluded_ids"]
    valid_ids     = {p["id"] for p in candidates}
    n             = min(MAX_RECOMMENDATIONS, len(candidates))

    # ── Build candidate summary for prompt ───────────────────────────────────
    candidate_lines = "\n".join(
        f"  - ID: {p['id']} | Name: {p['name']} | Categories: {', '.join(p['categories']) or 'general'}"
        for p in candidates
    )

    # ── Build excluded summary for prompt ────────────────────────────────────
    excluded_names = []
    excluded_set   = set(excluded_ids)
    for p in state["all_products"]:
        if p["id"] in excluded_set:
            excluded_names.append(f"{p['name']} (ID: {p['id']})")
    excluded_summary = "\n".join(f"  - {n}" for n in excluded_names) or "  (none)"

    prompt = f"""
You are a product recommendation engine for an online boutique store.

Your task is to select exactly {n} products to recommend to a user.

Selection rules:
1. Only pick product IDs from the CANDIDATE LIST below — do not invent IDs.
2. Avoid recommending the same category more than twice (ensure variety).
3. Choose products that COMPLEMENT what the user already has (listed under "Already has").
4. Return ONLY a valid JSON object — no markdown, no explanation, no preamble.

Output schema:
{{
  "recommended_product_ids": ["<ID1>", "<ID2>", ...]
}}

User already has:
{excluded_summary}

Candidate products (pick {n} from these):
{candidate_lines}
""".strip()

    logger.info(
        "[recommendation_reasoning] invoking LLM | user_id=%s candidates=%d selecting=%d",
        state["user_id"], len(candidates), n,
    )

    try:
        response    = await asyncio.to_thread(llm.invoke, prompt)
        raw         = response.text()
        in_tokens   = response.usage_metadata.get("input_tokens",  0)
        out_tokens  = response.usage_metadata.get("output_tokens", 0)

        logger.info("[recommendation_reasoning] LLM raw: %s", raw[:300])
        logger.info("[recommendation_reasoning] tokens in=%d out=%d", in_tokens, out_tokens)

        recommended = _parse_recommended_ids(raw, valid_ids)

        if not recommended:
            raise ValueError(f"LLM returned no valid product IDs. Raw: {raw!r}")

        # Clamp to n (LLM might return more)
        recommended = recommended[:n]

        logger.info(
            "[recommendation_reasoning] LLM selected %d products: %s",
            len(recommended), recommended,
        )

        return {
            **state,
            "recommended_ids":   recommended,
            "llm_used":          True,
            "error":             None,
            "total_input_tokens":  state["total_input_tokens"]  + in_tokens,
            "total_output_tokens": state["total_output_tokens"] + out_tokens,
            "total_llm_calls":     state["total_llm_calls"]     + 1,
        }

    except Exception as exc:
        logger.warning(
            "[recommendation_reasoning] LLM failed (%s) — falling back to random.sample()",
            exc,
        )

        # Fallback: original random.sample() algorithm
        candidate_ids = [p["id"] for p in candidates]
        fallback_n    = min(MAX_RECOMMENDATIONS, len(candidate_ids))
        indices       = random.sample(range(len(candidate_ids)), fallback_n)
        fallback_ids  = [candidate_ids[i] for i in indices]

        logger.info(
            "[recommendation_reasoning] fallback selected %d products: %s",
            len(fallback_ids), fallback_ids,
        )

        return {
            **state,
            "recommended_ids": fallback_ids,
            "llm_used":        False,
            "error":           str(exc),
        }


# ════════════════════════════════════════════════════════════════════════════
# Node 4 – format_response  (deterministic)
# ════════════════════════════════════════════════════════════════════════════

async def format_response_node(state: RecommendationAgentState) -> RecommendationAgentState:
    """
    Deterministic tool node.

    Validates the selected IDs against the catalog and logs the final
    selection with metadata (LLM vs fallback, token usage).
    """
    recommended = state.get("recommended_ids", [])
    valid_set   = {p["id"] for p in state["all_products"]}

    # Safety guard: strip any invalid IDs that somehow leaked through
    final = [pid for pid in recommended if pid in valid_set]

    logger.info(
        "[format_response] user_id=%s final_ids=%s llm_used=%s llm_calls=%d",
        state["user_id"],
        final,
        state.get("llm_used", False),
        state["total_llm_calls"],
    )

    return {**state, "recommended_ids": final}


# ════════════════════════════════════════════════════════════════════════════
# Graph assembly
# ════════════════════════════════════════════════════════════════════════════

def build_recommendation_agent():
    """Assemble and compile the LangGraph recommendation agent."""
    graph = StateGraph(RecommendationAgentState)

    graph.add_node("fetch_catalog",            fetch_catalog_node)
    graph.add_node("filter_products",          filter_products_node)
    graph.add_node("recommendation_reasoning", recommendation_reasoning_node)
    graph.add_node("return_empty",             return_empty_node)
    graph.add_node("format_response",          format_response_node)

    graph.set_entry_point("fetch_catalog")
    graph.add_edge("fetch_catalog", "filter_products")

    graph.add_conditional_edges(
        "filter_products",
        route_after_filter,
        {
            "recommendation_reasoning": "recommendation_reasoning",
            "return_empty":             "return_empty",
        },
    )

    graph.add_edge("recommendation_reasoning", "format_response")
    graph.add_edge("return_empty",             "format_response")
    graph.add_edge("format_response",          END)

    compiled = graph.compile()
    logger.info("[RecommendationAgent] graph compiled successfully")
    return compiled


# Singleton compiled graph
recommendation_graph = build_recommendation_agent()


# ════════════════════════════════════════════════════════════════════════════
# Public helper called by the gRPC servicer
# ════════════════════════════════════════════════════════════════════════════

async def run_recommendation_agent(
    user_id:      str,
    excluded_ids: list[str],
    catalog_stub: demo_pb2_grpc.ProductCatalogServiceStub,
    grpc_context: Any = None,
) -> RecommendationAgentState:
    """
    Build initial state and invoke the compiled graph.

    Args:
        user_id:      Current user identifier.
        excluded_ids: Product IDs already in cart / recently viewed.
        catalog_stub: Async ProductCatalogServiceStub.
        grpc_context: gRPC ServicerContext for abort calls on catalog failure.

    Returns:
        Final RecommendationAgentState.
        Read state["recommended_ids"] for the result.
        Read state["llm_used"] to know if LLM or fallback ran.
        Read state["error"] for any non-fatal warning.
    """
    initial_state: RecommendationAgentState = {
        "user_id":      user_id,
        "excluded_ids": excluded_ids,
        "catalog_stub": catalog_stub,
        "grpc_context": grpc_context,
        # intermediate
        "all_product_ids":    [],
        "all_products":       [],
        "candidate_products": [],
        # output
        "recommended_ids": [],
        "llm_used":        False,
        "error":           None,
        # metrics
        "total_input_tokens":  0,
        "total_output_tokens": 0,
        "total_llm_calls":     0,
    }

    logger.info(
        "[run_recommendation_agent] invoking graph | user_id=%s excluded=%d",
        user_id, len(excluded_ids),
    )

    result: RecommendationAgentState = await recommendation_graph.ainvoke(initial_state)

    logger.info(
        "[run_recommendation_agent] completed | user_id=%s "
        "recommended=%d llm_used=%s llm_calls=%d error=%s",
        user_id,
        len(result["recommended_ids"]),
        result["llm_used"],
        result["total_llm_calls"],
        result.get("error"),
    )

    return result
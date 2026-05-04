"""
productcatalogagent/agent.py

LangGraph product catalog agent — replaces simple search with intelligent
agentic product discovery while keeping the exact same gRPC interface.

Graph topology:
─────────────────────────────────────────────────────────────────────────────
  ┌──────────────────┐
  │  validate_query  │  (deterministic) parse & normalize search query
  └────────┬─────────┘
           │
     ┌─────▼──────┐  query valid & non-empty?
     │   route    │──────────────────────────────────────────────────┐
     └─────┬──────┘  no                                              │ yes
           │                                                         │
  ┌────────▼──────────┐                                  ┌───────────▼────────────┐
  │  semantic_search  │  (deterministic) vector/keyword  │  no_results_found      │
  │                   │  search from catalog              │  returns empty results │
  └────────┬──────────┘                                  └───────────┬────────────┘
           │                                                         │
  ┌────────▼──────────────────┐                                      │
  │ ranking_and_filtering     │  (LLM) re-ranks results by          │
  │                           │  relevance, applies filters         │
  └────────┬──────────────────┘                                      │
           │                                                         │
  ┌────────▼──────────────────┐◄────────────────────────────────────┘
  │  log_search_interaction   │  (deterministic) audit trail to MongoDB
  └────────┬──────────────────┘
           │
          END

Node roles
──────────
validate_query         Deterministic tool. Parses and normalizes the search
                       query. Checks for empty or malformed input.

semantic_search        Deterministic tool. Performs keyword/semantic search
                       against the product catalog. Extracts candidate
                       products matching query terms.

ranking_and_filtering  LLM node (Ollama llama3). Re-ranks search results
                       by relevance, filters by price/category if specified.
                       Returns top N results with reasoning.

no_results_found       Deterministic shortcut when query is invalid or
                       no results found. Returns empty results list.

log_search_interaction Deterministic tool. Records search query, results,
                       and LLM ranking decision to MongoDB audit trail.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import re
import uuid
from typing import Any, Dict, Optional
import os

from langchain_ollama import ChatOllama
from langgraph.graph import END, StateGraph
from motor.motor_asyncio import AsyncIOMotorClient
from typing_extensions import TypedDict

from ..shared import demo_pb2

logger = logging.getLogger("productcatalogagent")

# ── Ollama LLM (temperature=0 for deterministic ranking) ──────────────────────
llm = ChatOllama(model="llama3.2:3b", temperature=0.0, reasoning=False)


# ── MongoDB Configuration ─────────────────────────────────────────────────

# MONGODB_URI = os.getenv("MONGODB_URI", "mongodb://user:pass1@localhost:27017")
# MONGODB_DB = os.getenv("MONGODB_DB", "google_ms")

# # Global client (lazy-initialized)
# _mongodb_client: AsyncIOMotorClient = None


# async def get_mongodb_client() -> AsyncIOMotorClient:
#     """Get or create the MongoDB client."""
#     global _mongodb_client
#     if _mongodb_client is None:
#         _mongodb_client = AsyncIOMotorClient(MONGODB_URI)
#         # Verify connection
#         await _mongodb_client.admin.command("ping")
#         logger.info("Connected to MongoDB at %s", MONGODB_URI)
#     return _mongodb_client


# async def get_search_interactions_collection():
#     """Get the search_interactions collection."""
#     client = await get_mongodb_client()
#     db = client[MONGODB_DB]
#     collection = db["search_interactions"]
    
#     # Ensure indexes
#     await collection.create_index("search_id", unique=True)
#     await collection.create_index("query")
#     await collection.create_index("created_at")
#     return collection

# ════════════════════════════════════════════════════════════════════════════
# Agent state
# ════════════════════════════════════════════════════════════════════════════

class ProductSearchAgentState(TypedDict):
    """
    Shared state threaded through every node in the graph.

    Input fields (set before ainvoke):
        query – search string from user
        catalog – list of Product protobuf messages

    Intermediate fields (written by nodes):
        search_id         – UUID for this search interaction
        query_normalized  – cleaned/normalized query
        validation_error  – set if query is invalid; None on success
        candidate_results – products matching query keywords
        ranked_results    – final ranked/filtered results from LLM

    Output fields (written by ranking_and_filtering / no_results_found):
        decision          – {status: SUCCESS|NO_RESULTS, results: [...], reason: str}

    Metrics (accumulated by ranking_and_filtering):
        total_input_tokens
        total_output_tokens
        total_llm_calls
    """
    # ── inputs ────────────────────────────────────────────────────────────────
    query:   str
    catalog: list

    # ── intermediate ──────────────────────────────────────────────────────────
    search_id:         Optional[str]
    query_normalized:  Optional[str]
    validation_error:  Optional[str]
    candidate_results: Optional[list]
    ranked_results:    Optional[list]

    # ── output ────────────────────────────────────────────────────────────────
    decision:          Dict[str, Any]

    # ── metrics ───────────────────────────────────────────────────────────────
    total_input_tokens:  int
    total_output_tokens: int
    total_llm_calls:     int


# ════════════════════════════════════════════════════════════════════════════
# Node 1 – validate_query  (deterministic tool)
# ════════════════════════════════════════════════════════════════════════════

async def validate_query_node(state: ProductSearchAgentState) -> ProductSearchAgentState:
    """
    Deterministic tool node.

    Validates and normalizes the search query:
      • Strip whitespace
      • Check non-empty
      • Convert to lowercase
      • Remove special characters (keep only alphanumeric + space)

    On success  → populates query_normalized; leaves validation_error=None.
    On failure  → sets validation_error.
    """
    logger.info("[validate_query] validating | query=%r", state["query"])

    query = state["query"].strip() if state["query"] else ""

    if not query:
        logger.warning("[validate_query] empty query")
        return {
            **state,
            "search_id": str(uuid.uuid4()),
            "query_normalized": "",
            "validation_error": "Search query is empty.",
            "candidate_results": [],
            "ranked_results": [],
        }

    # Normalize: lowercase, remove extra spaces
    query_normalized = " ".join(query.lower().split())

    # Remove non-alphanumeric except spaces
    query_normalized = re.sub(r"[^a-z0-9\s]", " ", query_normalized)
    query_normalized = " ".join(query_normalized.split())  # clean extra spaces

    logger.info("[validate_query] passed | normalized=%r", query_normalized)

    return {
        **state,
        "search_id": str(uuid.uuid4()),
        "query_normalized": query_normalized,
        "validation_error": None,
    }


# ════════════════════════════════════════════════════════════════════════════
# Conditional router after validate_query
# ════════════════════════════════════════════════════════════════════════════

def route_after_validation(state: ProductSearchAgentState) -> str:
    """
    Conditional edge function — returns the name of the next node.

    If validation_error is set → go to no_results_found (empty query).
    Otherwise                  → go to semantic_search.
    """
    if state.get("validation_error"):
        logger.info("[route] query validation failed → no_results_found")
        return "no_results_found"
    logger.info("[route] query validation passed → semantic_search")
    return "semantic_search"


# ════════════════════════════════════════════════════════════════════════════
# Node 2a – semantic_search  (deterministic tool)
# ════════════════════════════════════════════════════════════════════════════

async def semantic_search_node(state: ProductSearchAgentState) -> ProductSearchAgentState:
    """
    Deterministic tool node — performs keyword search against the catalog.

    Searches product names and descriptions for query terms.
    Returns all matching products (candidates for LLM ranking).

    Sets:
        candidate_results – list of matching Product protobuf objects
    """
    logger.info("[semantic_search] searching | query=%r", state["query_normalized"])

    query = state["query_normalized"].lower()
    query_terms = query.split()
    
    candidates = []
    for product in state["catalog"]:
        product_text = (
            f"{product.name.lower()} {product.description.lower()} "
            f"{' '.join(product.categories)}"
        ).lower()
        
        # Match if any query term appears in product
        if any(term in product_text for term in query_terms):
            candidates.append(product)

    logger.info("[semantic_search] found %d candidates", len(candidates))

    if not candidates:
        logger.info("[semantic_search] no candidates found → go to no_results_found")
        return {
            **state,
            "candidate_results": [],
            "ranked_results": [],
        }

    return {
        **state,
        "candidate_results": candidates,
    }


# ════════════════════════════════════════════════════════════════════════════
# Node 2b – no_results_found  (deterministic shortcut)
# ════════════════════════════════════════════════════════════════════════════

async def no_results_found_node(state: ProductSearchAgentState) -> ProductSearchAgentState:
    """
    Deterministic shortcut node — reached when query invalid or no matches found.

    Bypasses semantic search + LLM ranking and directly returns empty results.
    """
    reason = state.get("validation_error", "No products match your query.")
    logger.info("[no_results_found] setting decision | reason=%s", reason)

    return {
        **state,
        "decision": {
            "status": "NO_RESULTS",
            "results": [],
            "reason": reason,
        },
    }


# ════════════════════════════════════════════════════════════════════════════
# Node 3 – ranking_and_filtering  (LLM / Ollama node)
# ════════════════════════════════════════════════════════════════════════════

def _parse_json_response(text: str) -> dict | None:
    """Extract the first JSON object from an LLM text response."""
    try:
        match = re.search(r"\{.*?\}", text, re.DOTALL)
        if match:
            return json.loads(match.group(0))
    except Exception as exc:
        logger.error("[ranking_and_filtering] JSON parse error: %s — raw: %s", exc, text)
    return None


def _product_to_dict(p) -> dict:
    """Convert Product protobuf to dict for LLM."""
    return {
        "id": p.id,
        "name": p.name,
        "description": p.description,
        "price_usd": {
            "units": p.price_usd.units,
            "nanos": p.price_usd.nanos,
            "formatted": f"USD {p.price_usd.units}.{p.price_usd.nanos // 10_000_000:02d}",
        },
        "categories": list(p.categories),
    }


async def ranking_and_filtering_node(state: ProductSearchAgentState) -> ProductSearchAgentState:
    """
    LLM node — the only non-deterministic step in the graph.

    Receives candidate search results and uses LLM to:
      • Re-rank by relevance to query
      • Filter out low-relevance items
      • Return top 10 results with reasoning

    The LLM must return a JSON decision:
        {
          "status": "SUCCESS",
          "ranked_product_ids": ["id1", "id2", ...],
          "reason": "<brief explanation of ranking>"
        }

    Token usage is accumulated in state for observability.
    """
    candidates_json = json.dumps(
        [_product_to_dict(p) for p in state["candidate_results"]],
        indent=2
    )

    prompt = f"""
You are a product search and ranking agent for an e-commerce platform.

Your task is to rank and filter search results by relevance.

Rules:
- Re-rank the candidate products by how well they match the search query.
- Filter out any products with very low relevance (< 30%).
- Return the top 10 most relevant products, sorted by relevance (highest first).
- Only return the product IDs of the top results.
- Return ONLY valid JSON. No markdown, no code blocks, no preamble.

Output schema:
{{
  "status": "SUCCESS",
  "ranked_product_ids": ["id1", "id2", ...]
}}

Search query: {state['query_normalized']}

Candidate products (already keyword-matched):
{candidates_json}
""".strip()

    logger.info("[ranking_and_filtering] invoking LLM | candidates=%d",
                len(state["candidate_results"]))

    response = await asyncio.to_thread(llm.invoke, prompt)

    raw        = response.text()
    in_tokens  = response.usage_metadata.get("input_tokens",  0)
    out_tokens = response.usage_metadata.get("output_tokens", 0)

    logger.info("[ranking_and_filtering] LLM raw response: %s", raw)
    logger.info("[ranking_and_filtering] tokens | in=%d out=%d", in_tokens, out_tokens)

    decision_json = _parse_json_response(raw)

    if not decision_json or decision_json.get("status") != "SUCCESS":
        logger.error("[ranking_and_filtering] invalid decision: %s", raw)
        raise ValueError(f"Invalid ranking decision from LLM: {raw!r}")

    ranked_ids = decision_json.get("ranked_product_ids", [])
    
    # Filter candidate_results to only ranked IDs, in order
    id_to_product = {p.id: p for p in state["candidate_results"]}
    ranked_results = [id_to_product[pid] for pid in ranked_ids if pid in id_to_product]

    logger.info("[ranking_and_filtering] ranked=%d products | reason=%s",
                len(ranked_results), decision_json.get("reason", ""))

    return {
        **state,
        "ranked_results": ranked_results,
        "decision": {
            "status": "SUCCESS",
            "results": [_product_to_dict(p) for p in ranked_results],
            "reason": decision_json.get("reason", ""),
        },
        "total_input_tokens":  state["total_input_tokens"]  + in_tokens,
        "total_output_tokens": state["total_output_tokens"] + out_tokens,
        "total_llm_calls":     state["total_llm_calls"]     + 1,
    }


# ════════════════════════════════════════════════════════════════════════════
# Node 4 – log_search_interaction  (deterministic tool)
# ════════════════════════════════════════════════════════════════════════════

async def log_search_interaction_node(state: ProductSearchAgentState) -> ProductSearchAgentState:
    """
    Deterministic tool node — logs search interaction to MongoDB.

    Records:
        search_id     – UUID for this search
        query         – original query
        query_normalized – normalized query
        decision      – LLM decision and results
        result_count  – number of results returned
        llm_metrics   – token usage
        created_at    – UTC timestamp
    """
    status = state["decision"].get("status", "NO_RESULTS")
    result_count = len(state["decision"].get("results", []))
    
    # collection = await get_search_interactions_collection()
    
    # doc = {
    #     "_id":              state["search_id"],
    #     "query":            state["query"],
    #     "query_normalized": state.get("query_normalized", ""),
    #     "decision":         state["decision"],
    #     "result_count":     result_count,
    #     "llm_metrics": {
    #         "input_tokens":  state["total_input_tokens"],
    #         "output_tokens": state["total_output_tokens"],
    #         "llm_calls":     state["total_llm_calls"],
    #     },
    #     "created_at":       datetime.datetime.now(tz=datetime.timezone.utc),
    # }

    logger.info("[log_search_interaction] logging | search_id=%s status=%s results=%d",
                state["search_id"], status, result_count)

    # try:
    #     await collection.insert_one(doc)
    #     logger.info("[log_search_interaction] logged successfully | search_id=%s", state["search_id"])
    # except Exception as exc:
    #     # Non-fatal — never block search for a DB write failure
    #     logger.error("[log_search_interaction] MongoDB write failed (non-fatal): %s", exc)

    return state


# ════════════════════════════════════════════════════════════════════════════
# Graph assembly
# ════════════════════════════════════════════════════════════════════════════

def build_product_search_agent() -> Any:
    """
    Assemble and compile the LangGraph product search agent.

    Returns the compiled graph, ready for ainvoke().
    """
    graph = StateGraph(ProductSearchAgentState)

    # Register nodes
    graph.add_node("validate_query",          validate_query_node)
    graph.add_node("semantic_search",         semantic_search_node)
    graph.add_node("no_results_found",        no_results_found_node)
    graph.add_node("ranking_and_filtering",   ranking_and_filtering_node)
    graph.add_node("log_search_interaction",  log_search_interaction_node)

    # Entry point
    graph.set_entry_point("validate_query")

    # Conditional edge after validation
    graph.add_conditional_edges(
        "validate_query",
        route_after_validation,
        {
            "semantic_search":   "semantic_search",
            "no_results_found":  "no_results_found",
        },
    )

    # After semantic search: check if candidates exist
    def route_after_search(state: ProductSearchAgentState) -> str:
        if state.get("candidate_results"):
            logger.info("[route] candidates found → ranking_and_filtering")
            return "ranking_and_filtering"
        logger.info("[route] no candidates → no_results_found")
        return "no_results_found"

    graph.add_conditional_edges(
        "semantic_search",
        route_after_search,
        {
            "ranking_and_filtering": "ranking_and_filtering",
            "no_results_found":      "no_results_found",
        },
    )

    # Happy path: rank → log
    graph.add_edge("ranking_and_filtering",  "log_search_interaction")

    # No results path: no_results → log
    graph.add_edge("no_results_found", "log_search_interaction")

    # Terminal
    graph.add_edge("log_search_interaction", END)

    compiled = graph.compile()
    logger.info("[ProductSearchAgent] graph compiled successfully")
    return compiled


# Singleton graph — built at import time
product_search_graph = build_product_search_agent()


# ════════════════════════════════════════════════════════════════════════════
# Public helper called by the gRPC servicer
# ════════════════════════════════════════════════════════════════════════════

async def run_product_search_agent(
    query: str,
    catalog: list,
) -> ProductSearchAgentState:
    """
    Build initial state and invoke the compiled graph.

    Returns the final ProductSearchAgentState after all nodes have run.
    Raises ValueError if the LLM returns an unparseable decision.
    """
    initial_state: ProductSearchAgentState = {
        # inputs
        "query":   query,
        "catalog": catalog,
        # intermediate
        "search_id":         None,
        "query_normalized":  None,
        "validation_error":  None,
        "candidate_results": None,
        "ranked_results":    None,
        # output
        "decision": {},
        # metrics
        "total_input_tokens":  0,
        "total_output_tokens": 0,
        "total_llm_calls":     0,
    }

    logger.info(
        "[run_product_search_agent] invoking graph | query=%r",
        query,
    )

    result: ProductSearchAgentState = await product_search_graph.ainvoke(initial_state)

    logger.info(
        "[run_product_search_agent] completed | status=%s results=%d llm_calls=%d",
        result["decision"].get("status"),
        len(result["decision"].get("results", [])),
        result["total_llm_calls"],
    )

    return result

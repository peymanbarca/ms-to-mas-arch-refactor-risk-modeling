"""
refactored_architecture/google_ms/adagent/adagent.py

LangGraph-based Ad Selection Agent with MongoDB persistence.

This module implements an agentic workflow for intelligent ad selection and serving,
replacing the deterministic ad matching logic in the baseline AdService.

Workflow:
  1. validate_context - Verify that context_keys are non-empty and valid
  2. select_candidates - Query ad catalog for matching candidates by keyword
  3. rank_recommendations - Use LLM to rank ads by relevance to context
  4. fallback_random - Fallback if no matches or invalid context
  5. persist_interaction - Log interaction 

MongoDB Collection: ad_interactions
  - interaction_id (UUID)
  - context_keys (list[str])
  - candidate_ads (list[dict]) - matched from catalog
  - ranked_ads (list[dict]) - LLM-ranked recommendations
  - decision (str) - RANKED, FALLBACK
  - llm_metrics (dict) - input_tokens, output_tokens, llm_calls
  - created_at (datetime)

LLM Role:
  - Reviews candidate ads and recommends top N by relevance to context
  - Ensures recommendations align with user intent
  - Returns structured decision with chosen ads and reasoning

Dependencies:
  - langchain_ollama (Ollama with llama3.2:3b model)
  - motor (async MongoDB client)
  - google.protobuf (for gRPC proto structures)
"""

import logging
import os
import json
import asyncio
from datetime import datetime
from typing import Any
from uuid import uuid4
from langgraph.graph import StateGraph, START, END
from typing_extensions import TypedDict
import re

from langchain_ollama import ChatOllama

logger = logging.getLogger(__name__)

# ════════════════════════════════════════════════════════════════════════════
# Configuration & Database Setup
# ════════════════════════════════════════════════════════════════════════════

OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2:3b")

# ── Ollama LLM (mirrors sample: temperature=0 for deterministic auth) ─────────
llm = ChatOllama(model=OLLAMA_MODEL, temperature=0.0, reasoning=False)



# ════════════════════════════════════════════════════════════════════════════
# State Definition
# ════════════════════════════════════════════════════════════════════════════

class AdRequestAgentState(TypedDict):
    """State for ad selection agent."""
    interaction_id: str  # UUID for this interaction
    context_keys: list[str]  # User's context (e.g., ["camera", "photography"])
    catalog: dict[str, list[dict]]  # Full ad catalog (category -> [AdEntry, ...])
    max_ads: int  # Max ads to return (default 5)
    candidate_ads: list[dict]  # Ads matching context keywords
    ranked_ads: list[dict]  # LLM-ranked recommendations
    decision: str  # RANKED, FALLBACK
    reasoning: str  # Why this decision was made
    llm_metrics: dict  # input_tokens, output_tokens, llm_calls
    created_at: datetime


# ════════════════════════════════════════════════════════════════════════════
# Node: validate_context
# ════════════════════════════════════════════════════════════════════════════

async def validate_context(state: AdRequestAgentState) -> AdRequestAgentState:
    """
    Validate that context_keys are non-empty and valid.
    
    Returns: state unchanged if valid, sets decision=FALLBACK if invalid
    """
    context_keys = state["context_keys"]
    
    if not context_keys or not isinstance(context_keys, list):
        logger.warning(
            "Invalid context_keys for interaction %s: %s",
            state["interaction_id"],
            context_keys
        )
        state["decision"] = "FALLBACK"
        state["reasoning"] = "Empty or invalid context_keys provided"
        return state
    
    # Check if any keys exist in catalog
    catalog = state["catalog"]
    has_match = any(key in catalog for key in context_keys)
    
    if not has_match:
        logger.info(
            "No matching categories for context_keys %s (available: %s)",
            context_keys,
            list(catalog.keys())
        )
        state["decision"] = "FALLBACK"
        state["reasoning"] = f"No matching ads for context_keys: {context_keys}"
        return state
    
    logger.info(
        "Context validation passed for interaction %s: %s",
        state["interaction_id"],
        context_keys
    )
    return state


# ════════════════════════════════════════════════════════════════════════════
# Node: select_candidates
# ════════════════════════════════════════════════════════════════════════════


async def select_candidates(state: AdRequestAgentState) -> AdRequestAgentState:
    """
    Query ad catalog for candidates matching context_keys.
    
    Candidates are gathered from both:
    1. Category-based matching (if context_key is a category)
    2. Could be extended for semantic matching via LLM
    
    Returns: state with candidate_ads populated
    """
    context_keys = state["context_keys"]
    catalog = state["catalog"]
    candidates = []
    seen_urls = set()  # Avoid duplicates
    
    # Simple strategy: get ads from matching categories
    for key in context_keys:
        if key in catalog:
            for ad in catalog[key]:
                # Avoid duplicate redirect_urls
                if ad.get("redirect_url") not in seen_urls:
                    candidates.append(ad)
                    seen_urls.add(ad.get("redirect_url"))
    
    state["candidate_ads"] = candidates
    logger.info(
        "Selected %d candidate ads for context_keys %s",
        len(candidates),
        context_keys
    )
    
    return state


# ════════════════════════════════════════════════════════════════════════════
# Node: rank_recommendations by LLM reasoning
# ════════════════════════════════════════════════════════════════════════════

def _parse_json_response(text: str) -> dict | None:
    """Extract the first JSON object from an LLM text response."""
    try:
        match = re.search(r"\{.*?\}", text, re.DOTALL)
        if match:
            return json.loads(match.group(0))
    except Exception as exc:
        logger.error("[ad_reasoning] JSON parse error: %s — raw: %s", exc, text)
    return None



async def rank_recommendations(state: AdRequestAgentState) -> AdRequestAgentState:
    """
    Use LLM to rank candidate ads by relevance to context.
    
    LLM receives:
      - context_keys: user's expressed interests
      - candidate_ads: available ads from catalog
    
    LLM returns:
      - top N ranked ads by relevance
    
    Returns: state with ranked_ads populated
    """
    context_keys = state["context_keys"]
    candidate_ads = state["candidate_ads"]
    max_ads = state["max_ads"]
    
    if not candidate_ads:
        logger.info("No candidate ads to rank for interaction %s", state["interaction_id"])
        state["ranked_ads"] = []
        return state
    
    # Build prompt for LLM
    ads_json = json.dumps(candidate_ads, indent=2)
    context_str = ", ".join(context_keys)
    
    prompt = f"""You are an ad recommendation engine. Your task is to rank ads by relevance to user context.

User Context: {context_str}

Available Ads:
{ads_json}

Task:
1. Evaluate each ad's relevance to the user context
2. Rank them from most to least relevant
3. Return the top {max_ads} ads position index in the ranked list (not the ad content itself)

Do not generate python code.
Return ONLY a JSON with "ranked_ad_ids" key of array of the top {max_ads} ranked ads only with position index in the ranked list (not the ad content itself).
Example format:
{{
    "ranked_ad_ids": [
        index_of_most_relevant_ad,
        index_of_second_most_relevant_ad,
        index_of_third_most_relevant_ad
    ]
}}
"""
    
    try:
        response = await asyncio.to_thread(llm.invoke, prompt)
        
        # Parse LLM response (extract JSON)
        raw = str(response.text())        
        in_tokens  = response.usage_metadata.get("input_tokens",  0)
        out_tokens = response.usage_metadata.get("output_tokens", 0)

        logger.info("[ad_rank_reasoning] LLM raw response: %s", raw)
        logger.info("[ad_rank_reasoning] tokens | in=%d out=%d", in_tokens, out_tokens)

        parsed = _parse_json_response(raw)
        print(parsed)
        ranked_ids  = parsed['ranked_ad_ids'] if parsed else None
        ranked = [candidate_ads[i] for i in ranked_ids] if ranked_ids else None
        if ranked is not None and isinstance(ranked, list):
            state["ranked_ads"] = ranked[:max_ads]
            state["decision"] = "RANKED"
            state["reasoning"] = "LLM ranked ads based on relevance to context"
            state["llm_metrics"]["input_tokens"] = in_tokens
            state["llm_metrics"]["output_tokens"] = out_tokens
            state["llm_metrics"]["llm_calls"] = state["llm_metrics"].get("llm_calls", 0) + 1
        else:
            logger.warning("[ad_rank_reasoning] LLM did not return valid ranked ads")
            state["ranked_ads"] = candidate_ads[:max_ads]
            state["decision"] = "RANKED"
            state["reasoning"] = f"Used first {len(state['ranked_ads'])} candidates (LLM parse failed)"
            state["llm_metrics"]["llm_calls"] = state["llm_metrics"].get("llm_calls", 0) + 1


    except Exception as e:
        logger.error(
            "LLM error during ranking for interaction %s: %s",
            state["interaction_id"],
            e
        )
              
        # Fallback: use first N candidates if LLM parsing fails
        logger.info(
            "Fallback ranking (LLM parse failed) for interaction %s",
            state["interaction_id"]
        )
        state["ranked_ads"] = candidate_ads[:max_ads]
        state["decision"] = "RANKED"
        state["reasoning"] = f"Used first {len(state['ranked_ads'])} candidates (LLM parse failed)"
        state["llm_metrics"]["llm_calls"] = state["llm_metrics"].get("llm_calls", 0) + 1
    
    return state


# ════════════════════════════════════════════════════════════════════════════
# Node: fallback_random
# ════════════════════════════════════════════════════════════════════════════

async def fallback_random(state: AdRequestAgentState) -> AdRequestAgentState:
    """
    Fallback ad selection when context validation fails or no matches found.
    
    Returns: state with ranked_ads populated from random selection
    """
    catalog = state["catalog"]
    max_ads = state["max_ads"]
    
    # Gather all ads from catalog
    all_ads = []
    for category_ads in catalog.values():
        all_ads.extend(category_ads)
    
    if all_ads:
        import random
        selected = random.sample(all_ads, min(max_ads, len(all_ads)))
        state["ranked_ads"] = selected
        state["decision"] = "FALLBACK"
        state["reasoning"] = f"Selected {len(selected)} random ads (context-based selection failed)"
        logger.info(
            "Fallback: selected %d random ads for interaction %s",
            len(selected),
            state["interaction_id"]
        )
    else:
        state["ranked_ads"] = []
        state["decision"] = "FALLBACK"
        state["reasoning"] = "No ads available in catalog"
        logger.warning("No ads available in catalog for fallback selection")
    
    return state


# ════════════════════════════════════════════════════════════════════════════
# Node: persist_interaction
# ════════════════════════════════════════════════════════════════════════════

async def persist_interaction(state: AdRequestAgentState) -> AdRequestAgentState:
    """
    Log ad selection interaction for audit trail.
    
    Document includes:
      - interaction_id (UUID)
      - context_keys (list)
      - candidate_ads (list)
      - ranked_ads (list)
      - decision (RANKED | FALLBACK)
      - reasoning (str)
      - llm_metrics (dict with token counts)
      - created_at (datetime)
    """
    doc = {
        "interaction_id": state["interaction_id"],
        "context_keys": state["context_keys"],
        "candidate_ads": state["candidate_ads"],
        "ranked_ads": state["ranked_ads"],
        "decision": state["decision"],
        "reasoning": state["reasoning"],
        "llm_metrics": state["llm_metrics"],
        "created_at": state["created_at"],
    }

    logger.info(
            "Log ad interaction %s (decision=%s), content=%s",
            state["interaction_id"],
            state["decision"],
            doc
        )
    
    return state


# ════════════════════════════════════════════════════════════════════════════
# Graph Construction & Routing
# ════════════════════════════════════════════════════════════════════════════

def _route_after_validation(state: AdRequestAgentState) -> str:
    """
    Route after context validation.
    
    If validation failed (decision=FALLBACK), go to fallback_random.
    Otherwise, proceed to select_candidates.
    """
    if state["decision"] == "FALLBACK":
        return "fallback_random"
    return "select_candidates"


def build_ad_request_agent():
    """Build LangGraph StateGraph for ad selection agent."""
    workflow = StateGraph(AdRequestAgentState)
    
    # Add nodes
    workflow.add_node("validate_context", validate_context)
    workflow.add_node("select_candidates", select_candidates)
    workflow.add_node("rank_recommendations", rank_recommendations)
    workflow.add_node("fallback_random", fallback_random)
    workflow.add_node("persist_interaction", persist_interaction)
    
    # Build graph topology
    workflow.add_edge(START, "validate_context")
    workflow.add_conditional_edges(
        "validate_context",
        _route_after_validation,
        {
            "select_candidates": "select_candidates",
            "fallback_random": "fallback_random",
        }
    )
    workflow.add_edge("select_candidates", "rank_recommendations")
    workflow.add_edge("rank_recommendations", "persist_interaction")
    workflow.add_edge("fallback_random", "persist_interaction")
    workflow.add_edge("persist_interaction", END)
    
    return workflow.compile()


# Singleton agent instance
ad_graph = None


def get_ad_request_agent():
    """Get or create singleton ad request agent."""
    global ad_graph
    if ad_graph is None:
        ad_graph = build_ad_request_agent()
    return ad_graph


# ════════════════════════════════════════════════════════════════════════════
# Public API
# ════════════════════════════════════════════════════════════════════════════

async def run_ad_request_agent(
    context_keys: list[str],
    catalog: dict[str, list[dict]],
    max_ads: int = 5,
) -> list[dict]:
    """
    Execute ad selection agent workflow.
    
    Args:
        context_keys: User's context/interests (e.g., ["camera", "photography"])
        catalog: Ad catalog (category -> [AdEntry, ...])
        max_ads: Max ads to return (default 5)
    
    Returns:
        list[dict]: Ranked ads (or fallback random ads)
    """
    
    # Initialize state
    interaction_id = str(uuid4())
    initial_state = AdRequestAgentState(
        interaction_id=interaction_id,
        context_keys=context_keys,
        catalog=catalog,
        max_ads=max_ads,
        candidate_ads=[],
        ranked_ads=[],
        decision="",
        reasoning="",
        llm_metrics={
            "input_tokens": 0,
            "output_tokens": 0,
            "llm_calls": 0,
        },
        created_at=datetime.utcnow(),
    )
    
    # Execute graph
    ad_graph = get_ad_request_agent()
    try:
        final_state = await ad_graph.ainvoke(initial_state)
        ads = final_state.get("ranked_ads", [])
        
        logger.info(
            "Ad request completed | interaction_id=%s | decision=%s | ads_returned=%d",
            interaction_id,
            final_state.get("decision"),
            len(ads),
        )
        
        return ads
    
    except Exception as e:
        logger.error(
            "Ad request agent error | interaction_id=%s: %s",
            interaction_id,
            e
        )
        # Return empty list on error (caller should handle gracefully)
        return []

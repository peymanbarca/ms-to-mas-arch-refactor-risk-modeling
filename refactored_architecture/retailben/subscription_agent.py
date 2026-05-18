"""
SUBSCRIPTION AGENT - Graph Topology

    START
      |
      v
  [validate_promo] (LLM Node - Validate Promo Code & Check Expiry)
      |
      ├─ INVALID ──────────────────> END (Error: Code not found)
      |
      ├─ EXPIRED ──────────────────> END (Error: Code expired)
      |
      └─ VALID ────> [check_duplicate] (LLM Node - Prevent Duplicate Subscriptions)
                           |
                           ├─ DUPLICATE ──────────> END (Error: Already subscribed)
                           |
                           └─ NEW ─────> [create_subscription] (Persist to DB)
                                              |
                                              v
                                             END

Alternative Flow for Fetching Subscriptions:

    START
      |
      v
  [fetch_user_subscriptions] (DB Query - Get Active Subscriptions)
      |
      v
  [filter_active] (LLM Node - Filter Expired & Sort by Discount)
      |
      v
      END


All three decision nodes now use Ollama (llama3) for intelligent reasoning:

✅ Promo validation with catalogue context
✅ Duplicate detection with contextual awareness
✅ Subscription filtering and ranking
Token metrics are tracked for all LLM calls throughout the subscription lifecycle.


Key Features:
- Validates promotion codes against catalogue
- Checks expiry dates (SUMMER20, WELCOME10, FLASH50, LOYALTY15)
- Prevents duplicate (user_id + promo_code) subscriptions
- Manages subscription lifecycle (creation, retrieval, validation)
- Returns active subscriptions sorted by discount percentage
- LLM-based decision making for validation and filtering
- Tracks token usage and LLM call metrics
- Integration with MongoDB for persistence
"""

import os
import logging
import time
import uuid
import datetime
import httpx
import json
import re

from fastapi import FastAPI, HTTPException, Path
from pydantic import BaseModel, Field, EmailStr
from typing import TypedDict, List, Dict, Any, Optional, Literal
from motor.motor_asyncio import AsyncIOMotorClient
from httpx import AsyncClient
from pymongo import ReturnDocument
from langgraph.graph import StateGraph, END
from langchain_ollama import ChatOllama
import asyncio


logger = logging.getLogger("subscription_agent")
logging.basicConfig(
    filename='./logs/subscription_agent.log',
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017/")
MONGO_DB = os.getenv("MONGO_DB", "retailben")
PORT = int(os.getenv("PORT", 8010))

llm = ChatOllama(model="llama3.2:3b", temperature=0.0, reasoning=False)

app = FastAPI(title="Subscription Agent")

# DB client will be set on startup
db_client: Optional[AsyncIOMotorClient] = None
db = None

# Promotion Catalogue
PROMO_CATALOGUE: Dict[str, Dict[str, Any]] = {
    "SUMMER20": {
        "title": "Summer Sale",
        "description": "20% off all orders this summer.",
        "discount_percent": 20.0,
        "expires_at": datetime.datetime(2026, 8, 31, 23, 59, 59),
    },
    "WELCOME10": {
        "title": "Welcome Discount",
        "description": "10% off your first order.",
        "discount_percent": 10.0,
        "expires_at": datetime.datetime(2026, 12, 31, 23, 59, 59),
    },
    "FLASH50": {
        "title": "Flash Deal",
        "description": "50% off for the next 24 hours!",
        "discount_percent": 50.0,
        "expires_at": datetime.datetime(2026, 6, 1, 23, 59, 59),
    },
    "LOYALTY15": {
        "title": "Loyalty Reward",
        "description": "15% off as a thank-you to loyal customers.",
        "discount_percent": 15.0,
        "expires_at": datetime.datetime(2026, 3, 31, 23, 59, 59),
    },
}

PROMO_CATALOGUE_S = {
    code: {
        "expires_at": promo["expires_at"].isoformat()
    }
    for code, promo in PROMO_CATALOGUE.items()
}


class PromoEntry(BaseModel):
    promo_code: str
    title: str
    description: str
    discount_percent: float
    expires_at: datetime.datetime
    is_subscribable: bool


class BuySubscriptionRequest(BaseModel):
    user_id: str = Field(..., description="Unique user/customer identifier")
    email: EmailStr
    promo_code: str = Field(..., description="Promotion code from the catalogue")


class SubscriptionRecord(BaseModel):
    subscription_id: str
    user_id: str
    email: Optional[str]
    promo_code: str
    promo_title: str
    promo_description: str
    discount_percent: float
    subscribed_at: datetime.datetime
    expires_at: datetime.datetime
    is_active: bool


class SubscriptionRecords(BaseModel):
    user_id: str
    subscriptions: List[SubscriptionRecord]
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_llm_calls: int = 0


class BuySubscriptionResponse(BaseModel):
    subscription: SubscriptionRecord
    total_input_tokens: int
    total_output_tokens: int
    total_llm_calls: int


class SubscriptionAgentState(TypedDict):
    operation: str  # "BUY" or "FETCH"
    user_id: str
    email: Optional[str]
    promo_code: Optional[str]
    validation_status: Optional[str]
    duplicate_check: Optional[str]
    catalogue_lookup: Optional[Dict[str, Any]]
    subscription_result: Optional[Dict[str, Any]]
    user_subscriptions: Optional[List[Dict[str, Any]]]
    result: Optional[Dict[str, Any]]
    error_message: Optional[str]
    total_input_tokens: int
    total_output_tokens: int
    total_llm_calls: int


@app.on_event("startup")
async def startup():
    global db_client, db
    db_client = AsyncIOMotorClient(MONGO_URI)
    db = db_client[MONGO_DB]
    logger.info("Connected to MongoDB at %s db=%s", MONGO_URI, MONGO_DB)
    
    # Create indices
    await db.user_subscriptions.create_index(
        [("user_id", 1), ("promo_code", 1)], unique=True
    )
    await db.user_subscriptions.create_index("user_id")


@app.on_event("shutdown")
async def shutdown():
    global db_client
    if db_client:
        db_client.close()
        logger.info("MongoDB connection closed")


def parse_json_response(text: str):
    import re
    try:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            return json.loads(m.group(0))
        return None
    except Exception as e:
        logging.error(f"parse error: {e} -- {text}")
        return None


async def validate_promo_node(state: SubscriptionAgentState) -> SubscriptionAgentState:
    """Validate promo code and check expiry using LLM reasoning"""
    logger.info(f'Calling validate_promo_node ... Current State is {state}')
    print(f'Calling validate_promo_node ... Current State is {state}')
    
    promo_code = state["promo_code"].upper().strip()
    
    prompt = f"""
    You are a promotion validation agent.
    
    Your task:
    - Validate the promo code against the catalogue
    - Check if the code is expired (current_time >= expires_at)
    - Return a decision: VALID, INVALID, or EXPIRED
    
    Rules:
    - If code does not exist in catalogue, respond: INVALID
    - If code exists but is expired, respond: EXPIRED
    - If code exists and not expired, respond: VALID
    - Return ONLY a JSON response with status field, Do not generate python code
    
    Output Schema:
    {{
      "status": "VALID" | "INVALID" | "EXPIRED"
    }}
    
    Current UTC Time: {datetime.datetime.now().isoformat()}
    
    Promo Code to Validate: {promo_code}
    
    Available Promotion Catalogue:
    {json.dumps(PROMO_CATALOGUE_S, default=str)}
    
    """
    
    logger.info(f'LLM Call Prompt: {prompt}')
    response = await asyncio.to_thread(llm.invoke, prompt)
    
    raw_response = response.text()
    input_tokens = response.usage_metadata.get("input_tokens")
    output_tokens = response.usage_metadata.get("output_tokens")
    
    logger.info(f'LLM Raw response: {raw_response}')
    print(f'LLM Raw response: {raw_response}')
    
    parsed = parse_json_response(raw_response)
    
    if not parsed:
        state["validation_status"] = "INVALID"
        state["error_message"] = "Failed to parse LLM response for promo validation"
        logger.error(f"Invalid JSON from validation agent: {raw_response}")
        return state
    
    status = parsed.get("status", "INVALID")
    reason = parsed.get("reason", "Unknown error")
    
    state["validation_status"] = status
    
    if status == "VALID":
        # Fetch promo details from catalogue
        promo = PROMO_CATALOGUE.get(promo_code)
        if promo:
            state["catalogue_lookup"] = promo
            state["promo_code"] = promo_code
        else:
            state["validation_status"] = "INVALID"
            state["error_message"] = f"Promo code '{promo_code}' not found in catalogue"
    else:
        state["error_message"] = reason
    
    state["total_input_tokens"] += input_tokens if input_tokens else 0
    state["total_output_tokens"] += output_tokens if output_tokens else 0
    state["total_llm_calls"] += 1
    
    logger.info(f'Promo validation result: {status} - {reason}')
    return state


async def check_duplicate_subscription_node(state: SubscriptionAgentState) -> SubscriptionAgentState:
    """Check if user already has this subscription using LLM reasoning"""
    logger.info(f'Calling check_duplicate_subscription_node ... Current State is {state}')
    print(f'Calling check_duplicate_subscription_node ... Current State is {state}')
    
    # Query database for existing subscription
    existing = await db.user_subscriptions.find_one({
        "user_id": state["user_id"],
        "promo_code": state["promo_code"]
    })
    
    existing_record = True if existing else False
    
    
    prompt = f"""
    You are a duplicate subscription detection agent.
    
    Your task:
    - Determine if a user has already subscribed to a promo code
    - Return a decision as json schema specified: DUPLICATE or NEW
    - Do not generate python code, only return JSON response
    
    Rules:
    - If EXISTING_SUBSCRIPTION is False, respond: NEW (no duplicate)
    - If EXISTING_SUBSCRIPTION is True, respond: DUPLICATE (user already subscribed)
    - Return ONLY a JSON response with decision field
    
    Input:
    USER_ID: {state["user_id"]}
    PROMO_CODE: {state["promo_code"]}
    EXISTING_SUBSCRIPTION: {existing_record}
    

    
    Schema:
    {{
      "decision": "NEW" | "DUPLICATE"
    }}
    """
    
    logger.info(f'LLM Call Prompt: {prompt}')
    response = await asyncio.to_thread(llm.invoke, prompt)
    
    raw_response = response.text()
    input_tokens = response.usage_metadata.get("input_tokens")
    output_tokens = response.usage_metadata.get("output_tokens")
    
    logger.info(f'LLM Raw response: {raw_response}')
    print(f'LLM Raw response: {raw_response}')
    logger.info(f'LLM Token Metrics: input_tokens: {input_tokens}, output_tokens: {output_tokens},' 
                f' total_tokens: {response.usage_metadata.get("total_tokens")}')
    
    parsed = parse_json_response(raw_response)
    
    if not parsed:
        # Fallback to database check
        state["duplicate_check"] = "DUPLICATE" if existing else "NEW"
        logger.warning(f"Failed to parse LLM response, using database check: {state['duplicate_check']}")
        return state
    
    decision = parsed.get("decision", "NEW" if not existing else "DUPLICATE")
    reason = parsed.get("reason", "")
    
    state["duplicate_check"] = decision
    
    if decision == "DUPLICATE":
        state["error_message"] = f"User '{state['user_id']}' already subscribed to '{state['promo_code']}'"
        logger.info(f'Duplicate subscription detected: {reason}')
    else:
        logger.info(f'No duplicate found: {reason}')
    
    state["total_input_tokens"] += input_tokens if input_tokens else 0
    state["total_output_tokens"] += output_tokens if output_tokens else 0
    state["total_llm_calls"] += 1
    
    return state


async def create_subscription_tool(state: SubscriptionAgentState) -> SubscriptionAgentState:
    """Create and persist subscription to database"""
    logger.info(f'Calling create_subscription_tool ... Current State is {state}')
    print(f'Calling create_subscription_tool ... Current State is {state}')
    
    promo = state["catalogue_lookup"]
    
    doc = {
        "subscription_id": str(uuid.uuid4()),
        "user_id": state["user_id"],
        "email": state["email"],
        "promo_code": state["promo_code"],
        "promo_title": promo["title"],
        "promo_description": promo["description"],
        "discount_percent": promo["discount_percent"],
        "subscribed_at": datetime.datetime.now(),
        "expires_at": promo["expires_at"],
        "is_active": True
    }
    
    await db.user_subscriptions.insert_one(doc)
    
    state["subscription_result"] = doc
    state["result"] = doc
    
    logger.info(f'Subscription created successfully for user {state["user_id"]}')
    return state


async def fetch_user_subscriptions_tool(state: SubscriptionAgentState) -> SubscriptionAgentState:
    """Fetch all subscriptions for a user"""
    logger.info(f'Calling fetch_user_subscriptions_tool ... Current State is {state}')
    print(f'Calling fetch_user_subscriptions_tool ... Current State is {state}')
    
    cursor = db.user_subscriptions.find({"user_id": state["user_id"]})
    docs = await cursor.to_list(length=500)
    
    state["user_subscriptions"] = docs
    
    logger.info(f'Fetched {len(docs)} subscriptions for user {state["user_id"]}')
    return state


async def filter_and_rank_subscriptions_node(state: SubscriptionAgentState) -> SubscriptionAgentState:
    """Filter active subscriptions and rank by discount using LLM reasoning"""
    logger.info(f'Calling filter_and_rank_subscriptions_node ... Current State is {state}')
    print(f'Calling filter_and_rank_subscriptions_node ... Current State is {state}')
    
    user_subscription_summary = [{'subscription_id': s['subscription_id'], 'promo_code': s['promo_code'], 'discount_percent': s['discount_percent'], 'expires_at': s['expires_at']}
                                 for s in state["user_subscriptions"]]
    prompt = f"""
    You are a subscription filtering and ranking agent.
    
    Your task:
    - Analyze all subscriptions for the user
    - Filter out expired subscriptions (where current_time >= expires_at)
    - Sort active subscriptions by discount_percent in descending order (highest discount first)
    - Return only the subscription IDs of active subscriptions in order
    - Return ONLY valid JSON, Do not generate python code
    
    Rules:
    - A subscription is active only if current_time < expires_at
    - Sort by discount_percent descending (highest first)
    - Return only the subscription_id values in the active_subscription_ids array and maintain the order descending based on discount
    - If no active subscriptions, return empty array
    
    Output Schema:
    {{
      "active_subscription_ids": [string],
      "total_active": number,
      "highest_discount": number
    }}
    
    Current UTC Time: {datetime.datetime.now().isoformat()}
    
    All User Subscriptions:
    {json.dumps(user_subscription_summary, default=str)}
    

    
    """
    
    logger.info(f'LLM Call Prompt: {prompt}')
    response = await asyncio.to_thread(llm.invoke, prompt)
    
    raw_response = response.text()
    input_tokens = response.usage_metadata.get("input_tokens")
    output_tokens = response.usage_metadata.get("output_tokens")
    
    logger.info(f'LLM Raw response: {raw_response}')
    print(f'LLM Raw response: {raw_response}')
    logger.info(f'LLM Token Metrics: input_tokens: {input_tokens}, output_tokens: {output_tokens},'
             f' total_tokens: {response.usage_metadata.get("total_tokens")}')
    
    parsed = parse_json_response(raw_response)
    
    if not parsed:
        logger.warning("Failed to parse LLM response, falling back to deterministic filtering")
        # Fallback: Filter active subscriptions deterministically
        now = datetime.datetime.now()
        active_subs = [
            s for s in state["user_subscriptions"]
            if s["expires_at"] >= now
        ]
        active_subs.sort(key=lambda s: s["discount_percent"], reverse=True)
    else:
        # Get active subscription IDs from LLM response
        active_ids = parsed.get("active_subscription_ids", [])
        
        # Build full subscription objects from IDs
        sub_map = {s["subscription_id"]: s for s in state["user_subscriptions"]}
        active_subs = [sub_map[sid] for sid in active_ids if sid in sub_map]
    
    state["result"] = {
        "subscriptions": active_subs,
        "total_active": len(active_subs),
        "highest_discount": max((s["discount_percent"] for s in active_subs), default=0) if active_subs else 0
    }
    
    state["total_input_tokens"] += input_tokens if input_tokens else 0
    state["total_output_tokens"] += output_tokens if output_tokens else 0
    state["total_llm_calls"] += 1
    
    logger.info(f'Filtered {len(active_subs)} active subscriptions')
    return state


def build_subscription_agent():
    """Build LangGraph for subscription management"""
    graph = StateGraph(SubscriptionAgentState)
    
    # Nodes for BUY operation
    graph.add_node("validate_promo", validate_promo_node)
    graph.add_node("check_duplicate", check_duplicate_subscription_node)
    graph.add_node("create_subscription", create_subscription_tool)
    
    # Nodes for FETCH operation
    graph.add_node("fetch_subscriptions", fetch_user_subscriptions_tool)
    graph.add_node("filter_and_rank", filter_and_rank_subscriptions_node)
    
    graph.set_entry_point("validate_promo")
    
    # Conditional routing based on validation
    graph.add_conditional_edges(
        "validate_promo",
        lambda s: s["validation_status"],
        {
            "INVALID": END,
            "EXPIRED": END,
            "VALID": "check_duplicate"
        }
    )
    
    # Conditional routing based on duplicate check
    graph.add_conditional_edges(
        "check_duplicate",
        lambda s: s["duplicate_check"],
        {
            "DUPLICATE": END,
            "NEW": "create_subscription"
        }
    )
    
    graph.add_edge("create_subscription", END)
    
    return graph.compile()


subscription_graph = build_subscription_agent()


# ==================== REST Endpoints ====================

@app.get("/health")
async def health():
    return {"status": "ok", "service": "subscription-agent", "port": PORT}


@app.get("/catalogue", response_model=List[PromoEntry])
async def get_catalogue():
    """Get all promotions with active/inactive status"""
    now = datetime.datetime.now()
    entries = [
        PromoEntry(
            promo_code=code,
            is_subscribable=now < promo["expires_at"],
            **promo,
        )
        for code, promo in PROMO_CATALOGUE.items()
    ]
    entries.sort(key=lambda e: e.discount_percent, reverse=True)
    logger.info(f"Catalogue retrieved with {len(entries)} promos")
    return entries


@app.post("/subscriptions", response_model=BuySubscriptionResponse, status_code=201)
async def buy_subscription(req: BuySubscriptionRequest):
    """Subscribe user to a promo code using agent reasoning"""
    try:
        state = {
            "operation": "BUY",
            "user_id": req.user_id,
            "email": req.email,
            "promo_code": req.promo_code,
            "validation_status": None,
            "duplicate_check": None,
            "catalogue_lookup": None,
            "subscription_result": None,
            "user_subscriptions": None,
            "result": None,
            "error_message": None,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "total_llm_calls": 0
        }
        
        logger.info(f'Request for buy_subscription, user_id = {req.user_id}, promo_code = {req.promo_code}')
        print(f'Request for buy_subscription, user_id = {req.user_id}, promo_code = {req.promo_code}')
        
        out = await subscription_graph.ainvoke(state)
        
        if out["validation_status"] == "INVALID":
            logger.warning(f'Invalid promo code: {req.promo_code}')
            raise HTTPException(status_code=404, detail=out["error_message"])
        
        if out["validation_status"] == "EXPIRED":
            logger.warning(f'Expired promo code: {req.promo_code}')
            raise HTTPException(status_code=410, detail=out["error_message"])
        
        if out["duplicate_check"] == "DUPLICATE":
            logger.warning(f'Duplicate subscription for user {req.user_id}')
            raise HTTPException(status_code=409, detail=out["error_message"])
        
        result = out["subscription_result"]
        logger.info(f'Subscription created successfully for user {req.user_id}')
        
        return BuySubscriptionResponse(
            subscription=SubscriptionRecord(
                subscription_id=result["subscription_id"],
                user_id=result["user_id"],
                email=result["email"],
                promo_code=result["promo_code"],
                promo_title=result["promo_title"],
                promo_description=result["promo_description"],
                discount_percent=result["discount_percent"],
                subscribed_at=result["subscribed_at"],
                expires_at=result["expires_at"],
                is_active=result["is_active"]
            ),
            total_input_tokens=out["total_input_tokens"],
            total_output_tokens=out["total_output_tokens"],
            total_llm_calls=out["total_llm_calls"]
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f'Error in buy_subscription: {e}')
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/subscriptions/{user_id}", response_model=SubscriptionRecords)
async def get_subscriptions(user_id: str = Path(..., description="The user/customer ID to query")):
    """Fetch active subscriptions for a user using agent reasoning"""
    try:
        state = {
            "operation": "FETCH",
            "user_id": user_id,
            "email": None,
            "promo_code": None,
            "validation_status": None,
            "duplicate_check": None,
            "catalogue_lookup": None,
            "subscription_result": None,
            "user_subscriptions": None,
            "result": None,
            "error_message": None,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "total_llm_calls": 0
        }
        
        logger.info(f'Request for get_subscriptions, user_id = {user_id}')
        print(f'Request for get_subscriptions, user_id = {user_id}')
        
        # For FETCH, bypass validate_promo and go directly to fetch
        await fetch_user_subscriptions_tool(state)
        out = await filter_and_rank_subscriptions_node(state)
        
        result = out["result"]
        logger.info(f'Retrieved {result["total_active"]} active subscriptions for user {user_id}')
        
        subscriptions = [
            SubscriptionRecord(
                subscription_id=s["subscription_id"],
                user_id=user_id,
                email=None,
                promo_code=s["promo_code"],
                promo_title=s["promo_title"],
                promo_description=s["promo_description"],
                discount_percent=s["discount_percent"],
                subscribed_at=s["subscribed_at"],
                expires_at=s["expires_at"],
                is_active=True  # We already filtered active ones
            )
            for s in result["subscriptions"]
        ]
        
        return SubscriptionRecords(
            user_id=user_id,
            subscriptions=subscriptions,
            total_input_tokens=out["total_input_tokens"],
            total_output_tokens=out["total_output_tokens"],
            total_llm_calls=out["total_llm_calls"]
        )
    except Exception as e:
        logger.exception(f'Error in get_subscriptions: {e}')
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/clear_subscriptions")
async def clear_subscriptions():
    """Clear all subscriptions (for testing)"""
    await db.user_subscriptions.delete_many({})
    logger.info("All subscriptions cleared")
    return {"message": "All subscriptions cleared"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)

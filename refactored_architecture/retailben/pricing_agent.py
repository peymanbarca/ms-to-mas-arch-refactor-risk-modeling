"""
PRICING AGENT - Graph Topology

    START
      |
      v
[fetch_prices] (Fetch Product Prices from DB)
      |
      v
[reason_price] (LLM Reasoning Node - Calculate Total & Apply Promos)
      |
      v
      END

Key Features:
- Linear 2-step pricing calculation workflow
- Fetches base unit prices from MongoDB
- LLM-based price computation with promotion logic
- Supports promo codes: PROMO10 (10% off), BUYS2SAVE5 ($5 off if qty >= 2)
- Returns itemized pricing with subtotal, discounts, and final total
- Tracks token usage metrics
"""

import os
import logging
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from typing import TypedDict, List, Dict, Any, Optional
from motor.motor_asyncio import AsyncIOMotorClient
from httpx import AsyncClient
import json
from langchain_ollama import ChatOllama
from langgraph.graph import StateGraph, END
import asyncio


logger = logging.getLogger("pricing_agent")
logging.basicConfig(
    filename='./logs/pricing_agent.log',
    level=logging.INFO,  # Log all messages with severity DEBUG or higher
    format='%(asctime)s - %(levelname)s - %(message)s'  # Define the message format
)

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017/")
MONGO_DB = os.getenv("MONGO_DB", "retailben")
PORT = int(os.getenv("PORT", 8002))

llm = ChatOllama(model="llama3", temperature=0.0, reasoning=False)

app = FastAPI(title="Pricing & Promotion Agent")

# DB client will be set on startup
db_client: Optional[AsyncIOMotorClient] = None
db = None

class PriceItem(BaseModel):
    product_id: str
    price: float

class PriceRequestItem(BaseModel):
    product_id: str
    qty: int = Field(1, gt=0)

class PriceRequest(BaseModel):
    items: List[PriceRequestItem]
    promo_codes: Optional[List[str]] = None
    currency: Optional[str] = "USD"
    only_final_price: bool = False

class PriceResponseItem(BaseModel):
    product_id: str
    unit_price: float

class PriceResponse(BaseModel):
    items: List[PriceResponseItem] = []
    subtotal: Optional[float] = None
    total_discount: float
    total: float
    currency: Optional[str] = None
    total_input_tokens: int
    total_output_tokens: int
    total_llm_calls: int

class PricingState(TypedDict):
    request: Dict[str, Any]
    price_map: Dict[str, float]
    result: Dict[str, Any]
    total_input_tokens: int
    total_output_tokens: int
    total_llm_calls: int

@app.on_event("startup")
async def startup():
    global db_client, db
    db_client = AsyncIOMotorClient(MONGO_URI)
    db = db_client[MONGO_DB]
    # Ensure index
    await db.prices.create_index("product_id", unique=True)
    logger.info("Connected to MongoDB at %s db=%s", MONGO_URI, MONGO_DB)


@app.on_event("shutdown")
async def shutdown():
    global db_client
    if db_client:
        db_client.close()
        logger.info("MongoDB connection closed")

# Tool: Fetch Prices from MongoDB
def make_fetch_prices():
    async def fetch_prices(state: PricingState) -> PricingState:
        logger.info(f'Calling fetch_prices_tool ... \n Current State is {state}')
        print(f'Calling fetch_prices_tool ... \n Current State is {state}')

        items = state["request"]["items"]
        product_ids = [i["product_id"] for i in items]

        docs = await db.prices.find(
            {"product_id": {"$in": product_ids}}
        ).to_list(length=len(product_ids))

        price_map = {d["product_id"]: d["price"] for d in docs}

        missing = set(product_ids) - set(price_map.keys())
        if missing:
            raise ValueError(f"Missing prices for products: {missing}")

        state["price_map"] = price_map

        logger.info(f'Response state of fetch_prices_tool ==> {state}, \n-------------------------------------')
        print(f'Response state of fetch_prices_tool ==> {state}, \n-------------------------------------')
        return state

    return fetch_prices


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


async def pricing_reasoning(state: PricingState) -> PricingState:
    prompt = f"""
    You are a pricing agent in a retail supply chain.
    
    - Tasks:
    You MUST compute prices using the unit prices provided, and return response as JSON  (not python code).
    Apply promotions if applicable (Only apply if promo_codes in REQUEST is not empty):
        - PROMO10 → 10% off line total
        - BUYS2SAVE5 → $5 off if qty >= 2
    
    - Do not return middle steps and thinking procedure in response
    - Return just and ONLY valid JSON for final step in the following schema:
    
    {{
      "total_discount": number,
      "total": number
    }}
    
    Input:
    REQUEST = {json.dumps(state["request"])}
    PRICE_MAP = {json.dumps(state["price_map"])}
    
    """

    # LangChain Ollama is synchronous → offload
    logger.info(f'LLM Call Prompt: {prompt}')
    response = await asyncio.to_thread(llm.invoke, prompt)

    raw_response = response.text()
    input_tokens = response.usage_metadata.get("input_tokens")
    output_tokens = response.usage_metadata.get("output_tokens")
    total_tokens = response.usage_metadata.get("total_tokens")
    logger.info(f'LLM Raw response: {raw_response}')
    print(f'LLM Raw response: {raw_response}')

    logger.info(f'LLM Token Metrics: input_tokens: {input_tokens}, output_tokens: {output_tokens},'
                f' total_tokens: {total_tokens}')
    print(f'LLM Token Metrics: input_tokens: {input_tokens}, output_tokens: {output_tokens},'
                f' total_tokens: {total_tokens}')

    try:
        parsed = parse_json_response(raw_response)
    except Exception as e:
        logger.info(f'Invalid JSON from pricing agent: {raw_response}, {e}')
        print(f'Invalid JSON from pricing agent: {raw_response}, {e}')
        raise ValueError(f"Invalid JSON from pricing agent: {raw_response}") from e

    state["result"] = parsed
    state["total_input_tokens"] += input_tokens
    state["total_output_tokens"] += output_tokens
    state["total_llm_calls"] += 1
    return state


def build_pricing_graph():
    graph = StateGraph(PricingState)

    graph.add_node("fetch_prices", make_fetch_prices())
    graph.add_node("reason_price", pricing_reasoning)

    graph.set_entry_point("fetch_prices")
    graph.add_edge("fetch_prices", "reason_price")
    graph.add_edge("reason_price", END)

    return graph.compile()


pricing_graph = build_pricing_graph()


@app.post("/price", response_model=PriceResponse)
async def compute_price(req: PriceRequest):
    try:
        state = {
            "request": req.dict(),
            "price_map": {},
            "result": {},
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "total_llm_calls": 0
        }
        logger.info(f'Request for compute_price, req = {req}, state={state}')
        print(f'Request for compute_price, req = {req}, state={state}')
        out = await pricing_graph.ainvoke(state)
        logger.info(f'Request for compute_price processed successfully, req = {req}, result={out.get("result")}')
        print(f'Request for compute_price processed successfully, req = {req}, result={out.get("result")}')

        product_ids = list(out["price_map"].keys())
        product_unit_prices = list(out["price_map"].values())
        return PriceResponse(
            items= [PriceResponseItem(product_id=product_ids[i], unit_price=product_unit_prices[i])
                    for i in range(len(product_ids))],
            subtotal= out["result"].get("subtotal"),
            total_discount= out["result"].get("total_discount", 0.0),
            total= out["result"].get("total", 0.0),
            currency= req.currency,
            total_input_tokens= out["total_input_tokens"],
            total_output_tokens= out["total_output_tokens"],
            total_llm_calls= out["total_llm_calls"]
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/price/put")
async def put_price(item: PriceItem):
    """Admin endpoint: insert/update price"""
    await db.prices.update_one({"product_id": item.product_id}, {"$set": {"price": item.price}}, upsert=True)
    return {"ok": True, "product_id": item.product_id, "price": item.price}


@app.get("/price/{product_id}", response_model=PriceItem)
async def get_price(product_id: str):
    doc = await db.prices.find_one({"product_id": product_id})
    if not doc:
        raise HTTPException(status_code=404, detail="price not found")
    return PriceItem(product_id=doc["product_id"], price=doc["price"])

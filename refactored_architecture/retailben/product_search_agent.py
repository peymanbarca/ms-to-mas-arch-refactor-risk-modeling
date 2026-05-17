import os
import logging
import time
import uuid
import datetime
import httpx

from fastapi import FastAPI, Query, HTTPException
from pydantic import BaseModel, Field
from typing import TypedDict, List, Dict, Any, Optional
from motor.motor_asyncio import AsyncIOMotorClient
from httpx import AsyncClient
import json
from langchain_ollama import ChatOllama
from langgraph.graph import StateGraph, END
import asyncio



logger = logging.getLogger("product_search_agent")
logging.basicConfig(
    filename='./logs/product_search_agent.log',
    level=logging.INFO,  # Log all messages with severity DEBUG or higher
    format='%(asctime)s - %(levelname)s - %(message)s'  # Define the message format
)

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017/")
MONGO_DB = os.getenv("MONGO_DB", "ms_baseline")
PORT = int(os.getenv("PORT", 8008))
PRICING_SERVICE_URL = os.getenv("PRICING_SERVICE_URL", "http://localhost:8002")
INVENTORY_SERVICE_URL = os.getenv("INVENTORY_SERVICE_URL", "http://localhost:8001")

llm = ChatOllama(model="llama3", temperature=0.0, reasoning=False)

app = FastAPI(title="Product Search Agent")

# DB client will be set on startup
db_client: Optional[AsyncIOMotorClient] = None
db = None

class ProductOut(BaseModel):
    sku: str
    name: str
    description: str

class ProductSearchResultItem(ProductOut):
    price: float
    score: float

class ProductCreate(BaseModel):
    sku: str
    name: str
    description: str

class ProductSearchResponse(BaseModel):
    query: str
    results: List[ProductSearchResultItem]
    total_input_tokens: int
    total_output_tokens: int
    total_llm_calls: int


class ProductSearchAgentState(TypedDict):
    query: str
    candidates: List[Dict[str, Any]]
    stocks: Dict[str, int]
    prices: List[Dict[str, Any]]
    results: List[Dict[str, Any]]
    total_input_tokens: int
    total_output_tokens: int
    total_llm_calls: int


async def fetch_candidates_tool(state: ProductSearchAgentState) -> ProductSearchAgentState:
    q = state["query"]

    # Prefer text search
    cursor = db.products.find(
        {"$text": {"$search": q}},
        {"score": {"$meta": "textScore"}}
    ).sort([("score", {"$meta": "textScore"})]).limit(10)

    docs = await cursor.to_list(length=10)

    # Fallback to regex (still deterministic DB logic)
    if not docs:
        docs = await db.products.find(
            {"name": {"$regex": q, "$options": "i"}}
        ).limit(10).to_list(length=10)

    candidates = []
    for doc in docs:
        current_candidate_skus = [c["sku"] for c in candidates]
        if doc["sku"] not in current_candidate_skus:
            candidates.append(doc)
    state["candidates"] = candidates
    state["stocks"] = {}
    return state


async def fetch_stock_tool(state: ProductSearchAgentState) -> ProductSearchAgentState:
    """Fetch stock levels from inventory service and filter candidates with stock > 0"""
    product_ids = [d["sku"] for d in state["candidates"]]
    
    if not product_ids:
        state["stocks"] = {}
        return state
    
    try:
        skus_query = ",".join(product_ids)
        logger.info(f"Calling inventory service for stock check, skus: {product_ids}")
        
        async with httpx.AsyncClient() as client:
            r = await client.get(f"{INVENTORY_SERVICE_URL}/stock?skus={skus_query}", timeout=10)
            r.raise_for_status()
            stock_data = r.json()
        
        stock_map = stock_data.get("stocks", {})
        logger.info(f"Inventory service response: {stock_data}")
        
        # Filter candidates to only those with stock > 0
        filtered_candidates = [
            doc for doc in state["candidates"] 
            if stock_map.get(doc["sku"], 0) > 0
        ]
        
        state["candidates"] = filtered_candidates
        state["stocks"] = stock_map
        
        logger.info(f"Filtered {len(state['candidates'])} products with stock > 0")
        
    except Exception as e:
        logger.exception(f"Error fetching stock from inventory service: {e}")
        # Fallback: keep all candidates if inventory service fails
        state["stocks"] = {sku: 1 for sku in product_ids}
    
    return state


async def fetch_prices_tool(state: ProductSearchAgentState) -> ProductSearchAgentState:
    product_ids = [d["sku"] for d in state["candidates"]]

    prices = {}
    if not product_ids:
        state["prices"] = prices
        return state

    payload = {
        "items": [{"product_id": pid, "qty": 1} for pid in product_ids],
        "promo_codes": []
    }

    async with httpx.AsyncClient() as client:
        r = await client.post(f"{PRICING_SERVICE_URL}/price", json=payload, timeout=10)
        r.raise_for_status()
        jr = r.json()

    for it in jr.get("items", []):
        prices[it["product_id"]] = it["unit_price"]

    state["prices"] = prices

    state["total_input_tokens"] += jr["total_input_tokens"]
    state["total_output_tokens"] += jr["total_output_tokens"]
    state["total_llm_calls"] += jr["total_llm_calls"]

    return state

@app.on_event("startup")
async def startup():
    global db_client, db
    db_client = AsyncIOMotorClient(MONGO_URI)
    db = db_client[MONGO_DB]
    logger.info("Connected to MongoDB at %s db=%s", MONGO_URI, MONGO_DB)


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


async def filter_and_rank_reasoning_node(state: ProductSearchAgentState) -> ProductSearchAgentState:
    products = [
        {
            "sku": d["sku"],
            "name": d["name"],
            "description": d.get("description", ""),
            "score": d.get("score", 1.0)
        }
        for d in state["candidates"]
    ]

    prices = state["prices"]

    prompt = f"""
    You are a product search ranking agent.
    
    Task:
    - Fill the final result by matching each product and price by sku from the Prices and Products input  
    - Return final result ONLY valid JSON with below schema without intermediate thinking responses:
    
    Schema:
    {{
        "results": [
          {{
            "sku": string,
            "price": number
          }}
        ]
    }}

    
    Prices:
    {prices}
    
    Products:
    {json.dumps(products, indent=2)}
    """

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
        results = parse_json_response(raw_response)
        assert isinstance(results["results"], list)
    except Exception as e:
        raise ValueError(f"Invalid result output: {raw_response}") from e

    state["results"] = results["results"]
    state["total_input_tokens"] += input_tokens
    state["total_output_tokens"] += output_tokens
    state["total_llm_calls"] += 1
    return state


def assemble_response_node(state: ProductSearchAgentState) -> ProductSearchAgentState:
    fetched_products = [
        {
            "sku": d["sku"],
            "name": d["name"],
            "description": d.get("description", ""),
            "score": d.get("score", 1.0)
        }
        for d in state["candidates"]
    ]
    rank_and_filter_results = state["results"]

    final_results = []
    for p in fetched_products:
        sku = p["sku"]
        if sku not in [res["sku"] for res in rank_and_filter_results]: # it means LLM filtered out this product, so we skip it in final assembly
            continue
        final_results.append({
            "sku": sku,
            "name": p["name"],
            "description": p.get("description", ""),
            "price": rank_and_filter_results[[res["sku"] for res in rank_and_filter_results].index(sku)]["price"],
            "score": float(p["score"])
        })

    state["results"] = final_results
    return state

def build_product_search_agent():
    graph = StateGraph(ProductSearchAgentState)

    graph.add_node("fetch_candidates", fetch_candidates_tool)
    graph.add_node("fetch_stock", fetch_stock_tool)
    graph.add_node("fetch_prices", fetch_prices_tool)
    graph.add_node("filter_and_rank_reason", filter_and_rank_reasoning_node)
    graph.add_node("assemble", assemble_response_node)

    graph.set_entry_point("fetch_candidates")
    graph.add_edge("fetch_candidates", "fetch_stock")
    graph.add_edge("fetch_stock", "fetch_prices")
    graph.add_edge("fetch_prices", "filter_and_rank_reason")
    graph.add_edge("filter_and_rank_reason", "assemble")
    graph.add_edge("assemble", END)

    return graph.compile()


search_graph = build_product_search_agent()


@app.post("/products")
async def create_product(p: ProductCreate):
    await db.products.insert_one(p.dict())
    return {"status": "created", "sku": p.sku}

@app.get("/search", response_model=ProductSearchResponse)
async def search_products(q: str = Query(...), limit: int = 5):
    state = {
        "query": q,
        "candidates": [],
        "stocks": {},
        "prices": {},
        "results": [],
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "total_llm_calls": 0
    }

    try:
        out = await search_graph.ainvoke(state)
        print(f'------------\n {out}')
        return ProductSearchResponse(
            query=q,
            results=out["results"][:limit],
            total_llm_calls=out["total_llm_calls"],
            total_input_tokens=out["total_input_tokens"],
            total_output_tokens=out["total_output_tokens"]
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

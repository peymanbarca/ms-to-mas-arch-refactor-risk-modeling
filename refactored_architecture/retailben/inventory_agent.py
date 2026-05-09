import os
import logging
import time
import uuid
import datetime
import httpx

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from typing import TypedDict, List, Dict, Any, Optional
from motor.motor_asyncio import AsyncIOMotorClient
from httpx import AsyncClient
from pymongo import ReturnDocument
import json
from langgraph.graph import StateGraph, END
from langchain_ollama import ChatOllama
import asyncio
import threading


logger = logging.getLogger("inventory_agent")
logging.basicConfig(
    filename='./logs/inventory_agent.log',
    level=logging.INFO,  # Log all messages with severity DEBUG or higher
    format='%(asctime)s - %(levelname)s - %(message)s'  # Define the message format
)

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017/")
MONGO_DB = os.getenv("MONGO_DB", "ms_baseline")
PORT = int(os.getenv("PORT", 8001))

llm = ChatOllama(model="llama3", temperature=0.0, reasoning=False)

app = FastAPI(title="Inventory Agent")

# DB client will be set on startup
db_client: Optional[AsyncIOMotorClient] = None
db = None

lock = threading.Lock()


class CartItem(BaseModel):
    sku: str
    qty: int = Field(1, gt=0)


class ReservationReq(BaseModel):
    order_id: str
    items: List[CartItem] = []
    atomic_update: bool = False
    delay: float = 0.0
    drop: int = 0


class InventoryAgentState(TypedDict):
    order_id: str
    items: List[Dict[str, int]]
    current_stock: Optional[int]
    qty: Optional[int]
    atomic: bool
    action: str
    result: Optional[Dict[str, Any]]
    total_input_tokens: int
    total_output_tokens: int
    total_llm_calls: int


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


async def fetch_stock_tool(state: InventoryAgentState) -> InventoryAgentState:
    logger.info(f'Calling fetch_stock_tool ... \n Current State is {state}')
    print(f'Calling fetch_stock_tool ... \n Current State is {state}')

    if state["action"] == 'ROLLBACK':
        return state

    # currently working for single item, can be extended to support multiple items in the future
    for item in state["items"]:
        doc = await db.inventory.find_one({"sku": item["sku"]})
        state["current_stock"] = doc["stock"] if doc else 0
        state["qty"] = item["qty"]

    state["action"] = "TRY_RESERVE"

    logger.info(f'Response state of fetch_stock_tool tool ==> {state}, \n-------------------------------------')
    print(f'Response state of fetch_stock_tool tool ==> {state}, \n-------------------------------------')
    return state



async def apply_reservation_tool(state: InventoryAgentState) -> InventoryAgentState:
    logger.info(f'Calling apply_reservation_tool ... \n Current State is {state}')
    print(f'Calling apply_reservation_tool ... \n Current State is {state}')
    results = []

    if state["atomic"]:
        with lock:
            for item in state["items"]:
                res = await db.inventory.find_one_and_update(
                    {"sku": item["sku"]},
                    {"$inc": {"stock": -item["qty"]}},
                    return_document=ReturnDocument.AFTER
                )
                results.append({"sku": item["sku"], "remaining": res["stock"]})
    else:
        for item in state["items"]:
            doc = await db.inventory.find_one({"sku": item["sku"]})
            new_stock = doc["stock"] - item["qty"]
            await db.inventory.update_one(
                {"sku": item["sku"]},
                {"$set": {"stock": new_stock}}
            )
            results.append({"sku": item["sku"], "remaining": new_stock})

    state["result"] = {
        "order_id": state["order_id"],
        "status": "RESERVED",
        "items": results
    }
    logger.info(f'Response state of apply_reservation_tool ==> {state["result"]}, \n-----------------------------')
    print(f'Response state of apply_reservation_tool ==> {state["result"]}, \n---------------------------------')

    return state


async def rollback_reservation_tool(state: InventoryAgentState) -> InventoryAgentState:
    logger.info(f'Calling rollback_reservation_tool ... \n Current State is {state}')
    print(f'Calling rollback_reservation_tool ... \n Current State is {state}')
    results = []

    if state["atomic"]:
        with lock:
            for item in state["items"]:
                res = await db.inventory.find_one_and_update(
                    {"sku": item["sku"]},
                    {"$inc": {"stock": item["qty"]}},
                    return_document=ReturnDocument.AFTER
                )
                results.append({"sku": item["sku"], "remaining": res["stock"]})
    else:
        for item in state["items"]:
            doc = await db.inventory.find_one({"sku": item["sku"]})
            new_stock = doc["stock"] + item["qty"]
            await db.inventory.update_one(
                {"sku": item["sku"]},
                {"$set": {"stock": new_stock}}
            )
            results.append({"sku": item["sku"], "remaining": new_stock})

    state["result"] = {
        "order_id": state["order_id"],
        "status": "RESERVED_ROLLBACK",
        "items": results
    }
    logger.info(f'Response state of rollback_reservation_tool ==> {state["result"]}, \n-----------------------------')
    print(f'Response state of rollback_reservation_tool ==> {state["result"]}, \n---------------------------------')
    return state


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


async def reasoning_action_node(state: InventoryAgentState) -> InventoryAgentState:
    prompt = f"""
    You are an inventory workflow manager agent.
    
    Task: 
    - If Action input is None or null, respond:
        {{"decision": "FETCH_STOCK"}}
    
    - Else if Action input is ROLLBACK, respond:
        {{"decision": "ROLLBACK_RESERVE"}}

    Input:
    Action: {state["action"]}
    
    Return ONLY valid JSON without intermediate thinking responses.
    """

    logger.info(f'LLM Call Prompt: {prompt}')
    response = await asyncio.to_thread(llm.invoke, prompt)

    raw_response = response.text()
    input_tokens = response.usage_metadata.get("input_tokens")
    output_tokens = response.usage_metadata.get("output_tokens")
    total_tokens = response.usage_metadata.get("total_tokens")
    reasoning_text = response.additional_kwargs.get("reasoning_content", None)
    reasoning_tokens = response.usage_metadata.get("output_token_details", {}).get("reasoning", 0)

    print(f'LLM Reasoning Text: {reasoning_text}')
    logger.info(f'LLM Raw response: {raw_response}')
    print(f'LLM Raw response: {raw_response}')

    logger.info(f'LLM Token Metrics: input_tokens: {input_tokens}, output_tokens: {output_tokens},'
                f' reasoning_tokens: {reasoning_tokens}, total_tokens: {total_tokens}')
    print(f'LLM Token Metrics: input_tokens: {input_tokens}, output_tokens: {output_tokens},'
                f' reasoning_tokens: {reasoning_tokens}, total_tokens: {total_tokens}')

    decision = parse_json_response(raw_response).get("decision", "OUT_OF_STOCK")

    state["action"] = decision
    state["total_input_tokens"] += input_tokens
    state["total_output_tokens"] += output_tokens
    state["total_llm_calls"] += 1
    return state


async def reasoning_reserve_node(state: InventoryAgentState) -> InventoryAgentState:
    prompt = f"""
    You are an inventory reservation agent.

    Task:    
        - If CURRENT_STOCK > QTY , respond {{"decision": "APPLY_RESERVE"}}
        - Else If CURRENT_STOCK == QTY , respond {{"decision": "APPLY_RESERVE"}}
        - Else respond {{"decision": "OUT_OF_STOCK"}}

    Input:
    CURRENT_STOCK: {state["current_stock"]}
    QTY: {state["qty"]}

    Return ONLY valid JSON without intermediate thinking responses.
    """

    logger.info(f'LLM Call Prompt: {prompt}')
    response = await asyncio.to_thread(llm.invoke, prompt)

    raw_response = response.text()
    input_tokens = response.usage_metadata.get("input_tokens")
    output_tokens = response.usage_metadata.get("output_tokens")
    total_tokens = response.usage_metadata.get("total_tokens")
    reasoning_text = response.additional_kwargs.get("reasoning_content", None)
    reasoning_tokens = response.usage_metadata.get("output_token_details", {}).get("reasoning", 0)

    print(f'LLM Reasoning Text: {reasoning_text}')
    logger.info(f'LLM Raw response: {raw_response}')
    print(f'LLM Raw response: {raw_response}')

    logger.info(f'LLM Token Metrics: input_tokens: {input_tokens}, output_tokens: {output_tokens},'
                f' reasoning_tokens: {reasoning_tokens}, total_tokens: {total_tokens}')
    print(f'LLM Token Metrics: input_tokens: {input_tokens}, output_tokens: {output_tokens},'
          f' reasoning_tokens: {reasoning_tokens}, total_tokens: {total_tokens}')

    decision = parse_json_response(raw_response).get("decision", "OUT_OF_STOCK")

    state["action"] = decision
    state["total_input_tokens"] += input_tokens
    state["total_output_tokens"] += output_tokens
    state["total_llm_calls"] += 1
    return state


def build_inventory_agent():
    g = StateGraph(InventoryAgentState)

    g.add_node("reason_action", reasoning_action_node)
    g.add_node("reason_reserve", reasoning_reserve_node)
    g.add_node("fetch", fetch_stock_tool)
    g.add_node("apply", apply_reservation_tool)
    g.add_node("rollback", rollback_reservation_tool)

    g.set_entry_point("reason_action")

    g.add_conditional_edges(
        "reason_action",
        lambda s: s["action"],
        {
            "FETCH_STOCK": "fetch",
            "ROLLBACK_RESERVE": "rollback"
        }
    )

    g.add_edge("fetch", "reason_reserve")
    g.add_conditional_edges(
        "reason_reserve",
        lambda s: s["action"],
        {
            "APPLY_RESERVE": "apply",
            "OUT_OF_STOCK": END
        }
    )

    g.add_edge("apply", END)
    g.add_edge("rollback", END)

    return g.compile()


inventory_graph = build_inventory_agent()


@app.post("/reset_stocks")
async def reset_stocks(request: dict):
    """

    :param request:
        {
          "items": [
            {
              "sku": "4cc0770f-91bc-4c0d-a26f-7b872f02ca94",
              "stock": 10
            }
          ]
        }
    :return:
    """
    await db.inventory.delete_many({})
    items: List[CartItem] = request["items"]
    for item in items:
        await db.inventory.insert_one({"sku": item['sku'], "stock": item['stock']})


@app.post("/reserve")
async def reserve_stock(req: ReservationReq):
    if not req.items:
        raise HTTPException(status_code=400, detail="empty_cart_items")

    state = {
        "order_id": req.order_id,
        "items": [it.dict() for it in req.items],
        "atomic": req.atomic_update,
        "current_stock": None,
        "qty": None,
        "action": None,
        "result": None,
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "total_llm_calls": 0
    }

    logger.info(f'Request for reserve_stock, req = {req}, state={state}')
    print(f'Request for reserve_stock, req = {req}, state={state}')

    out = await inventory_graph.ainvoke(state)

    if out.get('result') is None:
        out["result"] = {
            "order_id": state["order_id"],
            "status": "OUT_OF_STOCK",
            "items": state["items"]
        }

    logger.info(f'Request for reserve_stock processed successfully, req = {req}, result={out.get("result")}')
    print(f'Request for reserve_stock processed successfully, req = {req}, result={out.get("result")}')
    result = out.get("result")
    result["total_input_tokens"] = out.get("total_input_tokens")
    result["total_output_tokens"] = out.get("total_output_tokens")
    result["total_llm_calls"] = out.get("total_llm_calls")
    return result


@app.post("/reserve-rollback")
async def rollback_stock(req: ReservationReq):
    state = {
        "order_id": req.order_id,
        "items": [it.dict() for it in req.items],
        "current_stock": None,
        "qty": None,
        "atomic": req.atomic_update,
        "action": 'ROLLBACK',
        "result": None
    }
    out = await inventory_graph.ainvoke(state)
    result = out.get("result")
    result["total_input_tokens"] = out.get("total_input_tokens")
    result["total_output_tokens"] = out.get("total_output_tokens")
    result["total_llm_calls"] = out.get("total_llm_calls")
    return result

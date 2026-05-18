"""
SHIPMENT AGENT - Graph Topology

    START
      |
      v
[carrier_call] (External Carrier/Logistics API Call)
      |
      v
[reason_shipment] (LLM Reasoning Node - Validate Booking & Confirm)
      |
      v
      END

Key Features:
- Linear 2-step shipment booking workflow
- Integrates with external logistics/carrier systems
- LLM-based booking validation and confirmation
- Returns shipment ID and tracking ID
- Persists shipment records to MongoDB
- Tracks token usage and LLM call metrics
"""

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
import json
from langchain_ollama import ChatOllama
from langgraph.graph import StateGraph, END
import asyncio


logger = logging.getLogger("shipment_agent")
logging.basicConfig(
    filename='./logs/shipment_agent.log',
    level=logging.INFO,  # Log all messages with severity DEBUG or higher
    format='%(asctime)s - %(levelname)s - %(message)s'  # Define the message format
)

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017/")
MONGO_DB = os.getenv("MONGO_DB", "retailben")
PORT = int(os.getenv("PORT", 8006))

llm = ChatOllama(model="llama3", temperature=0.0, reasoning=False)

app = FastAPI(title="Shipment Booking Agent")

# DB client will be set on startup
db_client: Optional[AsyncIOMotorClient] = None
db = None

class ShipmentRequest(BaseModel):
    order_id: str
    address: str


class ShipmentResponse(BaseModel):
    shipment_id: str
    tracking_id: str
    total_input_tokens: int
    total_output_tokens: int
    total_llm_calls: int


class ShipmentState(TypedDict):
    request: Dict[str, Any]
    carrier_result: Dict[str, Any]
    result: Dict[str, Any]
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


# Tool: call external carrier
async def carrier_booking_tool(state: ShipmentState) -> ShipmentState:
    logger.info(f'Calling carrier_booking_tool ... \n Current State is {state}')
    print(f'Calling carrier_booking_tool ... \n Current State is {state}')

    # Simulate carrier API latency
    time.sleep(0.2)

    tracking_id = str(uuid.uuid4())
    state["carrier_result"] = {
        "tracking_id": tracking_id,
        "carrier": "MockCarrier"
    }
    logger.info(f'Response state of carrier_booking_tool ==> {state}, \n-------------------------------------')
    print(f'Response state of carrier_booking_tool ==> {state}, \n-------------------------------------')
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


async def shipment_reasoning(state: ShipmentState) -> ShipmentState:
    prompt = f"""
    You are a shipment booking agent in a retail supply chain.

    Your task:
    - Confirm existence of tracking_id in CARRIER_RESULT input
    - Return ONLY a JSON response not python code

    Rules:
    - tracking_id must come from CARRIER_RESULT input
    - if both tracking_id exist, success in response should be true, otherwise it should be false.
    
    - Do not return middle steps and thinking procedure in response
    - Return just and ONLY valid JSON for final step in the following schema:

    Schema:
    {{
      "success" bool
    }}

    Input:
    REQUEST = {json.dumps(state["request"])}
    CARRIER_RESULT = {json.dumps(state["carrier_result"])}

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
        parsed = parse_json_response(raw_response)
    except Exception as e:
        logger.info(f'Invalid JSON from shipment agent: {raw_response}, {e}')
        print(f'Invalid JSON from shipment agent: {raw_response}, {e}')
        raise ValueError(f"Invalid JSON from shipment agent: {raw_response}") from e

    state["result"] = parsed
    if parsed["success"]:
        state["result"]["shipment_id"] = str(uuid.uuid4())
    state["result"]["tracking_id"] = state["carrier_result"]["tracking_id"]
    state["total_input_tokens"] += input_tokens
    state["total_output_tokens"] += output_tokens
    state["total_llm_calls"] += 1
    return state


def build_shipment_graph():
    graph = StateGraph(ShipmentState)

    graph.add_node("carrier_call", carrier_booking_tool)
    graph.add_node("reason_shipment", shipment_reasoning)

    graph.set_entry_point("carrier_call")
    graph.add_edge("carrier_call", "reason_shipment")
    graph.add_edge("reason_shipment", END)

    return graph.compile()


shipment_graph = build_shipment_graph()


@app.post("/book", response_model=ShipmentResponse)
async def book_shipment(req: ShipmentRequest):
    try:
        state = {
            "request": req.dict(),
            "carrier_result": {},
            "result": {},
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "total_llm_calls": 0
        }
        logger.info(f'Request for book_shipment, req = {req}, state={state}')
        print(f'Request for book_shipment, req = {req}, state={state}')

        out = await shipment_graph.ainvoke(state)
        logger.info(f'Request for process_payment processed successfully, req = {req}, result={out.get("result")}')
        print(f'Request for process_payment processed successfully, req = {req}, result={out.get("result")}')

        success = out["result"]["success"]
        if success is None or success is not True:
            raise HTTPException(status_code=500, detail='Carrier unavailable')

        shipment_id = out["result"]["tracking_id"]
        tracking_id = out["result"]["shipment_id"]

        doc = {
            "shipment_id": shipment_id,
            "order_id": req.order_id,
            "address": req.address,
            "tracking_id": tracking_id,
            "created_at": datetime.datetime.utcnow()
        }

        await db.shipments.insert_one(doc)
        result = {
            "shipment_id": shipment_id,
            "tracking_id": tracking_id,
            "total_input_tokens": out["total_input_tokens"],
            "total_output_tokens": out["total_output_tokens"],
            "total_llm_calls": out["total_llm_calls"]
        }
        return ShipmentResponse(**result)

    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/clear_bookings")
async def clear_bookings():
    await db.shipments.delete_many({})

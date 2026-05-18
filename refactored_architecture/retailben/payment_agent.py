"""
PAYMENT AGENT - Graph Topology

    START
      |
      v
  [psp_call] (External PSP Service Call)
      |
      v
[decide_payment] (LLM Decision Node - Validate & Authorize)
      |
      v
[persist_payment] (Save Payment Result to DB)
      |
      v
      END

Key Features:
- Linear 3-step payment processing workflow
- Integrates with external Payment Service Provider (PSP)
- LLM-based payment decision using PSP tracking ID validation
- Persists payment records to MongoDB
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
from typing import TypedDict, List, Dict, Any, Optional, Literal
from motor.motor_asyncio import AsyncIOMotorClient
from httpx import AsyncClient
import json
# from langchain_community.llms import Ollama
from langgraph.graph import StateGraph, END
from langchain_ollama import ChatOllama
import asyncio


logger = logging.getLogger("payment_agent")
logging.basicConfig(
    filename='./logs/payment_agent.log',
    level=logging.INFO,  # Log all messages with severity DEBUG or higher
    format='%(asctime)s - %(levelname)s - %(message)s'  # Define the message format
)

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017/")
MONGO_DB = os.getenv("MONGO_DB", "retailben")
PORT = int(os.getenv("PORT", 8007))

llm = ChatOllama(model="llama3", temperature=0.0, reasoning=False)

app = FastAPI(title="Payment Agent")

# DB client will be set on startup
db_client: Optional[AsyncIOMotorClient] = None
db = None

class PaymentRequest(BaseModel):
    order_id: str
    final_price: float


class PaymentResponse(BaseModel):
    order_id: str
    status: Literal["SUCCESS", "FAILED"]
    total_input_tokens: int
    total_output_tokens: int
    total_llm_calls: int


class PaymentAgentState(TypedDict):
    order_id: str
    psp_tracking_id: Optional[str]
    final_price: float
    decision: Dict[str, Any]
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


# Tool: call external PSP
async def call_external_psp_tool(state: PaymentAgentState) -> PaymentAgentState:
    logger.info(f'Calling call_external_psp_tool ... \n Current State is {state}')
    print(f'Calling call_external_psp_tool ... \n Current State is {state}')

    # Simulate carrier API latency
    time.sleep(0.3)

    psp_tracking_id = str(uuid.uuid4())
    state["psp_tracking_id"] = psp_tracking_id
    logger.info(f'Response state of call_external_psp_tool ==> {state}, \n-------------------------------------')
    print(f'Response state of call_external_psp_tool ==> {state}, \n-------------------------------------')
    return state


# Tool: Persist Payment Result (Deterministic)
async def persist_payment_tool(state: PaymentAgentState) -> PaymentAgentState:
    logger.info(f'Calling persist_payment_tool ... \n Current State is {state}')
    print(f'Calling persist_payment_tool ... \n Current State is {state}')
    doc = {
        "order_id": state["order_id"],
        "final_price": state["final_price"],
        "status": state["decision"]["status"],
        "psp_tracking_id": state["psp_tracking_id"],
        "created_at": datetime.datetime.now()
    }
    await db.payments.insert_one(doc)
    logger.info('Called successfully persist_payment_tool')
    print('Called successfully persist_payment_tool')
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


async def payment_reasoning_node(state: PaymentAgentState) -> PaymentAgentState:
    prompt = f"""
    You are a payment authorization agent.
    
    You must decide whether a payment succeeds or fails.
    
    Rules:
    - Status must be either "SUCCESS" or "FAILED"
    - if PSP_TRACKING_ID input is not null the Status should be SUCCESS, otherwise it should be FAILED
    - Return ONLY a JSON response not python code

    - Do not return middle steps and thinking procedure in response    
    - Output MUST be only a valid JSON in the bellow schema:
        
    Schema:
    {{
      "status": "SUCCESS" | "FAILED"
    }}
    
    Input:
    PSP_TRACKING_ID = {state["psp_tracking_id"]}
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
        decision = parse_json_response(raw_response)
        assert decision["status"] in ["SUCCESS", "FAILED"]
    except Exception as e:
        logger.info(f'Invalid payment decision: {raw_response}, {e}')
        print(f'Invalid payment decision: {raw_response}, {e}')
        raise ValueError(f"Invalid payment decision: {raw_response}") from e

    state["decision"] = decision
    state["total_input_tokens"] += input_tokens
    state["total_output_tokens"] += output_tokens
    state["total_llm_calls"] += 1
    return state


def build_payment_agent():
    graph = StateGraph(PaymentAgentState)

    graph.add_node("psp_call", call_external_psp_tool)
    graph.add_node("decide_payment", payment_reasoning_node)
    graph.add_node("persist_payment", persist_payment_tool)

    graph.set_entry_point("psp_call")
    graph.add_edge("psp_call", "decide_payment")
    graph.add_edge("decide_payment", "persist_payment")
    graph.add_edge("persist_payment", END)

    return graph.compile()


payment_graph = build_payment_agent()


@app.post("/pay-order", response_model=PaymentResponse, summary="Process payment for an order")
async def process_payment(req: PaymentRequest):
    try:
        state = {
            "order_id": req.order_id,
            "final_price": req.final_price,
            "decision": {},
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "total_llm_calls": 0
        }
        logger.info(f'Request for process_payment, req = {req}, state={state}')
        print(f'Request for process_payment, req = {req}, state={state}')

        out = await payment_graph.ainvoke(state)
        logger.info(f'Request for process_payment processed successfully, req = {req}, result={out.get("decision")}')
        print(f'Request for process_payment processed successfully, req = {req}, result={out.get("decision")}')

        return PaymentResponse(
            order_id=req.order_id,
            status=out["decision"]["status"],
            total_input_tokens=out["total_input_tokens"],
            total_output_tokens=out["total_output_tokens"],
            total_llm_calls=out["total_llm_calls"]
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/clear_payments")
async def clear_payments():
    await db.payments.delete_many({})

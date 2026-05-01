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


logger = logging.getLogger("procurement_agent")
logging.basicConfig(level=logging.INFO)

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017/")
MONGO_DB = os.getenv("MONGO_DB", "ms_baseline")
PORT = int(os.getenv("PORT", 8009))

llm = ChatOllama(model="llama3", temperature=0.0, reasoning=False)

app = FastAPI(title="Procurement Agent")

# DB client will be set on startup
db_client: Optional[AsyncIOMotorClient] = None
db = None

class SupplierOrderRequest(BaseModel):
    sku: str
    qty: int = Field(..., gt=0)
    preferred_supplier: Optional[str] = None

class SupplierOrderResponse(BaseModel):
    supplier_order_id: str
    status: str
    eta_days: Optional[int]


class ProcurementState(TypedDict):
    request: Dict[str, Any]
    supplier_result: Dict[str, Any]
    result: Dict[str, Any]


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


# Tool: call external supplier
async def supplier_order_tool(state: ProcurementState) -> ProcurementState:
    req = state["request"]

    # simulate external supplier latency
    time.sleep(0.2)

    supplier = req.get("preferred_supplier") or "DefaultSupplier"

    state["supplier_result"] = {
        "supplier": supplier,
        "supplier_order_id": str(uuid.uuid4()),
        "status": "PLACED",
        "eta_days": 2
    }
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


async def procurement_reasoning(state: ProcurementState) -> ProcurementState:
    prompt = f"""
    You are a procurement agent in a retail supply chain.
    
    Your task:
    - Finalize a supplier order
    - Validate supplier response
    - Output ONLY valid JSON in the schema below
    
    Schema:
    {{
      "supplier_order_id": string,
      "status": string,
      "eta_days": number
    }}
    
    Input:
    REQUEST = {json.dumps(state["request"])}
    SUPPLIER_RESULT = {json.dumps(state["supplier_result"])}
    
    Rules:
    - supplier_order_id must come from SUPPLIER_RESULT
    - status must be PLACED or FAILED
    - eta_days must be an integer when status is PLACED
    """

    # LangChain Ollama is synchronous → offload
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
        raise ValueError(f"Invalid JSON from procurement agent: {raw_response}") from e

    state["result"] = parsed
    return state


def build_procurement_graph():
    graph = StateGraph(ProcurementState)

    graph.add_node("call_supplier", supplier_order_tool)
    graph.add_node("reason_procurement", procurement_reasoning)

    graph.set_entry_point("call_supplier")
    graph.add_edge("call_supplier", "reason_procurement")
    graph.add_edge("reason_procurement", END)

    return graph.compile()


procurement_graph = build_procurement_graph()


@app.post("/order_supplier", response_model=SupplierOrderResponse)
async def order_from_supplier(req: SupplierOrderRequest):
    try:
        state = {
            "request": req.dict(),
            "supplier_result": {},
            "result": {}
        }

        out = await procurement_graph.ainvoke(state)

        doc = {
            "supplier_order_id": out["result"]["supplier_order_id"],
            "sku": req.sku,
            "qty": req.qty,
            "status": out["result"]["status"],
            "eta_days": out["result"].get("eta_days"),
            "created_at": datetime.datetime.utcnow()
        }

        await db.proc_orders.insert_one(doc)

        return SupplierOrderResponse(**out["result"])

    except Exception as e:
        # failed procurement is persisted for auditability
        failed_id = str(uuid.uuid4())
        await db.proc_orders.insert_one({
            "supplier_order_id": failed_id,
            "sku": req.sku,
            "qty": req.qty,
            "status": "FAILED",
            "created_at": datetime.datetime.utcnow()
        })
        raise HTTPException(status_code=502, detail=str(e))

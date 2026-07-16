"""
ORDER AGENT - ReACT - Graph Topology

    START
      |
      v
    [reason] (LLM Decision Node - Orchestrator)
      |
      +─ FETCH_CART ─────────> [fetch_cart] ─────┐
      |                                           |
      +─ PRICE_CART ─────────> [price] ───────┐  |
      |                                       |  |
      +─ RESERVE_INVENTORY ──> [reserve] ──┐ |  |
      |                                    | |  |
      +─ PROCESS_PAYMENT ───> [pay] ─────┐| |  |
      |                                  || |  |
      +─ ROLLBACK_INVENTORY ──> [rollback]| |  |
      |                                  |  |  |
      +─ BOOK_SHIPMENT ───────> [ship] ──┐| |  |
      |                                  || |  |
      +─ FINISH ───────────────> END     || |  |
                                         || |  |
          All action nodes loop back ───┴┴┴┘  |
                                         |
                                         v
                                      [reason]

Key Features:
- Multi-step order orchestration workflow
- LLM-driven decision making at each step
- Looping architecture: each action feeds back to reason node
- Coordinates: cart fetching, pricing, inventory reservation, payment, rollback, and shipment
- State management across multiple agent interactions

Current workflow state
        ↓
Current system state
        ↓
LLM Reasoning
        ↓
Choose next unfinished stage
        ↓
Execute tool
        ↓
Update workflow state
        ↓
Repeat

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
from langchain.tools import tool
from langgraph.graph import StateGraph, END
import asyncio
import requests


logger = logging.getLogger("order_agent")
logging.basicConfig(
    filename='./logs/order_agent.log',
    level=logging.INFO,  # Log all messages with severity DEBUG or higher
    format='%(asctime)s - %(levelname)s - %(message)s'  # Define the message format
)

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017/")
MONGO_DB = os.getenv("MONGO_DB", "retailben")
PORT = int(os.getenv("PORT", 8000))

INVENTORY_SERVICE_RESERVE_URL = "http://127.0.0.1:8001/reserve"
INVENTORY_SERVICE_RESERVE_ROLLBACK_URL = "http://127.0.0.1:8001/reserve-rollback"
CART_SERVICE_URL = "http://127.0.0.1:8003/cart/"
PRICING_SERVICE_URL = "http://127.0.0.1:8002"
PAYMENT_SERVICE_URL = "http://127.0.0.1:8007/pay-order"
SHIPMENT_SERVICE_URL = "http://127.0.0.1:8006/book"

llm = ChatOllama(model="llama3.2:3b", temperature=0.0, reasoning=False)

app = FastAPI(title="Order Agent")

# DB client will be set on startup
db_client: Optional[AsyncIOMotorClient] = None
db = None


class CartItem(BaseModel):
    sku: str
    qty: int = Field(1, gt=0)

class Cart(BaseModel):
    cart_id: str
    items: List[CartItem] = []

class PriceResponseItem(BaseModel):
    product_id: str
    qty: int
    unit_price: float
    line_total: float
    discounts: float

class PriceResponse(BaseModel):
    items: List[PriceResponseItem]
    subtotal: float
    total_discount: float
    total: float
    currency: str

class OrderCreate(BaseModel):
    cart_id: str
    items: List[CartItem]
    final_price: float
    atomic_update: bool = False
    delay: float = 0.0
    drop: int = 0


# -------------------- Agent State --------------------------

class OrderState(TypedDict):
    trace_id: str
    order_id: str
    cart_id: str

    items: List[dict]
    final_price: float

    atomic_update: bool
    delay: float
    drop: int

    inventory_status: Optional[str]
    payment_status: Optional[str]
    shipment_status: Optional[str]

    decision: Optional[str]
    status: Optional[str]
    
    history: list[dict]


    # workflow completion flags
    fetch_cart_done: bool
    price_cart_done: bool
    reserve_inventory_done: bool
    process_payment_done: bool
    rollback_inventory_done: bool
    book_shipment_done: bool

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


# ------------------------- TOOLS ------------------

@tool
def fetch_cart(cart_id: str):
    """Fetch shopping cart items"""
    r = requests.get(CART_SERVICE_URL + cart_id, timeout=10)
    r.raise_for_status()
    return r.json()



def price_cart(state):
    """Fetch latest prices for cart items"""
    items = state['items']
    payload = {
        "items": [{"product_id": i["sku"], "qty": i["qty"]} for i in items],
        "promo_codes": [],
        "only_final_price": True
    }
    r = requests.post(f"{PRICING_SERVICE_URL}/price", json=payload, timeout=30)
    r.raise_for_status()
    return r.json()



def reserve_inventory(state):
    """Reserve inventory"""
    payload = {
        "order_id": state['order_id'],
        "items": state['items'],
        "atomic_update": state['atomic_update'],
        "delay": state['delay'],
        "drop": state['drop']
    }
    r = requests.post(INVENTORY_SERVICE_RESERVE_URL, json=payload, timeout=30)
    r.raise_for_status()
    return r.json()



def rollback_inventory(state):
    """Rollback inventory reservation"""
    payload = {
        "order_id": state['order_id'],
        "items": state['items'],
        "atomic_update": state['atomic_update'],
        "delay": state['delay'],
        "drop": state['drop']
    }
    requests.post(INVENTORY_SERVICE_RESERVE_ROLLBACK_URL, json=payload, timeout=30)



def process_payment(state):
    """Process payment"""
    r = requests.post(PAYMENT_SERVICE_URL,
                      json={"order_id": state['order_id'], "final_price": state['final_price']},
                      timeout=30)
    r.raise_for_status()
    return r.json()


@tool
def book_shipment(order_id: str):
    """Book shipment"""
    r = requests.post(SHIPMENT_SERVICE_URL,
                      json={"order_id": order_id, "address": "SAMPLE_ADDRESS"},
                      timeout=10)
    r.raise_for_status()
    return r.json()


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


# ----------------- Reasoning None (LLM-Driven) ------

def reason_node(state: OrderState):

    t1 = time.time()

    prompt = system_prompt(state)

    logger.info(
        "Order reasoning prompt trace=%s\n%s",
        state["trace_id"],
        prompt,
    )

    response = llm.invoke(prompt)

    raw = response.text().strip()
    
    logger.info(
        "Order reasoning raw response trace=%s\n%s",
        state["trace_id"],
        raw,)

    usage = response.usage_metadata or {}

    input_tokens = usage.get("input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0)

    decision = parse_json_response(raw)

    next_action = decision["next_action"]

    logger.info(
        "Order decision=%s input=%d output=%d",
        next_action,
        input_tokens,
        output_tokens,
    )

    history = list(state.get("history", []))

    history.append(
        {
            "stage": "reason",
            "raw": raw,
            "chosen_action": next_action,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "duration": round(time.time() - t1, 3),
        }
    )

    return {
        "decision": next_action,
        "history": history,
        "total_input_tokens":
            state.get("total_input_tokens", 0)
            + input_tokens,

        "total_output_tokens":
            state.get("total_output_tokens", 0)
            + output_tokens,

        "total_llm_calls":
            state.get("total_llm_calls", 0)
            + 1,
    }
    
def system_prompt(state: OrderState):

    workflow = [
        {
            "action": "FETCH_CART",
            "completed": state.get("fetch_cart_done", False),
            "rank": 1,
        },
        {
            "action": "PRICE_CART",
            "completed": state.get("price_cart_done", False),
            "rank": 2,
        },
        {
            "action": "RESERVE_INVENTORY",
            "completed": state.get("reserve_inventory_done", False),
            "rank": 3,
        },
        {
            "action": "PROCESS_PAYMENT",
            "completed": state.get("process_payment_done", False),
            "rank": 4,
        },
        {
            "action": "BOOK_SHIPMENT",
            "completed": state.get("book_shipment_done", False),
            "rank": 5,
        },
    ]
    
    state_view = {

        "status": state["status"],

        "inventory_status": state["inventory_status"],
        "payment_status": state["payment_status"],
        "shipment_status": state["shipment_status"],

        "atomic_update": state["atomic_update"],
    }
    
    actions = [x["action"] for x in workflow if x['completed'] is False] + ["FINISH", "ROLLBACK_INVENTORY"]
    all_done = all(stage.get("completed") is True for stage in workflow)  
    if all_done:
        workflow = []  # if all stages are completed, the workflow is empty 

    
    return f"""
        You are an Order workflow orchestrator.

        Your job is to choose exactly ONE next action from this list: {actions} based on decision policy and current workflow state.

        Decision policy:
        - If all stages in the workflow are completed or workflow is empty, choose "finish" as the next action.
        - Choose the first stage in the rank order from workflow which is not completed as next action.
        - Never skip a stage, choose any earlier stage or repeat a completed stage.
        

        Exception policy

        • If status == OUT_OF_STOCK choose FINISH
        • If status == PAYMENT_FAILED choose ROLLBACK_INVENTORY
        • If rollback_inventory_done == True choose FINISH


        Current order state:

        {json.dumps(state_view, indent=2)}

        Current workflow state:

        {json.dumps(workflow, indent=2)}


        Return ONLY JSON without intermediate reasoning and justification.

        {{
            "next_action":"..."
        }}

        """.strip()


# -------------- Action Nodes -----------
def fetch_cart_node(state: OrderState):
    t1 = time.time()
    logger.info(f'Calling fetch_cart_node tool ... \n Current State is {state}')
    print(f'Calling fetch_cart_node tool ... \n Current State is {state}')
    cart = fetch_cart.invoke(state["cart_id"])
    logger.info(f'Response of fetch_cart_node tool ==> {cart}, \n-------------------------------------')
    print(f'Response of fetch_cart_node tool ==> {cart}, \n-------------------------------------')

    state["items"] = cart["items"]
    state["fetch_cart_done"] = True

    state["total_input_tokens"] += cart["total_input_tokens"]
    state["total_output_tokens"] += cart["total_output_tokens"]
    state["total_llm_calls"] += cart["total_llm_calls"]
    t2 = time.time()
    
    history = list(state.get("history", []))
    history.append(
        {
            "stage": "act",
            "action": "FETCH_CART",
            "result": {"items": cart["items"]},
            "duration": round(t2 - t1, 3)
        }
    )
    state["history"] = history

    return state


def pricing_node(state: OrderState):
    t1 = time.time()
    logger.info(f'Calling pricing_node tool ... \n Current State is {state}')
    print(f'Calling pricing_node tool ... \n Current State is {state}')
    pricing = price_cart(state)
    logger.info(f'Response of pricing_node tool ==> {pricing}, \n-------------------------------------')
    print(f'Response of pricing_node tool ==> {pricing}, \n-------------------------------------')

    state["final_price"] = pricing["total"]


    state["total_input_tokens"] += pricing["total_input_tokens"]
    state["total_output_tokens"] += pricing["total_output_tokens"]
    state["total_llm_calls"] += pricing["total_llm_calls"]
    
    

    # init order in DB
    db.orders.insert_one({"_id": state['order_id'], "items": [{'sku': item['sku'], 'qty': item['qty']} for item in state['items']],
                           "cart_id": state['cart_id'], "status": "INIT",
                           "final_price": state['final_price']})
    state["price_cart_done"] = True

    
    t2 = time.time()
    history = list(state.get("history", []))
    history.append(
        {
            "stage": "act",
            "action": "PRICE_CART",
            "result": {"final_price": state['final_price']},
            "duration": round(t2 - t1, 3)
        }
    )
    state["history"] = history
    
    return state


def reserve_inventory_node(state: OrderState):
    t1 = time.time()
    logger.info(f'Calling reserve_inventory_node tool ... \n Current State is {state}')
    print(f'Calling reserve_inventory_node tool ... \n Current State is {state}')
    res = reserve_inventory(state)
    logger.info(f'Response of reserve_inventory_node tool ==> {res}, \n-------------------------------------')
    print(f'Response of reserve_inventory_node tool ==> {res}, \n-------------------------------------')

    state["inventory_status"] = res["status"]
    if res["status"] == "OUT_OF_STOCK":
        state["status"] = "OUT_OF_STOCK"

    state["total_input_tokens"] += res["total_input_tokens"]
    state["total_output_tokens"] += res["total_output_tokens"]
    state["total_llm_calls"] += res["total_llm_calls"]

    # update order status in DB
    db.orders.update_one({"_id": state['order_id']}, {"$set": {"status": state["inventory_status"]}})
    state["reserve_inventory_done"] = True
    
    t2 = time.time()
    history = list(state.get("history", []))
    history.append(
        {
            "stage": "act",
            "action": "RESERVE_INVENTORY",
            "result": {"inventory_status": state['inventory_status']},
            "duration": round(t2 - t1, 3)
        }
    )
    state["history"] = history   
   
    return state


def payment_node(state: OrderState):
    t1 = time.time()
    logger.info(f'Calling payment_node tool ... \n Current State is {state}')
    print(f'Calling payment_node tool ... \n Current State is {state}')
    try:
        res = process_payment(state)
        logger.info(f'Response of payment_node tool ==> {res}, \n-------------------------------------')
        print(f'Response of payment_node tool ==> {res}, \n-------------------------------------')

        state["payment_status"] = res["status"]
        state["status"] = "PAYMENT_SUCCEED" if res["status"] == "SUCCESS" else "PAYMENT_FAILED"

        state["total_input_tokens"] += res["total_input_tokens"]
        state["total_output_tokens"] +=  res["total_output_tokens"]
        state["total_llm_calls"] += res["total_llm_calls"]

    except Exception as e:
        logger.info(f'Exception in response of payment_node tool ==> {e}, \n-------------------------------------')
        print(f'Exception in response of payment_node tool ==> {e}, \n-------------------------------------')
        state["payment_status"] = "FAILED"
        state["status"] = "PAYMENT_FAILED"


    # update order status in DB
    db.orders.update_one({"_id": state['order_id']}, {"$set": {"status": state["status"]}})
    state["process_payment_done"] = True
    
    t2 = time.time()
    history = list(state.get("history", []))
    history.append(
        {
            "stage": "act",
            "action": "PROCESS_PAYMENT",
            "result": {"payment_status": state['payment_status']},
            "duration": round(t2 - t1, 3)
        }
    )
    state["history"] = history  

    return state


def rollback_node(state: OrderState):
    t1 = time.time()
    logger.info(f'Calling rollback_node tool ... \n Current State is {state}, \n-------------------------------------')
    print(f'Calling rollback_node tool ... \n Current State is {state}, \n-------------------------------------')
    rollback_inventory(state)
    state["rollback_inventory_done"] = True
    t2 = time.time()

    history = list(state.get("history", []))
    history.append(
        {
            "stage": "act",
            "action": "ROLLBACK_INVENTORY",
            "result": {"inventory_status": state['inventory_status']},
            "duration": round(t2 - t1, 3)
        }
    )
    state["history"] = history 
    
    return state


def shipment_node(state: OrderState):
    logger.info(f'Calling shipment_node tool ... \n Current State is {state}')
    print(f'Calling shipment_node tool ... \n Current State is {state}')
    try:
        t1 = time.time()
        res = book_shipment.invoke(state["order_id"])
        logger.info(f'Response of shipment_node tool ==> {res}, \n-------------------------------------')
        print(f'Response of shipment_node tool ==> {res}, \n-------------------------------------')

        state["shipment_status"] = "BOOKED"
        state["status"] = "COMPLETED"

        state["total_input_tokens"] += res["total_input_tokens"]
        state["total_output_tokens"] +=  res["total_output_tokens"]
        state["total_llm_calls"] += res["total_llm_calls"]

        # update order status in DB
        db.orders.update_one({"_id": state['order_id']}, {"$set": {"status": "COMPLETED"}})
        
        state["book_shipment_done"] = True
        t2 = time.time()

        history = list(state.get("history", []))
        history.append(
            {
                "stage": "act",
                "action": "BOOK_SHIPMENT",
                "result": {"shipment_status": state['shipment_status']},
                "duration": round(t2 - t1, 3)
            }
        )
        state["history"] = history 

    except Exception as e:
        logger.info(f'Exception in response of shipment_node tool ==> {e}, \n-------------------------------------')
        print(f'Exception in response of shipment_node tool ==> {e}, \n-------------------------------------')
        state["shipment_status"] = "FAILED"
        state["status"] = "SHIPMENT_FAILED"
        # update order status in DB
        db.orders.update_one({"_id": state['order_id']}, {"$set": {"status": "SHIPMENT_FAILED"}})

        history = list(state.get("history", []))
        history.append(
            {
                "stage": "act",
                "action": "BOOK_SHIPMENT",
                "result": {"shipment_status": state['shipment_status']},
                "duration": None
            }
        )
        state["history"] = history 
        
    return state


# ------------------- Langgraph --------

graph = StateGraph(OrderState)

graph.add_node("reason", reason_node)
graph.add_node("fetch_cart", fetch_cart_node)
graph.add_node("price", pricing_node)
graph.add_node("reserve", reserve_inventory_node)
graph.add_node("pay", payment_node)
graph.add_node("rollback", rollback_node)
graph.add_node("ship", shipment_node)

graph.set_entry_point("reason")

graph.add_conditional_edges(
    "reason",
    lambda s: s["decision"],
    {
        "FETCH_CART": "fetch_cart",
        "PRICE_CART": "price",
        "RESERVE_INVENTORY": "reserve",
        "PROCESS_PAYMENT": "pay",
        "ROLLBACK_INVENTORY": "rollback",
        "BOOK_SHIPMENT": "ship",
        "FINISH": END
    }
)

# loop back to reasoning
for n in ["fetch_cart", "price", "reserve", "pay", "rollback", "ship"]:
    graph.add_edge(n, "reason")

order_agent = graph.compile()


def checkout_cart_agent(cart_id: str):
    state: OrderState = {
        "trace_id": str(uuid.uuid4()),
        "order_id": str(uuid.uuid4()),
        "cart_id": cart_id,

        "items": [],
        "final_price": 0.0,

        "atomic_update": True,
        "delay": 0.0,
        "drop": 0,

        "inventory_status": None,
        "payment_status": None,
        "shipment_status": None,

        "decision": None,
        "status": None,

        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "total_llm_calls": 0
    }

    logger.info(f'Request for checkout_cart, cart_id = {cart_id}, state={state}')
    print(f'Request for checkout_cart, cart_id = {cart_id}, state={state}')

    final_state = order_agent.invoke(state, config={"recursion_limit": 12})

    logger.info("History for cart_id=%s: %s", cart_id, json.dumps(final_state.get("history", [])))
            
    logger.info(
        "Order flow completed for cart_id=%s, order_id=%s, total_input_tokens=%d, total_output_tokens=%d, total_llm_calls=%d",
        cart_id,
        final_state.get("order_id"),
        final_state.get("total_input_tokens", 0),
        final_state.get("total_output_tokens", 0),
        final_state.get("total_llm_calls", 0),
    )
        
        
    return {
        "order_id": final_state["order_id"],
        "status": final_state["status"],
        "total_input_tokens": final_state.get("total_input_tokens"),
        "total_output_tokens": final_state.get("total_output_tokens"),
        "total_llm_calls": final_state.get("total_llm_calls")
    }


@app.post("/cart/{cart_id}/checkout")
async def checkout_cart(cart_id: str):
    result = checkout_cart_agent(cart_id=cart_id)
    logger.info(f'Request for checkout_cart processed successfully, cart_id = {cart_id}, result={result}')
    print(f'Request for checkout_cart processed successfully, cart_id = {cart_id}, result={result}')    
    return result


@app.post("/clear_orders")
async def clear_orders():
    await db.orders.delete_many({})

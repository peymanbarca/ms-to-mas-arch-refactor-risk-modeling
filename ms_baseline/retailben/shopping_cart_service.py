# shopping_cart.py
import os
import logging
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from typing import List, Dict, Optional
from motor.motor_asyncio import AsyncIOMotorClient
import httpx
import uuid

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017/")
MONGO_DB = os.getenv("MONGO_DB", "ms_baseline")
PORT = int(os.getenv("PORT", 8003))


logger = logging.getLogger("shopping_cart")
logging.basicConfig(
    filename='../logs/shopping_cart_service.log',
    level=logging.INFO,  # Log all messages with severity DEBUG or higher
    format='%(asctime)s - %(levelname)s - %(message)s'  # Define the message format
)

app = FastAPI(title="Shopping Cart Service")

db_client = None
db = None
http_client: httpx.AsyncClient = None

class CartItem(BaseModel):
    sku: str
    qty: int = Field(1, gt=0)

class Cart(BaseModel):
    cart_id: str
    items: List[CartItem] = []
    total_input_tokens: int
    total_output_tokens: int
    total_llm_calls: int

@app.on_event("startup")
async def startup():
    global db_client, db, http_client
    db_client = AsyncIOMotorClient(MONGO_URI)
    db = db_client[MONGO_DB]
    await db.carts.create_index("cart_id", unique=True)
    http_client = httpx.AsyncClient(timeout=10.0)
    logger.info("ShoppingCart connected to mongo")

@app.on_event("shutdown")
async def shutdown():
    global db_client, http_client
    if http_client:
        await http_client.aclose()
    if db_client:
        db_client.close()

@app.post("/cart", response_model=Cart)
async def create_cart():
    cart_id = str(uuid.uuid4())
    await db.carts.insert_one({"cart_id": cart_id, "items": []})
    return Cart(cart_id=cart_id, items=[], total_input_tokens=0, total_output_tokens=0, total_llm_calls=0)

@app.get("/cart/{cart_id}", response_model=Cart)
async def get_cart(cart_id: str):
    logger.info(f"Request for get_cart, cart_id: {cart_id}")
    doc = await db.carts.find_one({"cart_id": cart_id})
    if not doc:
        logger.exception(f"Request for get_cart, not found cart_id: {cart_id}")
        raise HTTPException(status_code=404, detail="cart not found")
    result = Cart(cart_id=doc["cart_id"], items=doc.get("items", []),
                  total_input_tokens=0, total_output_tokens=0, total_llm_calls=0)
    logger.info(f"Request for get_cart, cart_id: {cart_id}, result: {result}")
    return result

@app.post("/cart/{cart_id}/items", response_model=Cart)
async def add_item(cart_id: str, item: CartItem):
    if cart_id == '-1': # create new cart
        cart_id = str(uuid.uuid4())
        items = [{"sku": item.sku, "qty": item.qty}]
        await db.carts.insert_one({"cart_id": cart_id, "items": items})
        return Cart(cart_id=cart_id, items=items, total_input_tokens=0, total_output_tokens=0, total_llm_calls=0)

    doc = await db.carts.find_one({"cart_id": cart_id})
    if not doc:
        raise HTTPException(status_code=404, detail="cart not found")
    items = doc.get("items", [])
    for it in items:
        if it["sku"] == item.sku:
            it["qty"] += item.qty
            break
    else:
        items.append({"sku": item.sku, "qty": item.qty})
    await db.carts.update_one({"cart_id": cart_id}, {"$set": {"items": items}})
    return Cart(cart_id=cart_id, items=items, total_input_tokens=0, total_output_tokens=0, total_llm_calls=0)

@app.delete("/cart/{cart_id}/items/{sku}", response_model=Cart)
async def remove_item(cart_id: str, sku: str):
    doc = await db.carts.find_one({"cart_id": cart_id})
    if not doc:
        raise HTTPException(status_code=404, detail="cart not found")
    items = [it for it in doc.get("items", []) if it["sku"] != sku]
    await db.carts.update_one({"cart_id": cart_id}, {"$set": {"items": items}})
    return Cart(cart_id=cart_id, items=items, total_input_tokens=0, total_output_tokens=0, total_llm_calls=0)



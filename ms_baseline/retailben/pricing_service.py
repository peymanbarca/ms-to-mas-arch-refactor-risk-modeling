# pricing.py
import os
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from typing import List, Optional
from motor.motor_asyncio import AsyncIOMotorClient
from httpx import AsyncClient
import logging


MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017/")
MONGO_DB = os.getenv("MONGO_DB", "ms_baseline")
PORT = int(os.getenv("PORT", 8002))

logger = logging.getLogger("pricing")
logging.basicConfig(
    filename='../logs/pricing_service.log',
    level=logging.INFO,  # Log all messages with severity DEBUG or higher
    format='%(asctime)s - %(levelname)s - %(message)s'  # Define the message format
)

app = FastAPI(title="Pricing & Promotion Service")

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
    qty: int
    unit_price: float
    line_total: float
    discounts: float


class PriceResponse(BaseModel):
    items: List[PriceResponseItem] = []
    subtotal: Optional[float] = None
    total_discount: float
    total: float
    currency: Optional[str] = None
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


@app.post("/price", response_model=PriceResponse)
async def compute_price(req: PriceRequest):
    logger.info(f"Request for compute_price, request: {req}")

    # Fetch unit prices from DB in bulk
    product_ids = [it.product_id for it in req.items]
    docs = await db.prices.find({"product_id": {"$in": product_ids}}).to_list(length=len(product_ids))
    price_map = {d["product_id"]: d["price"] for d in docs}

    subtotal = 0.0
    total_discount = 0.0
    items_out = []
    promos = req.promo_codes or []

    # Simple promo store (in DB could be separate)
    promos_map = {
        "PROMO10": {"type": "percentage", "value": 10.0, "min_qty": 1},
        "BUYS2SAVE5": {"type": "fixed", "value": 5.0, "min_qty": 2}
    }

    for it in req.items:
        unit = price_map.get(it.product_id)
        if unit is None:
            logger.error(f"Request for compute_price failed, status: 404, detail: product {it.product_id} not found,"
                         f" request: {req}")
            raise HTTPException(status_code=404, detail=f"product {it.product_id} not found")
        line = unit * it.qty
        discount = 0.0
        for code in promos:
            promo = promos_map.get(code)
            if not promo:
                continue
            if it.qty >= promo["min_qty"]:
                if promo["type"] == "percentage":
                    discount += line * (promo["value"] / 100.0)
                else:
                    discount += promo["value"]
        items_out.append(PriceResponseItem(
            product_id=it.product_id, qty=it.qty,
            unit_price=unit, line_total=round(line - discount, 2),
            discounts=round(discount, 2)
        ))
        subtotal += line
        total_discount += discount

    total = max(0.0, subtotal - total_discount)
    result = PriceResponse(items=items_out, subtotal=round(subtotal, 2),
                           total_discount=round(total_discount, 2),
                           total=round(total, 2), currency=req.currency,
                           total_input_tokens=0, total_output_tokens=0, total_llm_calls=0)
    logger.info(f"Request for compute_price successfully processed, result: {result}, request: {req}")

    return result


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

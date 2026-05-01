# procurement.py
import os
import logging
import time

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from typing import Optional
from motor.motor_asyncio import AsyncIOMotorClient
import httpx
import uuid
import datetime


MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017/")
MONGO_DB = os.getenv("MONGO_DB", "ms_baseline")
SUPPLIER_URL = os.getenv("SUPPLIER_URL", "http://localhost:9010/supplier/order")
PORT = int(os.getenv("PORT", 8009))

app = FastAPI(title="Procurement Service")

db_client = None
db = None
http_client: httpx.AsyncClient = None

logger = logging.getLogger("procurement")
logging.basicConfig(
    filename='../logs/procurement_service.log',
    level=logging.INFO,  # Log all messages with severity DEBUG or higher
    format='%(asctime)s - %(levelname)s - %(message)s'  # Define the message format
)

class SupplierOrderRequest(BaseModel):
    sku: str
    qty: int = Field(..., gt=0)
    preferred_supplier: Optional[str] = None

class SupplierOrderResponse(BaseModel):
    supplier_order_id: str
    status: str
    eta_days: Optional[int]

@app.on_event("startup")
async def startup():
    global db_client, db, http_client
    db_client = AsyncIOMotorClient(MONGO_URI)
    db = db_client[MONGO_DB]
    await db.proc_orders.create_index("supplier_order_id", unique=True)
    http_client = httpx.AsyncClient(timeout=10.0)
    logger.info("Procurement connected to mongo")

@app.on_event("shutdown")
async def shutdown():
    global db_client, http_client
    if http_client:
        await http_client.aclose()
    if db_client:
        db_client.close()


@app.post("/order_supplier", response_model=SupplierOrderResponse)
async def order_from_supplier(req: SupplierOrderRequest):
    logger.info(f"Request for order_from_supplier external, request: {req}")

    payload = {"sku": req.sku, "qty": req.qty}
    if req.preferred_supplier:
        payload["supplier"] = req.preferred_supplier
    try:
        # simulate order from external supplier service
        time.sleep(0.2)
        # r = await http_client.post(SUPPLIER_URL, json=payload)
        # r.raise_for_status()
        # jr = r.json()
        # supplier_order_id = jr.get("supplier_order_id", str(uuid.uuid4()))
        supplier_order_id = str(uuid.uuid4())

        doc = {
            "supplier_order_id": supplier_order_id,
            "sku": req.sku,
            "qty": req.qty,
            "status": "PLACED",
            "eta_days": 2,
            "created_at": datetime.datetime.utcnow()
        }
        await db.proc_orders.insert_one(doc)
        result = SupplierOrderResponse(supplier_order_id=supplier_order_id, status=doc["status"], eta_days=doc["eta_days"])
        logger.info(f"Request for order_from_supplier successfully processed, result: {result}, request: {req}")

        return result
    except httpx.RequestError:
        logger.exception("External supplier call failed")
        # store a failed entry
        order_id = str(uuid.uuid4())
        doc = {"supplier_order_id": order_id, "product_id": req.product_id, "qty": req.qty, "status": "FAILED"}
        await db.proc_orders.insert_one(doc)
        raise HTTPException(status_code=502, detail="Supplier unavailable")

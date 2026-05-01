# shipment.py
import datetime
import os
import logging
import time

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import List, Dict, Optional
from motor.motor_asyncio import AsyncIOMotorClient
import httpx
import uuid


MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017/")
MONGO_DB = os.getenv("MONGO_DB", "ms_baseline")
CARRIER_API = os.getenv("CARRIER_API", "http://localhost:9020/carrier/book")
PORT = int(os.getenv("PORT", 8006))

logger = logging.getLogger("shipment")
logging.basicConfig(
    filename='../logs/shipment_service.log',
    level=logging.INFO,  # Log all messages with severity DEBUG or higher
    format='%(asctime)s - %(levelname)s - %(message)s'  # Define the message format
)

app = FastAPI(title="Shipment Service")

db_client = None
db = None
http_client: httpx.AsyncClient = httpx.AsyncClient(timeout=10)


class ShipmentRequest(BaseModel):
    order_id: str
    address: str


class ShipmentResponse(BaseModel):
    shipment_id: str
    tracking_id: str
    total_input_tokens: int
    total_output_tokens: int
    total_llm_calls: int


@app.on_event("startup")
async def startup():
    global db_client, db, http_client
    db_client = AsyncIOMotorClient(MONGO_URI)
    db = db_client[MONGO_DB]
    await db.shipments.create_index("shipment_id", unique=True)
    http_client = httpx.AsyncClient(timeout=10.0)
    logger.info("Shipment connected to mongo")


@app.on_event("shutdown")
async def shutdown():
    global db_client, http_client
    if http_client:
        await http_client.aclose()
    if db_client:
        db_client.close()


@app.post("/clear_bookings")
def clear_bookings():
    db.shipments.delete_many({})


@app.post("/book", response_model=ShipmentResponse)
async def book_shipment(req: ShipmentRequest):
    logger.info(f"Request for book_shipment, order_id={req.order_id}, request: {req}")

    try:
        # simulate calling an external service
        # r = await http_client.post(CARRIER_API, json=payload)
        # r.raise_for_status()
        # jr = r.json()
        time.sleep(0.2)
        logger.info(f"Request for book_shipment, external carrier service called successfully, order_id={req.order_id},"
                    f" request: {req}")

        tracking_id = str(uuid.uuid4())

        shipment_id = str(uuid.uuid4())
        doc = {
            "shipment_id": shipment_id,
            "order_id": req.order_id,
            "address": req.address,
            "created_at": datetime.datetime.now(),
            "tracking_id": tracking_id
        }
        await db.shipments.insert_one(doc)
        result = ShipmentResponse(shipment_id=shipment_id, tracking_id=doc["tracking_id"],
                                  total_input_tokens=0, total_output_tokens=0, total_llm_calls=0)
        logger.info(f"Request for book_shipment successfully processed, request: {req}, order_id={req.order_id},"
                    f" result: {result}")

        return result
    except httpx.RequestError:
        logger.exception(f"External carrier booking failed, order_id={req.order_id}")
        raise HTTPException(status_code=502, detail="Carrier unavailable")

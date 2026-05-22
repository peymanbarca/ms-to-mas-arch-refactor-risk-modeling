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
MONGO_DB = os.getenv("MONGO_DB", "retailben")
CARRIER_API = os.getenv("CARRIER_API", "http://localhost:9020/carrier/book")
PORT = int(os.getenv("PORT", 8006))

logger = logging.getLogger("shipment")
logging.basicConfig(
    filename='./logs/shipment_service.log',
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
    shipment_date: Optional[datetime.datetime] = time.now() + datetime.timedelta(days=2)


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
        
        order_id = req.order_id.strip()
        address = req.address.strip()
        # implement some logic to check if the address is serviceable, for example by checking against a list of known undeliverable address patterns
        # this bring code complexity higher
        normalized_address = address.lower()
        if not order_id:
            raise HTTPException(status_code=400, detail="Order id is required")
        if len(address) < 5:
            raise HTTPException(status_code=400, detail="Address is required")
        undeliverable_markers = ("po box", "p.o. box", "apo ", "fpo ", "restricted", "unknown", "invalid")
        if any(marker in normalized_address for marker in undeliverable_markers):
            logger.warning(f"Request for book_shipment, booking failed due to invalid address, order_id={req.order_id},"
                           f" request: {req}")
            raise HTTPException(status_code=422, detail="Shipment address is not serviceable")
        # implement some logic to check shipment date is feasible, for example by checking if it's not in the past and not too far in the future, also some modification based on the destination address (e.g. international shipments might require longer lead time)
        if req.shipment_date < datetime.datetime.now():
            logger.warning(f"Request for book_shipment, booking failed due to invalid shipment date, order_id={req.order_id},"
                           f" request: {req}")
            raise HTTPException(status_code=422, detail="Shipment date cannot be in the past")
        if req.shipment_date > datetime.datetime.now() + datetime.timedelta(days=30):
            logger.warning(f"Request for book_shipment, booking failed due to invalid shipment date, order_id={req.order_id},"
                           f" request: {req}")
            raise HTTPException(status_code=422, detail="Shipment date cannot be more than 30 days in the future")
        if "international" in normalized_address and req.shipment_date < datetime.datetime.now() + datetime.timedelta(days=7):
            logger.warning(f"Request for book_shipment, booking failed due to invalid shipment date for international address, order_id={req.order_id},"
                           f" request: {req}")
            raise HTTPException(status_code=422, detail="International shipments require at least 7 days lead time")
        
        
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
            "tracking_id": tracking_id,
            "shipment_date": req.shipment_date
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

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from typing import Optional, List
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import PyMongoError
import time
import threading
import requests
import logging

app = FastAPI()
db_client = MongoClient("mongodb://localhost:27017/")
inventory_col = db_client["ms_baseline"]["inventory"]
PROCUREMENT_SERVICE_URL = "http://127.0.0.1:8009/order_supplier"

lock = threading.Lock()

logger = logging.getLogger("inventory")
logging.basicConfig(
    filename='./logs/inventory_service.log',
    level=logging.INFO,  # Log all messages with severity DEBUG or higher
    format='%(asctime)s - %(levelname)s - %(message)s'  # Define the message format
)


class CartItem(BaseModel):
    sku: str
    qty: int = Field(1, gt=0)


class Cart(BaseModel):
    cart_id: str
    items: List[CartItem] = []


class ReservationReq(BaseModel):
    order_id: str
    items: List[CartItem] = []
    atomic_update: bool = False
    delay: float = 0.0
    drop: int = 0


@app.post("/reset_stocks")
def reset_stocks(request: dict):
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
    inventory_col.delete_many({})
    items: List[CartItem] = request["items"]
    for item in items:
        inventory_col.insert_one({"sku": item['sku'], "stock": item['stock']})


def inject_failure(req: ReservationReq):
    INJECT_DELAY, INJECT_DROP_RATE = 0, 0
    if req.delay and req.delay > 0:
        INJECT_DELAY = req.delay
    if req.drop and req.drop > 0:
        INJECT_DROP_RATE = req.drop

    logger.info(f"Failure injected for inventory reserve_stock INJECT_DELAY: {INJECT_DELAY}, "
                f"INJECT_DROP_RATE: {INJECT_DROP_RATE}")

    # inject delay
    if INJECT_DELAY > 0:
        time.sleep(INJECT_DELAY)

    if INJECT_DROP_RATE > 0:
        import random
        if random.randint(0, 99) < INJECT_DROP_RATE:
            # simulate dropped request
            raise HTTPException(status_code=503, detail="simulated service drop")


@app.post("/reserve")
def reserve_stock(req: ReservationReq):
    if not req.items:
        raise HTTPException(status_code=400, detail="empty_cart_items")

    logger.info(f"Request for reserve_stock, order_id: {req.order_id}, request: {req}")
    # optional drop injection: simulate network failure by returning 500 occasionally
    inject_failure(req)

    # ============================
    # ATOMIC PATH (With Lock)
    # ============================
    if req.atomic_update:
        try:
            with lock:
                results = []

                # Step 1: validate all items
                for item in req.items:
                    doc = inventory_col.find_one(
                        {"sku": item.sku}
                    )
                    if not doc or doc["stock"] < item.qty:
                        logger.info(
                            f"Request for reserve_stock successfully processed, order_id: {req.order_id},"
                            f" failed_sku: {item.sku}, status: OUT_OF_STOCK")
                        raise ValueError(f"Out of stock: {item.sku}")

                # Step 2: decrement all
                for item in req.items:
                    res = inventory_col.find_one_and_update(
                        {"sku": item.sku},
                        {"$inc": {"stock": -item.qty}},
                        return_document=ReturnDocument.AFTER
                    )
                    results.append({
                        "sku": item.sku,
                        "remaining": res["stock"]
                    })

            logger.info(f"Request for reserve_stock successfully processed, order_id: {req.order_id},"
                        f" result items: {results}, status: RESERVED")

            return {
                "order_id": req.order_id,
                "status": "RESERVED",
                "items": results,
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "total_llm_calls": 0
            }

        except Exception as e:
            logger.error(
                f"Exception in Request for reserve_stock, order_id: {req.order_id},"
                f" status: OUT_OF_STOCK, Error: {str(e)}")
            return {
                "order_id": req.order_id,
                "status": "OUT_OF_STOCK",
                "reason": str(e),
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "total_llm_calls": 0
            }

    # ============================
    # NON-ATOMIC PATH (Stepwise)
    # ============================
    else:
        updated = []

        for item in req.items:
            doc = inventory_col.find_one({"sku": item.sku})
            if not doc or doc["stock"] < item.qty:
                logger.info(
                    f"Request for reserve_stock successfully processed, order_id: {req.order_id},"
                    f" failed_sku: {item.sku}, status: OUT_OF_STOCK")
                return {
                    "order_id": req.order_id,
                    "status": "OUT_OF_STOCK",
                    "failed_sku": item.sku
                }

            new_stock = doc["stock"] - item.qty
            inventory_col.update_one(
                {"sku": item.sku},
                {"$set": {"stock": new_stock}}
            )

            updated.append({
                "sku": item.sku,
                "remaining": new_stock
            })

        logger.info(f"Request for reserve_stock successfully processed, order_id: {req.order_id},"
                    f" result items: {updated}, status: RESERVED")

        return {
            "order_id": req.order_id,
            "status": "RESERVED",
            "items": updated,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "total_llm_calls": 0
        }


@app.post("/reserve-rollback")
def rollback_reserve_stock(req: ReservationReq):
    if not req.items:
        raise HTTPException(status_code=400, detail="empty_cart_items")

    # ============================
    # ATOMIC PATH (With Lock)
    # ============================
    if req.atomic_update:
        try:
            with lock:
                results = []

                # Step 1: increment all
                for item in req.items:
                    res = inventory_col.find_one_and_update(
                        {"sku": item.sku},
                        {"$inc": {"stock": +item.qty}},
                        return_document=ReturnDocument.AFTER
                    )
                    results.append({
                        "sku": item.sku,
                        "remaining": res["stock"]
                    })

            return {
                "order_id": req.order_id,
                "status": "RESERVED_ROLLBACK",
                "items": results,
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "total_llm_calls": 0
            }

        except Exception as e:
            return {
                "order_id": req.order_id,
                "status": "FAILED",
                "reason": str(e),
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "total_llm_calls": 0
            }

    # ============================
    # NON-ATOMIC PATH (Stepwise)
    # ============================
    else:
        updated = []

        for item in req.items:
            doc = inventory_col.find_one({"sku": item.sku})
            if not doc:
                return {
                    "order_id": req.order_id,
                    "status": "FAILED",
                    "failed_sku": item.sku
                }

            new_stock = doc["stock"] + item.qty
            inventory_col.update_one(
                {"sku": item.sku},
                {"$set": {"stock": new_stock}}
            )

            updated.append({
                "sku": item.sku,
                "remaining": new_stock
            })

        return {
            "order_id": req.order_id,
            "status": "RESERVED_ROLLBACK",
            "items": updated,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "total_llm_calls": 0
        }


@app.post("/reorder")
def reorder_inventory():
    # some rules for reordering ...
    dynamic_value = 2
    dynamic_reorder_value = 10
    low_stock_items_cur = inventory_col.find({'stock': {'$lt': dynamic_value}})
    for item in low_stock_items_cur:
        try:
            res = requests.post(PROCUREMENT_SERVICE_URL, json={'sku': item['sku'], 'qty': dynamic_reorder_value})
            res.raise_for_status()
        except Exception as e:
            logger.exception(f"Error occurred in calling procurement service for reorder inventory: {e}")

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from typing import Optional, List
import requests
from pymongo import MongoClient
import uuid
import time
import httpx
import logging

app = FastAPI()
ORDER_COLL = MongoClient("mongodb://localhost:27017/")["ms_baseline"]["orders"]
INVENTORY_SERVICE_RESERVE_URL = "http://127.0.0.1:8001/reserve"
INVENTORY_SERVICE_RESERVE_ROLLBACK_URL = "http://127.0.0.1:8001/reserve-rollback"
CART_SERVICE_URL = "http://127.0.0.1:8003/cart/"
PRICING_SERVICE_URL = "http://127.0.0.1:8002"
PAYMENT_SERVICE_URL = "http://127.0.0.1:8007/pay-order"
SHIPMENT_SERVICE_URL = "http://127.0.0.1:8006/book"

logger = logging.getLogger("order")
logging.basicConfig(
    filename='../logs/order_service.log',
    level=logging.INFO,  # Log all messages with severity DEBUG or higher
    format='%(asctime)s - %(levelname)s - %(message)s'  # Define the message format
)

http_client = httpx.AsyncClient(timeout=10.0)


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
    items: List[CartItem] = []
    cart_id: str
    final_price: float
    atomic_update: bool = False
    delay: float = 0.0
    drop: int = 0


@app.post("/clear_orders")
def clear_orders():
    ORDER_COLL.delete_many({})


@app.post("/cart/{cart_id}/checkout")
async def checkout_cart(cart_id: str):
    # 1. retrieve cart from cart service
    # 2. retrieve final price from pricing service
    # orchestrate order placement -> inventory reservation -> payment processing -> book shipment -> notify user

    trace_id = str(uuid.uuid4())
    logger.info(f"Request for checkout_cart, cart_id={cart_id},  trace_id={trace_id}")
    
    total_input_tokens = 0
    total_output_tokens = 0
    total_llm_calls = 0

    try:
        cart_resp = requests.get(CART_SERVICE_URL + f'{cart_id}', timeout=10)
        logger.info(f"Cart Service Called, request_cart_id: {cart_id},"
                    f" response_status: {cart_resp.status_code}, trace_id={trace_id}")
        if cart_resp.status_code != 200:
            raise HTTPException(status_code=500, detail="Cart service error")
        cart: Optional[Cart] = cart_resp.json()
        if cart is None:
            raise HTTPException(status_code=404, detail="Cart not found")
        cart_items = cart['items']
        try:
            price_payload = {"items": [{"product_id": item['sku'], "qty": item['qty']} for item in cart_items],
                             "promo_codes": [], "only_final_price": True}
            price_resp = await http_client.post(f"{PRICING_SERVICE_URL}/price", json=price_payload, timeout=10)
            logger.info(f"Pricing Service Called, req: {price_payload},"
                        f" response_status: {price_resp.status_code}, trace_id={trace_id}")

            price_resp.raise_for_status()
            j_resp: PriceResponse = price_resp.json()
            final_price = j_resp['total']
            total_input_tokens += j_resp['total_input_tokens']
            total_output_tokens += j_resp['total_output_tokens']
            total_llm_calls += j_resp['total_llm_calls']

            return orchestrate_order(OrderCreate(items=cart_items, cart_id=cart_id, final_price=final_price,
                                                 atomic_update=True, delay=0.0, drop=0), trace_id=trace_id,
                                                 total_input_tokens=total_input_tokens, total_output_tokens=total_output_tokens,
                                                 total_llm_calls=total_llm_calls)

        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def orchestrate_order(order: OrderCreate, trace_id: str, total_input_tokens:int, total_output_tokens:int, total_llm_calls: int):
    order_id = str(uuid.uuid4())
    logger.info(f"Request for orchestrate_order started, order_id={order_id}, trace_id={trace_id}")
    start_time = time.time()
    # Save order INIT
    ORDER_COLL.insert_one({"_id": order_id, "items": [{'sku': item.sku, 'qty': item.qty} for item in order.items],
                           "cart_id": order.cart_id, "status": "INIT",
                           "final_price": order.final_price})

    # Call inventory service
    try:
        reserve_payload = {"order_id": order_id,
                           "items": [{'sku': item.sku, 'qty': item.qty}
                                     for item in order.items],
                           "atomic_update": order.atomic_update,
                           "delay": order.delay,
                           "drop": order.drop}
        reserve_resp = requests.post(INVENTORY_SERVICE_RESERVE_URL, json=reserve_payload,
                                     timeout=30)
        logger.info(f"Inventory Reservation Service Called, req: {reserve_payload},"
                    f" response_status: {reserve_resp.status_code},"
                    f" response: {reserve_resp.json()}"
                    f" trace_id={trace_id}")
        if reserve_resp.status_code != 200:
            logger.exception(f"Error occurred in inventory reservation, order_id={order_id},"
                             f" res={reserve_resp.json()}, trace_id={trace_id}")
            raise HTTPException(status_code=500, detail="Inventory service error")
        reserve_result = reserve_resp.json()
        total_input_tokens += reserve_result['total_input_tokens']
        total_output_tokens += reserve_result['total_output_tokens']
        total_llm_calls += reserve_result['total_llm_calls']
        
        if reserve_result['status'] == 'OUT_OF_STOCK':
            order_final_status = "OUT_OF_STOCK"
            ORDER_COLL.update_one({"_id": order_id}, {"$set": {"status": order_final_status}})
            end_time = time.time()
            latency = end_time - start_time
            logger.info(
                f"Request for orchestrate_order completed, final_status:{order_final_status},  trace_id={trace_id}")
            return {"order_id": order_id, "status": order_final_status, "latency": latency,
                    "total_input_tokens": total_input_tokens,
                    "total_output_tokens": total_output_tokens,
                    "total_llm_calls": total_llm_calls}


        try:
            payment_payload = {'order_id': order_id, 'final_price': order.final_price}
            payment_resp = requests.post(PAYMENT_SERVICE_URL,
                                         json=payment_payload, timeout=30)
            logger.info(f"Payment Service Called, req: {payment_payload},"
                        f" response_status: {payment_resp.status_code},"
                        f" response: {payment_resp.json()}"
                        f" trace_id={trace_id}")
            if payment_resp.status_code != 200:
                logger.exception(f"Error occurred in payment processing, order_id={order_id},"
                                 f" res={payment_resp.json()}, trace_id={trace_id}")
                raise HTTPException(status_code=500, detail="Payment service error")
            payment_result = payment_resp.json()
            total_input_tokens += payment_result['total_input_tokens']
            total_output_tokens += payment_result['total_output_tokens']
            total_llm_calls += payment_result['total_llm_calls']


            # failed payment
            if payment_result['status'] != 'SUCCESS':

                order_final_status = "PAYMENT_FAILED"
                ORDER_COLL.update_one({"_id": order_id}, {"$set": {"status": order_final_status}})

                # rollback inventory reservation
                try:
                    reserve_rollback_payload = {"order_id": order_id,
                                                "items": [{'sku': item.sku, 'qty': item.qty}
                                                          for item in order.items],
                                                "atomic_update": order.atomic_update,
                                                "delay": order.delay,
                                                "drop": order.drop}
                    reserve_rollback_resp = requests.post(INVENTORY_SERVICE_RESERVE_ROLLBACK_URL,
                                                          json=reserve_rollback_payload,
                                                          timeout=30)
                    logger.info(f"Inventory Reservation Rollback Service Called, req: {reserve_rollback_payload},"
                                f" response_status: {reserve_rollback_resp.status_code},"
                                f" response: {reserve_rollback_resp.json()}"
                                f" trace_id={trace_id}")
                    if reserve_rollback_resp.status_code != 200:
                        logger.exception(f"Error occurred in rollback inventory reservation, order_id={order_id},"
                                         f" res={reserve_rollback_resp.json()}, trace_id={trace_id}")
                except Exception as e:
                    logger.exception(f"Error occurred in rollback inventory reservation, order_id={order_id},"
                                     f" trace_id={trace_id}, e={e}")
                    raise HTTPException(status_code=500, detail=str(e))

                end_time = time.time()
                latency = end_time - start_time
                logger.info(
                    f"Request for orchestrate_order completed, final_status:{order_final_status},  trace_id={trace_id}")
                return {"order_id": order_id, "status": order_final_status, "latency": latency,
                        "total_input_tokens": total_input_tokens,
                        "total_output_tokens": total_output_tokens,
                        "total_llm_calls": total_llm_calls
                        }

            # success payment
            else:
                order_final_status = "PAYMENT_SUCCEED"
                ORDER_COLL.update_one({"_id": order_id}, {"$set": {"status": order_final_status}})

                # book shipment
                try:
                    shipment_payload = {'order_id': order_id, 'address': 'SAMPLE_ADDRESS'}
                    shipment_resp = requests.post(SHIPMENT_SERVICE_URL,
                                                  json=shipment_payload,
                                                  timeout=30)
                    shipment_result = shipment_resp.json()
                    total_input_tokens += shipment_result['total_input_tokens']
                    total_output_tokens += shipment_result['total_output_tokens']
                    total_llm_calls += shipment_result['total_llm_calls']
                    logger.info(f"Shipment Service Called, req: {shipment_payload},"
                                f" response_status: {shipment_resp.status_code},"
                                f" response: {shipment_resp.json()}"
                                f" trace_id={trace_id}")
                    if shipment_resp.status_code != 200:
                        shipment_result = shipment_resp.json()
                        total_input_tokens += shipment_result['total_input_tokens']
                        total_output_tokens += shipment_result['total_output_tokens']
                        total_llm_calls += shipment_result['total_llm_calls']
                        order_final_status = "SHIPMENT_FAILED"
                        ORDER_COLL.update_one({"_id": order_id}, {"$set": {"status": order_final_status}})
                        logger.exception(f"Error occurred in shipment booking, order_id={order_id},"
                                         f" res={shipment_resp.json()}, trace_id={trace_id}")
                        raise HTTPException(status_code=500, detail="Shipment service error")

                except Exception as e:
                    logger.exception(f"Error occurred in shipment booking, order_id={order_id},"
                                     f" trace_id={trace_id}, e={e}")
                    raise HTTPException(status_code=500, detail=str(e))

        except Exception as e:
            logger.exception(f"Error occurred in payment processing, order_id={order_id},"
                             f" e={e}, trace_id={trace_id}")
            raise HTTPException(status_code=500, detail=str(e))

        order_final_status = "COMPLETED"
        ORDER_COLL.update_one({"_id": order_id}, {"$set": {"status": order_final_status}})
    except Exception as e:
        logger.exception(f"Error occurred in orchestrating order, order_id={order_id},"
                         f" e={e}, trace_id={trace_id}")
        # ORDER_DB.update_one({"_id": order_id}, {"$set": {"status": "error"}})
        raise HTTPException(status_code=500, detail=str(e))

    end_time = time.time()
    latency = end_time - start_time
    logger.info(f"Request for orchestrate_order completed, final_status:{order_final_status},  trace_id={trace_id}")
    return {"order_id": order_id, "status": order_final_status, "latency": latency,
            "total_input_tokens": total_input_tokens,
            "total_output_tokens": total_output_tokens,
            "total_llm_calls": total_llm_calls
            }

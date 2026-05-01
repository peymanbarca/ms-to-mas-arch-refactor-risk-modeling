# product_search.py
import os
import logging
from typing import List
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel
from motor.motor_asyncio import AsyncIOMotorClient
import httpx

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017/")
MONGO_DB = os.getenv("MONGO_DB", "ms_baseline")
PRICING_SERVICE_URL = os.getenv("PRICING_SERVICE_URL", "http://localhost:8002")
PORT = int(os.getenv("PORT", 8008))

logger = logging.getLogger("product_search")
logging.basicConfig(
    filename='../logs/product_search_service.log',
    level=logging.INFO,  # Log all messages with severity DEBUG or higher
    format='%(asctime)s - %(levelname)s - %(message)s'  # Define the message format
)

app = FastAPI(title="Product Search Service")

db_client: AsyncIOMotorClient = None
db = None
http_client: httpx.AsyncClient = None

class ProductOut(BaseModel):
    sku: str
    name: str
    description: str

class ProductSearchResultItem(ProductOut):
    price: float
    score: float

class ProductCreate(BaseModel):
    sku: str
    name: str
    description: str

class ProductSearchResponse(BaseModel):
    query: str
    results: List[ProductSearchResultItem]
    total_input_tokens: int
    total_output_tokens: int
    total_llm_calls: int

@app.on_event("startup")
async def startup():
    global db_client, db, http_client
    db_client = AsyncIOMotorClient(MONGO_URI)
    db = db_client[MONGO_DB]
    # await db.products.create_index("name")
    # await db.products.create_index(
    #     [("name", "text"), ("description", "text")],
    #     name="product_text_index"
    # )

    http_client = httpx.AsyncClient(timeout=10.0)
    logger.info("ProductSearch connected to mongo %s", MONGO_URI)

@app.on_event("shutdown")
async def shutdown():
    global db_client, http_client
    if http_client:
        await http_client.aclose()
    if db_client:
        db_client.close()

@app.post("/products")
def create_product(p: ProductCreate):
    # if db.products.find_one({"sku": p.sku}):
    #     raise HTTPException(400, "SKU already exists")

    db.products.insert_one(p.dict())
    return {"status": "created", "sku": p.sku}


@app.get("/search", response_model=ProductSearchResponse)
async def search_products(q: str = Query(..., example="noise cancelling headphones"), limit: int = 5):
    logger.info(f"Request for search_products, query: {q}")

    # Simple text search (in real systems use full-text or embeddings + vector DB)
    cursor = db.products.find({"$text": {"$search": q}}, {"score": {"$meta": "textScore"}}).sort([("score", {"$meta": "textScore"})]).limit(limit)
    docs = await cursor.to_list(length=limit)
    # If no text index or no results, fallback to name substring
    if not docs:
        docs = await db.products.find({"name": {"$regex": q, "$options": "i"}}).limit(limit).to_list(length=limit)

    product_ids = [d["sku"] for d in docs]
    # Call pricing service to get unit prices: we call /price endpoint with qty=1 items
    payload = {"items": [{"product_id": pid, "qty": 1} for pid in product_ids], "promo_codes": []}
    prices = {}
    total_input_tokens = 0
    total_output_tokens = 0
    total_llm_calls = 0
    try:
        logger.info(f"Calling pricing_service, req: {payload}")

        resp = await http_client.post(f"{PRICING_SERVICE_URL}/price", json=payload, timeout=10)
        resp.raise_for_status()
        jr = resp.json()
        logger.info(f"Called pricing_service, req: {payload}, response: {jr}")
        total_input_tokens += jr['total_input_tokens']
        total_output_tokens += jr['total_output_tokens']
        total_llm_calls += jr['total_llm_calls']

        for it in jr.get("items", []):
            prices[it["product_id"]] = it["unit_price"]
    except Exception as e:
        logger.exception("pricing call failed in request for search_products: %s", e)
        # fallback: attempt to read price from product doc if present
        for d in docs:
            prices.setdefault(d["sku"], d.get("price", 0.0))

    results = []
    for d in docs:
        price = prices.get(d["sku"], 0.0)
        # compute a naive score if text score not available
        score = d.get("score", 1.0)
        results.append(ProductSearchResultItem(sku=d["sku"], name=d["name"], description=d.get("description",""),
                                               price=price, score=float(score)))
    final_result = ProductSearchResponse(query=q, results=results,
                                         total_input_tokens=total_input_tokens, total_output_tokens=total_output_tokens, total_llm_calls=total_llm_calls)
    logger.info(f"Request for search_products successfully processed, query: {q}, result: {final_result}")

    return final_result

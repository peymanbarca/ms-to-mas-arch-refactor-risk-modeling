import json
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))
from ..shared import demo_pb2
from ..shared import demo_pb2_grpc
from ..shared.base_service import make_health_app, run_service
from .productcatalogagent import run_product_search_agent

logger = logging.getLogger(__name__)
GRPC_PORT = int(os.getenv("PORT", "5055"))

# ── Product catalog loader ───────────────────────────────────────────────────

_CATALOG_PATH = os.getenv(
    "PRODUCTS_JSON",
    os.path.join(os.path.dirname(__file__), "products.json"),
)

def _load_catalog() -> list[demo_pb2.Product]:
    with open(_CATALOG_PATH) as f:
        data = json.load(f)
    products = []
    for p in data.get("products", []):
        price = p.get("priceUsd", {})
        products.append(demo_pb2.Product(
            id=p["id"],
            name=p["name"],
            description=p.get("description", ""),
            picture=p.get("picture", ""),
            price_usd=demo_pb2.Money(
                currency_code=price.get("currencyCode", "USD"),
                units=int(price.get("units", 0)),
                nanos=int(price.get("nanos", 0)),
            ),
            categories=p.get("categories", []),
        ))
    return products


# ── gRPC Servicer ────────────────────────────────────────────────────────────

class ProductCatalogServicer(demo_pb2_grpc.ProductCatalogServiceServicer):

    def __init__(self):
        self._catalog: list[demo_pb2.Product] = _load_catalog()
        self._index: dict[str, demo_pb2.Product] = {p.id: p for p in self._catalog}
        logger.info("Loaded %d products from catalog", len(self._catalog))

    async def ListProducts(self, request, context):
        return demo_pb2.ListProductsResponse(products=self._catalog)

    async def GetProduct(self, request, context):
        product = self._index.get(request.id)
        if product is None:
            await context.abort(grpc.StatusCode.NOT_FOUND, f"Product {request.id!r} not found")
        return demo_pb2.GetProductResponse(product=product, llm_metrics=demo_pb2.LLMMetrics())

    async def SearchProducts(self, request, context):
        """
        gRPC SearchProducts endpoint – now powered by ProductSearchAgent (LangGraph).

        Flow:
          1. Extract search query from gRPC request.
          2. Invoke run_product_search_agent() – orchestrates the agentic workflow:
             • validate_query:        normalize & validate search query
             • semantic_search:       keyword search against catalog
             • ranking_and_filtering: LLM re-ranks results by relevance
             • log_search_interaction: audit trail to MongoDB
          3. Return ranked results in SearchProductsResponse (same interface as before).

        Key difference from baseline servicer:
          • Search results now go through LLM-powered ranking (non-deterministic).
          • Most relevant products appear first (vs simple keyword match).
          • Full audit trail stored in MongoDB for every search.
          • Token metrics included for observability.
        """
        agent_result = await run_product_search_agent(
            query=request.query,
            catalog=self._catalog,
        )

        # Extract results from agent decision
        results = agent_result["decision"].get("results", [])
        
        # Convert dict results back to Product protobuf
        products = []
        for result_dict in results:
            product = self._index.get(result_dict["id"])
            if product:
                products.append(product)

        return demo_pb2.SearchProductsResponse(results=products, llm_metrics=demo_pb2.LLMMetrics(
            total_input_tokens=agent_result.get("total_input_tokens", 0),
            total_output_tokens=agent_result.get("total_output_tokens", 0),
            total_llm_calls=agent_result.get("total_llm_calls", 0),
        ))


import grpc  # noqa: E402 (needed for abort above)

# ── FastAPI ──────────────────────────────────────────────────────────────────

app = make_health_app("productcatalogagent")

def _product_to_dict(p: demo_pb2.Product) -> dict:
    return {
        "id": p.id,
        "name": p.name,
        "description": p.description,
        "picture": p.picture,
        "price_usd": {
            "currency_code": p.price_usd.currency_code,
            "units": p.price_usd.units,
            "nanos": p.price_usd.nanos,
        },
        "categories": list(p.categories),
    }

_svc = None  # lazy singleton; avoids re-loading catalog on every request

def _get_svc() -> ProductCatalogServicer:
    global _svc
    if _svc is None:
        _svc = ProductCatalogServicer()
    return _svc

@app.get("/products", summary="List all products")
async def rest_list_products():
    svc = _get_svc()
    resp = await svc.ListProducts(demo_pb2.Empty(), None)
    return {"products": [_product_to_dict(p) for p in resp.products]}

@app.get("/products/search", summary="Search products")
async def rest_search_products(query: str = ""):
    svc = _get_svc()
    resp = await svc.SearchProducts(demo_pb2.SearchProductsRequest(query=query), None)
    return {"results": [_product_to_dict(p) for p in resp.results]}

@app.get("/products/{product_id}", summary="Get single product")
async def rest_get_product(product_id: str):
    svc = _get_svc()
    product = svc._index.get(product_id)
    if product is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=f"Product {product_id!r} not found")
    return _product_to_dict(product)


# ── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    run_service(
        demo_pb2_grpc.add_ProductCatalogServiceServicer_to_server,
        ProductCatalogServicer(),
        GRPC_PORT,
        app,
    )
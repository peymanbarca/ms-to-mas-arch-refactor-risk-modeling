"""
productcatalogservice/main.py

Replaces the original Go productcatalogservice.
- gRPC server on port 3550
- FastAPI HTTP server on port 4550
- Reads products from products.json (same format as original)
"""

import json
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))
from ..shared import demo_pb2
from ..shared import demo_pb2_grpc
from ..shared.base_service import make_health_app, run_service

logger = logging.getLogger(__name__)
GRPC_PORT = int(os.getenv("PORT", "3550"))

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
        return product

    async def SearchProducts(self, request, context):
        query = request.query.lower()
        results = [
            p for p in self._catalog
            if query in p.name.lower() or query in p.description.lower()
        ]
        return demo_pb2.SearchProductsResponse(results=results)


import grpc  # noqa: E402 (needed for abort above)

# ── FastAPI ──────────────────────────────────────────────────────────────────

app = make_health_app("productcatalogservice")

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
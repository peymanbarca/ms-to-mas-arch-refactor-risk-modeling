import grpc
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))
from ..shared import demo_pb2, demo_pb2_grpc


async def run_client():
    """gRPC client for ProductCatalogService"""
    async with grpc.aio.insecure_channel(
        "localhost:5055",
    ) as channel:
        stub = demo_pb2_grpc.ProductCatalogServiceStub(channel)

        # List all products
        print("\n=== ListProducts ===")
        response = await stub.ListProducts(demo_pb2.Empty())
        for product in response.products:
            print(f"ID: {product.id}, Name: {product.name}, Price: {product.price_usd.units}")

        # Get single product
        print("\n=== GetProduct ===")
        response = await stub.GetProduct(demo_pb2.GetProductRequest(id="66VCHSJNUP"))
        print(f"Product: {response}")

        # Search products
        print("\n=== SearchProducts ===")
        response = await stub.SearchProducts(demo_pb2.SearchProductsRequest(query="glass"))
        print(f"SearchProducts Res: {response}")


if __name__ == '__main__':
    import asyncio
    asyncio.run(run_client())

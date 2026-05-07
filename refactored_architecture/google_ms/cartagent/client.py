"""
client.py

gRPC client for CartService - invokes all CartService RPC methods and prints results.
"""

import asyncio
import logging
import sys
import os

import grpc

# Add parent directory to path to import shared modules
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ..shared import demo_pb2
from ..shared import demo_pb2_grpc

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class CartServiceClient:
    """gRPC client for CartService"""

    def __init__(self, target: str = "localhost:5054"):
        """
        Initialize the CartService client.

        Args:
            target: gRPC server address (default: localhost:5054)
        """
        self.target = target
        self.channel = None
        self.stub = None

    async def connect(self):
        """Establish connection to CartService"""
        self.channel = grpc.aio.secure_channel(
            self.target,
            grpc.ssl_channel_credentials()
        ) if "localhost" not in self.target else grpc.aio.insecure_channel(self.target)
        self.stub = demo_pb2_grpc.CartServiceStub(self.channel)
        logger.info(f"Connected to CartService at {self.target}")

    async def disconnect(self):
        """Close the connection"""
        if self.channel:
            await self.channel.close()
            logger.info("Disconnected from CartService")

    async def add_item(self, user_id: str, product_id: str, quantity: int = 1) -> bool:
        """
        Add an item to the user's cart.

        Args:
            user_id: User ID
            product_id: Product ID
            quantity: Quantity to add (default: 1)

        Returns:
            True if successful, False otherwise
        """
        try:
            request = demo_pb2.AddItemRequest(
                user_id=user_id,
                item=demo_pb2.CartItem(product_id=product_id, quantity=quantity)
            )
            response = await self.stub.AddItem(request)
            logger.info(f"✓ AddItem: user_id={user_id}, product_id={product_id}, quantity={quantity}")
            print(f"✓ AddItem - user_id: {user_id}, product_id: {product_id}, quantity: {quantity}")
            print(f"  Response: Empty (success)")
            print(f"  LLM Metrics: input_tokens={response.llm_metrics.total_input_tokens}, "
                  f"output_tokens={response.llm_metrics.total_output_tokens}, "
                  f"llm_calls={response.llm_metrics.total_llm_calls}")
            return True
        except grpc.RpcError as e:
            logger.error(f"✗ AddItem failed: {e.details()}")
            print(f"✗ AddItem failed: {e.details()}")
            return False

    async def get_cart(self, user_id: str) -> demo_pb2.Cart | None:
        """
        Get the user's cart.

        Args:
            user_id: User ID

        Returns:
            Cart object or None if failed
        """
        try:
            request = demo_pb2.GetCartRequest(user_id=user_id)
            res = await self.stub.GetCart(request)
            cart = res.cart
            llm_metrics = res.llm_metrics
            logger.info(f"✓ GetCart: user_id={user_id}")
            print(f"✓ GetCart - user_id: {user_id}")
            print(f"  Cart items ({len(cart.items)} items):")
            for item in cart.items:
                print(f"    - Product: {item.product_id}, Quantity: {item.quantity}")
            print(f"  LLM Metrics: input_tokens={llm_metrics.total_input_tokens}, "
                  f"output_tokens={llm_metrics.total_output_tokens}, "
                  f"llm_calls={llm_metrics.total_llm_calls}")
            return cart
        except grpc.RpcError as e:
            logger.error(f"✗ GetCart failed: {e.details()}")
            print(f"✗ GetCart failed: {e.details()}")
            return None

    async def empty_cart(self, user_id: str) -> bool:
        """
        Empty the user's cart.

        Args:
            user_id: User ID

        Returns:
            True if successful, False otherwise
        """
        try:
            request = demo_pb2.EmptyCartRequest(user_id=user_id)
            response = await self.stub.EmptyCart(request)
            llm_metrics = response.llm_metrics
            logger.info(f"✓ EmptyCart: user_id={user_id}")
            print(f"✓ EmptyCart - user_id: {user_id}")
            print(f"  Response: Empty (success)")
            print(f"  LLM Metrics: input_tokens={llm_metrics.total_input_tokens}, "
                  f"output_tokens={llm_metrics.total_output_tokens}, "
                  f"llm_calls={llm_metrics.total_llm_calls}")
            return True
        except grpc.RpcError as e:
            logger.error(f"✗ EmptyCart failed: {e.details()}")
            print(f"✗ EmptyCart failed: {e.details()}")
            return False


async def main():
    """Main function demonstrating all CartService operations"""
    print("=" * 70)
    print("CartService gRPC Client - Testing All Operations")
    print("=" * 70)
    print()

    # Initialize client
    client = CartServiceClient("localhost:5054")

    try:
        # Connect to service
        await client.connect()
        print()

        # Test data
        user_id = "test_user_123"
        products = [
            ("product_001", 2),
            ("product_002", 1),
            ("product_003", 3),
        ]

        # ─────────────────────────────────────────────────────────────────
        # Test 1: Add items to cart
        # ─────────────────────────────────────────────────────────────────
        print("\n" + "-" * 70)
        print("TEST 1: Adding items to cart")
        print("-" * 70)
        for product_id, quantity in products:
            success = await client.add_item(user_id, product_id, quantity)
            if not success:
                print(f"Warning: Failed to add {product_id}")
        print()

        # ─────────────────────────────────────────────────────────────────
        # Test 2: Retrieve cart
        # ─────────────────────────────────────────────────────────────────
        print("\n" + "-" * 70)
        print("TEST 2: Retrieving cart")
        print("-" * 70)
        cart = await client.get_cart(user_id)
        if cart:
            print(f"  Total items in cart: {len(cart.items)}")
        print()

        # ─────────────────────────────────────────────────────────────────
        # Test 3: Add more items (to show updates)
        # ─────────────────────────────────────────────────────────────────
        print("\n" + "-" * 70)
        print("TEST 3: Adding more items to cart")
        print("-" * 70)
        await client.add_item(user_id, "product_001", 2)  # Add same product again
        print()

        # ─────────────────────────────────────────────────────────────────
        # Test 4: Get updated cart
        # ─────────────────────────────────────────────────────────────────
        print("\n" + "-" * 70)
        print("TEST 4: Retrieving updated cart")
        print("-" * 70)
        cart = await client.get_cart(user_id)
        if cart:
            total_items = sum(item.quantity for item in cart.items)
            print(f"  Total quantity in cart: {total_items}")
        print()

        # ─────────────────────────────────────────────────────────────────
        # Test 5: Empty cart
        # ─────────────────────────────────────────────────────────────────
        print("\n" + "-" * 70)
        print("TEST 5: Emptying cart")
        print("-" * 70)
        await client.empty_cart(user_id)
        print()

        # ─────────────────────────────────────────────────────────────────
        # Test 6: Verify cart is empty
        # ─────────────────────────────────────────────────────────────────
        print("\n" + "-" * 70)
        print("TEST 6: Verifying cart is empty")
        print("-" * 70)
        cart = await client.get_cart(user_id)
        if cart:
            if len(cart.items) == 0:
                print("  ✓ Cart is successfully empty!")
            else:
                print(f"  ✗ Cart still contains {len(cart.items)} items")
        print()

        # ─────────────────────────────────────────────────────────────────
        # Test 7: Test with different user
        # ─────────────────────────────────────────────────────────────────
        print("\n" + "-" * 70)
        print("TEST 7: Testing with different user")
        print("-" * 70)
        user_id_2 = "test_user_456"
        await client.add_item(user_id_2, "product_999", 5)
        cart2 = await client.get_cart(user_id_2)
        print()

        print("=" * 70)
        print("All tests completed successfully!")
        print("=" * 70)

    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        print(f"\n✗ Unexpected error: {e}")
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())

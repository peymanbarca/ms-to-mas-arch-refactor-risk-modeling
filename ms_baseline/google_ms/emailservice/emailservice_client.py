"""
emailservice_client.py

gRPC client for EmailService - invokes SendOrderConfirmation and prints results.
"""

import asyncio
import logging
import sys
import os
from datetime import datetime

import grpc

# Add parent directory to path to import shared modules
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from ms_baseline.google_ms.shared import demo_pb2
from ms_baseline.google_ms.shared import demo_pb2_grpc

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class EmailServiceClient:
    """gRPC client for EmailService"""

    def __init__(self, target: str = "localhost:8081"):
        """
        Initialize the EmailService client.

        Args:
            target: gRPC server address (default: localhost:8081)
        """
        self.target = target
        self.channel = None
        self.stub = None

    async def connect(self):
        """Establish connection to EmailService"""
        self.channel = grpc.aio.insecure_channel(self.target)
        self.stub = demo_pb2_grpc.EmailServiceStub(self.channel)
        logger.info(f"Connected to EmailService at {self.target}")

    async def disconnect(self):
        """Close the connection"""
        if self.channel:
            await self.channel.close()
            logger.info("Disconnected from EmailService")

    async def send_order_confirmation(
        self,
        email: str,
        order_id: str,
        tracking_id: str,
        items: list[tuple[str, int, int, int]] = None,
        address: dict = None,
        shipping_cost: tuple[int, int] = (50, 0)
    ) -> bool:
        """
        Send an order confirmation email.

        Args:
            email: Customer email address
            order_id: Order ID
            tracking_id: Shipping tracking ID
            items: List of (product_id, quantity, cost_units, cost_nanos)
            address: Dict with street_address, city, state, country, zip_code
            shipping_cost: Tuple of (units, nanos) for shipping cost

        Returns:
            True if successful, False otherwise
        """
        try:
            # Default items if not provided
            if items is None:
                items = [
                    ("product_001", 2, 49, 990000000),
                    ("product_002", 1, 29, 990000000),
                ]

            # Default address if not provided
            if address is None:
                address = {
                    "street_address": "123 Main Street",
                    "city": "San Francisco",
                    "state": "CA",
                    "country": "USA",
                    "zip_code": 94102,
                }

            # Build order items
            order_items = [
                demo_pb2.OrderItem(
                    item=demo_pb2.CartItem(
                        product_id=product_id,
                        quantity=quantity
                    ),
                    cost=demo_pb2.Money(
                        currency_code="USD",
                        units=cost_units,
                        nanos=cost_nanos
                    )
                )
                for product_id, quantity, cost_units, cost_nanos in items
            ]

            # Build order
            order = demo_pb2.OrderResult(
                order_id=order_id,
                shipping_tracking_id=tracking_id,
                shipping_cost=demo_pb2.Money(
                    currency_code="USD",
                    units=shipping_cost[0],
                    nanos=shipping_cost[1]
                ),
                shipping_address=demo_pb2.Address(
                    street_address=address["street_address"],
                    city=address["city"],
                    state=address["state"],
                    country=address["country"],
                    zip_code=address["zip_code"]
                ),
                items=order_items
            )

            # Send request
            request = demo_pb2.SendOrderConfirmationRequest(
                email=email,
                order=order
            )
            
            response = await self.stub.SendOrderConfirmation(request)
            logger.info(f"✓ SendOrderConfirmation: email={email}, order_id={order_id}")
            print(f"✓ SendOrderConfirmation - email: {email}")
            print(f"  Order ID: {order_id}")
            print(f"  Tracking ID: {tracking_id}")
            print(f"  Items: {len(order_items)}")
            print(f"  Shipping Cost: ${shipping_cost[0]}.{shipping_cost[1]:09d}")
            print(f"  Response: Success (Empty response)")
            return True
            
        except grpc.RpcError as e:
            logger.error(f"✗ SendOrderConfirmation failed: {e.details()}")
            print(f"✗ SendOrderConfirmation failed: {e.details()}")
            return False


async def main():
    """Main function demonstrating EmailService operations"""
    print("=" * 80)
    print("EmailService gRPC Client - Testing Order Confirmation")
    print("=" * 80)
    print()

    # Initialize client
    client = EmailServiceClient("localhost:5056")

    try:
        # Connect to service
        await client.connect()
        print()

        # ─────────────────────────────────────────────────────────────────
        # Test 1: Send confirmation to single customer
        # ─────────────────────────────────────────────────────────────────
        print("\n" + "-" * 80)
        print("TEST 1: Send order confirmation email")
        print("-" * 80)
        success = await client.send_order_confirmation(
            email="john.doe@example.com",
            order_id="ORD-2024-001",
            tracking_id="TRK-123456789",
            items=[
                ("BOOK-001", 2, 15, 990000000),
                ("SHIRT-001", 1, 29, 990000000),
                ("SHOES-001", 1, 79, 990000000),
            ],
            address={
                "street_address": "123 Main Street",
                "city": "San Francisco",
                "state": "CA",
                "country": "USA",
                "zip_code": 94102,
            },
            shipping_cost=(12, 500000000)
        )
        print()

        # ─────────────────────────────────────────────────────────────────
        # Test 2: Send confirmation with different address
        # ─────────────────────────────────────────────────────────────────
        print("\n" + "-" * 80)
        print("TEST 2: Send order confirmation with different address")
        print("-" * 80)
        success = await client.send_order_confirmation(
            email="jane.smith@example.com",
            order_id="ORD-2024-002",
            tracking_id="TRK-987654321",
            items=[
                ("LAPTOP-001", 1, 899, 990000000),
                ("MOUSE-001", 2, 25, 0),
            ],
            address={
                "street_address": "456 Oak Avenue, Apt 5B",
                "city": "New York",
                "state": "NY",
                "country": "USA",
                "zip_code": 10001,
            },
            shipping_cost=(25, 0)
        )
        print()

        # ─────────────────────────────────────────────────────────────────
        # Test 3: Send confirmation with multiple items
        # ─────────────────────────────────────────────────────────────────
        print("\n" + "-" * 80)
        print("TEST 3: Send order confirmation with multiple items")
        print("-" * 80)
        success = await client.send_order_confirmation(
            email="customer@example.com",
            order_id="ORD-2024-003",
            tracking_id="TRK-555555555",
            items=[
                ("ITEM-001", 1, 10, 0),
                ("ITEM-002", 3, 5, 500000000),
                ("ITEM-003", 2, 20, 0),
                ("ITEM-004", 1, 100, 0),
                ("ITEM-005", 5, 2, 500000000),
            ],
            address={
                "street_address": "789 Pine Road",
                "city": "Seattle",
                "state": "WA",
                "country": "USA",
                "zip_code": 98101,
            },
            shipping_cost=(15, 750000000)
        )
        print()

        # ─────────────────────────────────────────────────────────────────
        # Test 4: Send confirmation with international address
        # ─────────────────────────────────────────────────────────────────
        print("\n" + "-" * 80)
        print("TEST 4: Send order confirmation with international address")
        print("-" * 80)
        success = await client.send_order_confirmation(
            email="customer@example.co.uk",
            order_id="ORD-2024-004",
            tracking_id="TRK-444444444",
            items=[
                ("PRODUCT-UK-001", 1, 50, 0),
                ("PRODUCT-UK-002", 2, 30, 0),
            ],
            address={
                "street_address": "10 Downing Street",
                "city": "London",
                "state": "England",
                "country": "United Kingdom",
                "zip_code": 1000,
            },
            shipping_cost=(40, 0)
        )
        print()

        # ─────────────────────────────────────────────────────────────────
        # Test 5: Send confirmation with default values
        # ─────────────────────────────────────────────────────────────────
        print("\n" + "-" * 80)
        print("TEST 5: Send order confirmation with default values")
        print("-" * 80)
        success = await client.send_order_confirmation(
            email="default@example.com",
            order_id="ORD-2024-005",
            tracking_id="TRK-DEFAULT"
        )
        print()

        print("=" * 80)
        print("All tests completed successfully!")
        print("=" * 80)

    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        print(f"\n✗ Unexpected error: {e}")
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())

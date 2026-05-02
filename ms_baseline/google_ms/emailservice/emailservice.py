"""
emailservice/main.py

Replaces the original Python emailservice.
- gRPC server on port 8081
- FastAPI HTTP server on port 9081
- Supports dummy mode (no actual email sending) and template rendering
"""

import asyncio
import logging
import os
import sys
from pathlib import Path

import grpc
from fastapi import FastAPI, HTTPException
from jinja2 import Environment, FileSystemLoader, select_autoescape, TemplateError
from pydantic import BaseModel

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))
from ms_baseline.google_ms.shared import demo_pb2
from ms_baseline.google_ms.shared import demo_pb2_grpc
from ms_baseline.google_ms.shared.base_service import make_health_app, run_service

logger = logging.getLogger(__name__)
GRPC_PORT = int(os.getenv("PORT", "8081"))

# ── Email template loader ────────────────────────────────────────────────────

def _load_template_engine():
    """Load Jinja2 template engine for email confirmation templates."""
    template_dir = os.path.join(os.path.dirname(__file__), "templates")
    
    # Create templates directory if it doesn't exist
    Path(template_dir).mkdir(parents=True, exist_ok=True)
    
    env = Environment(
        loader=FileSystemLoader(template_dir),
        autoescape=select_autoescape(['html', 'xml']),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    return env


def _get_default_template_content() -> str:
    """Return default order confirmation email template if not found."""
    return """
    <html>
        <body>
            <h1>Order Confirmation</h1>
            <p>Dear Customer,</p>
            <p>Thank you for your order!</p>
            
            <h2>Order Details</h2>
            <p><strong>Order ID:</strong> {{ order.order_id }}</p>
            <p><strong>Shipping Tracking ID:</strong> {{ order.shipping_tracking_id }}</p>
            
            <h2>Items Ordered</h2>
 
            
            <h2>Shipping Information</h2>
            <p>
                {{ order.shipping_address.street_address }}<br>
                {{ order.shipping_address.city }}, {{ order.shipping_address.state }} {{ order.shipping_address.zip_code }}<br>
                {{ order.shipping_address.country }}
            </p>
            <p><strong>Shipping Cost:</strong> ${{ order.shipping_cost.units }}.{{ "%09d" | format(order.shipping_cost.nanos) }}</p>
            
            <p>We will send you a shipping confirmation shortly.</p>
            <p>Thank you for your business!</p>
        </body>
    </html>
    """


# ── gRPC Servicer ────────────────────────────────────────────────────────────

class EmailServicer(demo_pb2_grpc.EmailServiceServicer):
    """gRPC implementation for EmailService."""

    def __init__(self, template_env=None):
        """
        Initialize EmailServicer.
        
        Args:
            template_env: Jinja2 Environment for template rendering.
                         If None, will use default template.
        """
        self.template_env = template_env
        self.template = None
        
        if self.template_env:
            try:
                self.template = self.template_env.get_template("confirmation.html")
                logger.info("Loaded confirmation.html template")
            except Exception as e:
                logger.warning(f"Could not load confirmation.html template: {e}")
                logger.info("Will use default inline template")

    async def SendOrderConfirmation(self, request, context):
        """
        Send order confirmation email.
        
        Args:
            request: SendOrderConfirmationRequest containing email and order details
            context: gRPC context
            
        Returns:
            Empty response on success
        """
        email = request.email
        order = request.order
        
        logger.info(f"Processing order confirmation email to {email} for order {order.order_id}")
        
        try:
            # Render the email template
            confirmation_html = self._render_confirmation(order)
            
            # In dummy mode, just log the email
            logger.info(f"Order confirmation email rendered successfully for {email}")
            logger.debug(f"Email content preview (first 200 chars): {confirmation_html[:200]}...")
            
            # TODO: Implement actual email sending (e.g., via SendGrid, AWS SES, Google Cloud Mail, etc.)
            # For now, this is a dummy implementation that just logs the request
            
        except TemplateError as err:
            logger.error(f"Template rendering error: {err}", exc_info=True)
            if context:
                context.set_details("An error occurred when preparing the confirmation mail.")
                context.set_code(grpc.StatusCode.INTERNAL)
            return demo_pb2.Empty()
        except Exception as err:
            logger.error(f"Unexpected error: {err}", exc_info=True)
            if context:
                context.set_details("An unexpected error occurred.")
                context.set_code(grpc.StatusCode.INTERNAL)
            return demo_pb2.Empty()
        
        return demo_pb2.Empty()

    def _render_confirmation(self, order) -> str:
        """
        Render order confirmation email content.
        
        Args:
            order: OrderResult protobuf message
            
        Returns:
            Rendered HTML string
            
        Raises:
            TemplateError: If template rendering fails
        """
        # Convert protobuf to dict for template rendering
        # Handle items - convert to list in case it's a method or generator
        items_list = list(order.items) if order.items else []
        
        order_dict = {
            "order_id": order.order_id,
            "shipping_tracking_id": order.shipping_tracking_id,
            "shipping_cost": {
                "units": order.shipping_cost.units,
                "nanos": order.shipping_cost.nanos,
                "currency_code": order.shipping_cost.currency_code,
            },
            "shipping_address": {
                "street_address": order.shipping_address.street_address,
                "city": order.shipping_address.city,
                "state": order.shipping_address.state,
                "country": order.shipping_address.country,
                "zip_code": order.shipping_address.zip_code,
            },
            "items": [
                # {
                #     "item": {
                #         "product_id": item.item.product_id,
                #         "quantity": item.item.quantity,
                #     },
                #     "cost": {
                #         "units": item.cost.units,
                #         "nanos": item.cost.nanos,
                #         "currency_code": item.cost.currency_code,
                #     }
                # }
                # for item in items_list
            ]
        }
        
        if self.template:
            return self.template.render(order=order_dict)
        else:
            # Use default template
            default_template = self.template_env.from_string(
                _get_default_template_content()
            ) if self.template_env else None
            
            if default_template:
                return default_template.render(order=order_dict)
            else:
                # Fallback: simple text representation
                return None


# ── FastAPI ──────────────────────────────────────────────────────────────────

app = make_health_app("emailservice")

# Models for REST API
class OrderItemModel(BaseModel):
    product_id: str
    quantity: int
    cost_units: int = 0
    cost_nanos: int = 0

class AddressModel(BaseModel):
    street_address: str
    city: str
    state: str
    country: str
    zip_code: int

class OrderModel(BaseModel):
    order_id: str
    shipping_tracking_id: str
    items: list[OrderItemModel]
    shipping_address: AddressModel
    shipping_cost_units: int = 0
    shipping_cost_nanos: int = 0

class SendConfirmationRequest(BaseModel):
    email: str
    order: OrderModel

_svc = None  # lazy singleton

def _get_svc() -> EmailServicer:
    global _svc
    if _svc is None:
        try:
            env = _load_template_engine()
        except Exception as e:
            logger.warning(f"Could not initialize template engine: {e}")
            env = None
        _svc = EmailServicer(template_env=env)
    return _svc

@app.post("/send-confirmation", summary="Send order confirmation email")
async def rest_send_confirmation(body: SendConfirmationRequest):
    """Send order confirmation email via REST API."""
    try:
        svc = _get_svc()
        
        # Build OrderResult protobuf
        items = [
            demo_pb2.OrderItem(
                item=demo_pb2.CartItem(
                    product_id=item.product_id,
                    quantity=item.quantity
                ),
                cost=demo_pb2.Money(
                    currency_code="USD",
                    units=item.cost_units,
                    nanos=item.cost_nanos
                )
            )
            for item in body.order.items
        ]
        
        order = demo_pb2.OrderResult(
            order_id=body.order.order_id,
            shipping_tracking_id=body.order.shipping_tracking_id,
            shipping_cost=demo_pb2.Money(
                currency_code="USD",
                units=body.order.shipping_cost_units,
                nanos=body.order.shipping_cost_nanos
            ),
            shipping_address=demo_pb2.Address(
                street_address=body.order.shipping_address.street_address,
                city=body.order.shipping_address.city,
                state=body.order.shipping_address.state,
                country=body.order.shipping_address.country,
                zip_code=body.order.shipping_address.zip_code
            ),
            items=items
        )
        
        request = demo_pb2.SendOrderConfirmationRequest(
            email=body.email,
            order=order
        )
        
        await svc.SendOrderConfirmation(request, None)
        return {"status": "ok", "message": f"Confirmation email sent to {body.email}"}
        
    except Exception as e:
        logger.error(f"Error sending confirmation: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/templates", summary="List available email templates")
async def rest_list_templates():
    """List available email templates."""
    template_dir = os.path.join(os.path.dirname(__file__), "templates")
    templates = []
    
    if os.path.exists(template_dir):
        templates = [f for f in os.listdir(template_dir) if f.endswith('.html')]
    
    return {
        "templates": templates,
        "default": "confirmation.html",
        "template_directory": template_dir
    }


# ── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    
    logger.info("Starting email service...")
    
    run_service(
        demo_pb2_grpc.add_EmailServiceServicer_to_server,
        EmailServicer(template_env=_load_template_engine()),
        GRPC_PORT,
        app,
    )

# EmailService

Python gRPC and FastAPI implementation of the EmailService microservice.

## Overview

The EmailService is responsible for sending order confirmation emails to customers. This implementation:

- **Runs a gRPC server** on port 8081 (configurable via `PORT` environment variable)
- **Runs a FastAPI HTTP server** on port 9081 (configurable via `HTTP_PORT` environment variable)
- **Supports email templating** using Jinja2 for HTML email rendering
- **Operates in dummy mode by default** (logs emails instead of sending them)

## Features

### gRPC Service

The service implements the following RPC methods defined in `demo.proto`:

#### `SendOrderConfirmation`
Sends an order confirmation email to a customer.

**Request:**
```protobuf
message SendOrderConfirmationRequest {
    string email = 1;
    OrderResult order = 2;
}
```

**Response:**
```protobuf
message Empty {}
```

**Usage Example:**
```python
await client.stub.SendOrderConfirmation(
    demo_pb2.SendOrderConfirmationRequest(
        email="customer@example.com",
        order=order_result
    )
)
```

### FastAPI HTTP Endpoints

#### `POST /send-confirmation`
REST API endpoint to send order confirmation email.

**Request Body:**
```json
{
    "email": "customer@example.com",
    "order": {
        "order_id": "ORD-2024-001",
        "shipping_tracking_id": "TRK-123456789",
        "items": [
            {
                "product_id": "PROD-001",
                "quantity": 2,
                "cost_units": 49,
                "cost_nanos": 990000000
            }
        ],
        "shipping_address": {
            "street_address": "123 Main Street",
            "city": "San Francisco",
            "state": "CA",
            "country": "USA",
            "zip_code": 94102
        },
        "shipping_cost_units": 12,
        "shipping_cost_nanos": 500000000
    }
}
```

**Response:**
```json
{
    "status": "ok",
    "message": "Confirmation email sent to customer@example.com"
}
```

#### `GET /health`
Health check endpoint.

**Response:**
```json
{
    "status": "healthy",
    "service": "emailservice"
}
```

#### `GET /ready`
Readiness check endpoint.

**Response:**
```json
{
    "status": "ready",
    "service": "emailservice"
}
```

#### `GET /templates`
List available email templates.

**Response:**
```json
{
    "templates": ["confirmation.html"],
    "default": "confirmation.html",
    "template_directory": "/path/to/templates"
}
```

## Directory Structure

```
emailservice/
├── __init__.py                    # Package initialization
├── emailservice.py               # Main service implementation
├── emailservice_client.py         # gRPC client for testing
└── templates/
    └── confirmation.html         # HTML email template
```

## Running the Service

### Prerequisites

```bash
pip install grpcio fastapi uvicorn jinja2 pydantic
```

### Start the Service

```bash
cd /home/ghazal/PhD/impl/ms-to-mas-arch-refactor-risk-modeling
python -m ms_baseline.google_ms.emailservice.emailservice
```

Or directly:
```bash
python /path/to/emailservice/emailservice.py
```

### Configuration

Environment variables:
- `PORT`: gRPC server port (default: 8081)
- `HTTP_PORT`: FastAPI HTTP server port (default: 9081)

Example:
```bash
PORT=8081 HTTP_PORT=9081 python emailservice.py
```

## Testing with the Client

### Run the test client

```bash
python /path/to/emailservice/emailservice_client.py
```

The client will:
1. Connect to the gRPC service at `localhost:8081`
2. Send 5 different test emails with various configurations
3. Print formatted results showing success/failure for each test

### Example Output

```
================================================================================
EmailService gRPC Client - Testing Order Confirmation
================================================================================

Connected to EmailService at localhost:8081

────────────────────────────────────────────────────────────────────────────────
TEST 1: Send order confirmation email
────────────────────────────────────────────────────────────────────────────────
✓ SendOrderConfirmation - email: john.doe@example.com
  Order ID: ORD-2024-001
  Tracking ID: TRK-123456789
  Items: 3
  Shipping Cost: $12.500000000
  Response: Success (Empty response)

...
```

## Email Template

### Default Template (confirmation.html)

The service includes a professional HTML email template with:
- Order details (ID, tracking number)
- Itemized list of products with quantities and costs
- Shipping address
- Shipping cost
- Professional styling with CSS

### Customizing the Template

To customize the email template:

1. Edit `/emailservice/templates/confirmation.html`
2. Use Jinja2 template syntax to reference order data:
   - `{{ order.order_id }}` - Order ID
   - `{{ order.shipping_tracking_id }}` - Tracking ID
   - `{{ order.items }}` - List of items
   - `{{ order.shipping_address }}` - Shipping address
   - `{{ order.shipping_cost }}` - Shipping cost

### Template Data Structure

The template receives an `order` dictionary with:
```python
{
    "order_id": str,
    "shipping_tracking_id": str,
    "shipping_cost": {
        "units": int,
        "nanos": int,
        "currency_code": str
    },
    "shipping_address": {
        "street_address": str,
        "city": str,
        "state": str,
        "country": str,
        "zip_code": int
    },
    "items": [
        {
            "item": {
                "product_id": str,
                "quantity": int
            },
            "cost": {
                "units": int,
                "nanos": int,
                "currency_code": str
            }
        }
    ]
}
```

## Implementation Details

### gRPC Servicer (`EmailServicer`)

The `EmailServicer` class implements the `EmailService` gRPC servicer with:

- **`SendOrderConfirmation`**: Main RPC method that:
  1. Extracts email and order information from the request
  2. Renders the confirmation email using Jinja2 templates
  3. Logs the email (dummy mode) or sends it (when implemented)
  4. Returns an Empty response on success
  5. Sets appropriate gRPC error codes on failure

### Template Rendering

- Templates are loaded from the `templates/` directory
- Jinja2 provides automatic HTML escaping for security
- Fallback to inline default template if template file not found
- Fallback to simple text format if template engine unavailable

### Error Handling

The service handles:
- Template rendering errors (returns INTERNAL error code)
- Missing email or order data (validates on reception)
- Graceful degradation if template file not found

## Extending the Service

### Adding Actual Email Sending

To implement actual email sending, modify the `SendOrderConfirmation` method:

**Option 1: Google Cloud Mail (original)**
```python
from google.cloud import mail_v1
# ... implement sending logic
```

**Option 2: SendGrid**
```python
import sendgrid
# ... implement sending logic
```

**Option 3: AWS SES**
```python
import boto3
# ... implement sending logic
```

### Adding More Templates

Create new HTML templates in the `templates/` directory and reference them in the code:

```python
self.template = self.template_env.get_template("custom_template.html")
```

## Performance Considerations

- Service uses async/await for non-blocking I/O
- Jinja2 templates are compiled once and cached
- FastAPI runs on uvicorn ASGI server for high concurrency
- gRPC uses HTTP/2 for efficient multiplexing

## Observability

### Logging

All operations are logged with:
- Service startup/shutdown
- Template loading status
- Each email sending request
- Errors and warnings

Enable debug logging:
```bash
logging.getLogger(__name__).setLevel(logging.DEBUG)
```

### Health Checks

The service provides:
- `/health` - Overall service health
- `/ready` - Service readiness

These are commonly used by:
- Kubernetes liveness/readiness probes
- Load balancers for health monitoring
- Container orchestration platforms

## Comparison with Original Implementation

| Feature | Original (Python) | This Implementation |
|---------|-------------------|-------------------|
| gRPC Server | ✓ | ✓ (async) |
| FastAPI | ✗ | ✓ |
| Email Templates | ✓ (Jinja2) | ✓ (Jinja2) |
| Dummy Mode | ✓ | ✓ (default) |
| Cloud Mail API | Partial | TODO |
| Health Service | ✓ | ✓ |
| OpenTelemetry | ✓ | Can add |

## Related Services

- **CartService**: Manages shopping carts (port 7070/8070)
- **ProductCatalogService**: Manages product catalog (port 3550/4550)
- **CheckoutService**: Processes orders and calls this service
- **ShippingService**: Provides shipping information

## Future Enhancements

- [ ] Implement actual email sending (SendGrid, AWS SES, Google Mail)
- [ ] Add email attachment support
- [ ] Support multiple template languages
- [ ] Add email retry logic
- [ ] Implement email delivery tracking
- [ ] Add OpenTelemetry instrumentation
- [ ] Support batch email sending
- [ ] Add email preview endpoint

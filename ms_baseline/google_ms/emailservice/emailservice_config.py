"""
emailservice_config.py

Configuration and constants for EmailService.
Can be imported by other services that need to communicate with EmailService.
"""

import os

# ── Service Configuration ────────────────────────────────────────────────────

# gRPC Configuration
GRPC_HOST = os.getenv("EMAIL_SERVICE_HOST", "localhost")
GRPC_PORT = int(os.getenv("EMAIL_SERVICE_GRPC_PORT", "8081"))
GRPC_ADDRESS = f"{GRPC_HOST}:{GRPC_PORT}"

# HTTP Configuration
HTTP_HOST = os.getenv("EMAIL_SERVICE_HTTP_HOST", "0.0.0.0")
HTTP_PORT = int(os.getenv("EMAIL_SERVICE_HTTP_PORT", "9081"))
HTTP_BASE_URL = f"http://{GRPC_HOST}:{HTTP_PORT}"

# Service Metadata
SERVICE_NAME = "emailservice"
SERVICE_VERSION = "1.0.0"

# ── Paths ────────────────────────────────────────────────────────────────────

SERVICE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = os.path.join(SERVICE_DIR, "templates")
DEFAULT_TEMPLATE_NAME = "confirmation.html"

# ── Constants ────────────────────────────────────────────────────────────────

# Email templates
TEMPLATES = {
    "confirmation": {
        "filename": "confirmation.html",
        "subject": "Order Confirmation",
        "content_type": "text/html",
    }
}

# Default address (used if not provided)
DEFAULT_ADDRESS = {
    "street_address": "123 Main Street",
    "city": "San Francisco",
    "state": "CA",
    "country": "USA",
    "zip_code": 94102,
}

# Default shipping cost
DEFAULT_SHIPPING_COST = (50, 0)  # $50.00 in (units, nanos)

# ── Debug/Logging ───────────────────────────────────────────────────────────

DEBUG_MODE = os.getenv("DEBUG", "false").lower() == "true"
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# ── Feature Flags ────────────────────────────────────────────────────────────

# Dummy mode: if True, emails are logged but not actually sent
DUMMY_MODE = os.getenv("DUMMY_MODE", "true").lower() == "true"

# Enable template rendering
ENABLE_TEMPLATES = os.getenv("ENABLE_TEMPLATES", "true").lower() == "true"

# ── Helper Functions ────────────────────────────────────────────────────────

def get_grpc_channel_credentials(use_ssl: bool = False):
    """
    Get gRPC channel credentials based on configuration.
    
    Args:
        use_ssl: Whether to use SSL (for production)
        
    Returns:
        grpc.ChannelCredentials or None for insecure
    """
    if use_ssl:
        import grpc
        return grpc.ssl_channel_credentials()
    return None


def get_service_info() -> dict:
    """Get service information dictionary."""
    return {
        "name": SERVICE_NAME,
        "version": SERVICE_VERSION,
        "grpc_address": GRPC_ADDRESS,
        "http_base_url": HTTP_BASE_URL,
        "dummy_mode": DUMMY_MODE,
        "templates_dir": TEMPLATES_DIR,
    }


# ── Example Usage ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import json
    
    print("EmailService Configuration")
    print("=" * 50)
    print(json.dumps(get_service_info(), indent=2))
    print("\nEnvironment Variables:")
    print(f"  EMAIL_SERVICE_HOST: {GRPC_HOST}")
    print(f"  EMAIL_SERVICE_GRPC_PORT: {GRPC_PORT}")
    print(f"  EMAIL_SERVICE_HTTP_HOST: {HTTP_HOST}")
    print(f"  EMAIL_SERVICE_HTTP_PORT: {HTTP_PORT}")
    print(f"  DUMMY_MODE: {DUMMY_MODE}")
    print(f"  DEBUG_MODE: {DEBUG_MODE}")
    print(f"  LOG_LEVEL: {LOG_LEVEL}")

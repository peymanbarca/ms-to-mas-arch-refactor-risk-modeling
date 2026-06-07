"""
jwt_helper.py — JWT token generation and validation.

The C++ UserHandler.h uses libjwt to produce a HS256-signed JWT containing:
  { "user_id": <i64>, "username": <str>, "timestamp": <unix_seconds> }

We replicate this payload exactly so tokens are cross-compatible with the
NGINX Lua JWT validation middleware in the original docker-compose setup.

The secret comes from service-config.json ["UserService"]["secret"].
Expiry is configurable via ["UserService"]["jwt_expiry_seconds"] (default 3600).
"""

import time
import jwt as pyjwt


def generate_token(
    user_id: int,
    username: str,
    secret: str,
    expiry_seconds: int = 3600,
) -> str:
    """
    Return a signed JWT string.

    Payload:
      user_id   : i64  — numeric user identifier
      username  : str
      timestamp : int  — Unix seconds at token creation  (matches C++)
      exp       : int  — standard JWT expiry claim
    """
    now = int(time.time())
    payload = {
        "user_id":   user_id,
        "username":  username,
        "timestamp": now,
        "exp":       now + expiry_seconds,
    }
    return pyjwt.encode(payload, secret, algorithm="HS256")


def decode_token(token: str, secret: str) -> dict:
    """
    Decode and verify a JWT. Returns the payload dict.
    Raises jwt.ExpiredSignatureError or jwt.InvalidTokenError on failure.
    """
    return pyjwt.decode(token, secret, algorithms=["HS256"])
"""
url_shortener.py — Core URL shortening logic.

Faithful port of the C++ UrlShortenHandler.h token generation algorithm:

  1. Compute MD5 of the expanded (original) URL.
  2. Base62-encode the raw 16 MD5 bytes.
  3. Take the first 10 characters as the short token.
  4. Prepend the configured hostname (e.g. "http://short-url/").

The same expanded URL always maps to the same shortened URL (deterministic),
which matches the C++ implementation and makes MongoDB upserts idempotent.

Two Redis cache directions are maintained:
  - expanded_url  → shortened_url   (for ComposeUrls fast path)
  - shortened_url → expanded_url    (for GetExtendedUrls fast path)
"""

import hashlib
import string

# Base62 alphabet used in the C++ implementation
_B62_ALPHABET = string.digits + string.ascii_uppercase + string.ascii_lowercase
_B62_LEN = len(_B62_ALPHABET)   # 62
_TOKEN_LEN = 10                  # characters taken from the base62 string


def _md5_bytes(text: str) -> bytes:
    return hashlib.md5(text.encode("utf-8")).digest()   # 16 raw bytes


def _base62_encode(raw_bytes: bytes) -> str:
    """Encode a bytes object as a base62 string (big-endian)."""
    n = int.from_bytes(raw_bytes, byteorder="big")
    if n == 0:
        return _B62_ALPHABET[0]
    chars = []
    while n:
        n, rem = divmod(n, _B62_LEN)
        chars.append(_B62_ALPHABET[rem])
    return "".join(reversed(chars))


def make_short_token(expanded_url: str) -> str:
    """Return the 10-char base62 token for the given URL."""
    raw = _md5_bytes(expanded_url)
    b62 = _base62_encode(raw)
    # Pad with leading '0' if shorter than TOKEN_LEN (very unlikely for 16 bytes)
    return b62.ljust(_TOKEN_LEN, "0")[:_TOKEN_LEN]


def make_shortened_url(hostname: str, expanded_url: str) -> str:
    """Return the full shortened URL: hostname + token."""
    token = make_short_token(expanded_url)
    # Ensure hostname ends with /
    base = hostname if hostname.endswith("/") else hostname + "/"
    return base + token

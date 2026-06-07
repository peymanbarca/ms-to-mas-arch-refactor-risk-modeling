"""
password.py — Password hashing helpers.

The C++ UserHandler.h uses:
  1. A random 32-byte salt (hex-encoded to 64 chars).
  2. SHA-256 of (password + salt) as the stored password_hashed value.

We faithfully replicate this scheme so that:
  - The MongoDB document layout is identical to the C++ original.
  - Any C++ service that reads or writes user documents remains compatible.

Document fields written:
  { "salt": "<64-char hex>", "password_hashed": "<64-char hex SHA-256>" }

Note: The C++ code uses picosha2 (a header-only SHA-256 library). We use
hashlib.sha256 which is wire-compatible — same algorithm, same output.
"""

import hashlib
import os


def generate_salt(length: int = 32) -> str:
    """Return a random hex-encoded salt string (64 chars for 32 bytes)."""
    return os.urandom(length).hex()


def hash_password(password: str, salt: str) -> str:
    """
    Return SHA-256 hex digest of (password + salt).

    Matches the C++ picosha2::hash256_hex_string(password + salt) call.
    """
    combined = (password + salt).encode("utf-8")
    return hashlib.sha256(combined).hexdigest()


def verify_password(password: str, salt: str, stored_hash: str) -> bool:
    """Return True if password + salt hashes to stored_hash."""
    return hash_password(password, salt) == stored_hash
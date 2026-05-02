"""
shippingservice/quote.py

Direct Python port of the Go shippingservice's quote.go helper file.

Go originals:
─────────────────────────────────────────────────────────────────────────────
  type Quote struct {
      Dollars uint32
      Cents   uint32
  }

  func CreateQuoteFromCount(count int) Quote {
      // Generate a fake quote based on number of items
      switch {
      case count == 0:
          return Quote{}
      case count > 0 && count < 3:
          return Quote{8, 99}
      case count >= 3 && count < 5:
          return Quote{15, 99}
      case count >= 5 && count < 8:
          return Quote{23, 99}
      case count >= 8 && count < 10:
          return Quote{31, 99}
      default:
          return Quote{39, 99}
      }
  }

  func CreateTrackingId(baseAddress string) string {
      return fmt.Sprintf("%s-%d-%s",
          generateVendorPrefix(),
          8digits(hash(baseAddress)),
          randomLetters(2))
  }

  func generateVendorPrefix() string {
      // returns a 2-letter uppercase prefix e.g. "FE"
      ...
  }
─────────────────────────────────────────────────────────────────────────────

The exact pricing tiers and tracking-ID format are faithfully reproduced.
"""

from __future__ import annotations

import hashlib
import random
import string
from dataclasses import dataclass


# ── Quote ─────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Quote:
    """
    Go equivalent:
        type Quote struct {
            Dollars uint32
            Cents   uint32
        }
    """
    dollars: int = 0
    cents: int   = 0

    @property
    def nanos(self) -> int:
        """
        The proto Money.nanos field value.

        Go:  int32(quote.Cents * 10000000)
        """
        return self.cents * 10_000_000


# Pricing tiers – identical to the Go switch statement in CreateQuoteFromCount.
#
#   count == 0          → $0.00
#   1 <= count < 3      → $8.99
#   3 <= count < 5      → $15.99
#   5 <= count < 8      → $23.99
#   8 <= count < 10     → $31.99
#   count >= 10         → $39.99
_TIERS: list[tuple[int, Quote]] = [
    (0,   Quote(0,  0)),
    (1,   Quote(8,  99)),
    (3,   Quote(15, 99)),
    (5,   Quote(23, 99)),
    (8,   Quote(31, 99)),
    (10,  Quote(39, 99)),
]


def create_quote_from_count(count: int) -> Quote:
    """
    Go: func CreateQuoteFromCount(count int) Quote

    Produces a shipping cost estimate based on the total number of items.
    The tiers match the Go switch statement exactly.

    Args:
        count: Total number of items across all cart entries.

    Returns:
        A Quote with dollars and cents fields.

    Examples:
        create_quote_from_count(0)  → Quote(0,  0)   → $0.00
        create_quote_from_count(2)  → Quote(8,  99)  → $8.99
        create_quote_from_count(4)  → Quote(15, 99)  → $15.99
        create_quote_from_count(6)  → Quote(23, 99)  → $23.99
        create_quote_from_count(9)  → Quote(31, 99)  → $31.99
        create_quote_from_count(12) → Quote(39, 99)  → $39.99
    """
    result = Quote(0, 0)
    for threshold, quote in _TIERS:
        if count >= threshold:
            result = quote
    return result


# ── Tracking ID ───────────────────────────────────────────────────────────────

def _generate_vendor_prefix() -> str:
    """
    Go: func generateVendorPrefix() string
    Returns a random 2-letter uppercase string, e.g. "FE", "UX".
    """
    return "".join(random.choices(string.ascii_uppercase, k=2))


def _hash_address(address: str) -> int:
    """
    Go: func hash(address string) uint32
    8-digit numeric hash of the address string.
    Using SHA-256 truncated to 8 decimal digits for determinism.
    """
    h = hashlib.sha256(address.encode("utf-8")).hexdigest()
    # Take first 8 hex chars → convert to decimal and clamp to 8 digits
    numeric = int(h[:8], 16) % 100_000_000   # always 0–99,999,999
    return numeric


def _random_letters(n: int) -> str:
    """
    Go: func randomLetters(n int) string
    Returns n random uppercase ASCII letters.
    """
    return "".join(random.choices(string.ascii_uppercase, k=n))


def create_tracking_id(base_address: str) -> str:
    """
    Go: func CreateTrackingId(baseAddress string) string

    Generates a mock shipment tracking ID in the format:
        <2-letter vendor prefix>-<8-digit hash>-<2 random letters>
    e.g. "FE-12345678-UX"

    The 8-digit portion is deterministic (SHA-256 of the address),
    while the prefix and suffix are random each call – matching the Go
    implementation which uses random.Intn for both.

    Args:
        base_address: Formatted as "street, city, state"
                      (Go: fmt.Sprintf("%s, %s, %s", street, city, state))

    Returns:
        Tracking ID string.
    """
    prefix  = _generate_vendor_prefix()
    hash_8d = _hash_address(base_address)
    suffix  = _random_letters(2)
    return f"{prefix}-{hash_8d:08d}-{suffix}"
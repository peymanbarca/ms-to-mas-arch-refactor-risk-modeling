"""
checkoutservice/money.py

Complete Python port of Go checkoutservice/money/money.go.

Go source (verbatim logic):
────────────────────────────────────────────────────────────────────────────
const nanosPerUnit = 1_000_000_000

var (
    ErrInvalidValue       = errors.New("one of the specified money values is invalid")
    ErrMismatchedCurrency = errors.New("currencies do not match")
)

func IsValid(m Money) bool {
    return signMatches(m) && validNanos(m.GetNanos())
}

func signMatches(m Money) bool {
    return m.GetNanos() == 0 || m.GetUnits() == 0 || (m.GetNanos() < 0) == (m.GetUnits() < 0)
}

func validNanos(nanos int32) bool {
    return -999999999 <= nanos && nanos <= 999999999
}

func Sum(l, r Money) (Money, error) {
    if l.GetCurrencyCode() != r.GetCurrencyCode() {
        return Money{}, ErrMismatchedCurrency
    }
    units := l.GetUnits() + r.GetUnits()
    nanos := l.GetNanos() + r.GetNanos()
    if (units == 0 && nanos == 0) || (units > 0 && nanos >= 0) || (units < 0 && nanos <= 0) {
        units += int64(nanos / nanosPerUnit)
        nanos  = nanos % nanosPerUnit
    } else {
        if units > 0 {
            units--
            nanos += nanosPerUnit
        } else {
            units++
            nanos -= nanosPerUnit
        }
    }
    return Money{CurrencyCode: l.GetCurrencyCode(), Units: units, Nanos: nanos}, nil
}

func Must(v Money, err error) Money {
    if err != nil { panic(err) }
    return v
}

func MultiplySlow(m Money, n uint32) Money {
    out := m
    for n > 1 {
        out = Must(Sum(out, m))
        n--
    }
    return out
}
────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations
from dataclasses import dataclass

# ── Constants ─────────────────────────────────────────────────────────────────
NANOS_PER_UNIT: int = 1_000_000_000   # Go: nanosPerUnit = 1_000_000_000


# ── Errors ────────────────────────────────────────────────────────────────────
class MoneyError(Exception):
    """Base money arithmetic error."""

class InvalidValueError(MoneyError):
    """Go: ErrInvalidValue"""

class MismatchedCurrencyError(MoneyError):
    """Go: ErrMismatchedCurrency"""


# ── Money dataclass ───────────────────────────────────────────────────────────
@dataclass
class Money:
    """Lightweight mirror of the proto Money message used for pure arithmetic."""
    currency_code: str
    units: int    # int64 in Go
    nanos: int    # int32 in Go


# ── Validation helpers ────────────────────────────────────────────────────────
def valid_nanos(nanos: int) -> bool:
    """Go: func validNanos(nanos int32) bool"""
    return -999_999_999 <= nanos <= 999_999_999


def sign_matches(m: Money) -> bool:
    """Go: func signMatches(m Money) bool"""
    return m.nanos == 0 or m.units == 0 or (m.nanos < 0) == (m.units < 0)


def is_valid(m: Money) -> bool:
    """Go: func IsValid(m Money) bool"""
    return sign_matches(m) and valid_nanos(m.nanos)


# ── Sum ───────────────────────────────────────────────────────────────────────
def money_sum(left: Money, right: Money) -> Money:
    """
    Go: func Sum(l, r Money) (Money, error)

    Adds two Money values with correct nanos carry/borrow logic.
    Raises MismatchedCurrencyError if currency codes differ.
    """
    if left.currency_code != right.currency_code:
        raise MismatchedCurrencyError(
            f"currencies do not match: {left.currency_code!r} vs {right.currency_code!r}"
        )

    units = left.units + right.units
    nanos = left.nanos + right.nanos

    # Go same-sign branch:
    # (units == 0 && nanos == 0) || (units > 0 && nanos >= 0) || (units < 0 && nanos <= 0)
    same_sign = (
        (units == 0 and nanos == 0)
        or (units > 0 and nanos >= 0)
        or (units < 0 and nanos <= 0)
    )

    if same_sign:
        units += nanos // NANOS_PER_UNIT
        nanos  = nanos  % NANOS_PER_UNIT
    else:
        # Different signs — borrow one unit to align
        if units > 0:
            units -= 1
            nanos += NANOS_PER_UNIT
        else:
            units += 1
            nanos -= NANOS_PER_UNIT

    return Money(currency_code=left.currency_code, units=units, nanos=nanos)


# ── Must ──────────────────────────────────────────────────────────────────────
def money_must(value: Money) -> Money:
    """
    Go: func Must(v Money, err error) Money

    In Python we raise inside money_sum instead of returning (val, err),
    so Must just validates and returns the value.
    """
    if value is None:
        raise InvalidValueError("money_must: received None")
    return value


# ── MultiplySlow ──────────────────────────────────────────────────────────────
def money_multiply_slow(m: Money, n: int) -> Money:
    """
    Go: func MultiplySlow(m Money, n uint32) Money

    Multiplies by n via repeated addition. O(n) — suitable for small cart quantities.
    """
    if n == 0:
        return Money(currency_code=m.currency_code, units=0, nanos=0)
    out = Money(currency_code=m.currency_code, units=m.units, nanos=m.nanos)
    for _ in range(n - 1):
        out = money_must(money_sum(out, m))
    return out


# ── Helpers ───────────────────────────────────────────────────────────────────
def zero_money(currency_code: str) -> Money:
    return Money(currency_code=currency_code, units=0, nanos=0)


def proto_to_money(proto_money) -> Money:
    """Convert a demo_pb2.Money proto to a Money dataclass."""
    return Money(
        currency_code=proto_money.currency_code,
        units=proto_money.units,
        nanos=proto_money.nanos,
    )


def money_to_proto(m: Money, pb2_money_cls):
    """Convert a Money dataclass back to a demo_pb2.Money."""
    return pb2_money_cls(currency_code=m.currency_code, units=m.units, nanos=m.nanos)


def format_money(m: Money) -> str:
    """Human-readable format: 'USD 12.99'"""
    cents = abs(m.nanos) // 10_000_000
    sign  = "-" if (m.units < 0 or m.nanos < 0) else ""
    return f"{m.currency_code} {sign}{abs(m.units)}.{cents:02d}"
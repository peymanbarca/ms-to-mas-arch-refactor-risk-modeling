"""
paymentservice/card_validator.py

Direct Python port of the Node.js charge.js card-validation logic.

Node.js originals (charge.js):
─────────────────────────────────────────────────────────────────────────────
  // Luhn algorithm to validate card number
  function isValidLuhn(number) {
      let s = 0;
      let doubleDigit = false;
      for (let i = number.length - 1; i >= 0; i--) {
          let digit = +number[i];
          if (doubleDigit) {
              digit *= 2;
              if (digit > 9) digit -= 9;
          }
          s += digit;
          doubleDigit = !doubleDigit;
      }
      return s % 10 === 0;
  }

  // Check card expiry
  function isExpired(year, month) {
      return year < currentYear ||
             (year === currentYear && month < currentMonth);
  }

  // Detect card type from number prefix + length
  const cardTypes = {
      'Visa':             /^4[0-9]{12}(?:[0-9]{3})?$/,
      'MasterCard':       /^5[1-5][0-9]{14}$/,
      'American Express': /^3[47][0-9]{13}$/,
      'Discover':         /^6(?:011|5[0-9]{2})[0-9]{12}$/,
  };

  // Main charge function
  function charge(request) {
      const { amount, credit_card: creditCard } = request;
      const cardNumber = creditCard.credit_card_number.replace(/ /g, '');

      if (!isValidLuhn(cardNumber))
          throw new CardError('Credit card info is invalid.');
      if (isExpired(creditCard.credit_card_expiration_year,
                    creditCard.credit_card_expiration_month))
          throw new CardError(`The credit card (4xxx) expired on ${...}`);

      let cardType;
      for (const [type, re] of Object.entries(cardTypes)) {
          if (re.test(cardNumber)) { cardType = type; break; }
      }
      logger.info(`Successfully charged ${cardType} card ending ${cardNumber.slice(-4)}`);
      return { transaction_id: uuidv4() };
  }
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import date


# ── Custom exception (mirrors JS CardError) ───────────────────────────────────

class CardValidationError(ValueError):
    """Raised when a credit card fails validation. Maps to gRPC INVALID_ARGUMENT."""
    pass


# ── Card-type patterns (identical to JS charge.js cardTypes object) ───────────

_CARD_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("Visa",             re.compile(r"^4[0-9]{12}(?:[0-9]{3})?$")),
    ("MasterCard",       re.compile(r"^5[1-5][0-9]{14}$")),
    ("American Express", re.compile(r"^3[47][0-9]{13}$")),
    ("Discover",         re.compile(r"^6(?:011|5[0-9]{2})[0-9]{12}$")),
]


# ── Luhn algorithm ────────────────────────────────────────────────────────────

def is_valid_luhn(number: str) -> bool:
    """
    JS: function isValidLuhn(number)

    Validates a credit card number using the Luhn (mod-10) algorithm.

    Args:
        number: Digit-only string (spaces already stripped).

    Returns:
        True if the number passes the Luhn check, False otherwise.
    """
    total = 0
    double_digit = False
    for ch in reversed(number):
        digit = int(ch)
        if double_digit:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
        double_digit = not double_digit
    return total % 10 == 0


# ── Expiry check ──────────────────────────────────────────────────────────────

def is_expired(exp_year: int, exp_month: int) -> bool:
    """
    JS: function isExpired(year, month)

    Returns True if the card is expired relative to today.

    Args:
        exp_year:  4-digit expiration year.
        exp_month: 1-12 expiration month.
    """
    today = date.today()
    return (exp_year < today.year) or (
        exp_year == today.year and exp_month < today.month
    )


# ── Card type detection ───────────────────────────────────────────────────────

def detect_card_type(number: str) -> str:
    """
    JS: for (const [type, re] of Object.entries(cardTypes))

    Returns the card brand name or "Unknown" if no pattern matches.

    Args:
        number: Digit-only card number string.
    """
    for card_type, pattern in _CARD_PATTERNS:
        if pattern.match(number):
            return card_type
    return "Unknown"


# ── Main charge function ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class ChargeResult:
    transaction_id: str
    card_type: str
    last_four: str


def charge(
    credit_card_number: str,
    credit_card_cvv: int,
    credit_card_expiration_year: int,
    credit_card_expiration_month: int,
    amount_currency_code: str,
    amount_units: int,
    amount_nanos: int,
) -> ChargeResult:
    """
    JS: function charge(request)

    Validates the card and returns a mock transaction ID.

    Steps (identical to JS charge.js):
      1. Strip spaces from card number.
      2. Run Luhn check → raise CardValidationError if invalid.
      3. Run expiry check → raise CardValidationError if expired.
      4. Detect card type.
      5. Return ChargeResult with a UUID transaction_id.

    Args:
        credit_card_number:          Raw PAN string (may contain spaces).
        credit_card_cvv:             3 or 4 digit CVV (validated for length).
        credit_card_expiration_year: 4-digit year.
        credit_card_expiration_month: 1–12 month.
        amount_currency_code:        ISO 4217 code, e.g. "USD".
        amount_units:                Whole currency units.
        amount_nanos:                Fractional nanosecond units.

    Returns:
        ChargeResult with transaction_id, card_type, last_four.

    Raises:
        CardValidationError: on any validation failure.
    """
    # ── 1. Sanitise card number ───────────────────────────────────────────────
    # JS: const cardNumber = creditCard.credit_card_number.replace(/ /g, '');
    card_number = credit_card_number.replace(" ", "").replace("-", "")

    if not card_number.isdigit():
        raise CardValidationError("Credit card info is invalid.")

    # ── 2. Luhn check ─────────────────────────────────────────────────────────
    # JS: if (!isValidLuhn(cardNumber)) throw new CardError('...');
    if not is_valid_luhn(card_number):
        raise CardValidationError("Credit card info is invalid.")

    # ── 3. Expiry check ───────────────────────────────────────────────────────
    # JS: if (isExpired(year, month)) throw new CardError(`The credit card (4xxx) expired on ...`);
    if is_expired(credit_card_expiration_year, credit_card_expiration_month):
        last_four = card_number[-4:]
        raise CardValidationError(
            f"The credit card (ending {last_four}) expired on "
            f"{credit_card_expiration_month:02d}/{credit_card_expiration_year}."
        )

    # ── 4. CVV basic length check ─────────────────────────────────────────────
    # Amex uses 4-digit CVV; all others use 3. Node.js original doesn't validate
    # CVV value (mock service), but we enforce minimum sanity.
    cvv_str = str(credit_card_cvv)
    if len(cvv_str) not in (3, 4):
        raise CardValidationError("Credit card CVV is invalid.")

    # ── 5. Detect card type ───────────────────────────────────────────────────
    # JS: for (const [type, re] of Object.entries(cardTypes)) { ... }
    card_type = detect_card_type(card_number)
    last_four = card_number[-4:]

    # ── 6. Generate transaction ID ────────────────────────────────────────────
    # JS: return { transaction_id: uuidv4() };
    transaction_id = str(uuid.uuid4())

    return ChargeResult(
        transaction_id=transaction_id,
        card_type=card_type,
        last_four=last_four,
    )
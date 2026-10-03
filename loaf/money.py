"""Order-input validation and small unit helpers.

You work entirely in human units: prices in **dollars**, quantities in
**tokens**, for both what you send and what you receive. The only limits to
respect (the SDK pre-checks these before a request goes out):

* limit price: at most 2 decimal places (cents);
* quantity: at most 1 decimal place (0.1 token).

Fees are expressed in basis points (``*Bps`` fields): 1 bp = 0.01%. Percentage
fields (``*Percent`` / ``*Percentage``) are already plain percentages (``5.2``
means 5.2%).
"""

from __future__ import annotations

import math
from decimal import Decimal

from .constants import MAX_PRICE_DECIMALS, MAX_QUANTITY_DECIMALS
from .exceptions import _client_validation_error


def bps_to_fraction(bps: float) -> float:
    """Basis points -> fraction. ``30`` bps -> ``0.003`` (i.e. 0.30%)."""
    return bps / 10_000


def fraction_to_bps(fraction: float) -> int:
    """Fraction -> basis points. ``0.003`` -> ``30``."""
    return int(round(fraction * 10_000))


def _decimal_places(value: float) -> int:
    d = Decimal(str(value)).normalize()
    exponent = d.as_tuple().exponent
    return -exponent if isinstance(exponent, int) and exponent < 0 else 0


def _as_number(value: object, what: str) -> float:
    """Coerce ``value`` to a float, rejecting bools and non-numeric types explicitly.

    ``bool`` is a subclass of ``int``, so ``validate_price(True)`` previously slipped
    through as the number ``1.0``; a ``str`` or ``Decimal`` reached ``math.isfinite``
    and raised a bare ``TypeError`` from deep inside the standard library instead of the
    documented :class:`~loaf.exceptions.LoafValidationError`. Callers always want the
    SDK's own exception type here.
    """
    if isinstance(value, bool):
        raise _client_validation_error(f"{what} must be a number, not a bool")
    if isinstance(value, (int, float, Decimal)):
        return float(value)
    raise _client_validation_error(
        f"{what} must be a number, got {type(value).__name__}"
    )


def validate_price(price: float) -> None:
    """Raise :class:`LoafValidationError` if ``price`` violates limit-price rules."""
    price = _as_number(price, "price")
    if not math.isfinite(price):
        raise _client_validation_error("price must be a finite number")
    if price < 0:
        raise _client_validation_error("price must not be negative")
    if _decimal_places(price) > MAX_PRICE_DECIMALS:
        raise _client_validation_error(
            f"price must have at most {MAX_PRICE_DECIMALS} decimal places (got {price!r})"
        )


def validate_quantity(quantity: float) -> None:
    """Raise :class:`LoafValidationError` if ``quantity`` violates quantity rules."""
    quantity = _as_number(quantity, "quantity")
    if not math.isfinite(quantity):
        raise _client_validation_error("quantity must be a finite number")
    if quantity <= 0:
        raise _client_validation_error("quantity must be positive")
    if _decimal_places(quantity) > MAX_QUANTITY_DECIMALS:
        raise _client_validation_error(
            f"quantity must have at most {MAX_QUANTITY_DECIMALS} decimal places (got {quantity!r})"
        )

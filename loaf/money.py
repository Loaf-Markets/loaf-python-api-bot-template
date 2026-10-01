"""Order-input validation and small unit helpers.

You work entirely in human units: prices in **dollars**, quantities in
**tokens**, for both what you send and what you receive. The only limits to
respect (the SDK pre-checks these before a request goes out):

* price: on the 0.01 grid (cents), from 0.01 to 1,000,000,000;
* quantity: on the 0.1 grid (0.1 token), from 0.1 to 1,000,000,000.

Off-grid input is **refused, never rounded**: derive prices with
``round(x, 2)``. Internally every order is signed over scaled integers
(price x 1000, quantity x 10); nothing you send or receive is scaled.

Fees are expressed in basis points (``*Bps`` fields): 1 bp = 0.01%. Percentage
fields (``*Percent`` / ``*Percentage``) are already plain percentages (``5.2``
means 5.2%).
"""

from __future__ import annotations

import numbers
from decimal import (
    ROUND_HALF_UP,
    Context,
    Decimal,
    DivisionByZero,
    InvalidOperation,
    Overflow,
    localcontext,
)
from typing import Any

from .constants import (
    MAX_PRICE_DECIMALS,
    MAX_QUANTITY_DECIMALS,
    ORDER_PRICE_MAX,
    ORDER_QUANTITY_MAX,
)
from .exceptions import _client_validation_error

_PRICE_TICK = Decimal(1).scaleb(-MAX_PRICE_DECIMALS)  # 0.01
_QUANTITY_TICK = Decimal(1).scaleb(-MAX_QUANTITY_DECIMALS)  # 0.1
# Order math never uses the caller's decimal context (its precision or traps).
_DECIMAL_CONTEXT = Context(prec=60, traps=[InvalidOperation, DivisionByZero, Overflow])
_ORDER_PRICE_MAX_DEC = Decimal(ORDER_PRICE_MAX).quantize(_PRICE_TICK, context=_DECIMAL_CONTEXT)


def bps_to_fraction(bps: float) -> float:
    """Basis points -> fraction. ``30`` bps -> ``0.003`` (i.e. 0.30%)."""
    return bps / 10_000


def fraction_to_bps(fraction: float) -> int:
    """Fraction -> basis points. ``0.003`` -> ``30``."""
    return int(round(fraction * 10_000))


def _to_decimal(value: Any, field: str) -> Decimal:
    """A finite number as the Decimal of what ``json.dumps`` would send; bool/str/None refused."""
    if isinstance(value, bool) or not isinstance(value, (numbers.Real, Decimal)):
        raise _client_validation_error(f"{field} must be a number (got {value!r})")
    if isinstance(value, Decimal):
        d = value
    elif isinstance(value, numbers.Integral):
        d = Decimal(int(value))
    else:
        try:
            d = Decimal(str(value))  # str(), not repr(): numpy 2 reprs are 'np.float64(..)'
        except InvalidOperation:
            raise _client_validation_error(f"{field} must be a number (got {value!r})") from None
    if not d.is_finite():
        raise _client_validation_error(f"{field} must be a finite number")
    return d


def _on_grid(value: Any, field: str, tick: Decimal, places: int, maximum: int) -> Decimal:
    d = _to_decimal(value, field)
    if d <= 0:
        raise _client_validation_error(f"{field} must be positive (got {value!r})")
    if d > maximum:  # bounds BEFORE quantize (1e300 would overflow it)
        raise _client_validation_error(f"{field} must be at most {maximum:,} (got {value!r})")
    with localcontext(_DECIMAL_CONTEXT):
        on_grid = d.quantize(tick)
    if d != on_grid:
        raise _client_validation_error(
            f"{field} must have at most {places} decimal places (got {value!r})"
        )
    return on_grid


def validate_price(value: float | Decimal, field: str = "price") -> Decimal:
    """Raise :class:`LoafValidationError` unless ``value`` is a price the
    exchange accepts; returns it as an exact ``Decimal`` on the 0.01 grid."""
    return _on_grid(value, field, _PRICE_TICK, MAX_PRICE_DECIMALS, ORDER_PRICE_MAX)


def validate_quantity(value: float | Decimal, field: str = "quantity") -> Decimal:
    """Raise :class:`LoafValidationError` unless ``value`` is a quantity the
    exchange accepts; returns it as an exact ``Decimal`` on the 0.1 grid."""
    return _on_grid(value, field, _QUANTITY_TICK, MAX_QUANTITY_DECIMALS, ORDER_QUANTITY_MAX)


def validate_slippage_bps(value: int, field: str = "max_slippage_bps") -> int:
    """Raise :class:`LoafValidationError` unless ``value`` is a whole number of
    basis points from 0 to 9999; returns it as an ``int``."""
    whole = isinstance(value, numbers.Integral) and not isinstance(value, bool)
    if not whole or not 0 <= value < 10_000:
        raise _client_validation_error(
            f"{field} must be a whole number of basis points from 0 to 9999 (got {value!r})"
        )
    return int(value)


def _check_side(side: Any) -> str:
    side = str(side)
    if side not in ("BUY", "SELL"):
        raise _client_validation_error(f"side must be 'BUY' or 'SELL' (got {side!r})")
    return side


def _worst_price(reference_price: Any, side: str, max_slippage_bps: int) -> Decimal:
    ref = _to_decimal(reference_price, "reference_price")
    if ref <= 0:
        raise _client_validation_error(
            f"reference_price must be positive (got {reference_price!r}); a market price of 0 "
            "means the exchange has no reference yet"
        )
    side = _check_side(side)
    bps = validate_slippage_bps(max_slippage_bps)
    factor = 10_000 + bps if side == "BUY" else 10_000 - bps
    with localcontext(_DECIMAL_CONTEXT):
        raw = ref * factor / 10_000
        if raw >= _ORDER_PRICE_MAX_DEC:
            return _ORDER_PRICE_MAX_DEC
        return max(raw.quantize(_PRICE_TICK, rounding=ROUND_HALF_UP), _PRICE_TICK)


def worst_price(reference_price: float | Decimal, side: str, max_slippage_bps: int) -> float:
    """The worst price the SDK signs off a reference.

    ``reference_price x (1 + max_slippage_bps/10,000)`` for a BUY and
    ``x (1 - max_slippage_bps/10,000)`` for a SELL, rounded half-up to the cent
    and never below 0.01. It is what :meth:`~loaf.resources.orders.OrdersResource.market_buy`
    / ``market_sell`` sign (off the market reference), what a ``*_MARKET``
    conditional books at (off its trigger), and the lowest price a TP/SL leg
    sells at (off its trigger, as a SELL). Use it to size a MARKET BUY: it
    freezes this price x quantity plus the taker fee.

    ``side`` is ``BUY`` or ``SELL``; ``max_slippage_bps`` a whole number from 0
    to 9999, required so your sizing cannot drift from what the client signs:
    pass ``client.max_slippage_bps`` for the client's default.
    """
    return float(_worst_price(reference_price, side, max_slippage_bps))

"""Order-input validation and small unit helpers.

Prices and quantities are human units: prices in **dollars**, quantities in
**tokens**, both in what you send and in the market data you receive. The
limits to respect (the SDK pre-checks these before a request goes out):

* price: on the 0.01 grid (cents), from 0.01 to 4,294,967.29;
* quantity: on the 0.1 grid (0.1 token), from 0.1 to 28,147,497,671,065.5;
* an order's price x quantity: below about $281 billion (K1's 48-bit fill
  quote).

Off-grid input is **refused, never rounded**: derive prices with
``round(x, 2)``. Internally every order is signed over scaled integers
(price x 1000, quantity x 10); nothing you send or receive is scaled.

Balances and transfer amounts are the exception: decimal strings in the
token's base units (``"1500000"`` is 1.5 of a 6-decimal token). Turn one into
tokens with :func:`from_base_units`.

Fees are expressed in basis points (``*Bps`` fields): 1 bp = 0.01%. Percentage
fields (``*Percent`` / ``*Percentage``) are already plain percentages (``5.2``
means 5.2%).
"""

from __future__ import annotations

import numbers
import re
from decimal import (
    ROUND_CEILING,
    ROUND_FLOOR,
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
_BASE_UNITS_RE = re.compile(r"0|[1-9][0-9]*")


def bps_to_fraction(bps: float) -> float:
    """Basis points -> fraction. ``30`` bps -> ``0.003`` (i.e. 0.30%)."""
    return bps / 10_000


def fraction_to_bps(fraction: float) -> int:
    """Fraction -> basis points. ``0.003`` -> ``30``."""
    return int(round(fraction * 10_000))


def from_base_units(amount: str | int, decimals: int) -> Decimal:
    """A balance or transfer amount — a uint256 decimal string in the token's
    base units — as an exact number of tokens:
    ``from_base_units("1500000", 6) == Decimal("1.5")``.

    Never goes through float (18-decimal amounts exceed it). ``amount`` is the
    decimal string (or a non-negative ``int``), ``decimals`` the token's
    ``decimals`` from the same row, 0 to 255.
    """
    whole = isinstance(amount, int) and not isinstance(amount, bool) and amount >= 0
    if not whole and not (isinstance(amount, str) and _BASE_UNITS_RE.fullmatch(amount)):
        raise _client_validation_error(
            f"amount must be a whole number of base units, a decimal string (got {amount!r})"
        )
    if isinstance(decimals, bool) or not isinstance(decimals, int) or not 0 <= decimals <= 255:
        raise _client_validation_error(
            f"decimals must be a whole number from 0 to 255 (got {decimals!r})"
        )
    # Built from its string: exact at any size, where scaleb would round to the context precision.
    return Decimal(f"{int(amount)}E-{decimals}")


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


def _on_grid(value: Any, field: str, tick: Decimal, places: int, maximum: Decimal) -> Decimal:
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
    with localcontext(_DECIMAL_CONTEXT):
        raw = ref * (10_000 + bps if side == "BUY" else 10_000 - bps) / 10_000
        if raw < ORDER_PRICE_MAX + _PRICE_TICK:  # bounded first: quantize overflows on huge values
            # Toward the reference, so the signed price never exceeds the tolerance.
            rounding = ROUND_FLOOR if side == "BUY" else ROUND_CEILING
            price = max(raw.quantize(_PRICE_TICK, rounding=rounding), _PRICE_TICK)
            if price <= ORDER_PRICE_MAX:
                return price
    raise _client_validation_error(
        f"The worst price {side} {bps} bps from {reference_price!r} is above "
        f"{ORDER_PRICE_MAX:,}, the highest price an order can sign"
    )


def worst_price(reference_price: float | Decimal, side: str, max_slippage_bps: int) -> float:
    """The worst price the SDK signs off a reference.

    ``reference_price x (1 + max_slippage_bps/10,000)`` for a BUY and
    ``x (1 - max_slippage_bps/10,000)`` for a SELL, rounded toward the
    reference to the cent (down for a BUY, up for a SELL), so it never exceeds
    the tolerance, and never below 0.01. It is what
    :meth:`~loaf.resources.orders.OrdersResource.market_buy` / ``market_sell``
    sign (off the market reference), what a ``*_MARKET`` conditional books at
    (off its trigger), and the lowest price a TP/SL leg sells at (off its
    trigger, as a SELL). Refused when it would be above 4,294,967.29, the
    highest price an order can sign.

    Use it to size a MARKET BUY: a BUY holds this price x quantity x
    (1 + :data:`~loaf.constants.L2_FEE_RATE_CAP` / 1,000,000) of your cash.

    ``side`` is ``BUY`` or ``SELL``; ``max_slippage_bps`` a whole number from 0
    to 9999, required so your sizing cannot drift from what the client signs:
    pass ``client.max_slippage_bps`` for the client's default.
    """
    return float(_worst_price(reference_price, side, max_slippage_bps))

"""Order-input validation and small unit helpers.

Prices and quantities are human units: prices in **dollars**, quantities in
**tokens**, both in what you send and in the market data you receive (the
perps reads, a perp's order limits and every fill's quantity, price, fee and
closed PnL as exact :class:`~decimal.Decimal` values). Each market has its own
price step and lot, set at its listing: the ``priceDecimals`` and
``quantityDecimals`` of ``market.get(ticker).market``. The limits to respect
(the SDK checks them before an order is signed):

* price: at most ``priceDecimals`` decimal places (a tick of
  10^-priceDecimals dollars), from one tick to the market's highest price, the
  largest on its tick whose signed price fits 32 bits: (2^32 - 1) /
  10^(7 - quantityDecimals) dollars rounded down to the tick, 429,496.72 on a
  market of 3 quantity decimals and 2 price decimals;
* quantity: at most ``quantityDecimals`` decimal places (a lot of
  10^-quantityDecimals tokens), from one lot to 1,000,000,000 tokens and
  2^48 - 1 lots;
* an order's price x quantity: below about $281.47 million (K1's 48-bit fill
  quote).

Off-grid input is **refused, never rounded**: derive prices with
``round(x, market.priceDecimals)``. A price or quantity that no market takes
(not a positive number, more than 6 decimal places, or above 1,000,000,000) is
refused before anything is read; the market's own grid and bounds once its
market is read (one public request, the first time an order names it).
Internally every order is signed over the market's integers, price x
10^(7 - quantityDecimals) and quantity x 10^quantityDecimals (its
``l2.signedPriceDecimals`` and ``l2.signedQuantityDecimals``); nothing you send
is scaled.

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

from .constants import L2_SIGNED_DECIMALS, ORDER_DECIMALS, ORDER_PRICE_MAX, ORDER_QUANTITY_MAX
from .exceptions import _client_validation_error
from .k1 import _MAX_32, _MAX_48

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


def _integral(value: Any) -> bool:
    return isinstance(value, numbers.Integral) and not isinstance(value, bool)


def _decimals_field(price_decimals: Any, quantity_decimals: Any) -> str | None:
    """The first of a market's ``priceDecimals`` and ``quantityDecimals`` that cannot be one, or
    ``None``: whole numbers, the price's from 0 and the quantity's from 1, at most
    :data:`~loaf.constants.ORDER_DECIMALS` together. A quantity's at least 1 makes a signed price
    unit a whole number of millionths, and the two together at most 6 make a tick at least 10 signed
    price units, so that K1's quote of an order on the grid is exact."""
    if not (_integral(price_decimals) and 0 <= price_decimals < ORDER_DECIMALS):
        return "priceDecimals"
    most = ORDER_DECIMALS - price_decimals
    if not (_integral(quantity_decimals) and 1 <= quantity_decimals <= most):
        return "quantityDecimals"
    return None


def _tick(places: int) -> Decimal:
    """One step of a grid of ``places`` decimal places: 10^-places."""
    return Decimal(1).scaleb(-places)


def _price_max(price_decimals: int, quantity_decimals: int) -> Decimal:
    """A market's highest price: the largest on its tick whose signed price, price x
    10^(7 - quantity_decimals), fits 32 bits."""
    with localcontext(_DECIMAL_CONTEXT):
        widest = Decimal(_MAX_32).scaleb(quantity_decimals - L2_SIGNED_DECIMALS)  # in dollars
        return widest.quantize(_tick(price_decimals), rounding=ROUND_FLOOR)


def _quantity_max(quantity_decimals: int) -> Decimal:
    """A market's largest quantity: :data:`~loaf.constants.ORDER_QUANTITY_MAX` tokens, and
    2^48 - 1 lots."""
    with localcontext(_DECIMAL_CONTEXT):
        return min(ORDER_QUANTITY_MAX, Decimal(_MAX_48).scaleb(-quantity_decimals))


def _any_market(value: Any, field: str, maximum: Decimal) -> Decimal:
    """``value`` as a Decimal, refused unless a number some market could take: positive, at most
    ``maximum``, at most :data:`~loaf.constants.ORDER_DECIMALS` decimal places."""
    d = _to_decimal(value, field)
    if d <= 0:
        raise _client_validation_error(f"{field} must be positive (got {value!r})")
    if d > maximum:  # bounds BEFORE quantize (1e300 would overflow it)
        raise _client_validation_error(f"{field} must be at most {maximum:,} (got {value!r})")
    with localcontext(_DECIMAL_CONTEXT):
        millionths = d.quantize(_tick(ORDER_DECIMALS))
    if d != millionths:
        raise _client_validation_error(
            f"{field} must have at most {ORDER_DECIMALS} decimal places (got {value!r})"
        )
    return d


def _on_grid(value: Decimal, field: str, places: int, maximum: Decimal, ticker: str) -> Decimal:
    """``value`` on ``ticker``'s grid of ``places`` decimal places and at most ``maximum``, as a
    Decimal on that grid; refused, never rounded."""
    if value > maximum:
        raise _client_validation_error(
            f"{field} must be at most {maximum:,} on {ticker} (got {value})"
        )
    with localcontext(_DECIMAL_CONTEXT):
        on_grid = value.quantize(_tick(places))
    if value != on_grid:
        raise _client_validation_error(
            f"{field} must have at most {places} decimal places on {ticker} (got {value})"
        )
    return on_grid


def _market_price(
    value: Decimal, field: str, price_decimals: int, quantity_decimals: int, ticker: str
) -> Decimal:
    """A price :func:`validate_price` took (``field``: an order's or a trigger) on ``ticker``'s
    tick and at most its highest price, a market of ``price_decimals`` and ``quantity_decimals``."""
    most = _price_max(price_decimals, quantity_decimals)
    return _on_grid(value, field, price_decimals, most, ticker)


def _market_quantity(value: Decimal, quantity_decimals: int, ticker: str) -> Decimal:
    """A quantity :func:`validate_quantity` took on ``ticker``'s lot and at most its largest
    quantity, a market of ``quantity_decimals``."""
    return _on_grid(value, "quantity", quantity_decimals, _quantity_max(quantity_decimals), ticker)


def validate_price(value: float | Decimal, field: str = "price") -> Decimal:
    """Raise :class:`LoafValidationError` unless ``value`` is a price some market takes: positive,
    at most :data:`~loaf.constants.ORDER_PRICE_MAX`, at most 6 decimal places; returns it as an
    exact ``Decimal``. An order checks it against its market's tick and highest price once the
    market is read."""
    return _any_market(value, field, ORDER_PRICE_MAX)


def validate_quantity(value: float | Decimal, field: str = "quantity") -> Decimal:
    """Raise :class:`LoafValidationError` unless ``value`` is a quantity some market takes:
    positive, at most :data:`~loaf.constants.ORDER_QUANTITY_MAX`, at most 6 decimal places; returns
    it as an exact ``Decimal``. An order checks it against its market's lot and largest quantity
    once the market is read."""
    return _any_market(value, field, ORDER_QUANTITY_MAX)


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


def _reference(reference_price: Any) -> Decimal:
    """A price the SDK derives one from, as a Decimal: refused unless a positive number."""
    ref = _to_decimal(reference_price, "reference_price")
    if ref <= 0:
        raise _client_validation_error(
            f"reference_price must be positive (got {reference_price!r}); a market price of 0 "
            "means the exchange has no reference yet"
        )
    return ref


def _worst_price(
    reference_price: Any,
    side: str,
    max_slippage_bps: int,
    price_decimals: int,
    quantity_decimals: int,
    where: str = "",
    whole_bps: int | None = None,
) -> Decimal:
    """:func:`worst_price` on a market of ``price_decimals`` and ``quantity_decimals`` (``where``
    names it in the refusals) as an exact Decimal.

    ``whole_bps`` is the order's whole max slippage when ``max_slippage_bps`` is less (a perp's
    move held to half its band; default: ``max_slippage_bps``): a BUY whose moved price rounds
    below one tick, the lowest price, signs one tick only where one tick lies within
    ``whole_bps`` above the reference, and is refused otherwise. A SELL's never rounds below one
    tick."""
    ref = _reference(reference_price)
    side = _check_side(side)
    bps = validate_slippage_bps(max_slippage_bps)
    whole = bps if whole_bps is None else validate_slippage_bps(whole_bps)
    tick, most = _tick(price_decimals), _price_max(price_decimals, quantity_decimals)
    with localcontext(_DECIMAL_CONTEXT):
        raw = ref * (10_000 + bps if side == "BUY" else 10_000 - bps) / 10_000
        if raw < most + tick:  # bounded first: quantize overflows on huge values
            # Toward the reference, so the signed price never exceeds the tolerance.
            rounding = ROUND_FLOOR if side == "BUY" else ROUND_CEILING
            price = raw.quantize(tick, rounding=rounding)
            if price < tick:
                # A BUY off a reference under one tick: one tick, the lowest price, lies above the
                # moved price, so it is signed only within the whole max slippage of the reference.
                if (tick - ref) * 10_000 > whole * ref:
                    shown = format(ref.normalize(), "f")  # the reference without trailing zeros
                    raise _client_validation_error(
                        f"No BUY price{where} lies within max_slippage_bps={whole} of {shown}: "
                        f"one tick ({tick}), its lowest price, lies further above it; place a "
                        "LIMIT order"
                    )
                price = tick
            if price <= most:
                return price
    raise _client_validation_error(
        f"The worst price {side} {bps} bps from {reference_price!r} is above {most:,}, the highest "
        f"price an order can sign{where}"
    )


def worst_price(
    reference_price: float | Decimal,
    side: str,
    max_slippage_bps: int,
    *,
    price_decimals: int,
    quantity_decimals: int,
) -> float:
    """The worst price the SDK signs off a reference, on a market of ``price_decimals`` and
    ``quantity_decimals``: its ``priceDecimals`` and ``quantityDecimals``
    (``market.get(ticker).market``).

    ``reference_price x (1 + max_slippage_bps/10,000)`` for a BUY and
    ``x (1 - max_slippage_bps/10,000)`` for a SELL, rounded toward the
    reference to the market's tick, 10^-price_decimals dollars (down for a BUY,
    up for a SELL), so it never exceeds the tolerance. A SELL's is at least one
    tick, the lowest price. A BUY's that rounds below one tick (off a reference
    under one tick) is refused, since one tick lies more than
    ``max_slippage_bps`` above the reference: a price the SDK derives never lies
    further against the order than its max slippage from its reference.
    It is what :meth:`~loaf.resources.orders.OrdersResource.market_buy` /
    ``market_sell`` sign (off the market reference), what a ``*_MARKET``
    conditional books at (off its trigger), and the lowest price a TP/SL leg
    sells at (off its trigger, as a SELL). On a perp the SDK holds the move to
    half the market's band first: pass ``min(max_slippage_bps, band // 2)``
    (``band``, the market's ``l2.band``; 0 bounds nothing) and, for a MARKET
    order, the live mark to get what it signs there, unless this price crosses
    a mark off the tick (a SELL's above it, a BUY's below it) or lies outside
    the band, or this refuses a BUY off a mark under one tick. The SDK then
    signs the tick on the mark's near side (a SELL's below the mark, a BUY's
    above it) when this price crossed and that tick is inside the band and
    within the order's whole max slippage of the mark (not the min passed
    here), else this price when it is inside the band; for a BUY off a mark
    under one tick, one tick when it lies inside the band and within the
    order's whole max slippage of the mark; else it refuses the order (the
    orders module, **The perps band**). A MARKET order's ``reference_price``
    with a max slippage of 0 signs this price whatever the band, and is
    refused where this is. Refused too when it would be above the market's
    highest price, the largest on its tick whose signed price, x 10^(7 -
    quantity_decimals), fits 32 bits.

    Use it to size a MARKET BUY: a BUY holds the price it signs x quantity x
    (1 + :data:`~loaf.constants.L2_FEE_RATE_CAP` / 1,000,000) of your cash:
    this price, or on a perp the tick above an off-grid mark, or one tick,
    where the SDK signs that instead.

    ``side`` is ``BUY`` or ``SELL``; ``max_slippage_bps`` a whole number from 0
    to 9999, required so your sizing cannot drift from what the client signs:
    pass ``client.max_slippage_bps`` for the client's default.
    """
    if _decimals_field(price_decimals, quantity_decimals) is not None:
        raise _client_validation_error(
            "price_decimals and quantity_decimals must be a market's priceDecimals and "
            "quantityDecimals: whole numbers, price_decimals from 0 and quantity_decimals from 1, "
            f"at most {ORDER_DECIMALS} together (got {price_decimals!r}, {quantity_decimals!r})"
        )
    price = _worst_price(
        reference_price, side, max_slippage_bps, int(price_decimals), int(quantity_decimals)
    )
    return float(price)

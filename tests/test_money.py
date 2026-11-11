"""Prices and amounts, offline: the worst price the SDK signs on a market, the
validators (what no market takes, then a market's own grid and bounds), and
base-unit balances as tokens."""

from __future__ import annotations

import enum
from decimal import Decimal
from fractions import Fraction
from typing import Any

import pytest

import loaf
from loaf import from_base_units, worst_price
from loaf.constants import ORDER_PRICE_MAX, ORDER_QUANTITY_MAX
from loaf.money import (
    _decimals_field,
    _market_price,
    _market_quantity,
    _price_max,
    _quantity_max,
    validate_price,
    validate_quantity,
    validate_slippage_bps,
)

# OPRA's decimals: a tick of $0.01, a lot of 0.001 tokens.
OPRA = {"price_decimals": 2, "quantity_decimals": 3}
# Every market's decimals the exchange lists: 1 to 6 quantity decimals, the two together at most 6.
EVERY_MARKET = [(pd, qd) for qd in range(1, 7) for pd in range(7 - qd)]

# --------------------------------------------------------------------------- #
# Worst price
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "reference, side, bps, expected",
    [
        (160, "BUY", 200, 163.20),
        (160, "SELL", 200, 156.80),
        (120, "SELL", 200, 117.60),
        (90, "SELL", 200, 88.20),
        (130, "BUY", 200, 132.60),
        (120, "SELL", 100, 118.80),
        (90, "SELL", 500, 85.50),
        (0.25, "SELL", 200, 0.25),
        # Rounded toward the reference (down for a BUY, up for a SELL): never past the tolerance.
        (0.75, "BUY", 200, 0.76),
        (100.005, "BUY", 50, 100.50),
        (100.005, "SELL", 50, 99.51),
        (171.555, "BUY", 0, 171.55),
        (171.555, "SELL", 0, 171.56),
        (0.004, "SELL", 200, 0.01),  # never below one tick
        (0.004, "BUY", 200, 0.01),
        (0.01, "SELL", 9999, 0.01),
        (429496.72, "BUY", 0, 429496.72),  # the highest price an order can sign on OPRA
        # The web app's own vectors, on a cent tick.
        (141.45, "BUY", 1000, 155.59),
        (141.45, "SELL", 1000, 127.31),
        (0.01, "SELL", 5000, 0.01),
        (0.03, "BUY", 5000, 0.04),
        (0.07, "SELL", 5000, 0.04),
        (167.49, "BUY", 1000, 184.23),
        (167.49, "SELL", 1000, 150.75),
    ],
)
def test_worst_price_table(reference, side, bps, expected):
    assert worst_price(reference, side, bps, **OPRA) == expected


@pytest.mark.parametrize(
    "reference, side, bps, decimals, expected",
    [
        # A tick of $1 (0 price decimals, a BTC-like listing): toward the reference to the dollar.
        (75_000.4, "BUY", 200, (0, 5), 76_500),  # 76,500.408
        (75_000.4, "SELL", 200, (0, 5), 73_501),  # 73,500.392
        (0.4, "BUY", 200, (0, 5), 1),  # never below one tick
        # A tick of $0.0001 (an H100-like listing near $3).
        (3.3217, "BUY", 200, (4, 1), 3.3881),  # 3.388134
        (3.3217, "SELL", 200, (4, 1), 3.2553),  # 3.255266
        (0.00004, "SELL", 200, (4, 1), 0.0001),
        # A tick of $0.00001 (a listing under $1).
        (0.5, "SELL", 333, (5, 1), 0.48335),
        (0.123456, "BUY", 0, (5, 1), 0.12345),
        (0.123456, "SELL", 0, (5, 1), 0.12346),
        # The highest price each can sign: (2^32 - 1) / 10^(7 - quantityDecimals), down to the tick.
        (429_496.729, "BUY", 0, (3, 3), 429_496.729),
        (4_294.9672, "BUY", 0, (4, 1), 4_294.9672),
        (4_294.96729, "BUY", 0, (5, 1), 4_294.96729),
        (42_949_672, "BUY", 0, (0, 5), 42_949_672),
        (429_496_729, "SELL", 0, (0, 6), 429_496_729),
    ],
)
def test_worst_price_rounds_to_the_markets_tick(reference, side, bps, decimals, expected):
    price_decimals, quantity_decimals = decimals
    price = worst_price(
        reference, side, bps, price_decimals=price_decimals, quantity_decimals=quantity_decimals
    )
    assert price == expected


@pytest.mark.parametrize(
    "reference, side, bps, decimals, most",
    [
        (429496.72, "BUY", 1, (2, 3), "429,496.72"),
        (429496.721, "SELL", 0, (2, 3), "429,496.72"),  # rounded up past the max: refused, not capped
        (4_294.9673, "BUY", 0, (4, 1), "4,294.9672"),
        (42_949_673, "SELL", 0, (0, 5), "42,949,672"),
        (10**30, "SELL", 200, (2, 3), "429,496.72"),
        (Fraction(10**400), "BUY", 200, (0, 6), "429,496,729"),  # beyond float range: no overflow
    ],
)
def test_worst_price_above_the_markets_max_refused(reference, side, bps, decimals, most):
    # Capping it would sign more slippage than asked for a SELL: refused instead.
    price_decimals, quantity_decimals = decimals
    with pytest.raises(loaf.LoafValidationError) as info:
        worst_price(
            reference, side, bps, price_decimals=price_decimals, quantity_decimals=quantity_decimals
        )
    assert info.value.status_code == 0
    assert info.value.message.startswith(f"The worst price {side} {bps} bps from ")
    assert info.value.message.endswith(f"is above {most}, the highest price an order can sign")


def test_worst_price_needs_max_slippage_and_the_markets_decimals_by_name():
    assert worst_price(160, loaf.OrderSide.BUY, loaf.DEFAULT_MAX_SLIPPAGE_BPS, **OPRA) == 163.2
    assert worst_price(reference_price=160, side="SELL", max_slippage_bps=100, **OPRA) == 158.4
    assert loaf.DEFAULT_MAX_SLIPPAGE_BPS == 200
    # No default: sizing must name the tolerance the client signs with (client.max_slippage_bps),
    # and the market's decimals, by name (swapped, two markets' decimals can both be valid).
    with pytest.raises(TypeError):
        worst_price(160, "BUY", **OPRA)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        worst_price(160, "BUY", 200)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        worst_price(160, "BUY", 200, 2, 3)  # type: ignore[misc]


@pytest.mark.parametrize(
    "reference, side, bps",
    [
        (0, "BUY", 200),
        (-1, "BUY", 200),
        (float("nan"), "BUY", 200),
        (True, "BUY", 200),
        ("160", "BUY", 200),
        (160, "BUY", -1),
        (160, "BUY", 10_000),
        (160, "BUY", 1.5),
        (160, "BUY", True),
        (160, "buy", 200),
    ],
)
def test_worst_price_rejects(reference, side, bps):
    with pytest.raises(loaf.LoafValidationError):
        worst_price(reference, side, bps, **OPRA)


@pytest.mark.parametrize(
    "price_decimals, quantity_decimals",
    [(-1, 3), (6, 1), (2, 0), (3, 4), (0, 7), (True, 3), (2, True), (2.0, 3), ("2", 3), (None, 3)],
)
def test_worst_price_refuses_decimals_no_market_has(price_decimals, quantity_decimals):
    with pytest.raises(loaf.LoafValidationError) as info:
        worst_price(
            160, "BUY", 200, price_decimals=price_decimals, quantity_decimals=quantity_decimals
        )
    assert info.value.status_code == 0
    assert info.value.message == (
        "price_decimals and quantity_decimals must be a market's priceDecimals and "
        "quantityDecimals: whole numbers, price_decimals from 0 and quantity_decimals from 1, at "
        f"most 6 together (got {price_decimals!r}, {quantity_decimals!r})"
    )


# --------------------------------------------------------------------------- #
# A market's decimals and bounds
# --------------------------------------------------------------------------- #


def test_the_decimals_a_market_may_have():
    assert len(EVERY_MARKET) == 21
    for price_decimals, quantity_decimals in EVERY_MARKET:
        assert _decimals_field(price_decimals, quantity_decimals) is None
    # The price's is checked first: its range leaves the quantity's at least 1.
    for price_decimals, quantity_decimals, field in [
        (-1, 3, "priceDecimals"),
        (6, 0, "priceDecimals"),
        (True, 3, "priceDecimals"),
        (2.0, 3, "priceDecimals"),
        (None, None, "priceDecimals"),
        (2, 0, "quantityDecimals"),
        (2, 5, "quantityDecimals"),
        (0, 7, "quantityDecimals"),
        (2, False, "quantityDecimals"),
        (2, "3", "quantityDecimals"),
    ]:
        assert _decimals_field(price_decimals, quantity_decimals) == field


@pytest.mark.parametrize("price_decimals, quantity_decimals", EVERY_MARKET)
def test_a_markets_bounds_are_its_signed_widths(price_decimals, quantity_decimals):
    tick, lot = Decimal(1).scaleb(-price_decimals), Decimal(1).scaleb(-quantity_decimals)
    signed_price, signed_quantity = 10 ** (7 - quantity_decimals), 10**quantity_decimals
    # The highest price: the largest on the tick whose signed price fits 32 bits.
    most = _price_max(price_decimals, quantity_decimals)
    assert most % tick == 0
    assert most * signed_price <= 2**32 - 1 < (most + tick) * signed_price
    assert _market_price(most, "price", price_decimals, quantity_decimals, "T") == most
    assert _market_price(tick, "price", price_decimals, quantity_decimals, "T") == tick
    with pytest.raises(loaf.LoafValidationError) as info:
        _market_price(most + tick, "price", price_decimals, quantity_decimals, "T")
    assert info.value.message == f"price must be at most {most:,} on T (got {most + tick})"
    # The largest quantity: 1,000,000,000 tokens, and 2^48 - 1 lots.
    largest = _quantity_max(quantity_decimals)
    assert largest % lot == 0
    assert largest == min(ORDER_QUANTITY_MAX, Decimal(2**48 - 1) / signed_quantity)
    assert _market_quantity(largest, quantity_decimals, "T") == largest
    assert _market_quantity(lot, quantity_decimals, "T") == lot
    with pytest.raises(loaf.LoafValidationError):
        _market_quantity(largest + lot, quantity_decimals, "T")


def test_a_markets_grid_refuses_and_never_rounds():
    # On OPRA's grid: returned on it, whatever the trailing zeros given.
    price = _market_price(Decimal("167.490"), "price", 2, 3, "OPRA")
    assert price == Decimal("167.49") and str(price) == "167.49"
    assert str(_market_quantity(Decimal(10), 3, "OPRA")) == "10.000"
    for given, field, places in (
        (Decimal("167.491"), "price", 2),
        (Decimal("90.005"), "trigger_price", 2),
    ):
        with pytest.raises(loaf.LoafValidationError) as info:
            _market_price(given, field, 2, 3, "OPRA")
        assert (info.value.status_code, info.value.message) == (
            0,
            f"{field} must have at most {places} decimal places on OPRA (got {given})",
        )
    with pytest.raises(loaf.LoafValidationError) as info:
        _market_quantity(Decimal("1.2345"), 3, "OPRA")
    assert info.value.message == "quantity must have at most 3 decimal places on OPRA (got 1.2345)"
    # A tick of $1: no cents; a lot of 0.1.
    with pytest.raises(loaf.LoafValidationError) as info:
        _market_price(Decimal("75000.5"), "price", 0, 5, "BTC")
    assert info.value.message == "price must have at most 0 decimal places on BTC (got 75000.5)"
    with pytest.raises(loaf.LoafValidationError) as info:
        _market_quantity(Decimal("0.05"), 1, "H100-PERP")
    assert info.value.message == (
        "quantity must have at most 1 decimal places on H100-PERP (got 0.05)"
    )


# --------------------------------------------------------------------------- #
# Validators: what no market takes, before anything is read
# --------------------------------------------------------------------------- #


def test_validate_returns_the_exact_decimal():
    assert validate_price(167.49) == Decimal("167.49")
    # As given: the market's grid quantizes it once it is read.
    assert str(validate_price(Decimal("167.490"))) == "167.490"
    assert validate_quantity(10) == Decimal(10)
    assert validate_quantity(0.000001) == Decimal("0.000001")
    # An IntEnum is read as its integer value (its str() is 'Lot.TEN' before Python 3.11).
    lot = enum.IntEnum("Lot", {"TEN": 10})
    assert str(validate_price(lot.TEN)) == "10" and str(validate_quantity(lot.TEN)) == "10"


def test_validate_bounds():
    # What the exchange takes on any market: up to 1,000,000,000, in millionths.
    assert ORDER_PRICE_MAX == ORDER_QUANTITY_MAX == 1_000_000_000
    assert validate_price(1_000_000_000) == ORDER_PRICE_MAX
    assert validate_quantity(Decimal("999999999.999999")) == Decimal("999999999.999999")
    for validate, field in ((validate_price, "price"), (validate_quantity, "quantity")):
        with pytest.raises(loaf.LoafValidationError) as info:
            validate(Decimal("1000000000.000001"))
        assert info.value.message == (
            f"{field} must be at most 1,000,000,000 (got Decimal('1000000000.000001'))"
        )
        with pytest.raises(loaf.LoafValidationError) as info:
            validate(0.0000001)
        assert info.value.message == f"{field} must have at most 6 decimal places (got 1e-07)"


def test_validate_rejects():
    bad: Any  # deliberately wrong types among them
    for bad in (
        0.0000001,
        1.0000001,
        0.1 + 0.2,  # 0.30000000000000004
        0,
        -1,
        float("nan"),
        float("inf"),
        float("-inf"),
        True,
        "1",
        None,
        1e9 + 0.01,
        1e300,
        Fraction(10**400),  # beyond float range: still a validation error
    ):
        with pytest.raises(loaf.LoafValidationError):
            validate_price(bad)
    for bad in (1.0000001, 2e9, 0, float("nan")):
        with pytest.raises(loaf.LoafValidationError):
            validate_quantity(bad)
    for bad in (-1, 10_000, 2.5, True):
        with pytest.raises(loaf.LoafValidationError):
            validate_slippage_bps(bad)


# --------------------------------------------------------------------------- #
# Base units
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "amount, decimals, expected",
    [
        ("1500000", 6, Decimal("1.5")),
        ("0", 18, Decimal(0)),
        ("1", 0, Decimal(1)),
        (1500000, 6, Decimal("1.5")),
    ],
)
def test_from_base_units(amount, decimals, expected):
    tokens = from_base_units(amount, decimals)
    assert isinstance(tokens, Decimal) and tokens == expected


def test_from_base_units_is_exact_at_uint256():
    digits = str(2**256 - 1)  # 78 digits: far beyond a float, and beyond the default context
    assert len(digits) == 78
    for amount in (digits, 2**256 - 1):
        assert str(from_base_units(amount, 18)) == digits[:-18] + "." + digits[-18:]


@pytest.mark.parametrize("amount", ["-1", "01", "1.5", " 1", "", "1e3", True, 1.5, -1, None])
def test_from_base_units_rejects_amount(amount):
    with pytest.raises(loaf.LoafValidationError) as info:
        from_base_units(amount, 6)
    assert info.value.status_code == 0
    assert info.value.message == (
        f"amount must be a whole number of base units, a decimal string (got {amount!r})"
    )


@pytest.mark.parametrize("decimals", [-1, 256, True, "6", 6.0])
def test_from_base_units_rejects_decimals(decimals):
    with pytest.raises(loaf.LoafValidationError) as info:
        from_base_units("1500000", decimals)
    assert info.value.status_code == 0
    assert info.value.message == f"decimals must be a whole number from 0 to 255 (got {decimals!r})"

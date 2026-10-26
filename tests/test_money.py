"""Prices and amounts, offline: the worst price the SDK signs, the grid
validators, and base-unit balances as tokens."""

from __future__ import annotations

import enum
from decimal import Decimal
from fractions import Fraction
from typing import Any

import pytest

import loaf
from loaf import from_base_units, worst_price
from loaf.constants import ORDER_PRICE_MAX, ORDER_QUANTITY_MAX
from loaf.money import validate_price, validate_quantity, validate_slippage_bps

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
        (0.004, "SELL", 200, 0.01),  # never below 0.01
        (0.004, "BUY", 200, 0.01),
        (0.01, "SELL", 9999, 0.01),
        (4294967.29, "BUY", 0, 4294967.29),  # the highest price an order can sign
        # The web app's own vectors.
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
    assert worst_price(reference, side, bps) == expected


@pytest.mark.parametrize(
    "reference, side, bps",
    [
        (4294967.29, "BUY", 1),
        (4294967.291, "SELL", 0),  # rounded up past the max: refused, not capped
        (10**30, "SELL", 200),
        (Fraction(10**400), "BUY", 200),  # beyond float range: refused the same, no overflow
    ],
)
def test_worst_price_above_the_max_refused(reference, side, bps):
    # Capping it would sign more slippage than asked for a SELL: refused instead.
    with pytest.raises(loaf.LoafValidationError) as info:
        worst_price(reference, side, bps)
    assert info.value.status_code == 0
    assert info.value.message.startswith(f"The worst price {side} {bps} bps from ")
    assert info.value.message.endswith("is above 4,294,967.29, the highest price an order can sign")


def test_worst_price_needs_max_slippage_and_takes_enum_side():
    assert worst_price(160, loaf.OrderSide.BUY, loaf.DEFAULT_MAX_SLIPPAGE_BPS) == 163.2
    assert worst_price(reference_price=160, side="SELL", max_slippage_bps=100) == 158.4
    assert loaf.DEFAULT_MAX_SLIPPAGE_BPS == 200
    # No default: sizing must name the tolerance the client signs with (client.max_slippage_bps).
    with pytest.raises(TypeError):
        worst_price(160, "BUY")  # type: ignore[call-arg]


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
        worst_price(reference, side, bps)


# --------------------------------------------------------------------------- #
# Validators
# --------------------------------------------------------------------------- #


def test_validate_returns_grid_decimal():
    assert validate_price(167.49) == Decimal("167.49")
    assert validate_price(Decimal("167.490")) == Decimal("167.49")
    quantity = validate_quantity(10)
    assert quantity == Decimal("10.0") and str(quantity) == "10.0"
    # An IntEnum is read as its integer value (its str() is 'Lot.TEN' before Python 3.11).
    lot = enum.IntEnum("Lot", {"TEN": 10})
    assert str(validate_price(lot.TEN)) == "10.00" and str(validate_quantity(lot.TEN)) == "10.0"


def test_validate_bounds():
    # The widths an order signs: price x 1000 in 32 bits, quantity x 10 in 48.
    assert int(ORDER_PRICE_MAX * 1000) == 4_294_967_290 <= 2**32 - 1
    assert int(ORDER_QUANTITY_MAX * 10) == 2**48 - 1
    assert validate_price(4294967.29) == ORDER_PRICE_MAX
    assert validate_quantity(28147497671065.5) == ORDER_QUANTITY_MAX
    with pytest.raises(loaf.LoafValidationError) as info:
        validate_price(4294967.3)
    assert info.value.message == "price must be at most 4,294,967.29 (got 4294967.3)"
    for above in (28147497671065.6, Decimal("28147497671065.6")):
        with pytest.raises(loaf.LoafValidationError) as info:
            validate_quantity(above)
        assert info.value.message == (
            f"quantity must be at most 28,147,497,671,065.5 (got {above!r})"
        )


def test_validate_rejects():
    bad: Any  # deliberately wrong types among them
    for bad in (
        1.234,
        0.001,
        0.1 + 0.2,
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
    for bad in (1.25, 0.05, 1e14, 0, float("nan")):
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

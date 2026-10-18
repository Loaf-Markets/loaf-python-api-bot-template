"""Order prices and quantities, offline: the worst-price rule and the grid validators.

The worst price rounds as the Loaf web app's lib/slippage.ts does, toward the
reference (a BUY down, a SELL up), so a signed price never passes the
tolerance by part of a cent.
"""

from __future__ import annotations

import enum
from decimal import Decimal
from fractions import Fraction
from typing import Any

import pytest

import loaf
from loaf.money import validate_price, validate_quantity, validate_slippage_bps, worst_price


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
        (0.75, "BUY", 200, 0.76),  # 0.765: down for a BUY
        (100.005, "BUY", 50, 100.50),  # 100.505025
        (100.005, "SELL", 50, 99.51),  # 99.504975: up for a SELL
        (171.555, "BUY", 0, 171.55),  # a half-cent reference: never past it either way
        (171.555, "SELL", 0, 171.56),
        (0.004, "SELL", 200, 0.01),  # never below 0.01
        (0.01, "SELL", 9999, 0.01),
        (999999999.99, "BUY", 200, 1000000000.00),  # capped at the exchange's maximum
        (10**30, "SELL", 200, 1000000000.00),
        (Fraction(10**400), "BUY", 200, 1000000000.00),  # beyond float range: capped the same
        # The web app's own vectors (loafx-frontend lib/slippage.ts), value for value.
        (141.45, "BUY", 1000, 155.59),
        (141.45, "SELL", 1000, 127.31),
        (0.01, "SELL", 5000, 0.01),
        (0.03, "BUY", 5000, 0.04),
        (0.07, "SELL", 5000, 0.04),
        # An exact grid value is not moved a cent by float noise.
        (100, "BUY", 200, 102.00),
        (100, "SELL", 200, 98.00),
    ],
)
def test_worst_price_table(reference, side, bps, expected):
    assert worst_price(reference, side, bps) == expected


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


def test_validate_returns_grid_decimal():
    assert validate_price(167.49) == Decimal("167.49")
    assert validate_price(Decimal("167.490")) == Decimal("167.49")
    quantity = validate_quantity(10)
    assert quantity == Decimal("10.0") and str(quantity) == "10.0"
    # An IntEnum is read as its integer value (its str() is 'Lot.TEN' before Python 3.11).
    lot = enum.IntEnum("Lot", {"TEN": 10})
    assert str(validate_price(lot.TEN)) == "10.00" and str(validate_quantity(lot.TEN)) == "10.0"


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
    for bad in (1.25, 0.05, 1e9 + 0.1, 0, float("nan")):
        with pytest.raises(loaf.LoafValidationError):
            validate_quantity(bad)
    for bad in (-1, 10_000, 2.5, True):
        with pytest.raises(loaf.LoafValidationError):
            validate_slippage_bps(bad)

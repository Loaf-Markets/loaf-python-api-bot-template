"""The integers an L2 order signs.

The backend re-scales an order body's price and quantity with JavaScript's ``Math.round`` and
checks the signature over those integers (Loaf-Backend ``formatters.ts`` scaleCurrency /
scaleTokenQuantity, Shared-Types ``scaleOrderPrice``), so the same expressions here give the same
integers. A MARKET order's worst price is :func:`loaf.worst_price`.
"""

from __future__ import annotations

import math

from ..constants import MAX_PRICE_DECIMALS, MAX_QUANTITY_DECIMALS

#: Signed price = dollars x 1000 (10^(price decimals + quantity decimals)); quantity = tokens x 10.
ORDER_PRICE_SCALE = 10 ** (MAX_PRICE_DECIMALS + MAX_QUANTITY_DECIMALS)
ORDER_QUANTITY_SCALE = 10**MAX_QUANTITY_DECIMALS


def js_round(x: float) -> int:
    """JavaScript's ``Math.round``: the nearest integer, a half toward +infinity."""
    f = math.floor(x)
    return int(f) + (1 if x - f >= 0.5 else 0)


def scale_price(price: float) -> int:
    """Dollars to the integer an order signs (x 1000)."""
    return js_round(price * ORDER_PRICE_SCALE)


def scale_quantity(quantity: float) -> int:
    """Tokens to the integer an order signs (x 10)."""
    return js_round(quantity * ORDER_QUANTITY_SCALE)


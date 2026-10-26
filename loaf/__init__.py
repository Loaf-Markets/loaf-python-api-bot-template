"""Loaf API SDK — a Python client + bot template for the Loaf trading platform.

Quick start::

    from loaf import LoafClient

    loaf = LoafClient()   # reads $LOAF_API_KEY and $LOAF_SECRET_KEY
    print(loaf.portfolio.component().balances)
    print(loaf.market.properties())
    loaf.orders.limit_buy("MUS", quantity=10, price=167.49)   # signed with your trading key

See the README for the full guide.
"""

from __future__ import annotations

from ._object import LoafObject
from ._version import __version__
from .client import LoafClient
from .constants import (
    DEFAULT_BASE_URL,
    DEFAULT_MAX_SLIPPAGE_BPS,
    L2_BUFFER_MS,
    L2_FEE_RATE_CAP,
    MAX_POOL,
    MAX_PRICE_DECIMALS,
    MAX_QUANTITY_DECIMALS,
    PERPS_SPOT_SIDE,
)
from .enums import (
    CONDITIONAL_ORDER_TYPES,
    CandleResolution,
    CompetitionRoundStatus,
    ConditionalOrderStatus,
    ConditionalOrderType,
    IpoStatus,
    OrderSide,
    OrderStatus,
    OrderType,
    PropertyStatus,
    TimeInForce,
    WSMessageType,
    is_conditional_order,
)
from .exceptions import (
    LoafAPIError,
    LoafAuthError,
    LoafConfigError,
    LoafConflictError,
    LoafConnectionError,
    LoafError,
    LoafForbiddenError,
    LoafNotFoundError,
    LoafRateLimitError,
    LoafServerError,
    LoafServiceUnavailableError,
    LoafValidationError,
    MarketHaltedError,
    MarketStateError,
    OrderOutcomeUnknownError,
    PerpsOutcomeUnknownError,
    PriceHeldError,
    TradingHaltedError,
)
from .money import bps_to_fraction, fraction_to_bps, from_base_units, worst_price
from .ws import LoafWebSocketClient

__all__ = [
    "__version__",
    # client
    "LoafClient",
    "LoafWebSocketClient",
    "LoafObject",
    # constants
    "DEFAULT_BASE_URL",
    "DEFAULT_MAX_SLIPPAGE_BPS",
    "L2_BUFFER_MS",
    "L2_FEE_RATE_CAP",
    "MAX_POOL",
    "MAX_PRICE_DECIMALS",
    "MAX_QUANTITY_DECIMALS",
    "PERPS_SPOT_SIDE",
    # enums
    "OrderSide",
    "OrderType",
    "ConditionalOrderType",
    "TimeInForce",
    "OrderStatus",
    "ConditionalOrderStatus",
    "PropertyStatus",
    "IpoStatus",
    "CandleResolution",
    "CompetitionRoundStatus",
    "WSMessageType",
    # order-row helpers
    "CONDITIONAL_ORDER_TYPES",
    "is_conditional_order",
    # unit helpers
    "bps_to_fraction",
    "fraction_to_bps",
    "from_base_units",
    "worst_price",
    # exceptions
    "LoafError",
    "LoafConfigError",
    "LoafConnectionError",
    "OrderOutcomeUnknownError",
    "PerpsOutcomeUnknownError",
    "LoafAPIError",
    "LoafAuthError",
    "LoafForbiddenError",
    "TradingHaltedError",
    "MarketStateError",
    "MarketHaltedError",
    "PriceHeldError",
    "LoafValidationError",
    "LoafNotFoundError",
    "LoafConflictError",
    "LoafRateLimitError",
    "LoafServerError",
    "LoafServiceUnavailableError",
]

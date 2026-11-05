"""Loaf API SDK — a Python client + bot template for the Loaf trading platform.

Quick start::

    from loaf import LoafClient

    loaf = LoafClient()   # reads $LOAF_TRADING_KEY
    print(loaf.portfolio.summary().balances)
    print(loaf.market.list())
    loaf.orders.limit_buy("MUS", quantity=10, price=167.49)   # signed with your trading key

See the README for the full guide.
"""

from __future__ import annotations

from ._object import LoafObject
from ._version import __version__
from .client import LoafClient
from .constants import (
    CANCEL_AFTER_MAX_MS,
    CANCEL_AFTER_MIN_MS,
    DEFAULT_BASE_URL,
    DEFAULT_MAX_SLIPPAGE_BPS,
    DEFAULT_STALE_AFTER,
    L2_BUFFER_MS,
    L2_FEE_RATE_CAP,
    MAX_POOL,
    MAX_PRICE_DECIMALS,
    MAX_QUANTITY_DECIMALS,
    ORDER_TAG_MAX,
    PERPS_DEFAULT_LEVERAGE,
    PERPS_SPOT_SIDE,
)
from .enums import (
    CONDITIONAL_ORDER_TYPES,
    AssetKind,
    CancelByIndexOutcome,
    CandleResolution,
    ConditionalOrderStatus,
    ConditionalOrderType,
    MarketStatus,
    MarketType,
    OrderErrorCode,
    OrderSide,
    OrderStatus,
    OrderType,
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
    PostOnlyWouldTakeError,
    PriceHeldError,
    ReplaceTargetGoneError,
    SendByPassedError,
    TradingHaltedError,
    TradingPausedError,
    WithdrawalOutcomeUnknownError,
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
    "CANCEL_AFTER_MAX_MS",
    "CANCEL_AFTER_MIN_MS",
    "DEFAULT_BASE_URL",
    "DEFAULT_MAX_SLIPPAGE_BPS",
    "DEFAULT_STALE_AFTER",
    "L2_BUFFER_MS",
    "L2_FEE_RATE_CAP",
    "MAX_POOL",
    "MAX_PRICE_DECIMALS",
    "MAX_QUANTITY_DECIMALS",
    "ORDER_TAG_MAX",
    "PERPS_DEFAULT_LEVERAGE",
    "PERPS_SPOT_SIDE",
    # enums
    "OrderSide",
    "OrderType",
    "ConditionalOrderType",
    "TimeInForce",
    "OrderStatus",
    "ConditionalOrderStatus",
    "MarketType",
    "MarketStatus",
    "AssetKind",
    "CandleResolution",
    "OrderErrorCode",
    "CancelByIndexOutcome",
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
    "WithdrawalOutcomeUnknownError",
    "LoafAPIError",
    "LoafAuthError",
    "LoafForbiddenError",
    "TradingHaltedError",
    "TradingPausedError",
    "MarketStateError",
    "MarketHaltedError",
    "PriceHeldError",
    "LoafValidationError",
    "PostOnlyWouldTakeError",
    "SendByPassedError",
    "LoafNotFoundError",
    "LoafConflictError",
    "ReplaceTargetGoneError",
    "LoafRateLimitError",
    "LoafServerError",
    "LoafServiceUnavailableError",
]

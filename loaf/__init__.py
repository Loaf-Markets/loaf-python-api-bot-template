"""Loaf API SDK — a Python client + bot template for the Loaf trading platform.

Quick start::

    from loaf import LoafClient

    loaf = LoafClient()   # reads $LOAF_API_KEY and $LOAF_AGENT_PRIVATE_KEY
    print(loaf.portfolio.component().cash)
    print(loaf.market.properties())
    loaf.orders.limit_buy("opera", quantity=10, price=167.49)   # signed with your agent key

See the README for the full guide.
"""

from __future__ import annotations

from ._object import LoafObject
from ._version import __version__
from .client import LoafClient
from .constants import (
    DEFAULT_BASE_URL,
    DEFAULT_MAX_SLIPPAGE_BPS,
    MAX_PRICE_DECIMALS,
    MAX_QUANTITY_DECIMALS,
)
from .enums import (
    CONDITIONAL_ORDER_TYPES,
    CandleResolution,
    CompetitionRoundStatus,
    ConditionalOrderStatus,
    ConditionalOrderType,
    IpoStatus,
    OfferingOrderStatus,
    OrderSide,
    OrderStatus,
    OrderType,
    PropertyStatus,
    TimeInForce,
    TransferStatus,
    TransferType,
    WSMessageType,
    is_conditional_order,
)
from .exceptions import (
    CompetitionEligibilityError,
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
    OrderOutcomeUnknownError,
    TradingHaltedError,
)
from .money import bps_to_fraction, fraction_to_bps, worst_price
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
    "MAX_PRICE_DECIMALS",
    "MAX_QUANTITY_DECIMALS",
    # enums
    "OrderSide",
    "OrderType",
    "ConditionalOrderType",
    "TimeInForce",
    "OrderStatus",
    "ConditionalOrderStatus",
    "OfferingOrderStatus",
    "PropertyStatus",
    "IpoStatus",
    "TransferType",
    "TransferStatus",
    "CandleResolution",
    "CompetitionRoundStatus",
    "WSMessageType",
    # order-row helpers
    "CONDITIONAL_ORDER_TYPES",
    "is_conditional_order",
    # unit helpers
    "bps_to_fraction",
    "fraction_to_bps",
    "worst_price",
    # exceptions
    "LoafError",
    "LoafConfigError",
    "LoafConnectionError",
    "OrderOutcomeUnknownError",
    "LoafAPIError",
    "LoafAuthError",
    "LoafForbiddenError",
    "TradingHaltedError",
    "CompetitionEligibilityError",
    "LoafValidationError",
    "LoafNotFoundError",
    "LoafConflictError",
    "LoafRateLimitError",
    "LoafServerError",
    "LoafServiceUnavailableError",
]

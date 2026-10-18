"""Loaf API SDK — a Python client + bot template for the Loaf trading platform.

Quick start::

    from loaf import LoafClient

    loaf = LoafClient()   # reads $LOAF_API_KEY and $LOAF_L2_SECRET_KEY
    print(loaf.portfolio.component().balances)
    print(loaf.market.properties())
    loaf.orders.limit_buy("OPRA", quantity=10, price=167.49)   # signed with your L2 trading key

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
    TradeProofStatus,
    TransferStatus,
    TransferType,
    WSMessageType,
    is_conditional_order,
)
from .exceptions import (
    CompetitionEligibilityError,
    KycRequiredError,
    L2SignerError,
    LoafAPIError,
    LoafAuthError,
    LoafBusinessRuleError,
    LoafConfigError,
    LoafConflictError,
    LoafConnectionError,
    LoafError,
    LoafForbiddenError,
    LoafL2Error,
    LoafNotFoundError,
    LoafRateLimitError,
    LoafServerError,
    LoafServiceUnavailableError,
    LoafValidationError,
    OrderOutcomeUnknownError,
    TradingHaltedError,
)
from .l2 import L2Config, OrderIntent
from .money import bps_to_fraction, fraction_to_bps, worst_price
from .ws import LoafWebSocketClient

__all__ = [
    "__version__",
    # client
    "LoafClient",
    "LoafWebSocketClient",
    "LoafObject",
    # L2 signing
    "L2Config",
    "OrderIntent",
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
    "TradeProofStatus",
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
    "LoafL2Error",
    "L2SignerError",
    "LoafAPIError",
    "LoafAuthError",
    "LoafForbiddenError",
    "TradingHaltedError",
    "CompetitionEligibilityError",
    "LoafValidationError",
    "LoafNotFoundError",
    "LoafConflictError",
    "LoafBusinessRuleError",
    "KycRequiredError",
    "LoafRateLimitError",
    "LoafServerError",
    "LoafServiceUnavailableError",
]

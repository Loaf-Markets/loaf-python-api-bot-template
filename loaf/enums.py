"""String enums for the Loaf API.

All values are exact, case-sensitive literals accepted/returned by the API.
Each enum subclasses ``str`` so a member is interchangeable with its string
value::

    >>> OrderSide.BUY == "BUY"
    True
    >>> json.dumps({"side": OrderSide.BUY})
    '{"side": "BUY"}'

You can always pass a plain string instead of an enum member.
"""

from __future__ import annotations

from enum import Enum


class _StrEnum(str, Enum):
    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return self.value


class OrderSide(_StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(_StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class TimeInForce(_StrEnum):
    #: Good-til-cancelled. The default; recommended for bots.
    GTC = "GTC"
    #: Immediate-or-cancel.
    IOC = "IOC"
    #: Fill-or-kill.
    FOK = "FOK"
    #: Good-til-date. Requires a future unix-seconds ``deadline``.
    GTD = "GTD"


class OrderStatus(_StrEnum):
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


class TradeStatus(_StrEnum):
    SETTLING = "SETTLING"
    SETTLED = "SETTLED"
    SETTLEMENT_FAILED = "SETTLEMENT_FAILED"


class OfferingOrderStatus(_StrEnum):
    """Status of an IPO/offering subscription order."""

    PENDING = "PENDING"
    ALLOCATED = "ALLOCATED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class PropertyStatus(_StrEnum):
    PENDING = "PENDING"
    LIVE = "LIVE"
    DELISTED = "DELISTED"


class IpoStatus(_StrEnum):
    PENDING = "PENDING"
    LIVE = "LIVE"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"


class TransferType(_StrEnum):
    DEPOSIT = "DEPOSIT"
    WITHDRAWAL = "WITHDRAWAL"


class TransferStatus(_StrEnum):
    PENDING = "PENDING"
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"


class CandleResolution(_StrEnum):
    """Bucket size for :meth:`loaf.resources.market.MarketResource.candles`."""

    ONE_MINUTE = "1m"
    FIVE_MINUTES = "5m"
    FIFTEEN_MINUTES = "15m"
    ONE_HOUR = "1h"
    FOUR_HOURS = "4h"
    ONE_DAY = "1d"
    ONE_WEEK = "1w"


class CompetitionRoundStatus(_StrEnum):
    """Lifecycle of a trading-competition round (leaderboard / competition info)."""

    DRAFT = "DRAFT"
    PENDING = "PENDING"
    PREPARED = "PREPARED"
    ACTIVE = "ACTIVE"
    ENDING = "ENDING"
    ENDED = "ENDED"


class WSMessageType(_StrEnum):
    """`type` discriminator on every WebSocket frame."""

    # lifecycle
    CONNECTION = "connection"
    AUTH = "auth"
    AUTH_RESULT = "auth_result"
    SUBSCRIBE = "subscribe"
    UNSUBSCRIBE = "unsubscribe"
    SUBSCRIPTION_CONFIRMED = "subscription_confirmed"
    ERROR = "error"
    ECHO = "echo"
    # public market data
    ORDERBOOK_UPDATE = "orderbook_update"
    TRADES_BATCH = "trades_batch"
    CANDLE_UPDATE = "candle_update"
    MARK_PRICE = "mark_price"
    IPO_ALLOCATION_UPDATE = "ipo_allocation_update"
    VOLUME_UPDATE = "volume_update"
    LEADERBOARD_UPDATE = "leaderboard_update"
    PROPERTY_HALT = "property_halt"
    # private portfolio deltas
    BALANCES_UPDATE = "balances_update"
    POSITION_UPDATE = "position_update"
    ORDER_STATUS = "order_status"
    ORDER_UPDATE = "order_update"
    TRADE_NEW = "trade_new"
    LIFETIME_VOLUME_UPDATE = "lifetime_volume_update"
    TRANSFER_UPDATE = "transfer_update"
    OFFERING_ORDER_UPDATE = "offering_order_update"

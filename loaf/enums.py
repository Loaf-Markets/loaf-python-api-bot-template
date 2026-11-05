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
from typing import Any, Mapping


class _StrEnum(str, Enum):
    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return self.value


class OrderSide(_StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(_StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class ConditionalOrderType(_StrEnum):
    STOP_LIMIT = "STOP_LIMIT"
    STOP_MARKET = "STOP_MARKET"
    TAKE_LIMIT = "TAKE_LIMIT"
    TAKE_MARKET = "TAKE_MARKET"


class TimeInForce(_StrEnum):
    """An order's time in force (``time_in_force=``). Order rows carry ``GTC``, ``IOC`` or
    ``FOK``: the exchange keeps a post-only order as ``GTC``, and a MARKET order sent as
    ``GTC`` as ``IOC``."""

    #: Good-til-cancelled: what does not fill at once rests on the book (a MARKET order never
    #: rests: the exchange runs it as IOC).
    GTC = "GTC"
    #: Immediate-or-cancel: what does not fill at once is cancelled; one that fills nothing is
    #: refused.
    IOC = "IOC"
    #: Fill-or-kill: fills its whole quantity at once, or is refused and fills nothing.
    FOK = "FOK"
    #: A LIMIT order's alone: refused if it would trade at once, so it only ever rests.
    POST_ONLY = "POST_ONLY"


class OrderStatus(_StrEnum):
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"


class ConditionalOrderStatus(_StrEnum):
    #: An attached TP/SL leg waiting on its parent BUY to fill COMPLETELY.
    PENDING = "PENDING"
    #: Watching the mark; fires the moment it sits at or through the trigger.
    ARMED = "ARMED"
    #: Triggered and booked — follow ``placedOrderId`` from here for the fills.
    PLACED = "PLACED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class MarketType(_StrEnum):
    """A market's type, which its index decides (a perp's ticker ends in ``-PERP``)."""

    SPOT = "SPOT"
    PERP = "PERP"


class MarketStatus(_StrEnum):
    PENDING = "PENDING"
    LIVE = "LIVE"
    DELISTED = "DELISTED"


class AssetKind(_StrEnum):
    """What an asset is: a market's ``kind`` is its asset's."""

    PROPERTY = "PROPERTY"
    COMMODITY = "COMMODITY"
    CRYPTO = "CRYPTO"
    INDEX = "INDEX"


class CandleResolution(_StrEnum):
    """Bucket size for :meth:`loaf.resources.market.MarketResource.candles`."""

    ONE_MINUTE = "1m"
    FIVE_MINUTES = "5m"
    FIFTEEN_MINUTES = "15m"
    ONE_HOUR = "1h"
    FOUR_HOURS = "4h"
    ONE_DAY = "1d"
    ONE_WEEK = "1w"


class OrderErrorCode(_StrEnum):
    """The stable reason of a refused order request: ``code`` on the
    :class:`~loaf.exceptions.LoafAPIError` of a placement, a cancel,
    ``cancel_after``, ``cancel_by_index`` or a withdrawal
    (:meth:`~loaf.resources.funds.FundsResource.withdraw`).

    Compare an error's ``code`` with these, never its message: the message is
    for people and may change, a code never is renamed, reused or removed. The
    exchange may add codes, so an error can carry one this list does not name:
    treat it by its status (its exception class).
    """

    # The order's terms: the same order is refused again; change it.
    #: Its free balance or margin cannot cover it (a withdrawal: your spot USDC available).
    INSUFFICIENT_BALANCE = "INSUFFICIENT_BALANCE"
    #: Below the minimum order value (a withdrawal: below the minimum withdrawal).
    BELOW_MIN_ORDER_VALUE = "BELOW_MIN_ORDER_VALUE"
    #: Above its own maximum order value.
    ABOVE_MAX_ORDER_VALUE = "ABOVE_MAX_ORDER_VALUE"
    #: Its price is outside the market's price band: on a perp, the band around its mark, or a
    #: stop's, take's or TP/SL leg's limit outside the band around its own trigger.
    PRICE_BAND = "PRICE_BAND"
    #: :class:`~loaf.exceptions.PostOnlyWouldTakeError`.
    POST_ONLY_WOULD_TAKE = "POST_ONLY_WOULD_TAKE"
    #: An IOC or MARKET order of which nothing could fill.
    NOT_FILLED = "NOT_FILLED"
    FOK_NOT_FILLED = "FOK_NOT_FILLED"
    #: Self-trade prevention stopped it against your own resting order. Not an SDK order's:
    #: the SDK's orders cancel the resting one instead, and go on matching.
    SELF_TRADE = "SELF_TRADE"
    OPEN_ORDER_LIMIT = "OPEN_ORDER_LIMIT"
    #: A stop or take whose trigger is already reached.
    TRIGGER_THROUGH = "TRIGGER_THROUGH"
    #: Its signed fee-rate cap is below the rate it trades at.
    FEE_RATE_CAP_TOO_LOW = "FEE_RATE_CAP_TOO_LOW"
    # The signed fields: sign again.
    #: The signature, its key or its account does not verify (401).
    INVALID_SIGNATURE = "INVALID_SIGNATURE"
    #: Its order number is used, by another order or voided by ``cancel_by_index``. A
    #: withdrawal's number is its nonce: one spent first by another op (withdraw again).
    ORDER_INDEX_USED = "ORDER_INDEX_USED"
    #: Its expiry is inside the buffer or past the longest order life from now.
    ORDER_EXPIRY_INVALID = "ORDER_EXPIRY_INVALID"
    #: Its epoch is not the account's: the account was reset or cancelled on chain.
    SETTLE_EPOCH_MISMATCH = "SETTLE_EPOCH_MISMATCH"
    # Timing and replace: nothing placed.
    #: :class:`~loaf.exceptions.SendByPassedError`: its order number is not spent.
    SEND_BY_PASSED = "SEND_BY_PASSED"
    #: :class:`~loaf.exceptions.ReplaceTargetGoneError`: nothing changed.
    REPLACE_TARGET_GONE = "REPLACE_TARGET_GONE"
    # The market's state: nothing placed; the same order may be taken later.
    #: No market has its index.
    MARKET_NOT_FOUND = "MARKET_NOT_FOUND"
    #: Not live (pending, delisted, expired) or its trading has not started.
    MARKET_NOT_LIVE = "MARKET_NOT_LIVE"
    #: Live, but the exchange has no book for it: a fault on Loaf's side (503).
    MARKET_UNAVAILABLE = "MARKET_UNAVAILABLE"
    MARKET_HALTED = "MARKET_HALTED"
    #: Outside its trading hours.
    MARKET_CLOSED = "MARKET_CLOSED"
    #: The market takes only orders that reduce a position.
    REDUCE_ONLY = "REDUCE_ONLY"
    #: Reduce-only while its latest price waits for review.
    PRICE_HELD = "PRICE_HELD"
    #: Reduce-only while its price is stale.
    PRICE_STALE = "PRICE_STALE"
    #: The platform's trading halt.
    TRADING_HALTED = "TRADING_HALTED"
    #: The exchange paused trading: send it again after ``retry_after`` (503).
    TRADING_PAUSED = "TRADING_PAUSED"
    # Cancels.
    ORDER_NOT_FOUND = "ORDER_NOT_FOUND"
    ORDER_NOT_OWNED = "ORDER_NOT_OWNED"
    #: Already filled, cancelled or expired.
    ORDER_NOT_CANCELLABLE = "ORDER_NOT_CANCELLABLE"
    #: ``cancel_by_index``: no live range reserved to the account holds the number.
    ORDER_INDEX_NOT_RESERVED = "ORDER_INDEX_NOT_RESERVED"
    # The request and the service.
    #: Malformed; ``details`` names the fields when the exchange's validation refused it.
    INVALID_REQUEST = "INVALID_REQUEST"
    UNAUTHENTICATED = "UNAUTHENTICATED"
    #: Too many requests (429). A withdrawal's 422: too many withdrawals, yours or everyone's, not
    #: yet settled on chain: withdraw again later.
    RATE_LIMITED = "RATE_LIMITED"
    #: The exchange did not process it, and nothing was placed: send it again (503).
    NOT_PROCESSED = "NOT_PROCESSED"
    #: Unconfirmed, and no admitted order carries its number: send the same order again (503).
    UNCONFIRMED = "UNCONFIRMED"
    #: Refused for a reason the exchange does not classify: the message says it.
    UNKNOWN_REASON = "UNKNOWN_REASON"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class CancelByIndexOutcome(_StrEnum):
    """What :meth:`~loaf.resources.orders.OrdersResource.cancel_by_index` did."""

    #: The order rested on the number and is now cancelled.
    CANCELLED = "CANCELLED"
    #: An admitted order carries the number and no longer rests (``status`` says how it
    #: ended); nothing changed.
    ENDED = "ENDED"
    #: No admitted order carried the number, and none ever will: an order still in flight on
    #: it is refused ``ORDER_INDEX_USED`` whenever it arrives.
    VOIDED = "VOIDED"


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
    VOLUME_UPDATE = "volume_update"
    MARKET_HALT = "market_halt"
    PERPS_MARKETS_LIVE = "perps_markets_live"
    MARKET_TERMS_UPDATE = "market_terms_update"
    # private portfolio deltas
    BALANCES_UPDATE = "balances_update"
    ORDER_STATUS = "order_status"
    ORDER_UPDATE = "order_update"
    TRADE_NEW = "trade_new"
    ORDER_QUEUE = "order_queue"
    LIFETIME_VOLUME_UPDATE = "lifetime_volume_update"
    PERPS_ACCOUNT_UPDATE = "perps_account_update"
    FUNDING_PAYMENT = "funding_payment"
    HOLDING_UPDATE = "holding_update"
    TRANSFER_UPDATE = "transfer_update"
    FEES_UPDATE = "fees_update"
    L2_KEYS_UPDATE = "l2_keys_update"
    PERPS_LEVERAGE_UPDATE = "perps_leverage_update"


#: The four ``type`` values that mark an order row as a conditional (stop / take).
CONDITIONAL_ORDER_TYPES = frozenset(t.value for t in ConditionalOrderType)


def is_conditional_order(order: Mapping[str, Any]) -> bool:
    return order.get("type") in CONDITIONAL_ORDER_TYPES

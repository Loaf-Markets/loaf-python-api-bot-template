"""Typed descriptions of the documented response shapes.

These are :class:`typing.TypedDict` definitions used purely for editor
autocomplete, type-checking and documentation. At runtime the SDK returns
:class:`~loaf._object.LoafObject` instances (dicts), which also support
attribute access (``order.price``). New/extra fields the backend adds are
preserved at runtime even though they are not listed here.

Every monetary field is in plain dollars and every quantity in plain tokens
unless explicitly noted (see :mod:`loaf.money`). Timestamps are unix seconds.
"""

from __future__ import annotations

from typing import Any, Optional, TypedDict


class PriceLevel(TypedDict):
    price: float  # dollars
    quantity: float  # tokens


class OrderBook(TypedDict, total=False):
    propertyId: int
    bids: list[PriceLevel]
    asks: list[PriceLevel]


class Candle(TypedDict):
    time: int  # unix seconds (bucket start)
    open: float
    high: float
    low: float
    close: float
    volume: float  # tokens


class CandleHistory(TypedDict, total=False):
    """Response to ``GET /trade/{token}/candles`` (see ``market.candles``)."""

    resolution: str  # CandleResolution
    candles: list[Candle]  # oldest -> newest
    oldestTs: Optional[int]  # pass as `to` to page back; None when empty
    hasMore: bool  # at least one older candle exists before oldestTs


class TradeTick(TypedDict, total=False):
    """A public trade (from a property detail page or the ``trades`` WS channel)."""

    tradeId: int
    propertyId: int
    aggressorSide: str  # OrderSide of the taker
    price: float
    quantity: float
    timestamp: int


class OrderResult(TypedDict, total=False):
    """Response to ``POST /orders`` and the cancel endpoints (exchange acknowledgement)."""

    success: bool
    orderId: int
    errorMessage: str


class CancelAllResult(TypedDict, total=False):
    requestedCount: int
    cancelledOrderIds: list[int]
    failedOrders: list[dict[str, Any]]  # [{orderId, errorMessage}]


class OrderHistoryItem(TypedDict, total=False):
    """One row of ``openOrders`` / ``orderHistory`` / ``history.orders()``.

    A row is either a booked order or a resting conditional (stop / take), and
    ``type`` is the discriminator — use
    :func:`loaf.enums.is_conditional_order`. A conditional carries
    ``triggerPrice`` and a ``status`` from
    :class:`~loaf.enums.ConditionalOrderStatus`, and has NO ``filledQuantity``
    and NO ``filledAt``: the keys are absent, not null, so reading either one
    off a conditional row raises. Narrow before you touch them.
    """

    id: int
    propertyId: int
    tokenName: str  # "" on a conditional for a delisted/uncached property — key on propertyId
    side: str  # OrderSide
    type: str  # OrderType, or ConditionalOrderType — THIS is the discriminator
    timeInForce: str
    quantity: float
    price: Optional[float]  # None for market orders; on a conditional, the SIGNED limit price
    status: str  # OrderStatus, or ConditionalOrderStatus on a conditional
    filledQuantity: float  # booked orders only — absent on a conditional
    rejectionReason: Optional[str]  # on a live ARMED row: the last deferred attempt, not a failure
    deadline: int
    filledAt: Optional[int]  # booked orders only — absent on a conditional
    cancelledAt: Optional[int]
    createdAt: int
    triggerPrice: float  # conditional only; dollars; the mark level that fires the row
    parentOrderId: Optional[int]  # conditional only; the BUY a TP/SL leg arms on
    placedOrderId: Optional[int]  # conditional only; the booked order id once PLACED — cancel THIS
    triggeredAt: Optional[int]  # conditional only; unix seconds


class TradeHistoryItem(TypedDict, total=False):
    tradeId: int
    propertyId: int
    tokenName: str
    txHash: str
    side: str  # OrderSide, relative to this user
    quantity: float
    price: float
    fee: float  # this user's fee, dollars
    executedAt: int
    status: str  # TradeStatus


class Position(TypedDict, total=False):
    propertyId: int
    tokenName: str
    quantity: float  # tradeable (total minus frozen)
    totalQuantity: float
    averageEntryPrice: float
    marketPrice: float
    percentChange: float  # plain percent
    percentOfPortfolio: float
    propertyPnl: float
    propertyPnlPercent: float
    isIpoAllocation: bool
    imageUrl: str


class PortfolioComponent(TypedDict, total=False):
    cash: float
    frozen: float
    portfolioValue: float
    portfolioPnl: float
    portfolioPnlPercent: float
    lifetimeVolume: float  # dollars traded over the account's lifetime
    positions: list[Position]
    applicableFees: dict[str, int]  # {takerFeeBps, makerFeeBps}
    offeringOrders: list[dict[str, Any]]
    openOrders: list[OrderHistoryItem]  # booked orders + PENDING/ARMED conditionals, merged
    tradeHistory: list[TradeHistoryItem]
    orderHistory: list[OrderHistoryItem]  # newest rows of both kinds, ANY status
    transfers: list[dict[str, Any]]


class IpoSubscribeResult(TypedDict, total=False):
    success: bool
    subscriptionId: int  # the IPO order id to track
    allocatedQuantity: float  # may be less than requested if partial
    errorMessage: str


class LeaderboardEntry(TypedDict, total=False):
    rank: int
    handle: Optional[str]
    walletAddress: str
    points: float  # rounded to 2 decimals


class PrizePoolEntry(TypedDict, total=False):
    """One row of a round's ``prizePool``.

    A row is either a single place or a BAND: when ``toPlace`` is present the
    row covers ``place``..``toPlace`` INCLUSIVE, and ``amount`` is what EACH
    participant in that band receives (not the band's total). Test a finishing
    place with ``e["place"] <= place <= e.get("toPlace", e["place"])`` — an
    equality check on ``place`` misses everyone inside a band. Rows never
    overlap. Display only: the backend does not distribute prizes.
    """

    place: int  # first paid place in this row (1 = winner)
    toPlace: int  # inclusive last place of the band; absent for a single place
    amount: float  # whole USDC, per participant


class VolumeMultiplierTier(TypedDict, total=False):
    """A traded-volume tier of the competition points formula."""

    minVolume: float  # dollars traded to reach this tier
    multiplier: float


class CompetitionRoundWinner(TypedDict, total=False):
    """A paid place on a finished round's frozen final leaderboard."""

    place: int
    handle: Optional[str]
    walletAddress: str


class CompetitionRound(TypedDict, total=False):
    """One round in ``competition.info()['rounds']`` (newest first).

    Every round is listed whatever its ``status`` — including ones that have
    not started — so select by ``status``, not by position.
    """

    roundNumber: int
    name: str
    rules: str
    startsAt: Optional[int]  # unix seconds; None until the round starts
    endsAt: Optional[int]
    startingBalanceUsdl: float  # whole USDC seeded per participant
    participantBatchSize: int  # how many the queue admits into the round
    status: str  # CompetitionRoundStatus
    newAssetProperty: Optional[dict[str, Any]]  # the round's headline property
    prizePool: list[PrizePoolEntry]
    volumeMultiplierTiers: list[VolumeMultiplierTier]
    bottomCullPercent: float  # 0-100, lowest-ranked share dropped at round end
    # Top 10 paid places only (a podium, not the full paid list); None until the
    # round is ENDING/ENDED, and while a finished round's results are pending.
    winners: Optional[list[CompetitionRoundWinner]]


class CompetitionInfo(TypedDict, total=False):
    """Response to ``GET /competition`` (see ``competition.info``)."""

    nextRoundSoon: bool  # operator's "a round is coming" announcement flag
    rounds: Optional[list[CompetitionRound]]  # None when no round exists yet
    makerFeeBps: int  # base (lowest volume tier) fees
    takerFeeBps: int


class QueuePosition(TypedDict, total=False):
    """Response to ``GET /competition/queue-position``.

    ``leaderboardPosition`` is your rank on the currently-served board (live
    while a round is ACTIVE, your final placement during the break) and
    ``position`` is your place in the admission queue. Usually only one is set,
    but both are during the break if you were bottom-culled — back in the queue
    and still on the frozen board.
    """

    position: Optional[int]  # place in the admission queue, if still queued
    queueCount: int
    leaderboardPosition: Optional[int]  # your rank on the served board
    referralCount: int
    estimatedRoundEntry: Optional[int]  # round that position is on track to enter


__all__ = [
    "PriceLevel",
    "OrderBook",
    "Candle",
    "CandleHistory",
    "TradeTick",
    "OrderResult",
    "CancelAllResult",
    "OrderHistoryItem",
    "TradeHistoryItem",
    "Position",
    "PortfolioComponent",
    "IpoSubscribeResult",
    "LeaderboardEntry",
    "PrizePoolEntry",
    "VolumeMultiplierTier",
    "CompetitionRoundWinner",
    "CompetitionRound",
    "CompetitionInfo",
    "QueuePosition",
]

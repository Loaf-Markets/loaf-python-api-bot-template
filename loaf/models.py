"""Typed descriptions of the documented response shapes.

These are :class:`typing.TypedDict` definitions used purely for editor
autocomplete, type-checking and documentation. At runtime the SDK returns
:class:`~loaf._object.LoafObject` instances (dicts), which also support
attribute access (``order.price``). New/extra fields the backend adds are
preserved at runtime even though they are not listed here.

Every monetary field is in plain dollars and every quantity in plain tokens
(see :mod:`loaf.money`), except ``balances`` and a transfer's ``amount``:
decimal strings in the token's base units (:func:`loaf.from_base_units`).
Timestamps are unix seconds, except ``expiryMs`` (unix milliseconds).
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
    """Response to ``GET /trade/{ticker}/candles`` (see ``market.candles``)."""

    resolution: str  # CandleResolution
    candles: list[Candle]  # oldest -> newest
    oldestTs: Optional[int]  # pass as `to` to page back; None when empty
    hasMore: bool  # at least one older candle exists before oldestTs


class L2Market(TypedDict):
    """``property.l2Market`` of ``GET /trade/{ticker}``: the L2 market every order
    on the property signs (the SDK reads it for you)."""

    chainId: int  # the chain the proxy is on, not what an order signs
    proxyAddress: str  # the ZkLoaf proxy, or a competition round's vault
    l2ChainId: int
    marketIndex: int
    baseAssetId: int  # the property token's balances row
    quoteAssetId: int  # the cash balances row


class TradeTick(TypedDict, total=False):
    """A public trade: the detail's ``recentTrades`` (newest first) or a ``trades_batch`` frame."""

    tradeId: int
    propertyId: int
    txHash: str  # the trade's content hash (0x + 64 hex), not an on-chain transaction
    aggressorSide: str  # OrderSide of the taker
    price: float
    quantity: float
    timestamp: int


class OrderResult(TypedDict, total=False):
    """Response to ``POST /orders`` (``orders.create`` and its wrappers)."""

    success: bool
    orderId: int
    # OrderStatus as of the commit that admitted it, or for a duplicate as it is now (it may have
    # filled or been cancelled since); OPEN / PARTIALLY_FILLED mean it is resting.
    status: str
    quantityLeft: float  # tokens not filled (the cancelled remainder on CANCELLED)
    duplicate: bool  # an order with this account and order number already existed
    attachedLegIds: list[int]  # the legs' conditional ids, tp first; [] if none or a duplicate


class OrderAck(TypedDict, total=False):
    """Response to ``POST /orders/conditional`` and the single cancels."""

    success: bool
    orderId: int


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
    ticker: str  # "" on a conditional for a delisted/uncached property — key on propertyId
    side: str  # OrderSide
    type: str  # OrderType, or ConditionalOrderType — THIS is the discriminator
    timeInForce: str
    quantity: float
    # The signed limit; for a MARKET order the worst price it was signed at; on a
    # conditional, the price it books at.
    price: float
    status: str  # OrderStatus, or ConditionalOrderStatus on a conditional
    filledQuantity: float  # booked orders only — absent on a conditional
    # Conditionals only: on a live ARMED row the last deferred attempt, not a failure;
    # always None on booked orders.
    rejectionReason: Optional[str]
    expiryMs: int  # unix ms, the signed expiry
    filledAt: Optional[int]  # booked orders only — absent on a conditional
    cancelledAt: Optional[int]
    createdAt: int
    triggerPrice: float  # conditional only; dollars; the mark level that fires the row
    parentOrderId: Optional[int]  # conditional only; the BUY a TP/SL leg arms on
    placedOrderId: Optional[int]  # conditional only; the booked order id once PLACED — cancel THIS
    triggeredAt: Optional[int]  # conditional only; unix seconds


class TradeProofStatus(TypedDict, total=False):
    """A trade's ``proof``: ``{"status": "PENDING"}`` until a proven batch includes
    the trade, then ``IN_BATCH`` with the other fields."""

    status: str  # "PENDING" or "IN_BATCH"
    batchNumber: int
    l2BlockNumber: int
    rowIndex: int
    l2TradeDigest: str  # 64 hex, no 0x
    batchTxHash: Optional[str]  # the transaction that executed the batch; None until then
    final: bool


class TradeHistoryItem(TypedDict, total=False):
    tradeId: int
    propertyId: int
    ticker: str
    txHash: str  # the trade's content hash (0x + 64 hex), not an on-chain transaction
    side: str  # OrderSide, relative to this user
    quantity: float
    price: float
    fee: float  # this user's fee, dollars, to 0.001
    executedAt: int
    proof: Optional[TradeProofStatus]  # None with proofs off, and for a competition round's trade


class L2AssetBalance(TypedDict):
    """One row of ``balances``: amounts are decimal strings in the token's base units."""

    assetId: int
    symbol: str
    decimals: int
    isQuote: bool  # a market's quote asset: cash
    total: str
    hold: str  # held for your resting orders
    available: str  # total minus hold: what a new order can use


class Position(TypedDict, total=False):
    """A held property: facts only. An L2 holding's amounts are its ``balances`` row."""

    propertyId: int
    ticker: str
    tokenName: str  # display name
    totalTokens: float  # the property's total token supply
    averageEntryPrice: Optional[float]  # None when the cost is unknown
    marketPrice: float
    isIpoAllocation: bool
    assetId: int  # L2 holding only: its balances row
    quantity: float  # IPO allocation only, tokens
    imageUrl: str
    sparkline: list[float]


class PortfolioComponent(TypedDict, total=False):
    balances: list[L2AssetBalance]  # the whole L2 book; a missing row is 0
    lifetimeVolume: float  # dollars traded over the account's lifetime
    positions: list[Position]
    applicableFees: dict[str, int]  # {takerFeeBps, makerFeeBps}
    offeringOrders: list[dict[str, Any]]
    openOrders: list[OrderHistoryItem]  # booked orders + PENDING/ARMED conditionals, merged
    tradeHistory: list[TradeHistoryItem]
    orderHistory: list[OrderHistoryItem]  # newest rows of both kinds, ANY status
    transfers: list[dict[str, Any]]  # newest first, keyed by journalSeq; `amount` in base units


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
    startsAt: Optional[int]  # unix seconds; the schedule, re-stamped when the round goes ACTIVE
    endsAt: Optional[int]  # likewise, re-stamped when it leaves ACTIVE
    startingBalanceUsdl: float  # whole play cUSD seeded on the round's vault (+ referral bonus)
    participantBatchSize: int  # how many the queue admits into the round
    status: str  # CompetitionRoundStatus
    newAssetProperty: Optional[dict[str, Any]]  # the round's market (a GET /trade row)
    prizePool: list[PrizePoolEntry]
    volumeMultiplierTiers: list[VolumeMultiplierTier]
    volumeExponent: float  # e in points = a * volume^e + b * pnl
    pnlMultiplier: float  # b
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
    participant: bool  # a round is PREPARED/ACTIVE and you hold a seat in it


__all__ = [
    "PriceLevel",
    "OrderBook",
    "Candle",
    "CandleHistory",
    "L2Market",
    "TradeTick",
    "OrderResult",
    "OrderAck",
    "CancelAllResult",
    "OrderHistoryItem",
    "TradeProofStatus",
    "TradeHistoryItem",
    "L2AssetBalance",
    "Position",
    "PortfolioComponent",
    "LeaderboardEntry",
    "PrizePoolEntry",
    "VolumeMultiplierTier",
    "CompetitionRoundWinner",
    "CompetitionRound",
    "CompetitionInfo",
    "QueuePosition",
]

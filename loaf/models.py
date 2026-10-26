"""Typed descriptions of the documented response shapes.

These are :class:`typing.TypedDict` definitions used purely for editor
autocomplete, type-checking and documentation. At runtime the SDK returns
:class:`~loaf._object.LoafObject` instances (dicts), which also support
attribute access (``order.price``). New/extra fields the backend adds are
preserved at runtime even though they are not listed here.

Every monetary field is in plain dollars and every quantity in plain tokens
(see :mod:`loaf.money`), except ``balances`` and a transfer's ``amount``:
decimal strings in the token's base units (:func:`loaf.from_base_units`), and
the perps shapes, in the exchange engine's units
(:meth:`loaf.resources.perps.PerpsResource.positions`). Timestamps are unix
seconds, except ``expiryMs`` (unix milliseconds).
"""

from __future__ import annotations

from typing import Any, Optional, TypedDict, Union


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


class L2SpotMarket(TypedDict):
    """``property.l2Market`` of ``GET /trade/{ticker}`` for a spot market: the L2
    market every order on the property signs (the SDK reads it for you)."""

    kind: str  # "spot"
    chainId: int  # the chain the proxy is on, not what an order signs
    proxyAddress: str  # the ZkLoaf proxy
    l2ChainId: int
    marketIndex: int  # 2048 to 4094
    baseAssetId: int  # the property token's balances row
    quoteAssetId: int  # the cash balances row


class L2PerpsMarket(TypedDict):
    """``property.l2Market`` for a perps market, and each row of
    ``perps.markets()``: a perpetual future settled in USDC, with the terms its
    orders are admitted against."""

    kind: str  # "perps"
    chainId: int
    proxyAddress: str
    l2ChainId: int
    marketIndex: int  # 0 to 254
    quoteAssetId: int  # 3, USDC; a perps market has no base asset
    status: str  # "ACTIVE", or "EXPIRED": no more orders; positions leave by perps.exit()
    sem: int  # the market's size extension multiplier
    qem: str  # its quote extension multiplier, a decimal string (it can pass 2^53)
    takerFee: int  # per 1,000,000; an order's signed fee-rate cap must cover it
    makerFee: int
    risk: int  # the maintenance margin factor per 65,536 (initial margin is twice it)
    band: int  # how far (bp) an order's price may sit from the index; 0: no bound
    flags: int  # PERPS_FLAG_CROSS (pool 0 may trade it) | PERPS_FLAG_REDUCE_ONLY
    oiCap: int  # the open-interest cap, in base units
    minBase: int  # the least signed quantity of an order that adds to a position; 0: none
    minQuote: int  # the least quote (signed quantity x signed price / 10) of one; 0: none
    quoteLimit: int  # the most quote of any order; 0: none


#: ``property.l2Market``: a spot or a perps market, by ``kind``.
L2Market = Union[L2SpotMarket, L2PerpsMarket]


class PerpsCollateral(TypedDict):
    """``perps.collateral()``: the one asset every margin pool holds (USDC)."""

    assetId: int  # 3
    symbol: str
    decimals: int  # the token's: a display amount is base units / 10^decimals
    tickSize: str  # the token's base units per tick, a decimal string: an allocate moves ticks
    extensionMultiplier: str  # the reads' extended units per tick, a decimal string


class PerpsMarkets(TypedDict):
    """``perps.markets()``: every perps market, ascending by ``marketIndex``, and the
    collateral their pools hold."""

    markets: list[L2PerpsMarket]
    collateral: PerpsCollateral


class PerpsPosition(TypedDict, total=False):
    """One open position of a pool. Amounts are decimal strings in the engine's units."""

    market: int  # the perps market, 0 to 254
    base: str  # signed, in base units (an order's signed quantity): + long, - short
    fundingIndex: str  # the funding index it last settled at, 32.32 fixed point
    entryQuote: str  # the quote of the fills that opened and grew it
    entryPrice: str  # 10 x entryQuote / |base|, in the market's price units
    fundingOwed: str  # extended USDC units: positive pays, negative receives
    notional: str  # |base| x index x qem, extended USDC units
    liquidationPrice: Optional[str]  # the index past which the pool is liquidatable, or None
    adlRank: Optional[int]  # where served: 1 (deleveraged last) to 5 (first); None: unranked


class PerpsPool(TypedDict):
    """One margin pool of yours: its collateral and positions."""

    pool: int  # 0, the cross pool, or 1 to 7
    collateral: str  # signed, extended USDC units
    value: Optional[str]  # collateral + each position's signed notional - its funding owed
    requirement: Optional[str]  # maintenance margin, extended USDC units x 65,536
    liquidatable: Optional[bool]  # value < 0 or value x 65,536 < requirement
    initialMarginOk: Optional[bool]  # value x 65,536 >= 2 x requirement: collateral may leave
    positions: list[PerpsPosition]  # ascending by market, at most 8


class PerpsAccount(TypedDict):
    """``perps.positions()``: your pools that hold anything, ascending."""

    l2AccountIndex: int
    pools: list[PerpsPool]


class PerpsFunding(TypedDict):
    """One row of ``perps.funding()``."""

    pool: int
    market: int
    fundingIndex: str
    fundingOwed: str  # extended USDC units: positive pays, negative receives


class PerpsOpAccepted(TypedDict):
    """The 202 to a perps allocate or exit."""

    journalSeq: int  # its place in the exchange's journal
    duplicate: bool  # a re-send of an op applied before: nothing was applied again


class TradeTick(TypedDict, total=False):
    """A public trade: the detail's ``recentTrades`` or a ``trades_batch`` frame (newest first),
    or a ``market.trades`` row (oldest first).

    On a perps market the tape also carries liquidation takes: a liquidated position taken
    against a resting order. A take has ``isLiquidation: True`` and is no trade: it may have no
    ``txHash``, and its ``aggressorSide`` is the liquidated side. A trade always has its
    ``txHash``, and ``isLiquidation`` absent or ``False``."""

    tradeId: int
    propertyId: int
    txHash: str  # the trade's content hash (0x + 64 hex), not an on-chain transaction
    aggressorSide: str  # OrderSide of the taker (a take's: the liquidated side)
    price: float
    quantity: float
    timestamp: int
    isLiquidation: bool  # True on a liquidation take; absent or False on a trade


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


class OrderQueueEntry(TypedDict):
    """One of your resting orders in an ``order_queue`` frame and its place in its price
    level's queue (time priority)."""

    orderId: int
    side: str  # OrderSide
    price: float  # dollars
    quantity: float  # tokens: its unfilled quantity in the book
    quantityAhead: float  # tokens: the unfilled quantity of the level's orders ahead of it
    ordersAhead: int  # how many of the level's orders are ahead of it


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
    orderId: int  # this user's order the fill belongs to
    propertyId: int
    ticker: str
    txHash: str  # the trade's content hash (0x + 64 hex), not an on-chain transaction
    side: str  # OrderSide, relative to this user
    role: str  # "MAKER" | "TAKER": the resting order is the MAKER, the aggressor's the TAKER
    quantity: float
    price: float
    fee: float  # this user's fee, dollars, to 0.001
    executedAt: int
    proof: Optional[TradeProofStatus]  # None with proofs off


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
    participantBatchSize: int  # how many the queue admits into the round
    status: str  # CompetitionRoundStatus
    newAssetProperty: Optional[dict[str, Any]]  # the round's headline asset (a GET /trade row)
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
    while a round is ACTIVE, your final placement during the break),
    ``leaderboardPoints`` your points on it, and ``position`` is your place in
    the admission queue. Usually only one of the rank and the place is set, but
    both are during the break if you were bottom-culled — back in the queue and
    still on the frozen board.
    """

    position: Optional[int]  # place in the admission queue, if still queued
    queueCount: int
    leaderboardPosition: Optional[int]  # your rank on the served board
    leaderboardPoints: Optional[float]  # your points there; None exactly when the rank is
    referralCount: int
    estimatedRoundEntry: Optional[int]  # round that position is on track to enter


__all__ = [
    "PriceLevel",
    "OrderBook",
    "Candle",
    "CandleHistory",
    "L2SpotMarket",
    "L2PerpsMarket",
    "L2Market",
    "PerpsCollateral",
    "PerpsMarkets",
    "PerpsPosition",
    "PerpsPool",
    "PerpsAccount",
    "PerpsFunding",
    "PerpsOpAccepted",
    "TradeTick",
    "OrderResult",
    "OrderAck",
    "CancelAllResult",
    "OrderHistoryItem",
    "OrderQueueEntry",
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

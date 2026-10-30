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

from typing import Any, Optional, TypedDict


class PriceLevel(TypedDict):
    price: float  # dollars
    quantity: float  # tokens


class OrderBook(TypedDict, total=False):
    marketIndex: int
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


class MarketL2(TypedDict):
    """``market.l2`` of ``GET /trade/{ticker}`` for a spot market (``type`` ``"SPOT"``): with
    the market's ``marketIndex``, what every order on the market signs, and its custody (the
    SDK reads both for you)."""

    chainId: int  # the chain the proxy is on, not what an order signs
    proxyAddress: str  # the ZkLoaf proxy
    l2ChainId: int
    baseAssetId: int  # the asset token's balances row
    quoteAssetId: int  # the cash balances row


class PerpMarketL2(TypedDict):
    """``market.l2`` of ``GET /trade/{ticker}`` for a perp (``type`` ``"PERP"``): a perpetual
    future settled in USDC, its custody and the terms its orders are admitted against. It has
    no base asset: an order signs its pool."""

    chainId: int
    proxyAddress: str
    l2ChainId: int
    quoteAssetId: int  # 3, USDC
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


class L2PerpsMarket(PerpMarketL2):
    """A row of ``perps.markets()``: a perp's ``marketIndex`` and its ``l2``."""

    marketIndex: int  # 0 to 254


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
    collateral: Optional[PerpsCollateral]  # None when the exchange could not read it just now


class PerpsPosition(TypedDict):
    """One open position of a pool. Amounts are decimal strings in the engine's units."""

    market: int  # the perps market, 0 to 254
    base: str  # signed, in base units (an order's signed quantity): + long, - short
    fundingIndex: str  # the funding index it last settled at, 32.32 fixed point
    entryQuote: str  # the quote of the fills that opened and grew it
    entryPrice: str  # 10 x entryQuote / |base|, in the market's price units
    fundingOwed: str  # extended USDC units: positive pays, negative receives
    notional: str  # |base| x index x qem, extended USDC units
    liquidationPrice: Optional[str]  # the index past which the pool is liquidatable, or None
    adlRank: Optional[int]  # 1 (deleveraged last) to 5 (first); None: unranked


class PerpsOpenOrder(TypedDict):
    """One of a pool's open perps orders, as the engine's admission counts them."""

    orderId: int
    market: int  # the perp's market index
    side: str  # OrderSide
    quantityLeft: str  # what is left of it, base units (tokens x 10), a decimal string
    price: int  # its signed limit price, dollars x 1000
    reduceOnly: bool


class PerpsPool(TypedDict):
    """One margin pool of yours: its collateral, positions and open orders."""

    pool: int  # 0, the cross pool, or 1 to 7
    collateral: str  # signed, extended USDC units
    value: Optional[str]  # collateral + each position's signed notional - its funding owed
    requirement: Optional[str]  # maintenance margin, extended USDC units x 65,536
    liquidatable: Optional[bool]  # value < 0 or value x 65,536 < requirement
    initialMarginOk: Optional[bool]  # value x 65,536 >= 2 x requirement: collateral may leave
    positions: list[PerpsPosition]  # ascending by market, at most 8
    openOrders: list[PerpsOpenOrder]  # ascending by orderId; at most 200 across your pools


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


class PerpsHeldPrice(TypedDict):
    """A perps market's newest price report held for an operator's answer."""

    price: int
    observedAtMs: int


class PerpsMarketLive(TypedDict):
    """One row of ``perps.markets_live()``: a perps market's live state, the engine's."""

    marketIndex: int  # 0 to 254
    indexPrice: int  # the accepted index (0 before the first report), as of observedAtMs
    observedAtMs: int
    openInterest: str  # base units, a decimal string
    fundingIndex: str  # 32.32 fixed point, signed, a decimal string
    tickAtMs: int  # the last funding tick
    nextTickAtMs: int  # when the next is due; 0: none (an expired or unpriced market)
    fundingPeriodMs: int  # the period premiumSampleBp and fundingRateCbp are per
    premiumSampleBp: Optional[int]  # what a tick would apply now; None: the market is not rated
    fundingRateCbp: Optional[int]  # centi-bp per period; None with premiumSampleBp
    heldPrice: Optional[PerpsHeldPrice]  # while one stands, no order that can add risk is admitted
    halted: bool  # the engine's trading pause: every new order refused
    riskOnly: bool  # the engine's risk-only mode: only orders that reduce, and no withdrawal
    asOfMs: int  # the engine clock halted, riskOnly, tradingState and stateReason are as of
    tradingState: Optional[str]  # OPEN, REDUCE_ONLY, PRICE_STALE or HALTED; None: no book
    stateReason: Optional[str]  # why an order that adds risk is refused; None: one is admitted


class PerpsMarketsLive(TypedDict):
    """``perps.markets_live()``, and a ``perps_markets_live`` frame's ``markets``."""

    markets: list[PerpsMarketLive]


class PerpsFundingTick(TypedDict):
    """One applied funding tick of a perps market."""

    journalSeq: int
    sampleBp: int  # the premium sample, bp per period
    rateCbp: int  # the rate the funding index moved by, centi-bp per period
    moved: bool  # False: the index was stale, so the tick paid nothing
    fundingIndex: str  # after the tick, 32.32 fixed point, signed, a decimal string
    indexPrice: int
    tickAtMs: int


class PerpsFundingTicks(TypedDict):
    """``perps.funding_ticks()``: newest first."""

    ticks: list[PerpsFundingTick]


class PerpsClose(TypedDict):
    """One of your positions closed by the engine or by an exit."""

    journalSeq: int
    atMs: int  # its commit's clock
    kind: str  # LIQUIDATION, DELEVERAGE (auto-deleveraging), EXIT or SETTLE (an expired market)
    market: int
    pool: int
    side: str  # the position's: LONG or SHORT
    baseAfter: str  # its signed base after the close, a decimal string: "0" closed, never flipped
    price: int
    base: str  # the close's base units, a decimal string
    quote: int  # in the market's quote units (10 x qem extended units)
    fee: int  # yours, likewise
    quoteExtended: str  # quote in extended USDC units, a decimal string
    feeExtended: str  # fee in extended USDC units, a decimal string
    adl: bool  # True: you were a deleverage's counterparty, closed against the deleveraged


class PerpsCloses(TypedDict):
    """``perps.closes()``: newest first."""

    closes: list[PerpsClose]


class PerpsOpAccepted(TypedDict):
    """The 202 to a perps allocate or exit."""

    journalSeq: int  # its place in the exchange's journal
    duplicate: bool  # a re-send of an op applied before: nothing was applied again


class TradeTick(TypedDict, total=False):
    """A public trade: the detail's ``recentTrades`` or a ``trades_batch`` frame (newest first),
    or a ``market.trades`` row (oldest first).

    On a perps market the tape also carries liquidation takes: a liquidated position taken
    against a resting order. A take has ``isLiquidation: True`` and is no trade: it has no
    ``txHash`` key, its ``aggressorSide`` is the liquidated side, and its ``tradeId`` is the
    liquidation's journal seq, the ``perpsSeq`` of its maker's ``liquidation_fill``. A trade
    always has its ``txHash``, and ``isLiquidation`` absent or ``False``: read
    ``tick.get("txHash")``."""

    tradeId: int
    marketIndex: int
    txHash: str  # a trade's content hash (0x + 64 hex), not an on-chain tx; absent on a take
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
    marketIndex: int  # the order's market: the index it signed
    # The market's, a delisted one's too; "" only for a market no asset is bound to: key on
    # marketIndex.
    ticker: str
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


class LiquidationFill(TypedDict):
    """A ``liquidation_fill`` frame's ``fill``: a liquidation took from your resting order on a
    perps market. It is a fill of that order but no trade (no ``tradeId``, no ``txHash``):
    ``perpsSeq`` names it, and the take's tape print (``isLiquidation: True``) carries it as its
    ``tradeId``. You are its maker and pay no fee; amounts are display units, as a trade's."""

    perpsSeq: int  # the liquidation's journal seq = its tape print's tradeId
    orderId: int  # your order it filled
    marketIndex: int
    ticker: str  # the market's
    side: str  # OrderSide of your order
    role: str  # "MAKER"
    quantity: float
    price: float
    fee: float  # dollars; 0 for the maker of a liquidation
    executedAt: int  # unix seconds


class TradeHistoryItem(TypedDict, total=False):
    tradeId: int
    orderId: int  # this user's order the fill belongs to
    marketIndex: int
    ticker: str  # the market's
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


class Holding(TypedDict, total=False):
    """A held asset's token: facts only. An L2 holding's amounts are its ``balances`` row."""

    ticker: str  # the asset's, its spot market's too
    marketIndex: Optional[int]  # its spot market's, which mark_price frames name; None without one
    name: str  # the asset's display name
    kind: str  # AssetKind
    totalTokens: Optional[float]  # a property's token supply; None for any other kind
    averageEntryPrice: Optional[float]  # None when the cost is unknown
    # The mark when read: its spot market's for an L2 holding; an IPO allocation's unit price
    # until it trades.
    marketPrice: float
    isIpoAllocation: bool
    assetId: int  # L2 holding only: its balances row
    quantity: float  # IPO allocation only, tokens
    imageUrl: str
    sparkline: list[float]


class PortfolioComponent(TypedDict, total=False):
    balances: list[L2AssetBalance]  # the whole L2 book; a missing row is 0
    lifetimeVolume: float  # dollars traded over the account's lifetime
    holdings: list[Holding]
    applicableFees: dict[str, int]  # {takerFeeBps, makerFeeBps}
    offeringOrders: list[dict[str, Any]]
    openOrders: list[OrderHistoryItem]  # booked orders + PENDING/ARMED conditionals, merged
    tradeHistory: list[TradeHistoryItem]
    # Your newest liquidation fills, newest first: each a liquidation_fill frame's fill (no
    # trade: tradeHistory never holds one).
    liquidationFills: list[LiquidationFill]
    orderHistory: list[OrderHistoryItem]  # newest rows of both kinds, ANY status
    transfers: list[dict[str, Any]]  # newest first, keyed by journalSeq; `amount` in base units


__all__ = [
    "PriceLevel",
    "OrderBook",
    "Candle",
    "CandleHistory",
    "MarketL2",
    "PerpMarketL2",
    "L2PerpsMarket",
    "PerpsCollateral",
    "PerpsMarkets",
    "PerpsPosition",
    "PerpsOpenOrder",
    "PerpsPool",
    "PerpsAccount",
    "PerpsFunding",
    "PerpsHeldPrice",
    "PerpsMarketLive",
    "PerpsMarketsLive",
    "PerpsFundingTick",
    "PerpsFundingTicks",
    "PerpsClose",
    "PerpsCloses",
    "PerpsOpAccepted",
    "TradeTick",
    "OrderResult",
    "OrderAck",
    "CancelAllResult",
    "OrderHistoryItem",
    "OrderQueueEntry",
    "TradeProofStatus",
    "TradeHistoryItem",
    "LiquidationFill",
    "L2AssetBalance",
    "Holding",
    "PortfolioComponent",
]

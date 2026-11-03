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
    # An orderbook_update frame's: the engine's clock (unix ms) at the commit whose book it is. A
    # book is sent again every 5 s unchanged, with its old engineMs: now - engineMs is its age.
    engineMs: int


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
    qem: str  # its quote extension multiplier, a decimal string (it can pass 2^53)
    takerFee: int  # per 1,000,000; an order's signed fee-rate cap must cover it
    makerFee: int
    risk: int  # the maintenance margin factor per 65,536 (initial margin is twice it)
    maxLeverage: int  # the most leverage it takes: 65,536 // (2 x risk), at least 1
    band: int  # how far (bp) an order's price may sit from the index; 0: no bound
    flags: int  # PERPS_FLAG_CROSS (pool 0 may trade it) | PERPS_FLAG_REDUCE_ONLY
    minBase: int  # the least signed quantity of an order that adds to a position; 0: none
    minQuote: int  # the least quote (signed quantity x signed price / 10) of one; 0: none
    quoteLimit: int  # the most quote of any order; 0: none


class PerpsCollateral(TypedDict):
    """``perps.collateral()``: the one asset every margin pool holds (USDC)."""

    assetId: int  # 3
    symbol: str
    decimals: int  # the token's: a display amount is base units / 10^decimals
    tickSize: str  # the token's base units per tick, a decimal string: an allocate moves ticks
    extensionMultiplier: str  # the reads' extended units per tick, a decimal string


class _PerpsPositionRank(TypedDict, total=False):
    # Its auto-deleverage indicator, 1 to 5: 5 is the fifth of its side deleveraged first, 1 the
    # last; re-ranked at most once a minute, and absent while it is not ranked.
    adlRank: int


class PerpsPosition(_PerpsPositionRank):
    """One open position of a pool. Amounts are decimal strings in the engine's units, valued
    at the mark."""

    market: int  # the perps market, 0 to 254
    ticker: str  # the market's
    qem: str  # its quote extension multiplier, a decimal string
    risk: int  # its maintenance margin factor per 65,536
    maxLeverage: int  # the most leverage it takes
    band: int  # how far (bp) an order's price may sit from its index; 0: no bound
    base: str  # signed, in base units (an order's signed quantity): + long, - short
    fundingIndex: str  # the funding index it last settled at: it owes base x (F - this)
    entryQuote: str  # the quote of the fills that opened and grew it
    entryPrice: str  # 10 x entryQuote / |base|, in the market's price units
    fundingOwed: str  # extended USDC units: positive pays, negative receives
    notional: str  # |base| x mark x qem, extended USDC units
    liquidationPrice: Optional[str]  # the mark past which the pool is liquidatable, or None


class PerpsPool(TypedDict):
    """One margin pool of yours: its collateral and positions."""

    pool: int  # 0, the cross pool, or 1 to 7
    collateral: str  # signed, extended USDC units
    value: Optional[str]  # collateral + each position's signed notional - its funding owed
    requirement: Optional[str]  # maintenance margin, extended USDC units x 65,536
    liquidatable: Optional[bool]  # value < 0 or value x 65,536 < requirement
    positions: list[PerpsPosition]  # ascending by market, at most 8


class PerpsAccount(TypedDict):
    """``perps.positions()``, a summary's ``perps``: the collateral, and your pools that hold
    anything, ascending."""

    collateral: PerpsCollateral
    l2AccountIndex: Optional[int]  # None: no trading account yet, so no pools
    pools: list[PerpsPool]


class FundingPayment(TypedDict):
    """What one funding tick charged one of your positions, ``base`` x the tick's move of F: a
    ``fundingHistory`` or ``history.funding()`` row, or a ``funding_payment`` frame's
    ``funding``. Keyed by ``(journalSeq, pool)``."""

    journalSeq: int  # the tick's journal row
    marketIndex: int
    ticker: str  # the market's
    pool: int
    base: str  # the position's signed base at the tick, a decimal string: + long, - short
    payment: str  # extended USDC units, a decimal string: + the position paid, - it received
    rateCbp: int  # the rate the tick applied, centi-bp of the index per period
    atMs: int  # the tick's clock, unix ms


class PerpsHeldPrice(TypedDict):
    """A perps market's newest price report held for an operator's answer."""

    price: int
    observedAtMs: int


class PerpsMarketLive(TypedDict):
    """``market.get(ticker).market.live``, a perp's, and a row of a ``perps_markets_live``
    frame: its live state, the engine's."""

    marketIndex: int  # 0 to 254
    status: str  # "ACTIVE", or "EXPIRED": no more orders; positions leave by perps.exit()
    markPrice: int  # what positions are valued at, dollars x 1000 (0 before the first report)
    indexPrice: int  # the accepted index (0 before the first report), as of observedAtMs
    observedAtMs: int
    openInterest: str  # base units, a decimal string: longs plus shorts (one side's is half)
    fundingIndex: str  # F, extended USDC units per base unit, signed, a decimal string
    nextTickAtMs: int  # when the next is due; 0: none (an expired or unpriced market)
    fundingPeriodMs: int  # the period fundingRateCbp is per
    fundingRateCbp: Optional[int]  # what a tick would apply now, centi-bp of the index per period; None: not rated
    heldPrice: Optional[PerpsHeldPrice]  # while one stands, no order that can add risk is admitted
    halted: bool  # the engine's trading pause: every new order refused
    riskOnly: bool  # the engine's risk-only mode: only orders that reduce, and no withdrawal
    asOfMs: int  # the engine clock halted, riskOnly, tradingState and stateReason are as of
    tradingState: Optional[str]  # OPEN, REDUCE_ONLY, PRICE_STALE or HALTED; None: no book
    stateReason: Optional[str]  # why an order that adds risk is refused; None: one is admitted


class PerpsMarketsLive(TypedDict):
    """A ``perps_markets_live`` frame: ``markets``, every perp's live state, ascending."""

    markets: list[PerpsMarketLive]


class PerpsOpAccepted(TypedDict):
    """The 202 to a perps allocate or exit."""

    journalSeq: int  # its place in the exchange's journal
    duplicate: bool  # a re-send of an op applied before: nothing was applied again


class WithdrawalAccepted(TypedDict):
    """The 202 to a withdrawal (``funds.withdraw``): journaled, and taken from your spot balance."""

    journalSeq: int  # its place in the exchange's journal: its transfer row's journalSeq
    accountIndex: int  # your account
    assetId: int  # 3, USDC
    amount: int  # in the asset's ticks: USDC x 10^decimals / tickSize
    nonce: int  # the nonce it spent
    duplicate: bool  # a re-send of one journaled before: nothing was journaled again
    status: dict[str, Any]  # {"status": "PENDING"}: follow it in portfolio.component().transfers


class TradeTick(TypedDict, total=False):
    """A public trade: the detail's ``recentTrades`` (no ``marketIndex`` or ``txHash``), or a
    ``trades_batch`` frame (newest first), or a ``market.trades`` row (oldest first).

    On a perps market the tape also carries liquidation takes: a liquidated position taken
    against a resting order. A take has ``isLiquidation: True`` and is no trade: it has no
    ``txHash`` key, its ``aggressorSide`` is the liquidated side, and its ``tradeId`` is the
    liquidation's journal seq, the ``tradeId`` of its maker's row (``source``
    ``LIQUIDATION``). A trade always has its ``txHash``, and ``isLiquidation`` absent or
    ``False``: read ``tick.get("txHash")`` on a frame or a ``market.trades`` row."""

    tradeId: int
    marketIndex: int
    txHash: str  # a trade's content hash (0x + 64 hex), not an on-chain tx; absent on a take
    aggressorSide: str  # OrderSide of the taker (a take's: the liquidated side)
    price: float
    quantity: float
    timestamp: int
    isLiquidation: bool  # True on a liquidation take; absent or False on a trade


class OrderResult(TypedDict, total=False):
    """Response to ``POST /orders`` (``orders.create`` and its wrappers). A replace's
    (``replaces_order_id=``) 200 means the order it replaced is cancelled."""

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
    """Response to ``POST /orders/cancel-all`` (``orders.cancel_all``)."""

    requestedCount: int  # len(cancelledOrderIds) + len(failedOrders)
    cancelledOrderIds: list[int]  # conditional rows' ids first, then booked orders'
    failedOrders: list[dict[str, Any]]  # [{orderId, errorMessage}]: still open; retry is safe


class CancelAfterResult(TypedDict):
    """Response to ``POST /orders/cancel-after`` (``orders.cancel_after``)."""

    # When the scope's orders are cancelled unless you call again first: unix ms of the
    # engine's clock; 0 when the call turned the scope's timer off.
    deadlineMs: int


class CancelByIndexResult(TypedDict):
    """Response to ``POST /orders/cancel-by-index`` (``orders.cancel_by_index``)."""

    outcome: str  # CancelByIndexOutcome: CANCELLED, ENDED or VOIDED
    orderId: Optional[int]  # the order that carries the number; None when VOIDED
    status: Optional[str]  # OrderStatus: CANCELLED, or how an ENDED order ended; None when VOIDED


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
    pool: Optional[int]  # a perps order's margin pool; None on spot
    reduceOnly: bool  # its fills may only shrink the pool's position; False on spot
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
    l2OrderIndex: int  # the order number it signed (orders.cancel_by_index takes it)
    orderTag: int  # booked orders only: the order_tag= it was placed with; absent without one


class _OrderQueueEntryKeys(TypedDict, total=False):
    l2OrderIndex: int  # the order number it signed
    orderTag: int  # the order_tag= it was placed with; absent without one


class OrderQueueEntry(_OrderQueueEntryKeys):
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
    """Your fill: a ``tradeHistory`` or ``history.trades()`` row, or a ``trade_new`` frame's
    ``trade``. Keyed by ``(tradeId, side)``: a deleverage between two of your pools is two rows
    of one ``tradeId``, one each side."""

    tradeId: int  # the row's journal seq: a trade's id, or a perps fill's journal row
    orderId: Optional[int]  # this user's order the fill belongs to; None: it filled no order
    marketIndex: int
    ticker: str  # the market's
    # A trade's content hash (0x + 64 hex), not an on-chain transaction; None: no trade.
    txHash: Optional[str]
    side: str  # OrderSide, relative to this user
    # "MAKER" | "TAKER": a TRADE's resting or aggressing order; on any other source TAKER is the
    # pool the row closed and MAKER the other side.
    role: str
    pool: Optional[int]  # a perps fill's margin pool; None on spot
    quantity: float
    price: float
    fee: float  # this user's fee, dollars, to 0.001
    closedPnl: Optional[float]  # dollars, signed, before fees; None unless it reduced a position
    # TRADE (an order matched), or on a perp LIQUIDATION, BACKSTOP_LIQUIDATION, DELEVERAGE, EXIT,
    # SETTLE, FORCED or FORCED_DELEVERAGE.
    source: str
    direction: str  # BUY or SELL on spot; OPEN_LONG, CLOSE_LONG, OPEN_SHORT or CLOSE_SHORT
    executedAt: int
    proof: Optional[TradeProofStatus]  # history.trades() alone; None with proofs off or no trade


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
    averageEntryPrice: Optional[float]  # None when the cost is unknown
    # The mark when read: its spot market's for an L2 holding; an IPO allocation's unit price
    # until it trades.
    marketPrice: float
    isIpoAllocation: bool
    assetId: int  # L2 holding only: its balances row
    quantity: float  # IPO allocation only, tokens


class HoldingDetails(TypedDict):
    """A holding's display facts: a component's ``holdingDetails`` row, and a ``holding_update``
    frame's ``details``."""

    name: str  # the asset's display name
    totalTokens: Optional[float]  # a property's token supply; None for any other kind
    imageUrl: str


class PerpsLeverage(TypedDict):
    """A leverage you saved on a perp: a component's ``perpsLeverage`` row, the answer to
    ``portfolio.set_perps_leverage()``, and a ``perps_leverage_update`` frame's fields."""

    marketIndex: int  # the perp's, 0 to 254
    leverage: int  # 1 to the market's maxLeverage


class PortfolioSummary(TypedDict):
    """``portfolio.summary()``: what you hold."""

    balances: list[L2AssetBalance]  # the whole L2 book; a missing row is 0
    holdings: list[Holding]
    perps: Optional[PerpsAccount]  # None: not read just now, which never means you hold none


class PortfolioComponent(TypedDict, total=False):
    """``portfolio.component()``: your recent activity, fees, lifetime volume, holdings' details and
    saved leverage."""

    lifetimeVolume: float  # dollars traded over the account's lifetime
    applicableFees: dict[str, int]  # {takerFeeBps, makerFeeBps}
    offeringOrders: list[dict[str, Any]]
    openOrders: list[OrderHistoryItem]  # booked orders + PENDING/ARMED conditionals, merged
    tradeHistory: list[TradeHistoryItem]  # without proof
    orderHistory: list[OrderHistoryItem]  # newest rows of both kinds, ANY status
    transfers: list[dict[str, Any]]  # newest first, keyed by journalSeq; `amount` in base units
    fundingHistory: list[FundingPayment]  # newest first
    holdingDetails: dict[str, HoldingDetails]  # by holding ticker
    holdingSparklines: dict[str, list[float]]  # by holding ticker: up to 48 closes, oldest first
    perpsLeverage: list[PerpsLeverage]  # the ones saved, one a market


__all__ = [
    "PriceLevel",
    "OrderBook",
    "Candle",
    "CandleHistory",
    "MarketL2",
    "PerpMarketL2",
    "PerpsCollateral",
    "PerpsPosition",
    "PerpsPool",
    "PerpsAccount",
    "FundingPayment",
    "PerpsHeldPrice",
    "PerpsMarketLive",
    "PerpsMarketsLive",
    "PerpsOpAccepted",
    "WithdrawalAccepted",
    "TradeTick",
    "OrderResult",
    "OrderAck",
    "CancelAllResult",
    "CancelAfterResult",
    "CancelByIndexResult",
    "OrderHistoryItem",
    "OrderQueueEntry",
    "TradeProofStatus",
    "TradeHistoryItem",
    "L2AssetBalance",
    "Holding",
    "HoldingDetails",
    "PerpsLeverage",
    "PortfolioSummary",
    "PortfolioComponent",
]

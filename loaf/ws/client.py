"""Threaded, callback-based WebSocket client — no asyncio knowledge required.

Typical usage::

    loaf = LoafClient()   # reads $LOAF_TRADING_KEY
    ws = loaf.websocket()

    @ws.on_orderbook
    def handle_book(msg):
        print(msg.ticker, msg.bids[0].price)

    ws.subscribe_orderbook("OPRA")
    ws.subscribe_trades("OPRA")
    ws.run_forever()            # blocking; Ctrl-C to stop

Or run it in the background and keep using the REST client::

    with loaf.websocket() as ws:          # starts a background thread
        ws.on_trade(lambda m: ...)        # your own fills (private channel)
        ws.subscribe_portfolio()          # your own account's private stream
        ...                               # do other work; handlers fire live

Channels (``"type:key"`` strings — market channels key by the market's
``ticker``, exact case, a perp's ending in ``-PERP``: ``orderbook:opra`` is
refused; ``portfolio`` has no key, and ``perps:markets`` is one channel for every
perp):

==================  ========  ===========================================
Channel             Auth      What you receive
==================  ========  ===========================================
orderbook:{ticker}  public    the full bid/ask book, on change (at most one frame per 100 ms) and
                              again every 5 s unchanged, empty books included (no frame on subscribe)
trades:{ticker}     public    every trade, at most one frame per 100 ms (the recent ones on subscribe)
chart:{ticker}      public    OHLCV candle updates (note: singular "chart")
markprice:{ticker}  public    canonical mark price (1s, on change)
volume:{ticker}     public    rolling-24h traded volume (replaces ``volume24h``)
market:{ticker}     public    halt / resume status for one market
perps:markets       public    every perp's live state, about once a second, and its terms
                              when they change
portfolio           PRIVATE   your order transitions, fills, queue places, whole L2 balance book,
                              perps account (and its snapshot about once a second), funding
                              payments, holdings, transfers, fees, keys and saved perps leverage
==================  ========  ===========================================

A market channel's frames name its market as ``ticker``, the channel's own
string; a trade in a ``trades_batch`` names none.

Prices and quantities are human units (dollars / tokens); ``balances_update``
amounts are base-unit decimal strings (see :func:`loaf.from_base_units`);
timestamps are unix seconds (an order row's ``expiryMs`` is milliseconds). The
perps frames' prices, sizes and amounts, a ``mark_price`` frame's price and a
fill's quantity, price, fee and closed PnL are exact
:class:`~decimal.Decimal` values, as the REST reads give them: the exchange
serves each as an exact decimal string, and a frame with one in any other form
is dropped and logged, never read in another unit.

**Engine stamps.** A frame that relays a change the exchange's engine made
carries ``engineMs``, the engine's clock (unix ms) when the batch that made it
committed, and ``batchSeq``, that batch's journal number: the private frames
(``order_status``, ``order_update``, ``trade_new``, ``balances_update``,
``holding_update``, ``perps_account_update``, ``funding_payment``,
``l2_keys_update``) and ``orderbook_update``, whose ``engineMs`` is its book's.
An account snapshot (``perps_account_update`` with ``snapshot: True``) is the
exception: its ``engineMs`` is the engine's clock at the read it was taken at.
``timestamp`` stays the server's send time in whole unix seconds, so
``timestamp * 1000 - engineMs`` is a frame's age when it left, to within a
second. ``batchSeq`` does not order frames: one batch's frames share it.

**The beat.** While ``portfolio`` is subscribed, the exchange sends your perps
account as a snapshot (``perps_account_update`` with ``snapshot: True``) about
once a second, changed or not, as long as its engine runs. The client watches
it: :attr:`LoafWebSocketClient.fresh` turns ``True`` at a connection's first
snapshot, and ``False`` once no stamped private frame came for ``stale_after``
seconds (default 3: two beats missed), the connection drops, or your handlers
fall behind: a handler too slow for the feed leaves frames queued, so what is
being handled is older than ``stale_after`` (the client compares how far the
snapshots' ``engineMs`` and its own clock each advanced, allows a second more,
as a snapshot waits for the next beat before it is sent, and tolerates the
drift between the two machines' clocks). A connection behind for 15 s is ended
and reconnected: the next one starts without the queued frames. A frame
counts from the moment it is taken off the socket, before its handlers run.
:meth:`LoafWebSocketClient.on_stale` and :meth:`LoafWebSocketClient.on_fresh`
fire at those turns. Pull your quotes on stale. A missing snapshot means no
news, never no positions. A deployment without perps sends no snapshots, so
``fresh`` stays ``None`` there.

A client with a trading key authenticates each connection with a login token
the key signs (the ``auth`` frame), and sends a fresh one on the live
connection each time the one it holds passes half its life.

Handlers run on the client's internal event-loop thread: keep them short. An
exception raised in a handler (``asyncio.CancelledError`` included) is logged
and swallowed; a ``SystemExit`` ends the client's run, once
:meth:`LoafWebSocketClient.on_stale`, then
:meth:`LoafWebSocketClient.on_disconnect`, have fired and an error is logged,
and so does a ``KeyboardInterrupt`` in the background thread
(:meth:`LoafWebSocketClient.start`); under
:meth:`LoafWebSocketClient.run_forever` a ``KeyboardInterrupt`` is Ctrl-C, and
stops the client.
Don't place orders from a handler: a placement can block the feed for minutes while the exchange is
slow to answer (long enough for the server to drop the connection), and an
:class:`~loaf.exceptions.OrderOutcomeUnknownError` raised there is only logged.
Record the event and act from your own loop, as ``bot.py`` does.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import numbers
import random
import ssl
import threading
import time
from typing import TYPE_CHECKING, Any, Callable

import httpx

from .. import _served
from .._object import LoafObject, parse
from ..constants import DEFAULT_STALE_AFTER
from ..enums import WSMessageType
from ..exceptions import LoafConfigError

if TYPE_CHECKING:
    from .._l2 import LoginTokens
    from ..client import LoafClient

try:
    import websockets
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "The WebSocket client requires the 'websockets' package. "
        "Install it with: pip install websockets"
    ) from exc

logger = logging.getLogger("loaf.ws")

Handler = Callable[[Any], None]

# Internal pseudo-types for lifecycle callbacks. A wire frame whose type is one of these, or "*",
# reaches the catch-all handlers only.
_ON_CONNECT = "__connect__"
_ON_DISCONNECT = "__disconnect__"
_ON_STALE = "__stale__"
_ON_FRESH = "__fresh__"
_INTERNAL_KEYS = frozenset({_ON_CONNECT, _ON_DISCONNECT, _ON_STALE, _ON_FRESH, "*"})

# The private frames that relay an engine change, and so carry its engineMs: the beat's signs
# of life. orderbook_update carries one too, but says nothing about your private stream.
_STAMPED_PRIVATE = frozenset({
    WSMessageType.ORDER_STATUS.value,
    WSMessageType.ORDER_UPDATE.value,
    WSMessageType.TRADE_NEW.value,
    WSMessageType.BALANCES_UPDATE.value,
    WSMessageType.HOLDING_UPDATE.value,
    WSMessageType.PERPS_ACCOUNT_UPDATE.value,
    WSMessageType.FUNDING_PAYMENT.value,
    WSMessageType.L2_KEYS_UPDATE.value,
})

# The shortest stale_after: the beat is about one a second, so a shorter wait always goes stale.
_MIN_STALE_AFTER = 1.0

# How fast the lag baseline may rise, in seconds per second elapsed (0.1%): above any drift between
# this machine's clock and the engine's (NTP slews a clock by at most 0.05%), so drift never adds
# up to a lag, and far below any backlog that matters, so the baseline never absorbs one.
_LAG_CREEP = 0.001

# How much more than stale_after a snapshot may lag before the handlers count as behind, in
# seconds: the exchange reads the account at each beat (about one a second) but sends that read
# only once the next beat has come, so a snapshot leaves up to a beat old, and its offset ranges
# over about a second above the least one.
_BEAT_ALLOWANCE = 1.0

# How long a connection may stay behind before the client ends it and reconnects, in seconds of
# this machine's clock from the snapshot that first read behind. The next connection starts
# without the frames queued on this one and with a baseline of its own, so a consumer that fell
# behind gets current frames again, and a lasting shift of the offset (a step of the engine's
# clock) heals.
_BEHIND_LIMIT = 15.0

# The reason on_disconnect reports for a connection the client ended at _BEHIND_LIMIT.
_BEHIND_REASON = "the handlers stayed behind the feed"

# The longest wait before a reconnect attempt, in seconds.
_MAX_RECONNECT_DELAY = 30.0

# How long a connection stays live after on_connect before the reconnect wait starts over from
# reconnect_delay, in seconds: a server that accepts and closes at once keeps the wait growing.
_STABLE_CONNECTION = 10.0

# The largest frame the client takes, in bytes: a whole book can outgrow websockets' 1 MiB default.
_MAX_FRAME_SIZE = 16 * 1024 * 1024

# The most characters of a server error frame's code and message that go to the log.
_ERROR_TEXT_LIMIT = 200


def derive_ws_url(base_url: str) -> str:
    """Map a REST base URL to its WebSocket URL.

    ``https://host/api`` -> ``wss://host/ws``; ``http://host:8005/api`` ->
    ``ws://host:8005/ws``. The scheme is matched in any case, and the scheme and
    host come back lowercased; a URL that is not http(s) with a host raises
    :class:`~loaf.exceptions.LoafConfigError`.
    """
    from .. import _urls

    return _urls.derive_ws_url(base_url)


def _bounded(value: Any) -> str:
    """A server-sent value for the log: on one line, at most ``_ERROR_TEXT_LIMIT`` characters."""
    text = " ".join((value if isinstance(value, str) else repr(value)).split())
    return text if len(text) <= _ERROR_TEXT_LIMIT else text[:_ERROR_TEXT_LIMIT] + "..."


class LoafWebSocketClient:
    """A reconnecting, thread-backed WebSocket client.

    Construct via :meth:`loaf.client.LoafClient.websocket`. Register handlers
    with the ``on_*`` decorators/methods, declare subscriptions with the
    ``subscribe_*`` helpers, then call :meth:`run_forever` (blocking) or use the
    instance as a context manager (background thread).

    ``reconnect_delay`` is the longest wait before the first reconnect attempt,
    in seconds; it doubles after each attempt that does not stay live for 10 s
    after :meth:`on_connect`, up to 30 s, and each wait is drawn at random below
    it.

    Each connection authenticates with a login token that ``client``'s trading
    key signs, and stays authenticated (see :meth:`on_disconnect`). Without a
    ``client``, or with one that has no trading key, the connection is
    anonymous: the public channels only.

    ``ws_url`` must be a ``wss://`` URL, or ``ws://`` to this machine
    (``localhost``, 127.0.0.0/8 or ``[::1]``), else
    :class:`~loaf.exceptions.LoafConfigError`; ``allow_http=True`` also takes
    ``ws://`` to other hosts, for a dev server on your own network or in a
    container network, never a remote host. It defaults to ``client``'s own
    setting, else ``False``. A ``ws_url`` on another host than ``client``'s
    ``base_url`` warns (``UserWarning``): the login token is sent there too.

    ``stale_after`` is how many seconds may pass without a stamped private
    frame before the client calls the feed stale (:attr:`fresh`), at least 1;
    default 3, two of the exchange's account beats missed.
    """

    def __init__(
        self,
        client: LoafClient | None = None,
        *,
        ws_url: str | None = None,
        allow_http: bool | None = None,
        verify: bool | ssl.SSLContext | None = None,
        auto_reconnect: bool = True,
        reconnect_delay: float = 1.0,
        stale_after: float = DEFAULT_STALE_AFTER,
    ) -> None:
        #: Signs each connection's login tokens; None for an anonymous connection.
        self._login_tokens: LoginTokens | None = client._login_tokens if client else None
        from .. import _urls

        if allow_http is None:
            allow_http = client._allow_http if client is not None else False
        if ws_url:
            self.ws_url: str = _urls.ws_url(ws_url, source="ws_url", allow_http=bool(allow_http))
            if client is not None:
                _urls.warn_other_host(self.ws_url, client.base_url, source="ws_url")
        elif client is not None:
            self.ws_url = client.ws_url
        else:
            raise ValueError("ws_url is required when no LoafClient is provided")
        verify = verify if verify is not None else (client._verify if client else True)
        # The context httpx builds for REST from the same setting, not websockets' default: the
        # system store, while REST trusts certifi's CAs. Built now, so a bad CA path raises here.
        self._ssl = (
            httpx.create_ssl_context(verify=verify) if self.ws_url.startswith("wss://") else None
        )
        self.auto_reconnect = auto_reconnect
        real = isinstance(reconnect_delay, numbers.Real) and not isinstance(reconnect_delay, bool)
        if not real or not reconnect_delay > 0:  # NaN is not > 0
            raise LoafConfigError(
                f"reconnect_delay must be a positive number of seconds, got {reconnect_delay!r}"
            )
        self.reconnect_delay = reconnect_delay
        real = isinstance(stale_after, numbers.Real) and not isinstance(stale_after, bool)
        if not real or not math.isfinite(stale_after) or stale_after < _MIN_STALE_AFTER:
            raise LoafConfigError(
                f"stale_after must be a number of seconds from {_MIN_STALE_AFTER:g} (the "
                f"exchange's account beat is about one a second), got {stale_after!r}"
            )
        self.stale_after = float(stale_after)

        # The beat, kept on the loop's thread: None until a connection's first snapshot, then
        # whether a stamped private frame came within stale_after (_note_beat, _watch_beats).
        self._fresh: bool | None = None
        # time.monotonic() when the last beat's frame was taken off the socket.
        self._last_beat: float | None = None
        self._engine_ms: int | None = None
        # While the handlers are behind the feed (the last snapshot's lag was over stale_after +
        # _BEAT_ALLOWANCE): when the first snapshot of that stretch was taken off the socket.
        self._behind_since: float | None = None
        # The connection's lag baseline: the least arrival-minus-engineMs offset of its
        # snapshots since portfolio was subscribed, in seconds, allowed to rise by _LAG_CREEP per
        # second (_snapshot_lag).
        self._lag_base: float | None = None
        self._lag_base_at = 0.0  # time.monotonic() of the snapshot that last moved it
        self._live_at = 0.0  # time.monotonic() when the live connection's on_connect fired
        # The run ended on a SystemExit, KeyboardInterrupt or unasked-for cancellation (_halt).
        self._halted = False

        self._handlers: dict[str, list[Handler]] = {}
        self._channels: set[str] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._run_task: asyncio.Task | None = None
        self._ws: Any = None
        self._auth_token: str | None = None  # the login token the live connection sent last
        self._auth_refused = False  # the live connection's auth_result was false
        # The current run's stop token: each run gets its own (_begin_run), so a run that is still
        # ending after start() began the next one (a close can outlast stop()'s wait) holds an
        # older one, already set, and leaves the current run's state alone.
        self._stop_event = threading.Event()
        # The stop token of the run run_forever() is running, None when it runs none.
        self._foreground: threading.Event | None = None
        self._connected_event = threading.Event()

    # ------------------------------------------------------------------ #
    # Handler registration (usable as decorators or direct calls)
    # ------------------------------------------------------------------ #

    def on(self, message_type: str, handler: Handler | None = None) -> Any:
        """Register ``handler`` for a message ``type``. Usable as a decorator."""

        def register(fn: Handler) -> Handler:
            self._handlers.setdefault(str(message_type), []).append(fn)
            return fn

        return register if handler is None else register(handler)

    def on_message(self, handler: Handler | None = None) -> Any:
        """Catch-all: receive every message regardless of type."""
        return self.on("*", handler)

    # public market data
    def on_orderbook(self, handler: Handler | None = None) -> Any:
        """A market's whole book (``orderbook_update``: ``{ticker, bids, asks,
        engineMs}``, a :class:`~loaf.models.OrderBook`): replace what you hold
        for ``ticker``.

        Sent on change, at most one frame per market every 100 ms, and again
        every 5 s unchanged (an empty book too), so the first frame can take up
        to 5 s after you subscribe. ``engineMs`` is the engine's clock at the
        commit whose book it is: a quiet market's frames keep coming with
        their old ``engineMs``, so frames show the exchange is up and
        ``engineMs`` how old the book is.
        """
        return self.on(WSMessageType.ORDERBOOK_UPDATE, handler)

    def on_trades(self, handler: Handler | None = None) -> Any:
        """A market's public trades (``trades_batch``: ``{ticker, trades}``,
        each a :class:`~loaf.models.TradeTick`; on a perp a liquidation take
        too, ``isLiquidation: True``, with no ``txHash``).

        Every trade is pushed, in at most one frame per market every 100 ms:
        each frame holds every trade printed since the previous one, newest
        first. Right after the subscription (so on every connection, before
        :meth:`on_connect`), one frame brings the market's recent trades, if
        any, up to 20, once; after a reconnect it repeats trades you already
        hold, so dedupe by ``tradeId``. For the trades missed while the feed
        was down, see :meth:`loaf.resources.market.MarketResource.trades`.
        """
        return self.on(WSMessageType.TRADES_BATCH, handler)

    def on_candle(self, handler: Handler | None = None) -> Any:
        """A market's candle changed (``candle_update``: ``{ticker, candle}``, a
        :class:`~loaf.models.Candle`)."""
        return self.on(WSMessageType.CANDLE_UPDATE, handler)

    def on_mark_price(self, handler: Handler | None = None) -> Any:
        """A market's canonical mark price (``mark_price``: ``{ticker, price}``),
        checked once a second and pushed on change.

        Every ``markprice`` channel you subscribe feeds this one handler: match
        on ``ticker``. A holding's ticker is its spot market's
        (:meth:`loaf.resources.portfolio.PortfolioResource.summary`); an IPO
        allocation keeps its unit price until it trades, so no frame values
        it. ``price`` is dollars, an exact :class:`~decimal.Decimal`, on every
        market; on a perp it is the engine's mark, the ``markPrice`` of
        :meth:`on_perps_markets_live` (it can fall between ticks): what
        positions are valued at and stops fire on.
        """
        return self.on(WSMessageType.MARK_PRICE, handler)

    def on_volume(self, handler: Handler | None = None) -> Any:
        """A market's rolling-24h traded volume changed (``volume_update``:
        ``{ticker, volume24h}``). Replaces the REST ``volume24h``."""
        return self.on(WSMessageType.VOLUME_UPDATE, handler)

    def on_market_halt(self, handler: Handler | None = None) -> Any:
        """A market was halted or resumed (``market_halt``: ``{ticker, isHalted}``).

        ``isHalted`` is the EFFECTIVE state — the market's own flag OR'd with
        the platform-wide kill switch — so assign it straight over the
        ``isHalted`` you seeded from REST. While it is true, order placement on
        that market raises :class:`~loaf.exceptions.TradingHaltedError`.
        """
        return self.on(WSMessageType.MARKET_HALT, handler)

    def on_perps_markets_live(self, handler: Handler | None = None) -> Any:
        """Every perps market's live state (``perps_markets_live``: ``{markets}``,
        the whole list, each row a perp's ``market.get(ticker).market.live``),
        once when you subscribe and then about once a second while you hold
        ``perps:markets``: replace what you hold. A second the exchange could
        not read the engine pushes nothing; bound a row's age by its
        ``asOfMs``."""
        return self.on(WSMessageType.PERPS_MARKETS_LIVE, handler)

    def on_market_terms(self, handler: Handler | None = None) -> Any:
        """A perp's terms changed (``market_terms_update``: ``{marketIndex, ticker,
        l2}``, its ``market.get(ticker).market.l2``), while you hold
        ``perps:markets``: replace what you hold for it. A perp that expired
        arrives this way, ``l2.status`` ``EXPIRED``."""
        return self.on(WSMessageType.MARKET_TERMS_UPDATE, handler)

    # private portfolio deltas
    def on_balances(self, handler: Handler | None = None) -> Any:
        """Your whole L2 balance book (``balances_update``: ``{balances}``).

        Rows are ``{assetId, symbol, decimals, isQuote, total, hold,
        available}``, the amounts decimal strings in the token's base units
        (:func:`loaf.from_base_units`); the ``isQuote`` rows are your
        cash. Sent after every engine commit that moves the book: replace yours
        with it, and treat a missing row as 0.
        """
        return self.on(WSMessageType.BALANCES_UPDATE, handler)

    def on_order_status(self, handler: Handler | None = None) -> Any:
        """A booked order's status/quantity changed (``order_status``:
        ``{orderId, status, quantityLeft, l2OrderIndex, orderTag}``).

        Booked orders only; a conditional never reaches this frame. One frame
        per fill. ``quantityLeft`` is the unfilled remainder: what is left after
        a fill, and the cancelled quantity on ``CANCELLED``, so filled =
        quantity - quantityLeft always. ``l2OrderIndex`` is the order number it
        signed and ``orderTag`` its ``order_tag=`` (absent without one), so a
        quoting loop can match a frame to an order whose placement has not
        answered yet.

        ``CANCELLED`` also arrives for cancels you did not request: the
        exchange's sweep 72 h before the order's ``expiryMs``, an epoch move (a
        trading-key reset or a cancel-all sent on chain), a delisted (or
        unlisted) market, a BUY that can no longer fund a fill within its hold
        (budget exhausted), an aggressing SELL at K1's bound (its remainder does
        not rest), and your own order crossing your resting one (self-trade). A
        firing stop or take cancels none of your orders.
        """
        return self.on(WSMessageType.ORDER_STATUS, handler)

    def on_order_update(self, handler: Handler | None = None) -> Any:
        """One of your orders changed (``order_update``: ``{order}``).

        ``order`` is a booked order OR a conditional (stop / take) row — narrow
        with :func:`loaf.is_conditional_order` before reading ``filledQuantity``,
        which a conditional does not carry. Every conditional transition
        publishes one of these, TP/SL legs included (they are conditional rows).
        A firing conditional cancels none of your orders: a TP/SL leg cancels
        only its OCO sibling (the other leg, ``CANCELLED``), and one that fires
        short of funds or tokens is ``FAILED``, with the refusal's text as its
        ``rejectionReason``.

        For a booked order it is sent after the exchange commits and carries the
        committed row, with ``expiryMs`` (unix ms, the signed expiry),
        ``l2OrderIndex`` and its ``orderTag`` (absent without one). For an
        order that trades immediately it can arrive AFTER that order's
        ``order_status`` / ``trade_new`` frames and already show
        ``PARTIALLY_FILLED`` or ``FILLED``, so upsert by id and do not assume
        ``OPEN`` first. A MARKET row's ``price`` is the worst price you signed.
        """
        return self.on(WSMessageType.ORDER_UPDATE, handler)

    def on_trade(self, handler: Handler | None = None) -> Any:
        """Your own fills on the private portfolio channel (``trade_new``:
        ``{trade}``, a :class:`~loaf.models.TradeHistoryItem` as
        ``history.trades()`` lists it, without ``proof``).

        Exactly one frame per fill of yours, final when it matches.
        ``trade.source`` says what made it: ``TRADE`` (``orderId`` your order,
        ``role`` ``"MAKER"`` if it rested, ``"TAKER"`` if it aggressed,
        ``txHash`` the trade's content hash, not an on-chain transaction) or,
        on a perp, ``LIQUIDATION``, ``BACKSTOP_LIQUIDATION``, ``DELEVERAGE``,
        ``EXIT``, ``SETTLE``, ``FORCED`` or ``FORCED_DELEVERAGE``: no trade, so
        ``txHash`` is ``None``. ``trade.tradeId`` is always the fill's journal
        seq, and a fill is keyed by ``(tradeId, side)``. A liquidation's take
        from your resting order is ``LIQUIDATION``, ``role`` ``"MAKER"``,
        ``orderId`` your order, ``fee`` 0, and its public print
        (:meth:`on_trades`, ``isLiquidation: True``) has the same ``tradeId``.
        ``trade.direction`` and ``trade.closedPnl`` say what it did to the
        position; ``trade.quantity``, ``price``, ``fee`` (dollars to 0.000001)
        and ``closedPnl`` are exact Decimals. Each engine update's burst
        carries its ``order_status`` frames, then its ``trade_new`` frames,
        then ``balances_update``; an update with more than 200 statuses or
        trades comes as several such bursts, ``balances_update`` in the last
        only. Match a fill to its order by ``trade.orderId``, not by frame
        order. Delivery is at most once (no replay after a reconnect), so
        reconcile with ``history.trades()``.
        """
        return self.on(WSMessageType.TRADE_NEW, handler)

    def on_order_queue(self, handler: Handler | None = None) -> Any:
        """Your resting orders' places in one market's book (``order_queue``:
        ``{marketIndex, orders}``).

        ``orders`` lists your orders resting there, bids best first, then asks
        best first, each ``{orderId, side, price, quantity, quantityAhead,
        ordersAhead}``, and optionally ``l2OrderIndex`` and ``orderTag``
        (:class:`~loaf.models.OrderQueueEntry`): ``quantity`` is its unfilled
        remainder, ``ordersAhead`` how many orders at its price are ahead of it
        (time priority), ``quantityAhead`` their unfilled tokens,
        ``l2OrderIndex`` its signed order number and ``orderTag`` your label for
        it. The frame is absolute for ``marketIndex``: replace what you hold
        for it, and an empty list means none of your orders rests there now.
        Each time a connection your trading key authenticated newly subscribes
        ``portfolio``, as the client does on every connection (subscribing
        again while subscribed sends nothing), one arrives for each market
        where you have resting orders (before or after :meth:`on_connect` on a
        new connection), then another whenever your entries there change, and
        each non-empty list again every 5 s, changed or not, so treat a frame
        as the list, not as a change. A market where none rests sends nothing,
        and no REST read serves queue places, so drop every list you hold in
        :meth:`on_disconnect` and, after you :meth:`unsubscribe` ``portfolio``,
        at the ``subscription_confirmed`` that answers it (frames sent before it
        can still arrive; :meth:`on` ``"subscription_confirmed"`` gets it), not
        in :meth:`on_connect`. It can arrive before the order's
        :meth:`on_order_update`.
        """
        return self.on(WSMessageType.ORDER_QUEUE, handler)

    def on_lifetime_volume(self, handler: Handler | None = None) -> Any:
        """Your lifetime traded volume changed (``lifetime_volume_update``):
        absolute; recomputed after fills."""
        return self.on(WSMessageType.LIFETIME_VOLUME_UPDATE, handler)

    def on_perps_account(self, handler: Handler | None = None) -> Any:
        """Your perps account (``perps_account_update``: ``{perps,
        accountMode, withdrawable}``, ``perps`` ``{l2AccountIndex, pools}`` as
        :meth:`loaf.resources.perps.PerpsResource.positions` serves them, the
        other two as :meth:`loaf.resources.portfolio.PortfolioResource.summary`
        does), after every engine commit that touches it, and as a snapshot
        (``snapshot: True``) about once a second, changed or not, flat
        accounts included: replace the pools you hold (a pool it does not list
        holds nothing). Its values are as of ``engineMs``: between pushes
        recompute them from ``perps:markets`` (README, **Perps**). Frames come
        in order, and a snapshot never goes out after a newer frame, so
        applying each as it comes never undoes a change. A missing snapshot
        means no news, never no positions; the client watches them
        (:attr:`fresh`). Delivery is at most once: the next snapshot brings
        what a reconnect lost."""
        return self.on(WSMessageType.PERPS_ACCOUNT_UPDATE, handler)

    def on_funding_payment(self, handler: Handler | None = None) -> Any:
        """A funding tick charged one of your positions (``funding_payment``:
        ``{funding}``, a :class:`~loaf.models.FundingPayment`): add it unless
        you hold its ``(journalSeq, pool)``. Delivery is at most once:
        reconcile with ``history.funding()``."""
        return self.on(WSMessageType.FUNDING_PAYMENT, handler)

    def on_holding(self, handler: Handler | None = None) -> Any:
        """One of your holdings changed (``holding_update``: ``{holding,
        details}``, ``holding`` whole, as ``portfolio.summary().holdings`` lists
        it, ``details`` its row of ``portfolio.component().holdingDetails``):
        upsert by ``holding.ticker``. An IPO allocation's ``quantity`` 0 removes it; an
        L2 holding's amounts stay its ``balances`` row. Fills close together
        send one frame. Its ``averageEntryPrice`` is ``None`` until the
        exchange has read its fills, and a later frame brings it: show none
        meanwhile, never the earlier one."""
        return self.on(WSMessageType.HOLDING_UPDATE, handler)

    def on_transfer(self, handler: Handler | None = None) -> Any:
        """A deposit or withdrawal of yours was journaled or moved on
        (``transfer_update``: ``{transfer}``, whole, as
        ``portfolio.component().transfers`` lists it): upsert by
        ``transfer.journalSeq``; it can repeat."""
        return self.on(WSMessageType.TRANSFER_UPDATE, handler)

    def on_fees(self, handler: Handler | None = None) -> Any:
        """The fees that apply to you changed (``fees_update``:
        ``{applicableFees}``): replace what
        ``portfolio.component().applicableFees`` gave you."""
        return self.on(WSMessageType.FEES_UPDATE, handler)

    def on_l2_keys(self, handler: Handler | None = None) -> Any:
        """A change to your account's keys was journaled (``l2_keys_update``:
        ``{apiKeyIndices}``, the api key indices it names), such as a reset of
        your trading key in the web app: read ``GET /l2/keys`` once
        (``loaf.get("/l2/keys", params={"apiKeyIndex": 0})``), which raises
        :class:`~loaf.exceptions.LoafAuthError` once your trading key is no
        longer the one in force."""
        return self.on(WSMessageType.L2_KEYS_UPDATE, handler)

    def on_perps_leverage(self, handler: Handler | None = None) -> Any:
        """You saved a leverage on a perp (``perps_leverage_update``:
        ``{ticker, leverage}``), here
        (:meth:`loaf.resources.portfolio.PortfolioResource.set_perps_leverage`)
        or in the web app: replace that ticker's row of
        ``portfolio.component().perpsLeverage``."""
        return self.on(WSMessageType.PERPS_LEVERAGE_UPDATE, handler)

    # lifecycle / control
    def on_error(self, handler: Handler | None = None) -> Any:
        """Server-sent ``error`` frames: ``{message, code}``.

        ``code`` is ``UNKNOWN_CHANNEL`` (no such channel: check the ticker and
        its case), ``UNAUTHORIZED`` (``portfolio`` without an accepted login
        token), ``SUBSCRIPTION_LIMIT``, ``TOO_MANY_CHANNELS``,
        ``INVALID_CHANNELS``, ``RATE_LIMITED`` or ``INVALID_MESSAGE``. A
        refused subscription or a rate-limited frame is dropped. Without a
        handler here, each one is logged as a warning with its code and the
        start of its message.
        """
        return self.on(WSMessageType.ERROR, handler)

    def on_connect(self, handler: Handler | None = None) -> Any:
        """Called once each time a connection goes live: the server has
        answered its auth and subscriptions.

        A refused subscription arrives before it as an :meth:`on_error` frame,
        and a ``trades`` subscription's recent trades too (:meth:`on_trades`).
        While ``portfolio`` is subscribed, a connection whose login token the
        server does not accept is dropped and retried (see
        :meth:`on_disconnect`). Nothing is replayed across a reconnect, so flag
        a re-seed here and do the REST reads from your own thread, as
        ``bot.py`` does: handlers run on the socket's thread, and a slow read
        inside one stalls the socket until the server's heartbeat drops it.
        The private reads (``portfolio.summary()``, ``portfolio.component()``
        and the ``history`` reads) first wait, up to 2 s, until every change
        pushed before them is readable.
        """
        return self.on(_ON_CONNECT, handler)

    def on_disconnect(self, handler: Handler | None = None) -> Any:
        """Called each time a connection attempt ends, except when you
        :meth:`stop` the client, with ``{code, reason}``.

        After a connection closes, ``code`` is the close code received, or the
        one the client sent when it closed without receiving one (1009 for a
        frame over its 16 MiB limit), 1006 when it dropped without either, and
        ``reason`` its reason (``""`` if none). After an attempt that made no
        connection (refused, DNS, TLS, the handshake answered with an HTTP
        error), ``code`` is ``None`` and ``reason`` the error text. When a
        handler's ``SystemExit``, a handler's ``KeyboardInterrupt`` in the
        background thread (:meth:`start`; under :meth:`run_forever` it stops the
        client), or a cancellation that :meth:`stop` did not ask for, ends the
        client's run, ``code`` is ``None`` and ``reason`` the exception's type
        name (``"SystemExit"``), and nothing reconnects until :meth:`start` or
        :meth:`run_forever` is called again. A run that ends after :meth:`stop`
        has stopped waiting for it reports nothing, even once :meth:`start`
        began the next. 1012 with reason ``resync`` (pushes may have
        been lost; see below) means re-seed from REST once :meth:`on_connect`
        fires again. 4001 with reason ``expired`` means the connection's login
        token expired before a fresh one reached the server (the client sends
        one each time the token passes half its life, so the machine slept, or
        a handler held the socket's thread): the next connection signs a fresh
        one. Calls with no :meth:`on_connect` between them mean the feed is
        down (the client keeps retrying with backoff). Drop every
        :meth:`on_order_queue` list you hold here (and, after you
        :meth:`unsubscribe` ``portfolio``, at the ``subscription_confirmed``
        that answers it), not in :meth:`on_connect`: the next connection sends
        them again, possibly before :meth:`on_connect`.

        The server sends ``resync`` after a loss on its side: its message bus
        reconnected, it was overloaded and shed pushes, or the matching engine
        dropped some or restarted without a clean shutdown (a clean restart
        closes nothing). It closes every connection holding ``portfolio`` and,
        about 2 s after the last bus reconnect or engine-side loss, every one
        holding the ``trades`` channel of a market that traded since or whose
        recent trades the server had missed: each at most once every 30 s, and
        a connection holding both only once.

        While ``portfolio`` is subscribed, the client closes a connection whose
        login token the server does not accept, with ``code`` 1006 and
        ``reason`` ``"the server did not accept the login token"``, and signs a
        fresh token for the next. The server also answers so when it cannot
        check the token at that moment, and a later attempt then gets through;
        if it keeps repeating, your trading key is not the one in force, and
        REST calls raise :class:`~loaf.exceptions.LoafAuthError` saying why.
        The server checks the key only when a token arrives: after a reset of
        your trading key in the web app, a live connection keeps its private
        stream until the server refuses its next fresh token, at most 5 minutes
        later, or, should none reach it, until its token expires (4001
        ``expired``), at most 10 minutes later; every connection after that is
        refused until you set the key in force (README §2) and restart.

        While ``portfolio`` is subscribed, the client also closes a connection
        whose handlers stayed behind the feed (:meth:`on_stale` ``"behind"``)
        for 15 s, with ``code`` 1006 and ``reason`` ``"the handlers stayed
        behind the feed"``, and reconnects with backoff: the next connection
        starts without the frames queued on that one. With
        ``auto_reconnect=False`` the run ends there, as after any other
        connection that ended.
        """
        return self.on(_ON_DISCONNECT, handler)

    def on_stale(self, handler: Handler | None = None) -> Any:
        """Your private stream went stale: :attr:`fresh` turned ``False``, with
        ``{reason, engineMs, silentSeconds}``.

        ``reason`` is ``"silent"`` (no stamped private frame for
        ``stale_after`` seconds: the exchange's engine or its feed stalled, or
        a handler held the client's loop that long, which the first frame
        after it reports before it brings :meth:`on_fresh`), ``"behind"`` (a
        handler is too slow for the feed, so what is being handled is older
        than ``stale_after``: an account snapshot lags more than
        ``stale_after`` and one second, the most a snapshot waits for the next
        beat before it is sent, behind the least lag seen on this connection,
        which may rise by 0.1% of the time since, for the drift between the two
        clocks. The next snapshot back within that bound fires
        :meth:`on_fresh`; a connection still behind after 15 s is closed and
        reconnected, see :meth:`on_disconnect`), ``"disconnected"`` (the
        connection dropped or the client's run ended; :meth:`on_disconnect`
        follows) or ``"unsubscribed"`` (you unsubscribed ``portfolio``).
        ``engineMs`` is the newest engine stamp seen (``None`` if none) and
        ``silentSeconds`` how long since the last stamped frame that counted
        as a beat was taken off the socket. Fires once per turn, only from
        ``True``. Pull your quotes here (from your own loop, as
        for every handler: set a flag), and keep
        :meth:`~loaf.resources.orders.OrdersResource.cancel_after` running as
        the exchange's side of the same guard.
        """
        return self.on(_ON_STALE, handler)

    def on_fresh(self, handler: Handler | None = None) -> Any:
        """Your private stream is live: :attr:`fresh` turned ``True``, with
        ``{engineMs}``. Fires at a connection's first account snapshot, and when
        stamped private frames come again after :meth:`on_stale` (after a
        ``"behind"`` turn, an account snapshot that is no longer behind). It
        fires after that frame's own handlers, so the snapshot is applied
        first."""
        return self.on(_ON_FRESH, handler)

    @property
    def fresh(self) -> bool | None:
        """Whether your private stream is live, by the exchange's account beat.

        ``None`` until this connection's first account snapshot (and on a
        deployment without perps, which sends none), ``True`` while stamped
        private frames keep coming within ``stale_after`` seconds and the
        handlers keep up with them, ``False`` once they stop, the handlers fall
        behind (an account snapshot lags more than ``stale_after`` and one
        second, see :meth:`on_stale`) or the connection drops. A new connection
        starts at ``None`` again, and :meth:`stop` leaves it ``None``. Once a
        ``SystemExit``, a background ``KeyboardInterrupt`` or a cancellation
        :meth:`stop` did not ask for has ended a run, it is ``False`` until
        :meth:`start`, :meth:`run_forever` or :meth:`stop`. Requires
        ``portfolio`` subscribed. Safe to read from any thread: it reads
        ``False`` once ``stale_after`` seconds have passed since the last beat,
        even while a handler blocks the client's loop (:meth:`on_stale` waits
        for the loop, and fires once it runs again, before the next
        :meth:`on_fresh`).
        """
        fresh = self._fresh
        if fresh is True:
            last = self._last_beat
            if last is None or time.monotonic() - last > self.stale_after:
                return False
        return fresh

    @property
    def engine_ms(self) -> int | None:
        """The newest ``engineMs`` of a private frame this client received:
        the exchange's engine clock (unix ms) as of your last private news.
        ``None`` before any."""
        return self._engine_ms

    # ------------------------------------------------------------------ #
    # Subscriptions
    # ------------------------------------------------------------------ #

    def subscribe(self, *channels: str) -> LoafWebSocketClient:
        """Subscribe to one or more raw ``"type:id"`` channel strings."""
        new = {str(c) for c in channels}
        self._channels |= new
        self._send_now({"type": "subscribe", "channels": sorted(new)})
        return self

    def unsubscribe(self, *channels: str) -> LoafWebSocketClient:
        gone = {str(c) for c in channels}
        self._channels -= gone
        self._send_now({"type": "unsubscribe", "channels": sorted(gone)})
        if not self._holds_portfolio():
            self._on_loop(self._portfolio_dropped, self._stop_event)
        return self

    def subscribe_orderbook(self, ticker: str) -> LoafWebSocketClient:
        return self.subscribe(f"orderbook:{ticker}")

    def subscribe_trades(self, ticker: str) -> LoafWebSocketClient:
        return self.subscribe(f"trades:{ticker}")

    def subscribe_chart(self, ticker: str) -> LoafWebSocketClient:
        """Candlestick channel (note: the channel name is singular ``chart``)."""
        return self.subscribe(f"chart:{ticker}")

    def subscribe_mark_price(self, ticker: str) -> LoafWebSocketClient:
        return self.subscribe(f"markprice:{ticker}")

    def subscribe_volume(self, ticker: str) -> LoafWebSocketClient:
        """Rolling-24h traded volume pushes for a market (seed from the REST
        ``volume24h``, then let ``volume_update`` frames replace it)."""
        return self.subscribe(f"volume:{ticker}")

    def subscribe_market_status(self, ticker: str) -> LoafWebSocketClient:
        """Halt/resume pushes for a market (the ``market:{ticker}`` channel —
        market status, not market data). Seed from the REST
        ``market.get(ticker).market.isHalted``, then let ``market_halt`` frames
        replace it."""
        return self.subscribe(f"market:{ticker}")

    def subscribe_perps_markets(self) -> LoafWebSocketClient:
        """Every perps market's live state and terms changes (no id — one global
        channel): see :meth:`on_perps_markets_live` and :meth:`on_market_terms`."""
        return self.subscribe("perps:markets")

    def subscribe_portfolio(self) -> LoafWebSocketClient:
        """Subscribe to your PRIVATE portfolio channel.

        Requires a client with a trading key: an anonymous connection is
        refused with an ``error`` frame (:meth:`on_error`), and one whose login
        token the server does not accept is dropped and retried
        (:meth:`on_disconnect`).
        """
        return self.subscribe("portfolio")

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def start(self) -> LoafWebSocketClient:
        """Connect and run in a background daemon thread. Non-blocking.

        While that thread's run is live, calling it again changes nothing.
        While :meth:`run_forever` is running on another thread (and not
        stopped), it raises ``RuntimeError``: :meth:`stop` that run first.
        """
        if self._thread and self._thread.is_alive():
            return self
        if self._foreground is not None and not self._foreground.is_set():
            raise RuntimeError(
                "start() was called while run_forever() is running: stop() the client first"
            )
        stop = self._begin_run()
        loop = self._loop = asyncio.new_event_loop()
        task = self._run_task = loop.create_task(self._run(stop))
        self._thread = threading.Thread(
            target=self._thread_main, args=(loop, task, stop), name="loaf-ws", daemon=True
        )
        self._thread.start()
        return self

    def run_forever(self) -> None:
        """Connect and block the current thread until stopped (Ctrl-C).

        A ``KeyboardInterrupt`` stops the client and returns, wherever it lands
        (a handler's code included): this is your thread, so it is Ctrl-C, and
        as with :meth:`stop`, neither :meth:`on_stale` nor :meth:`on_disconnect`
        fires. A ``SystemExit`` raised in a handler ends the run and is raised
        from here, after :meth:`on_stale` and :meth:`on_disconnect`.

        While another run is live (:meth:`start`'s background thread, or
        ``run_forever()`` on another thread) and not stopped, it raises
        ``RuntimeError``: :meth:`stop` that run first.
        """
        live = self._thread is not None and self._thread.is_alive()
        if (live and not self._stop_event.is_set()) or (
            self._foreground is not None and not self._foreground.is_set()
        ):
            raise RuntimeError(
                "run_forever() was called while another run of the client is live: stop() it "
                "first"
            )
        stop = self._foreground = self._begin_run()
        loop = self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        task = self._run_task = loop.create_task(self._run(stop, foreground=True))
        try:
            loop.run_until_complete(task)
        except KeyboardInterrupt:
            # Ctrl-C. One raised in the run's own code (a handler's) has already stopped the run:
            # asyncio would never stop the loop for that task again.
            stop.set()
            if not task.done():
                task.cancel()
                try:
                    loop.run_until_complete(task)
                except (asyncio.CancelledError, KeyboardInterrupt):
                    pass
        except asyncio.CancelledError:
            pass  # a cancellation stop() did not ask for: _run reported it
        except BaseException as exc:
            self._halt(exc, stop)  # raised outside the run's own task, by the beat's watch say
            raise
        finally:
            self._drain_and_close(loop)
            if self._loop is loop:  # not when another thread began the next run meanwhile
                self._loop = None
                self._run_task = None
            if self._foreground is stop:
                self._foreground = None

    def stop(self) -> None:
        """Stop the connection and tear down the background thread/loop.

        Safe to call from any thread and idempotent. The connection's
        ``_run`` task is cancelled on the loop thread, which lets that thread
        close its own loop cleanly (no cross-thread ``loop.stop`` race). It
        waits up to 5 s for the thread to end, then returns and logs a warning
        if it is still running: a handler is blocking it, or the server is not
        answering the close (the client waits up to 10 s for that). The
        thread then ends by itself; a handler that is running finishes, and
        no frame reaches a handler after it. The run it ends reports nothing
        and changes nothing of a run :meth:`start` begins meanwhile.
        """
        self._stop_event.set()
        loop, task = self._loop, self._run_task
        if loop is not None and task is not None:
            def _cancel() -> None:
                if not task.done():
                    task.cancel()

            try:
                loop.call_soon_threadsafe(_cancel)
            except RuntimeError:
                pass  # loop already closed
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=5)
            if thread.is_alive():
                logger.warning(
                    "Loaf WebSocket thread is still running 5 s after stop(): a handler is "
                    "blocking it or the server is not answering the close; it ends by itself"
                )
            self._thread = None
            self._run_task = None
            self._loop = None
        if thread is None or not thread.is_alive():
            # No thread left to reset the beat as its run ends (one a SystemExit ended, say);
            # a blocking run_forever's run resets it again as it ends.
            self._fresh = self._last_beat = None

    def wait_until_connected(self, timeout: float | None = None) -> bool:
        """Block until a connection is live, the moment :meth:`on_connect` fires
        (returns ``True``), or until ``timeout`` (returns ``False``)."""
        return self._connected_event.wait(timeout)

    def __enter__(self) -> LoafWebSocketClient:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    def _thread_main(
        self, loop: asyncio.AbstractEventLoop, task: asyncio.Task[None], stop: threading.Event
    ) -> None:
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(task)
        except BaseException as exc:  # noqa: BLE001
            # Reported, and the thread ends: nothing is left to raise it to. Raised outside the
            # run's own task (by the beat's watch, say), _halt reports it here.
            self._halt(exc, stop)
        finally:
            self._drain_and_close(loop)

    def _begin_run(self) -> threading.Event:
        """Make a new run the current one, and return its stop token. The run before it, if
        still ending, is stopped too: its token is set, so it changes nothing from here on."""
        self._stop_event.set()
        stop = self._stop_event = threading.Event()
        self._connected_event.clear()
        self._halted = False
        self._fresh = self._last_beat = None
        self._ws = None
        self._auth_token = None
        return stop

    def _drain_and_close(self, loop: asyncio.AbstractEventLoop) -> None:
        """Cancel any leftover tasks and close the loop (runs on the loop's thread)."""
        try:
            pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
            for t in pending:
                t.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        except Exception:  # noqa: BLE001
            pass
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:  # noqa: BLE001
            pass
        if not loop.is_closed():
            loop.close()

    async def _run(self, stop: threading.Event, *, foreground: bool = False) -> None:
        """One run, until ``stop`` (its own token) is set: ``foreground`` under run_forever()."""
        try:
            await self._connect_until_stopped(stop, foreground)
        except BaseException as exc:
            # A handler's SystemExit or KeyboardInterrupt, or a cancellation stop() did not ask
            # for: the run ends, never silently unless it is Ctrl-C (_ended).
            self._ended(exc, stop, foreground)
            raise
        finally:
            # Stopped, however the run ended (stop() cancels it, which skips the loop's own
            # checks): nothing is watched. A run start() has replaced leaves the beat to the next.
            if stop.is_set() and self._stop_event is stop:
                self._fresh = self._last_beat = None

    async def _connect_until_stopped(self, stop: threading.Event, foreground: bool) -> None:
        connect_kwargs: dict[str, Any] = {"max_size": _MAX_FRAME_SIZE}
        if self._ssl is not None:
            connect_kwargs["ssl"] = self._ssl
        ceiling = min(self.reconnect_delay, _MAX_RECONNECT_DELAY)
        while not stop.is_set():
            ws = None
            try:
                async with websockets.connect(self.ws_url, **connect_kwargs) as ws:
                    if stop.is_set():
                        break  # stopped during the handshake: the state is the next run's
                    self._ws = ws
                    self._auth_refused = False
                    # Each connection waits for its own beat, and measures its own lag.
                    self._fresh = self._last_beat = self._lag_base = self._behind_since = None
                    keeper = await self._on_open(ws, stop)
                    watch = asyncio.ensure_future(self._watch_beats(stop))
                    try:
                        async for raw in ws:
                            if stop.is_set():
                                break  # stopped: frames already queued reach no handler
                            self._dispatch(raw, stop)
                    except BaseException as exc:
                        if not isinstance(exc, Exception):
                            # Before the close below waits on the server.
                            self._ended(exc, stop, foreground)
                        raise
                    finally:
                        if keeper is not None:
                            keeper.cancel()
                        watch.cancel()
                # A normal close (1000/1001/1005), read once the context has finished it: the
                # legacy client (websockets 12) reports close_code only in the CLOSED state.
                code = int(ws.close_code) if ws.close_code is not None else 1006
                reason = ws.close_reason or ""
            except asyncio.CancelledError:
                if stop.is_set():
                    break
                raise  # not a stop: _run reports it
            except websockets.ConnectionClosed as exc:
                # The close frame received, else the one the client sent (1009 for a frame over
                # max_size), else none: dropped.
                frame = exc.rcvd if exc.rcvd is not None else exc.sent
                code = int(frame.code) if frame is not None else 1006  # not websockets' enum
                reason = frame.reason if frame is not None else ""
                logger.warning("Loaf WebSocket closed: %s %s", code, reason)
            except Exception as exc:  # noqa: BLE001
                if stop.is_set():
                    break
                # No connection (refused, DNS, TLS, an HTTP error answering the handshake), a
                # failure on a live one, or the client's own end of one (a refused login token,
                # handlers behind the feed). The type stands in for an empty text: websockets
                # 12 and 13 time a handshake out with a bare TimeoutError.
                code = None if ws is None else 1006
                reason = str(exc) or type(exc).__name__
                logger.warning("Loaf WebSocket connection error: %s", reason)
            finally:
                # Live long enough after on_connect for the next wait to start over.
                stable = (
                    self._connected_event.is_set()
                    and time.monotonic() - self._live_at >= _STABLE_CONNECTION
                )
                # Only this run's own connection: once start() has begun the next run (a close
                # can outlast stop()'s wait), the state is that run's.
                owned = self._stop_event is stop and self._ws is ws
                if owned:
                    self._ws = None
                    self._auth_token = None
                    self._connected_event.clear()
            if stop.is_set():
                break  # _run resets the beat
            if owned:
                self._beat_lost("disconnected", stop)
            self._emit(_ON_DISCONNECT, LoafObject({"code": code, "reason": reason}), stop)
            if stop.is_set() or not self.auto_reconnect:
                break
            if stable:
                ceiling = min(self.reconnect_delay, _MAX_RECONNECT_DELAY)
            # float(): a numpy float32 delay stays float32 through the draw, and from Python 3.10
            # macOS's selector (kqueue) refuses it as a timeout, outside the try: this thread ends.
            await asyncio.sleep(random.uniform(0, float(ceiling)))
            # Doubled, never computed as a power: 2.0 ** failures overflows after about 1,024
            # failures (hours offline), and an error here, outside the try, ends this thread.
            ceiling = min(ceiling * 2, _MAX_RECONNECT_DELAY)

    async def _on_open(self, ws: Any, stop: threading.Event) -> asyncio.Task[None] | None:
        """Send a connection's first frames: the auth frame with a login token (when the
        client has a trading key), the subscribe and a ping. Returns the task that keeps
        the connection authenticated, to cancel when it ends, or None for an anonymous one.
        ``stop`` is its run's token."""
        tokens = self._login_tokens
        renew_in = None
        if tokens is not None:
            self._auth_token, renew_in = tokens.token_and_renewal()
            await ws.send(self._frame("auth", token=self._auth_token))
        if self._channels:
            await ws.send(self._frame("subscribe", channels=sorted(self._channels)))
        # The server handles a socket's frames in order, so it echoes this one after it has
        # answered the auth and the subscribe: _dispatch marks the connection live then.
        await ws.send(self._frame("ping"))
        if tokens is None or renew_in is None:
            return None
        return asyncio.ensure_future(self._keep_authenticated(ws, tokens, renew_in, stop))

    async def _keep_authenticated(
        self, ws: Any, tokens: LoginTokens, renew_in: float, stop: threading.Event
    ) -> None:
        """Send a fresh login token on the live connection each time the one it sent last
        passes half its life: the server closes a connection whose token expires (4001
        ``expired``). ``renew_in`` is the seconds until the first one does; ``stop`` is its
        run's token."""
        try:
            while True:
                await asyncio.sleep(renew_in)
                if stop.is_set():
                    return  # the token it holds may be the next run's
                token, renew_in = tokens.token_and_renewal()
                if token != self._auth_token:  # not when the sleep ended a little early
                    self._auth_token = token
                    await ws.send(self._frame("auth", token=token))
        except websockets.ConnectionClosed:
            pass  # the connection's run reports how it ended
        except Exception:  # noqa: BLE001
            # The connection stays up until its token expires; the next one signs again.
            logger.exception("Loaf WebSocket could not send a fresh login token")

    async def _watch_beats(self, stop: threading.Event) -> None:
        """Turn :attr:`fresh` False once no stamped private frame came for ``stale_after``
        seconds (one task per connection, on the loop's thread), until ``stop``, its run's
        token, is set."""
        while not stop.is_set():
            last = self._last_beat
            if self._fresh is not True or last is None:
                await asyncio.sleep(self.stale_after)  # nothing to time yet
                continue
            wait = last + self.stale_after - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            else:
                self._beat_lost("silent", stop)

    def _note_beat(self, message: dict, arrived: float, stop: threading.Event) -> None:
        """A stamped private frame, taken off the socket at ``arrived``: news from the exchange's
        engine (on the loop's thread). The watch starts at a connection's first account
        snapshot, which shows the exchange beats. A snapshot also shows whether the handlers
        keep up: while one lags more than ``stale_after + _BEAT_ALLOWANCE``, no frame is a beat,
        and once that has lasted ``_BEHIND_LIMIT`` the connection is ended (ConnectionError),
        to be reconnected as after a refused login token. ``stop`` is the token of the run
        that took the frame off its socket."""
        engine_ms = message.get("engineMs")
        if not isinstance(engine_ms, int) or isinstance(engine_ms, bool):
            return  # a bool is an int to isinstance
        if not self._holds_portfolio():
            return
        if self._engine_ms is None or engine_ms > self._engine_ms:
            self._engine_ms = engine_ms
        if message.get("snapshot") is True:
            if self._snapshot_lag(arrived, engine_ms) > self.stale_after + _BEAT_ALLOWANCE:
                if self._behind_since is None:
                    self._behind_since = arrived
                    self._beat_lost("behind", stop)
                elif arrived - self._behind_since > _BEHIND_LIMIT:
                    raise ConnectionError(_BEHIND_REASON)
                return
            self._behind_since = None
        elif self._fresh is None or self._behind_since is not None:
            return  # only a snapshot starts the watch, or shows the handlers caught up
        last = self._last_beat
        if self._fresh is True and last is not None and arrived - last > self.stale_after:
            # A handler held the loop past stale_after, so the watch could not run: the gap is
            # reported before this frame, which then brings on_fresh, as fresh read meanwhile. The
            # gap ended when the frame arrived, before its own handlers ran.
            self._beat_lost("silent", stop, arrived)
            if stop.is_set():
                return  # an on_stale handler outlasted stop()'s wait: the state is the next run's
        self._last_beat = arrived
        if self._fresh is not True:
            self._fresh = True
            self._emit(_ON_FRESH, LoafObject({"engineMs": self._engine_ms}), stop)

    def _snapshot_lag(self, arrived: float, engine_ms: int) -> float:
        """How much later than the connection's best one this snapshot was handled, in seconds.

        Each snapshot's offset is when it was taken off the socket (this machine's clock) less
        its ``engineMs`` (the engine's at the read the snapshot was taken at, which advances at
        wall-clock rate while the engine runs): a handler too slow for the feed leaves frames
        queued, and the offset grows by the time they wait. The baseline is the least offset
        seen on this connection, but rises by up to ``_LAG_CREEP`` of the time elapsed, so drift
        between the two clocks never adds up to a lag. A larger lasting shift (a step of the
        engine's clock) stays behind until ``_BEHIND_LIMIT`` ends the connection, and the next
        one measures from a baseline of its own.
        """
        offset = arrived - engine_ms / 1000
        base = self._lag_base
        if base is None:
            base = offset
        else:
            base = min(offset, base + _LAG_CREEP * max(arrived - self._lag_base_at, 0.0))
        self._lag_base, self._lag_base_at = base, arrived
        return offset - base

    def _beat_lost(self, reason: str, stop: threading.Event, now: float | None = None) -> None:
        """The private stream is no longer known to be live (on the loop's thread), as of
        ``now`` (``time.monotonic()``, by default the moment of the call). Nothing once
        ``stop``, the token of the run that saw it, is set."""
        if self._fresh is not True or stop.is_set():
            return
        last = self._last_beat
        self._fresh = False
        if now is None:
            now = time.monotonic()
        silent = None if last is None else round(now - last, 3)
        self._emit(
            _ON_STALE,
            LoafObject({"reason": reason, "engineMs": self._engine_ms, "silentSeconds": silent}),
            stop,
        )

    def _ended(self, exc: BaseException, stop: threading.Event, foreground: bool) -> None:
        """The run ``stop`` is the token of is ending on ``exc``, a BaseException no handler
        swallowed. Under run_forever() (``foreground``, the caller's own thread) a
        KeyboardInterrupt is Ctrl-C, landed in a handler's code: a stop, reported as none. Any
        other is a halt (_halt), a KeyboardInterrupt in the background thread included: only a
        handler's code can raise one there."""
        if foreground and isinstance(exc, KeyboardInterrupt):
            stop.set()
        else:
            self._halt(exc, stop)

    def _halt(self, exc: BaseException, stop: threading.Event) -> None:
        """The run ``stop`` is the token of is ending on ``exc`` without a stop() (a handler's
        SystemExit, a handler's KeyboardInterrupt in the background thread, a cancellation
        stop() did not ask for): say so through the beat, on_disconnect and the log, once,
        before the thread ends or run_forever raises it. Nothing for a run asked to stop, as
        every run that start() has since replaced was."""
        if self._halted or stop.is_set():
            return
        self._halted = True
        self._connected_event.clear()
        self._beat_lost("disconnected", stop)
        if stop.is_set():
            return  # an on_stale handler outlasted stop()'s wait: the state is the next run's
        self._fresh = False  # until start(), run_forever() or stop()
        name = type(exc).__name__
        self._emit(_ON_DISCONNECT, LoafObject({"code": None, "reason": name}), stop)
        if stop.is_set():
            return
        logger.error(
            "Loaf WebSocket feed has stopped: %s ended the client's run; it does not reconnect "
            "until start() or run_forever() is called again",
            name,
            exc_info=exc,
        )

    def _portfolio_dropped(self, stop: threading.Event) -> None:
        """After an unsubscribe left no portfolio channel (on the loop's thread): not when one
        was subscribed again meanwhile, from another thread, nor once ``stop``, the token of the
        run current at the unsubscribe, is set. The lag is measured afresh from the next
        subscription's snapshots."""
        if stop.is_set() or self._holds_portfolio():
            return
        self._behind_since = self._lag_base = None
        self._beat_lost("unsubscribed", stop)

    def _holds_portfolio(self) -> bool:
        # A tuple first: another thread may subscribe meanwhile.
        return any(c == "portfolio" or c.startswith("portfolio:") for c in tuple(self._channels))

    def _on_loop(self, fn: Callable[..., None], *args: Any) -> None:
        """Run ``fn(*args)`` on the loop's thread: at once from that thread or with no loop
        running, else as soon as the loop gets to it."""
        loop = self._loop
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if loop is None or running is loop:
            fn(*args)
            return
        try:
            loop.call_soon_threadsafe(fn, *args)
        except RuntimeError:  # the run closed its loop after the read above
            fn(*args)

    def _dispatch(self, raw: Any, stop: threading.Event | None = None) -> None:
        """Hand a frame to its handlers and note what it says. ``stop`` is the token of the run
        that took it off its socket, as the run always passes it; ``None`` (a frame handed in
        directly) stands for the current run's. Once it is set, a handler's call can have
        outlasted stop()'s wait, so the frame reaches no further handler and changes nothing."""
        arrived = time.monotonic()  # taken off the socket: before any handler can hold the loop
        if stop is None:
            stop = self._stop_event
        if stop.is_set():
            return
        try:
            message = json.loads(raw)
        except (TypeError, ValueError):
            logger.debug("Ignoring non-JSON WebSocket frame: %r", raw)
            return
        obj = parse(message)
        mtype = message.get("type") if isinstance(message, dict) else None
        try:
            _served.frame(obj)
        except _served.Malformed as exc:
            # Never a value in another unit: the frame is dropped, as one that is not JSON is.
            logger.error("Loaf WebSocket dropped a %s frame with %s", mtype, exc)
            return
        if (
            mtype == WSMessageType.ECHO
            and not self._connected_event.is_set()
            and isinstance(message.get("data"), dict)
            and message["data"].get("type") == "ping"
        ):
            self._live_at = arrived
            self._connected_event.set()
            self._emit(_ON_CONNECT, None, stop)
        if mtype is not None and str(mtype) not in _INTERNAL_KEYS:
            self._emit(str(mtype), obj, stop)
        self._emit("*", obj, stop)
        if stop.is_set():
            return
        if mtype == WSMessageType.ERROR and not self._handlers.get(WSMessageType.ERROR.value):
            logger.warning(
                "Loaf WebSocket server error %s: %s",
                _bounded(message.get("code")),
                _bounded(message.get("message")),
            )
        # After the frame's own handlers, so a snapshot is applied before on_fresh; timed by its
        # arrival, so a slow handler never makes an old frame look new.
        if isinstance(mtype, str) and mtype in _STAMPED_PRIVATE:
            self._note_beat(message, arrived, stop)
            if stop.is_set():
                return
        if mtype == WSMessageType.AUTH_RESULT:
            self._auth_refused = message.get("success") is False
            if self._auth_refused and self._login_tokens is not None and self._auth_token:
                self._login_tokens.forget(self._auth_token)  # the next auth frame signs anew
        if (
            self._auth_refused
            and "portfolio" in self._channels
            and (
                mtype == WSMessageType.AUTH_RESULT
                or (mtype == WSMessageType.ERROR and message.get("code") == "UNAUTHORIZED")
            )
        ):
            # A refused auth, even a transient one, leaves the socket anonymous and open: end
            # the attempt, so that the private stream is retried with backoff. Portfolio
            # subscribed after that answer is refused UNAUTHORIZED: end it at that reply, which
            # comes once the subscribe has gone out (a queued send fails on a closed socket).
            raise ConnectionError("the server did not accept the login token")

    def _emit(self, key: str, payload: Any, stop: threading.Event) -> None:
        """Call ``key``'s handlers with ``payload``, none once ``stop`` (the token of the run
        that calls them) is set."""
        for handler in list(self._handlers.get(key, ())):
            if stop.is_set():
                return
            try:
                handler(payload)
            except (Exception, asyncio.CancelledError):  # noqa: BLE001
                # A task's cancellation reaches it only at an await, never inside a handler's own
                # code: a CancelledError here is the handler's, as any other exception.
                logger.exception("Loaf WebSocket handler for %r raised", key)

    def _send_now(self, message: dict) -> None:
        """Send immediately if connected; otherwise rely on resubscribe-on-connect."""
        loop = self._loop
        ws = self._ws
        if loop is None or ws is None:
            return
        frame = self._frame(message["type"], **{k: v for k, v in message.items() if k != "type"})
        coro = ws.send(frame)
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            loop.create_task(coro)
        else:
            try:
                asyncio.run_coroutine_threadsafe(coro, loop)
            except RuntimeError:  # the run closed its loop after the reads above
                coro.close()  # unsent, without a "never awaited" warning

    @staticmethod
    def _frame(message_type: str, **fields: Any) -> str:
        payload = {"type": message_type, "timestamp": int(time.time())}
        payload.update(fields)
        return json.dumps(payload)

    def __repr__(self) -> str:
        # getattr: crash reporters repr a client whose __init__ raised part-way.
        ws_url = getattr(self, "ws_url", None)
        connected = getattr(self, "_connected_event", None)
        state = "connected" if connected is not None and connected.is_set() else "idle"
        channels = len(getattr(self, "_channels", ()))
        return f"<LoafWebSocketClient {ws_url!r} {state} channels={channels}>"

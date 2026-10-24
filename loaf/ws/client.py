"""Threaded, callback-based WebSocket client — no asyncio knowledge required.

Typical usage::

    loaf = LoafClient(api_key="...")
    ws = loaf.websocket()

    @ws.on_orderbook
    def handle_book(msg):
        print(msg.propertyId, msg.bids[0].price)

    ws.subscribe_orderbook("OPRA")
    ws.subscribe_trades("OPRA")
    ws.run_forever()            # blocking; Ctrl-C to stop

Or run it in the background and keep using the REST client::

    with loaf.websocket() as ws:          # starts a background thread
        ws.on_trade(lambda m: ...)        # your own fills (private channel)
        ws.subscribe_portfolio()          # your own account's private stream
        ...                               # do other work; handlers fire live

Channels (``"type:key"`` strings — market-data channels key by the property's
``ticker``, exact case: ``orderbook:opra`` is refused; ``portfolio``,
``leaderboard`` and ``competition`` have no key):

==================  ========  ===========================================
Channel             Auth      What you receive
==================  ========  ===========================================
orderbook:{ticker}  public    full bid/ask book snapshots, on change (no initial frame)
trades:{ticker}     public    every trade, at most one frame per 100 ms (the recent ones on subscribe)
chart:{ticker}      public    OHLCV candle updates (note: singular "chart")
markprice:{ticker}  public    canonical mark price (1s, on change)
volume:{ticker}     public    rolling-24h traded volume (replaces ``volume24h``)
property:{ticker}   public    halt / resume status for one property
leaderboard         public    competition leaderboard (top entries) on change
competition         public    round transitions (PREPARED, ACTIVE, ENDING, ENDED, DRAFT)
portfolio           PRIVATE   your order transitions, fills, queue places and whole L2 balance book
==================  ========  ===========================================

Prices and quantities are human units (dollars / tokens); ``balances_update``
amounts are base-unit decimal strings (see :func:`loaf.from_base_units`);
timestamps are unix seconds (an order row's ``expiryMs`` is milliseconds).

Handlers run on the client's internal event-loop thread: keep them short. An
exception raised in a handler is logged and swallowed. Don't place orders from
a handler: a placement can block the feed for minutes while the exchange is
slow to answer (long enough for the server to drop the connection), and an
:class:`~loaf.exceptions.OrderOutcomeUnknownError` raised there is only logged.
Record the event and act from your own loop, as ``bot.py`` does.
"""

from __future__ import annotations

import asyncio
import json
import logging
import numbers
import random
import ssl
import threading
import time
from typing import TYPE_CHECKING, Any, Callable

import httpx

from .._object import LoafObject, parse
from ..client import _api_key_refusal
from ..enums import WSMessageType
from ..exceptions import LoafConfigError

if TYPE_CHECKING:
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

# Internal pseudo-types for lifecycle callbacks (never collide with wire types).
_ON_CONNECT = "__connect__"
_ON_DISCONNECT = "__disconnect__"

# The longest wait before a reconnect attempt, in seconds.
_MAX_RECONNECT_DELAY = 30.0


def derive_ws_url(base_url: str) -> str:
    """Map a REST base URL to its WebSocket URL.

    ``https://host/api`` -> ``wss://host/ws``; ``http://host:8005/api`` ->
    ``ws://host:8005/ws``.
    """
    url = base_url.rstrip("/")
    if url.endswith("/api"):
        url = url[: -len("/api")]
    if url.startswith("https://"):
        url = "wss://" + url[len("https://") :]
    elif url.startswith("http://"):
        url = "ws://" + url[len("http://") :]
    return url.rstrip("/") + "/ws"


class LoafWebSocketClient:
    """A reconnecting, thread-backed WebSocket client.

    Construct via :meth:`loaf.client.LoafClient.websocket`. Register handlers
    with the ``on_*`` decorators/methods, declare subscriptions with the
    ``subscribe_*`` helpers, then call :meth:`run_forever` (blocking) or use the
    instance as a context manager (background thread).

    ``reconnect_delay`` is the longest wait before the first reconnect attempt,
    in seconds; it doubles after each attempt that does not reach
    :meth:`on_connect`, up to 30 s, and each wait is drawn at random below it.
    """

    def __init__(
        self,
        client: LoafClient | None = None,
        *,
        api_key: str | None = None,
        ws_url: str | None = None,
        verify: bool | ssl.SSLContext | None = None,
        auto_reconnect: bool = True,
        reconnect_delay: float = 1.0,
    ) -> None:
        try:
            # Sent in every auth frame, so refused as LoafClient refuses its own (which a key
            # taken from it has passed).
            refusal = _api_key_refusal(api_key) if api_key else None
            if refusal:
                raise LoafConfigError(refusal)
            self.api_key = api_key if api_key is not None else (client.api_key if client else None)
        finally:
            api_key = None  # crash reporters record frame locals: no refused key stays here
        resolved_ws_url = ws_url or (client.ws_url if client is not None else None)
        if not resolved_ws_url:
            raise ValueError("ws_url is required when no LoafClient is provided")
        self.ws_url: str = resolved_ws_url
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

        self._handlers: dict[str, list[Handler]] = {}
        self._channels: set[str] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._run_task: asyncio.Task | None = None
        self._ws: Any = None
        self._auth_refused = False  # the live connection's auth_result was false
        self._stop_event = threading.Event()
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
        return self.on(WSMessageType.ORDERBOOK_UPDATE, handler)

    def on_trades(self, handler: Handler | None = None) -> Any:
        """A property's public trades (``trades_batch``: ``{propertyId, trades}``,
        each a :class:`~loaf.models.TradeTick`).

        Every trade is pushed, in at most one frame per property every 100 ms:
        each frame holds every trade printed since the previous one, newest
        first. Right after the subscription (so on every connection, before
        :meth:`on_connect`), one frame brings the property's recent trades, if
        any, up to 20, once; after a reconnect it repeats trades you already
        hold, so dedupe by ``tradeId``. For the trades missed while the feed
        was down, see :meth:`loaf.resources.market.MarketResource.trades`.
        """
        return self.on(WSMessageType.TRADES_BATCH, handler)

    def on_candle(self, handler: Handler | None = None) -> Any:
        return self.on(WSMessageType.CANDLE_UPDATE, handler)

    def on_mark_price(self, handler: Handler | None = None) -> Any:
        return self.on(WSMessageType.MARK_PRICE, handler)

    def on_volume(self, handler: Handler | None = None) -> Any:
        """A property's rolling-24h traded volume changed (``volume_update``:
        ``{propertyId, volume24h}``). Replaces the REST ``volume24h``."""
        return self.on(WSMessageType.VOLUME_UPDATE, handler)

    def on_property_halt(self, handler: Handler | None = None) -> Any:
        """A property was halted or resumed (``property_halt``:
        ``{propertyId, ticker, isHalted}``).

        ``isHalted`` is the EFFECTIVE state — the property's own flag OR'd with
        the platform-wide kill switch — so assign it straight over the
        ``isHalted`` you seeded from REST. While it is true, order placement on
        that property raises :class:`~loaf.exceptions.TradingHaltedError`.
        """
        return self.on(WSMessageType.PROPERTY_HALT, handler)

    def on_leaderboard(self, handler: Handler | None = None) -> Any:
        """The competition leaderboard changed (``leaderboard_update``:
        ``{leaderboard}`` — the same board ``GET /leaderboard`` serves, top
        entries only, so churn below the served cut does not wake you). No frame
        arrives on subscribe: seed from ``leaderboard.get()``."""
        return self.on(WSMessageType.LEADERBOARD_UPDATE, handler)

    def on_round_update(self, handler: Handler | None = None) -> Any:
        """A competition round changed status (``round_update``: ``{roundNumber,
        status, startsAt, endsAt, arenaTicker}``, times in unix seconds,
        ``arenaTicker`` the round's headline asset, if any).

        A round changes nothing about trading. Delivery is best-effort:
        ``competition.info()`` catches you up (after a reconnect, say), but keep
        a frame over a read made soon after it, which can be stale.
        """
        return self.on(WSMessageType.ROUND_UPDATE, handler)

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
        """A booked order's status/quantity changed (``order_status``).

        Booked orders only; a conditional never reaches this frame. One frame
        per fill. ``quantityLeft`` is the unfilled remainder: what is left after
        a fill, and the cancelled quantity on ``CANCELLED``, so filled =
        quantity - quantityLeft always.

        ``CANCELLED`` also arrives for cancels you did not request: the
        exchange's sweep 72 h before the order's ``expiryMs``, an epoch move (a
        trading-key reset or a cancel-all sent on chain), a closed market, a BUY
        that can no longer fund a fill within its hold (budget exhausted), an
        aggressing SELL at K1's bound (its remainder does not rest), your own
        order crossing your resting one (self-trade), and a firing stop freeing
        funds by cancelling your resting orders.
        """
        return self.on(WSMessageType.ORDER_STATUS, handler)

    def on_order_update(self, handler: Handler | None = None) -> Any:
        """One of your orders changed (``order_update``: ``{order}``).

        ``order`` is a booked order OR a conditional (stop / take) row — narrow
        with :func:`loaf.is_conditional_order` before reading ``filledQuantity``,
        which a conditional does not carry. Every conditional transition
        publishes one of these, TP/SL legs included (they are conditional rows).
        A firing conditional also cancels your own resting orders on that side
        of that property to free its funds, so you can receive ``CANCELLED``
        frames for orders you never cancelled.

        For a booked order it is sent after the exchange commits and carries the
        committed row, with ``expiryMs`` (unix ms, the signed expiry). For an
        order that trades immediately it can arrive AFTER that order's
        ``order_status`` / ``trade_new`` frames and already show
        ``PARTIALLY_FILLED`` or ``FILLED``, so upsert by id and do not assume
        ``OPEN`` first. A MARKET row's ``price`` is the worst price you signed.
        """
        return self.on(WSMessageType.ORDER_UPDATE, handler)

    def on_trade(self, handler: Handler | None = None) -> Any:
        """Your own fills on the private portfolio channel (``trade_new``).

        Exactly one frame per fill, final when it matches. ``trade.orderId`` is
        your order the fill belongs to, and ``trade.role`` is ``"MAKER"`` (that
        order rested) or ``"TAKER"`` (it aggressed). ``trade.txHash`` is the
        trade's content hash (``0x`` + 64 hex), not an on-chain transaction;
        ``trade.proof`` is ``{"status": "PENDING"}``, or ``None`` with proofs
        off; ``trade.fee`` is dollars to 0.001. Each engine update's burst
        carries its ``order_status`` frames, then its ``trade_new`` frames,
        then ``balances_update``; an update with more than 200 statuses or
        trades comes as several such bursts, ``balances_update`` in the last
        only. Match a fill to its order by ``trade.orderId``, not by frame
        order. Delivery is at most once (no replay after a reconnect), so
        reconcile with ``history.trades()``.
        """
        return self.on(WSMessageType.TRADE_NEW, handler)

    def on_order_queue(self, handler: Handler | None = None) -> Any:
        """Your resting orders' places in one property's book (``order_queue``:
        ``{propertyId, orders}``).

        ``orders`` lists your orders resting there, bids best first, then asks
        best first, each ``{orderId, side, price, quantity, quantityAhead,
        ordersAhead}`` (:class:`~loaf.models.OrderQueueEntry`): ``quantity`` is
        its unfilled remainder, ``ordersAhead`` how many orders at its price
        are ahead of it (time priority) and ``quantityAhead`` their unfilled
        tokens. The frame is absolute for ``propertyId``: replace what you hold
        for it, and an empty list means none of your orders rests there now.
        Each time a connection newly subscribes ``portfolio`` with your API
        key, as the client does on every connection (subscribing again while
        subscribed sends nothing), one arrives for each property where you
        have resting orders (before or after :meth:`on_connect` on a new
        connection), then another whenever your entries there change. A
        property where none rests sends nothing, and no REST read serves queue
        places, so drop every list you hold in :meth:`on_disconnect` and,
        after you :meth:`unsubscribe` ``portfolio``, at the
        ``subscription_confirmed`` that answers it (frames sent before it can
        still arrive; :meth:`on` ``"subscription_confirmed"`` gets it), not in
        :meth:`on_connect`. It can arrive before the order's
        :meth:`on_order_update`.
        """
        return self.on(WSMessageType.ORDER_QUEUE, handler)

    def on_lifetime_volume(self, handler: Handler | None = None) -> Any:
        """Your lifetime traded volume changed (``lifetime_volume_update``):
        absolute; recomputed after fills."""
        return self.on(WSMessageType.LIFETIME_VOLUME_UPDATE, handler)

    # lifecycle / control
    def on_error(self, handler: Handler | None = None) -> Any:
        """Server-sent ``error`` frames: ``{message, code}``.

        ``code`` is ``UNKNOWN_CHANNEL`` (no such channel: check the ticker and
        its case), ``UNAUTHORIZED`` (``portfolio`` without an accepted API
        key), ``SUBSCRIPTION_LIMIT``, ``TOO_MANY_CHANNELS``,
        ``INVALID_CHANNELS``, ``RATE_LIMITED`` or ``INVALID_MESSAGE``. A
        refused subscription or a rate-limited frame is dropped.
        """
        return self.on(WSMessageType.ERROR, handler)

    def on_connect(self, handler: Handler | None = None) -> Any:
        """Called once each time a connection goes live: the server has
        answered its auth and subscriptions.

        A refused subscription arrives before it as an :meth:`on_error` frame,
        and a ``trades`` subscription's recent trades too (:meth:`on_trades`).
        While ``portfolio`` is subscribed, a connection whose API key the
        server does not accept is dropped and retried (see
        :meth:`on_disconnect`). Nothing is replayed across a reconnect, so flag
        a re-seed here and do the REST reads from your own thread, as
        ``bot.py`` does: handlers run on the socket's thread, and a slow read
        inside one stalls the socket until the server's heartbeat drops it.
        The private reads (``portfolio.component()`` and the ``history`` reads)
        first wait, up to 2 s, until every change pushed before them is
        readable.
        """
        return self.on(_ON_CONNECT, handler)

    def on_disconnect(self, handler: Handler | None = None) -> Any:
        """Called each time a connection attempt ends, except when you
        :meth:`stop` the client, with ``{code, reason}``.

        After a connection closes, ``code`` is the close code received (1006
        when it dropped without one) and ``reason`` its reason (``""`` if
        none). After an attempt that made no connection (refused, DNS, TLS, the
        handshake answered with an HTTP error), ``code`` is ``None`` and
        ``reason`` the error text. 1012 with reason ``resync`` (pushes may have
        been lost; see below) means re-seed from REST once :meth:`on_connect`
        fires again. 4001 with reason ``revoked`` or ``expired`` means the API
        key was revoked or has expired and will not authenticate again: create
        a new one. Calls with no :meth:`on_connect` between them mean the feed
        is down (the client keeps retrying with backoff). Drop every
        :meth:`on_order_queue` list you hold here (and, after you
        :meth:`unsubscribe` ``portfolio``, at the ``subscription_confirmed``
        that answers it), not in :meth:`on_connect`: the next connection sends
        them again, possibly before :meth:`on_connect`.

        The server sends ``resync`` after a loss on its side: its message bus
        reconnected, it was overloaded and shed pushes, or the matching engine
        dropped some or restarted without a clean shutdown (a clean restart
        closes nothing). It closes every connection holding ``portfolio`` and,
        about 2 s after the last bus reconnect or engine-side loss, every one
        holding the ``trades`` channel of a property that traded since or whose
        recent trades the server had missed: each at most once every 30 s, and
        a connection holding both only once. An API-key connection can also
        get ``resync`` so that its key is checked again.

        While ``portfolio`` is subscribed, the client closes a connection whose
        API key the server does not accept, with ``code`` 1006 and ``reason``
        ``"the server did not accept the API key"``. The server also answers so
        when it cannot check the key at that moment, and a later attempt then
        gets through; if it keeps repeating, the key has expired or been
        revoked, and REST calls raise :class:`~loaf.exceptions.LoafAuthError`.
        """
        return self.on(_ON_DISCONNECT, handler)

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
        """Rolling-24h traded volume pushes for a property (seed from the REST
        ``volume24h``, then let ``volume_update`` frames replace it)."""
        return self.subscribe(f"volume:{ticker}")

    def subscribe_property_status(self, ticker: str) -> LoafWebSocketClient:
        """Halt/resume pushes for a property (the ``property:{ticker}``
        channel — property status, not market data). Seed from the REST
        ``market.property(ticker).property.isHalted``, then let
        ``property_halt`` frames replace it."""
        return self.subscribe(f"property:{ticker}")

    def subscribe_leaderboard(self) -> LoafWebSocketClient:
        """Live competition-leaderboard updates (no id — one global channel)."""
        return self.subscribe("leaderboard")

    def subscribe_competition(self) -> LoafWebSocketClient:
        """Competition round transitions (no id — one global channel): a
        ``round_update`` on each of PREPARED, ACTIVE, ENDING, ENDED and DRAFT.
        No frame arrives on subscribe: seed from ``competition.info()``."""
        return self.subscribe("competition")

    def subscribe_portfolio(self) -> LoafWebSocketClient:
        """Subscribe to your PRIVATE portfolio channel.

        Requires an ``api_key`` on the client: an anonymous connection is
        refused with an ``error`` frame (:meth:`on_error`), and one whose API
        key the server does not accept is dropped and retried
        (:meth:`on_disconnect`).
        """
        return self.subscribe("portfolio")

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def start(self) -> LoafWebSocketClient:
        """Connect and run in a background daemon thread. Non-blocking."""
        if self._thread and self._thread.is_alive():
            return self
        self._stop_event.clear()
        self._connected_event.clear()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._thread_main, name="loaf-ws", daemon=True)
        self._thread.start()
        return self

    def run_forever(self) -> None:
        """Connect and block the current thread until stopped (Ctrl-C)."""
        self._stop_event.clear()
        self._connected_event.clear()
        loop = self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        task = self._run_task = loop.create_task(self._run())
        try:
            loop.run_until_complete(task)
        except KeyboardInterrupt:  # pragma: no cover
            self._stop_event.set()
            task.cancel()
            try:
                loop.run_until_complete(task)
            except (asyncio.CancelledError, KeyboardInterrupt):
                pass
        except asyncio.CancelledError:  # pragma: no cover
            pass
        finally:
            self._drain_and_close(loop)
            self._loop = None
            self._run_task = None

    def stop(self) -> None:
        """Stop the connection and tear down the background thread/loop.

        Safe to call from any thread and idempotent. The connection's
        ``_run`` task is cancelled on the loop thread, which lets that thread
        close its own loop cleanly (no cross-thread ``loop.stop`` race).
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
            self._thread = None
            self._run_task = None
            self._loop = None

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

    def _thread_main(self) -> None:
        loop = self._loop
        assert loop is not None
        asyncio.set_event_loop(loop)
        task = self._run_task = loop.create_task(self._run())
        try:
            loop.run_until_complete(task)
        except asyncio.CancelledError:
            pass
        finally:
            self._drain_and_close(loop)

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

    async def _run(self) -> None:
        connect_kwargs: dict[str, Any] = {} if self._ssl is None else {"ssl": self._ssl}
        ceiling = min(self.reconnect_delay, _MAX_RECONNECT_DELAY)
        while not self._stop_event.is_set():
            ws = None
            try:
                async with websockets.connect(self.ws_url, **connect_kwargs) as ws:
                    self._ws = ws
                    self._auth_refused = False
                    await self._on_open(ws)
                    async for raw in ws:
                        self._dispatch(raw)
                # A normal close (1000/1001/1005), read once the context has finished it: the
                # legacy client (websockets 12) reports close_code only in the CLOSED state.
                code = ws.close_code if ws.close_code is not None else 1006
                reason = ws.close_reason or ""
            except asyncio.CancelledError:  # pragma: no cover
                break
            except websockets.ConnectionClosed as exc:
                code = exc.rcvd.code if exc.rcvd is not None else 1006
                reason = exc.rcvd.reason if exc.rcvd is not None else ""
                logger.warning("Loaf WebSocket closed: %s %s", code, reason)
            except Exception as exc:  # noqa: BLE001
                if self._stop_event.is_set():
                    break
                # No connection (refused, DNS, TLS, an HTTP error answering the handshake),
                # or a failure on a live one. The type stands in for an empty text: websockets
                # 12 and 13 time a handshake out with a bare TimeoutError.
                code = None if ws is None else 1006
                reason = str(exc) or type(exc).__name__
                logger.warning("Loaf WebSocket connection error: %s", reason)
            finally:
                connected = self._connected_event.is_set()
                # Only this attempt's own state: a stop() that stopped waiting (a close can
                # outlast its join) lets start() begin the next run before this one ends.
                if self._ws is ws:
                    self._ws = None
                    self._connected_event.clear()
            if self._stop_event.is_set():
                break
            self._emit(_ON_DISCONNECT, LoafObject({"code": code, "reason": reason}))
            if not self.auto_reconnect:
                break
            if connected:
                ceiling = min(self.reconnect_delay, _MAX_RECONNECT_DELAY)
            # float(): a numpy float32 delay stays float32 through the draw, and from Python 3.10
            # macOS's selector (kqueue) refuses it as a timeout, outside the try: this thread ends.
            await asyncio.sleep(random.uniform(0, float(ceiling)))
            # Doubled, never computed as a power: 2.0 ** failures overflows after about 1,024
            # failures (hours offline), and an error here, outside the try, ends this thread.
            ceiling = min(ceiling * 2, _MAX_RECONNECT_DELAY)

    async def _on_open(self, ws: Any) -> None:
        if self.api_key:
            await ws.send(self._frame("auth", token=self.api_key))
        if self._channels:
            await ws.send(self._frame("subscribe", channels=sorted(self._channels)))
        # The server handles a socket's frames in order, so it echoes this one after it has
        # answered the auth and the subscribe: _dispatch marks the connection live then.
        await ws.send(self._frame("ping"))

    def _dispatch(self, raw: Any) -> None:
        try:
            message = json.loads(raw)
        except (TypeError, ValueError):
            logger.debug("Ignoring non-JSON WebSocket frame: %r", raw)
            return
        obj = parse(message)
        mtype = message.get("type") if isinstance(message, dict) else None
        if (
            mtype == WSMessageType.ECHO
            and not self._connected_event.is_set()
            and isinstance(message.get("data"), dict)
            and message["data"].get("type") == "ping"
        ):
            self._connected_event.set()
            self._emit(_ON_CONNECT, None)
        if mtype is not None:
            self._emit(str(mtype), obj)
        self._emit("*", obj)
        if mtype == WSMessageType.AUTH_RESULT:
            self._auth_refused = message.get("success") is False
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
            raise ConnectionError("the server did not accept the API key")

    def _emit(self, key: str, payload: Any) -> None:
        for handler in list(self._handlers.get(key, ())):
            try:
                handler(payload)
            except Exception:  # noqa: BLE001
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

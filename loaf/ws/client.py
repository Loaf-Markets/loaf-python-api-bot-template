"""Threaded, callback-based WebSocket client — no asyncio knowledge required.

Typical usage::

    loaf = LoafClient(api_key="...")
    ws = loaf.websocket()

    @ws.on_orderbook
    def handle_book(msg):
        print(msg.propertyId, msg.bids[0].price)

    ws.subscribe_orderbook("opera")
    ws.subscribe_trades("opera")
    ws.run_forever()            # blocking; Ctrl-C to stop

Or run it in the background and keep using the REST client::

    with loaf.websocket() as ws:          # starts a background thread
        ws.on_trade(lambda m: ...)        # your own fills (private channel)
        ws.subscribe_portfolio()          # your own account's private stream
        ...                               # do other work; handlers fire live

Channels (``"type:key"`` strings — market-data channels key by ``tokenName``;
``portfolio`` and ``leaderboard`` have no key):

=====================  ========  ===========================================
Channel                Auth      What you receive
=====================  ========  ===========================================
orderbook:{tokenName}  public    full bid/ask book snapshots
trades:{tokenName}     public    rolling recent-trades batches
chart:{tokenName}      public    OHLCV candle updates (note: singular "chart")
markprice:{tokenName}  public    canonical mark price (1s, on change)
volume:{tokenName}     public    session volume replacing ``volume24h``
property:{tokenName}   public    halt / resume status for one property
ipo:{ipoId}            public    primary-market allocation progress
leaderboard            public    competition leaderboard (top entries) on change
portfolio              PRIVATE   your balances/positions/orders/trades deltas
=====================  ========  ===========================================

All values are already human units (dollars / tokens); timestamps are unix
seconds. Handlers run on the client's internal event-loop thread.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from typing import TYPE_CHECKING, Any, Callable

from .._object import parse
from ..enums import WSMessageType

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
_ON_TRANSPORT_ERROR = "__transport_error__"


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
    """

    def __init__(
        self,
        client: LoafClient | None = None,
        *,
        api_key: str | None = None,
        ws_url: str | None = None,
        verify: bool | str | None = None,
        auto_reconnect: bool = True,
        reconnect_delay: float = 2.0,
    ) -> None:
        self._client = client
        self.api_key = api_key if api_key is not None else (client.api_key if client else None)
        resolved_ws_url = ws_url or (client.ws_url if client is not None else None)
        if not resolved_ws_url:
            raise ValueError("ws_url is required when no LoafClient is provided")
        self.ws_url: str = resolved_ws_url
        self._verify = verify if verify is not None else (getattr(client, "_verify", True))
        self.auto_reconnect = auto_reconnect
        self.reconnect_delay = reconnect_delay

        self._handlers: dict[str, list[Handler]] = {}
        self._channels: set[str] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._run_task: asyncio.Task | None = None
        self._ws: Any = None
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
        """Public recent-trades batches for a property (``trades_batch``)."""
        return self.on(WSMessageType.TRADES_BATCH, handler)

    def on_candle(self, handler: Handler | None = None) -> Any:
        return self.on(WSMessageType.CANDLE_UPDATE, handler)

    def on_mark_price(self, handler: Handler | None = None) -> Any:
        return self.on(WSMessageType.MARK_PRICE, handler)

    def on_volume(self, handler: Handler | None = None) -> Any:
        """A property's session volume changed (``volume_update``:
        ``{propertyId, volume24h}``). Replaces the REST ``volume24h``."""
        return self.on(WSMessageType.VOLUME_UPDATE, handler)

    def on_property_halt(self, handler: Handler | None = None) -> Any:
        """A property was halted or resumed (``property_halt``:
        ``{propertyId, tokenName, isHalted}``).

        ``isHalted`` is the EFFECTIVE state — the property's own flag OR'd with
        the platform-wide kill switch — so assign it straight over the
        ``isHalted`` you seeded from REST. While it is true, order placement on
        that property raises :class:`~loaf.exceptions.TradingHaltedError`.
        """
        return self.on(WSMessageType.PROPERTY_HALT, handler)

    def on_ipo(self, handler: Handler | None = None) -> Any:
        return self.on(WSMessageType.IPO_ALLOCATION_UPDATE, handler)

    def on_leaderboard(self, handler: Handler | None = None) -> Any:
        """The competition leaderboard changed (``leaderboard_update``:
        ``{leaderboard}`` — the same board ``GET /leaderboard`` serves, top
        entries only, so churn below the served cut does not wake you)."""
        return self.on(WSMessageType.LEADERBOARD_UPDATE, handler)

    # private portfolio deltas
    def on_balances(self, handler: Handler | None = None) -> Any:
        return self.on(WSMessageType.BALANCES_UPDATE, handler)

    def on_position(self, handler: Handler | None = None) -> Any:
        return self.on(WSMessageType.POSITION_UPDATE, handler)

    def on_order_status(self, handler: Handler | None = None) -> Any:
        return self.on(WSMessageType.ORDER_STATUS, handler)

    def on_order_update(self, handler: Handler | None = None) -> Any:
        return self.on(WSMessageType.ORDER_UPDATE, handler)

    def on_trade(self, handler: Handler | None = None) -> Any:
        """Your own fills on the private portfolio channel (``trade_new``)."""
        return self.on(WSMessageType.TRADE_NEW, handler)

    def on_lifetime_volume(self, handler: Handler | None = None) -> Any:
        """Your lifetime traded volume changed (``lifetime_volume_update``)."""
        return self.on(WSMessageType.LIFETIME_VOLUME_UPDATE, handler)

    def on_transfer(self, handler: Handler | None = None) -> Any:
        return self.on(WSMessageType.TRANSFER_UPDATE, handler)

    def on_offering_order(self, handler: Handler | None = None) -> Any:
        return self.on(WSMessageType.OFFERING_ORDER_UPDATE, handler)

    # lifecycle / control
    def on_error(self, handler: Handler | None = None) -> Any:
        """Server-sent ``error`` frames (e.g. unauthorized subscription)."""
        return self.on(WSMessageType.ERROR, handler)

    def on_connect(self, handler: Handler | None = None) -> Any:
        """Called once each time the socket (re)connects and (re)subscribes."""
        return self.on(_ON_CONNECT, handler)

    def on_transport_error(self, handler: Handler | None = None) -> Any:
        """Called with the exception when the connection drops/fails."""
        return self.on(_ON_TRANSPORT_ERROR, handler)

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

    def subscribe_orderbook(self, token_name: str) -> LoafWebSocketClient:
        return self.subscribe(f"orderbook:{token_name}")

    def subscribe_trades(self, token_name: str) -> LoafWebSocketClient:
        return self.subscribe(f"trades:{token_name}")

    def subscribe_chart(self, token_name: str) -> LoafWebSocketClient:
        """Candlestick channel (note: the channel name is singular ``chart``)."""
        return self.subscribe(f"chart:{token_name}")

    def subscribe_mark_price(self, token_name: str) -> LoafWebSocketClient:
        return self.subscribe(f"markprice:{token_name}")

    def subscribe_volume(self, token_name: str) -> LoafWebSocketClient:
        """Session-volume pushes for a property (seed from the REST
        ``volume24h``, then let ``volume_update`` frames replace it)."""
        return self.subscribe(f"volume:{token_name}")

    def subscribe_property_status(self, token_name: str) -> LoafWebSocketClient:
        """Halt/resume pushes for a property (the ``property:{tokenName}``
        channel — property status, not market data). Seed from the REST
        ``market.property(token).property.isHalted``, then let
        ``property_halt`` frames replace it."""
        return self.subscribe(f"property:{token_name}")

    def subscribe_ipo(self, ipo_id: int) -> LoafWebSocketClient:
        return self.subscribe(f"ipo:{int(ipo_id)}")

    def subscribe_leaderboard(self) -> LoafWebSocketClient:
        """Live competition-leaderboard updates (no id — one global channel)."""
        return self.subscribe("leaderboard")

    def subscribe_portfolio(self) -> LoafWebSocketClient:
        """Subscribe to your PRIVATE portfolio channel.

        Requires an ``api_key`` on the client. An anonymous connection is
        refused with an ``error`` frame (:meth:`on_error`).
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
        """Block until the socket has connected once (returns ``True``) or times out."""
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
        connect_kwargs = self._connect_kwargs()
        while not self._stop_event.is_set():
            try:
                async with websockets.connect(self.ws_url, **connect_kwargs) as ws:
                    self._ws = ws
                    await self._on_open(ws)
                    async for raw in ws:
                        self._dispatch(raw)
            except asyncio.CancelledError:  # pragma: no cover
                break
            except Exception as exc:  # noqa: BLE001
                if self._stop_event.is_set():
                    break
                logger.warning("Loaf WebSocket connection error: %s", exc)
                self._emit(_ON_TRANSPORT_ERROR, exc)
            finally:
                self._ws = None
                self._connected_event.clear()
            if self._stop_event.is_set() or not self.auto_reconnect:
                break
            await asyncio.sleep(self.reconnect_delay)

    def _connect_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {}
        if self.ws_url.startswith("wss://") and self._verify is False:
            import ssl

            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            kwargs["ssl"] = ctx
        return kwargs

    async def _on_open(self, ws: Any) -> None:
        if self.api_key:
            await ws.send(self._frame("auth", token=self.api_key))
        if self._channels:
            await ws.send(self._frame("subscribe", channels=sorted(self._channels)))
        self._connected_event.set()
        self._emit(_ON_CONNECT, None)

    def _dispatch(self, raw: Any) -> None:
        try:
            message = json.loads(raw)
        except (TypeError, ValueError):
            logger.debug("Ignoring non-JSON WebSocket frame: %r", raw)
            return
        obj = parse(message)
        mtype = message.get("type") if isinstance(message, dict) else None
        if mtype is not None:
            self._emit(str(mtype), obj)
        self._emit("*", obj)

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
            asyncio.run_coroutine_threadsafe(coro, loop)

    @staticmethod
    def _frame(message_type: str, **fields: Any) -> str:
        payload = {"type": message_type, "timestamp": int(time.time())}
        payload.update(fields)
        return json.dumps(payload)

    def __repr__(self) -> str:
        state = "connected" if self._connected_event.is_set() else "idle"
        return f"<LoafWebSocketClient {self.ws_url!r} {state} channels={len(self._channels)}>"

"""Offline lifecycle tests for the WebSocket client against a real local server.

``_Server`` answers like the exchange's: it greets, answers ``auth`` (checking
its login token as the exchange does), confirms subscriptions (refusing
``portfolio`` without an accepted auth, and following a new ``trades:`` one with
its recent trades) and echoes any other frame, then runs the connection's
scripted action. Backoff waits come from a stand-in for
``loaf.ws.client.random`` that returns 0, so nothing really sleeps. Also guards
the bug where stop() raised "Event loop is closed" after a successful
connection.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import ssl
import sys
import threading
import time
import warnings
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Collection

import certifi
import pytest
import websockets

from loaf import LoafClient, LoafWebSocketClient

from conftest import KAT, L2_PUBLIC_KEY, TRADING_KEY, login_refusal, verify_login

# Another trading key (K1's second known-answer key): in force on the account once yours was reset.
OTHER_KEY = "0x" + KAT["keys"][1]["pk"]
Action = Callable[[Any], Awaitable[None]]
# What the client reports when the server refuses its login token with portfolio subscribed.
REFUSED = (1006, "the server did not accept the login token")


async def _push_book(ws: Any) -> None:
    await ws.send(json.dumps({
        "type": "orderbook_update", "ticker": "OPRA",
        "bids": [{"price": 100.0, "quantity": 1.0}], "asks": [], "timestamp": 0,
    }))


async def _close_resync(ws: Any) -> None:
    await ws.close(1012, "resync")


async def _close_expired(ws: Any) -> None:
    await ws.close(4001, "expired")


async def _go_away(ws: Any) -> None:
    await ws.close(1001, "going away")


async def _drop(ws: Any) -> None:
    ws.transport.close()  # the TCP connection, after what was sent, with no close frame


class _Server:
    """A local Loaf-like WS server. ``actions`` run one per connection, in order,
    after the echo of the connection's first ping; later connections just idle.
    It answers an auth frame true when the exchange would take its login token
    (checked against ``key_in_force`` as the frame arrives: the test key, until a
    test resets it), except the ``refused`` ones (counted from 1 across
    connections), and answers a new ``trades:`` subscription with ``recent_trades``
    (when any)."""

    def __init__(
        self,
        *actions: Action,
        refused: Collection[int] = (),
        port: int = 0,
        recent_trades: list[dict[str, Any]] | None = None,
    ) -> None:
        self.actions = list(actions)
        self.refused = refused
        self.port = port
        self.recent_trades = recent_trades or []
        self.key_in_force = L2_PUBLIC_KEY  # the account's api key 0
        self.received: list[list[dict[str, Any]]] = []  # the client's frames, per connection
        self.auth_at_ms: list[int] = []  # when each auth frame arrived, unix ms
        self.loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stop: asyncio.Event | None = None

    def start(self) -> _Server:
        ready = threading.Event()

        def run() -> None:
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)

            async def main() -> None:
                self._stop = asyncio.Event()
                async with websockets.serve(self._handle, "127.0.0.1", self.port) as server:
                    self.port = server.sockets[0].getsockname()[1]
                    ready.set()
                    await self._stop.wait()

            self.loop.run_until_complete(main())
            self.loop.close()

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()
        assert ready.wait(5), "server failed to start"
        return self

    def stop(self) -> None:
        if self.loop and self._stop:
            self.loop.call_soon_threadsafe(self._stop.set)
        if self._thread:
            self._thread.join(timeout=5)

    async def _handle(self, ws: Any) -> None:
        frames: list[dict[str, Any]] = []
        self.received.append(frames)
        action = self.actions.pop(0) if self.actions else None
        authed = False
        channels: list[str] = []
        await self._send(ws, type="connection")
        try:
            async for raw in ws:
                frame = json.loads(raw)
                frames.append(frame)
                if frame["type"] == "auth":
                    now_ms = time.time_ns() // 1_000_000
                    self.auth_at_ms.append(now_ms)
                    authed = (
                        len(self.auth_at_ms) not in self.refused
                        and login_refusal(frame["token"], self.key_in_force, now_ms) is None
                    )
                    await self._send(ws, type="auth_result", success=authed)
                elif frame["type"] == "subscribe":
                    refused = [] if authed else [c for c in frame["channels"] if c == "portfolio"]
                    if refused:
                        await self._send(ws, type="error", code="UNAUTHORIZED",
                                         message="Not authorized to subscribe to 1 channel(s)")
                    added = [c for c in frame["channels"] if c not in refused + channels]
                    if added:
                        channels += added
                        await self._send(ws, type="subscription_confirmed", channels=channels)
                    for channel in added:
                        if channel.startswith("trades:") and self.recent_trades:
                            await self._send(
                                ws, type="trades_batch", ticker="OPRA", trades=self.recent_trades
                            )
                else:
                    await self._send(ws, type="echo", data=frame)
                    if action is not None:
                        await action(ws)
                        action = None
        except websockets.ConnectionClosed:
            pass  # a scripted close or drop

    @staticmethod
    async def _send(ws: Any, **fields: Any) -> None:
        await ws.send(json.dumps({**fields, "timestamp": 0}))


class _Log:
    """What the client's handlers saw, appended on its thread and awaited from the test's."""

    def __init__(self) -> None:
        self.events: list[Any] = []
        self._changed = threading.Condition()

    def add(self, event: Any) -> None:
        with self._changed:
            self.events.append(event)
            self._changed.notify_all()

    def wait_for(self, done: Callable[[list[Any]], bool], timeout: float = 5) -> None:
        with self._changed:
            assert self._changed.wait_for(lambda: done(self.events), timeout), self.events


class _Uniform:
    """Stands in for ``loaf.ws.client.random``: records each backoff ceiling, waits 0."""

    def __init__(self) -> None:
        self.ceilings: list[float] = []

    def uniform(self, low: float, high: float) -> float:
        assert low == 0
        self.ceilings.append(high)
        return 0.0


@pytest.fixture(autouse=True)
def ceilings(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    uniform = _Uniform()
    monkeypatch.setattr("loaf.ws.client.random", uniform)
    return uniform.ceilings


def _closed_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _client(
    port: int, log: _Log, trading_key: str | None = None, **kwargs: Any
) -> LoafWebSocketClient:
    # Built as bot.py builds it: the trading key and the URL come from the LoafClient.
    ws = LoafClient(trading_key, base_url=f"http://127.0.0.1:{port}/api").websocket(**kwargs)
    ws.on_connect(lambda _: log.add("on_connect"))
    ws.on_disconnect(lambda m: log.add((m.code, m.reason)))
    return ws


def test_connect_subscribe_stop_is_clean():
    server = _Server(_push_book).start()
    log = _Log()
    ws = _client(server.port, log, TRADING_KEY, auto_reconnect=False)
    ws.on_message(lambda m: log.add(m.type))
    ws.subscribe_orderbook("OPRA")
    ws.start()
    thread = ws._thread
    try:
        assert ws.wait_until_connected(timeout=5)
        log.wait_for(lambda events: "orderbook_update" in events)
    finally:
        ws.stop()  # must not raise (the regression)
        server.stop()
    # on_connect fires once the server has answered the auth and the subscribe.
    assert log.events == [
        "connection", "auth_result", "subscription_confirmed", "on_connect", "echo",
        "orderbook_update",
    ]
    assert [frame["type"] for frame in server.received[0]] == ["auth", "subscribe", "ping"]
    # A login token the LoafClient's trading key signed for its account: the key is never sent.
    token = server.received[0][0]["token"]
    assert verify_login(token)["l2AccountIndex"] == 5 and TRADING_KEY[-80:] not in token
    assert token not in repr(ws)
    # The loop thread has ended (stop() clears _thread only when it still had to wait for it).
    assert thread is not None and not thread.is_alive()


def test_a_handler_that_raises_leaves_the_connection_up(caplog: pytest.LogCaptureFixture):
    # A handler's exception is logged and swallowed: only the client's own refused-token error,
    # raised by _dispatch, ends an attempt.
    server = _Server(_push_book).start()
    log = _Log()
    ws = _client(server.port, log, auto_reconnect=False)

    def handler(m: Any) -> None:
        if m.type == "connection":
            raise RuntimeError("a handler bug")
        log.add(m.type)

    ws.on_message(handler)
    ws.start()
    try:
        log.wait_for(lambda events: "orderbook_update" in events)
    finally:
        ws.stop()
        server.stop()
    assert log.events == ["on_connect", "echo", "orderbook_update"]
    assert "RuntimeError: a handler bug" in caplog.text


@pytest.mark.parametrize(
    "close, code, reason",
    [(_close_resync, 1012, "resync"), (_close_expired, 4001, "expired")],
    ids=["resync", "expired"],
)
def test_a_server_close_reconnects_authenticates_and_resubscribes(
    ceilings: list[float], close: Action, code: int, reason: str
):
    server = _Server(close).start()
    log = _Log()
    ws = _client(server.port, log, TRADING_KEY)
    ws.subscribe_portfolio()
    ws.start()
    try:
        log.wait_for(lambda events: events.count("on_connect") == 2)
    finally:
        ws.stop()
        server.stop()
    assert log.events == ["on_connect", (code, reason), "on_connect"]
    # Each connection authenticates first, then subscribes.
    opening = [[(f["type"], f.get("channels")) for f in frames[:2]] for frames in server.received]
    assert opening == [[("auth", None), ("subscribe", ["portfolio"])]] * 2
    for frames in server.received:
        verify_login(frames[0]["token"])
    assert ceilings == [1.0]


def test_a_trades_subscription_brings_its_recent_trades_before_on_connect():
    # The server follows a new trades subscription with the recent trades, newest first, before
    # it reads the ping: they reach on_trades ahead of on_connect, again on every connection.
    recent = [{"tradeId": 9, "price": 101.0}, {"tradeId": 7, "price": 100.5}]
    server = _Server(_close_resync, recent_trades=recent).start()
    log = _Log()
    ws = _client(server.port, log)
    ws.on_trades(lambda m: log.add([t.tradeId for t in m.trades]))
    ws.subscribe_trades("OPRA")
    ws.start()
    try:
        log.wait_for(lambda events: events.count("on_connect") == 2)
    finally:
        ws.stop()
        server.stop()
    assert log.events == [[9, 7], "on_connect", (1012, "resync"), [9, 7], "on_connect"]


def test_subscribe_and_unsubscribe_from_another_thread_while_connected():
    # From the bot's own thread a live connection's (un)subscribe goes out at once
    # (run_coroutine_threadsafe), and the reconnect re-sends only the channels still held.
    async def close_after_unsubscribe(ws: Any) -> None:
        async def close() -> None:
            while not any(f["type"] == "unsubscribe" for f in server.received[0]):
                await asyncio.sleep(0.01)
            await ws.close(1012, "resync")

        asyncio.ensure_future(close())  # meanwhile _handle goes on reading the client's frames

    server = _Server(close_after_unsubscribe).start()
    log = _Log()
    ws = _client(server.port, log)
    ws.subscribe_orderbook("OPRA")
    ws.start()
    try:
        assert ws.wait_until_connected(timeout=5)
        ws.subscribe_trades("OPRA")
        ws.unsubscribe("orderbook:OPRA")
        log.wait_for(lambda events: events.count("on_connect") == 2)
    finally:
        ws.stop()
        server.stop()
    assert log.events == ["on_connect", (1012, "resync"), "on_connect"]
    assert [[(f["type"], f.get("channels")) for f in frames] for frames in server.received] == [
        [("subscribe", ["orderbook:OPRA"]), ("ping", None), ("subscribe", ["trades:OPRA"]),
         ("unsubscribe", ["orderbook:OPRA"])],
        [("subscribe", ["trades:OPRA"]), ("ping", None)],
    ]


def test_a_normal_close_reports_its_code_and_reason():
    server = _Server(_go_away).start()
    log = _Log()
    ws = _client(server.port, log)
    ws.start()
    try:
        log.wait_for(lambda events: events.count("on_connect") == 2)
    finally:
        ws.stop()
        server.stop()
    assert log.events == ["on_connect", (1001, "going away"), "on_connect"]


def test_a_dropped_connection_reports_1006():
    server = _Server(_drop).start()
    log = _Log()
    ws = _client(server.port, log)
    ws.start()
    try:
        log.wait_for(lambda events: events.count("on_connect") == 2)
    finally:
        ws.stop()
        server.stop()
    assert log.events == ["on_connect", (1006, ""), "on_connect"]


class _Ahead:
    """Stands in for ``loaf.ws.client``'s ``time``: the real clocks, ``monotonic()`` moved on by
    ``ahead`` seconds."""

    def __init__(self) -> None:
        self.ahead = 0.0

    def time(self) -> float:
        return time.time()

    def monotonic(self) -> float:
        return time.monotonic() + self.ahead


@pytest.fixture
def ahead(monkeypatch: pytest.MonkeyPatch) -> _Ahead:
    clock = _Ahead()
    monkeypatch.setattr("loaf.ws.client.time", clock)
    return clock


@pytest.mark.parametrize(
    "delay, below_cap, lived, after",
    [
        (3.0, [3.0, 6.0, 12.0, 24.0], 10.0, 3.0),
        (45.0, [], 10.0, 30.0),
        (3.0, [3.0, 6.0, 12.0, 24.0], 9.9, 30.0),  # not live long enough: no reset
    ],
)
def test_backoff_doubles_to_30_s_and_resets_once_a_connection_stayed_live_10_s(
    ceilings: list[float], ahead: _Ahead, delay: float, below_cap: list[float], lived: float,
    after: float,
):
    port = _closed_port()
    log = _Log()
    ws = _client(port, log, reconnect_delay=delay)

    def live_for(_: Any) -> None:  # before the server's close: the connection lived this long
        ahead.ahead += lived

    ws.on_connect(live_for)
    ws.start()
    server = None
    try:
        log.wait_for(lambda events: len(events) >= 8)
        server = _Server(_close_resync, port=port).start()
        log.wait_for(lambda events: events.count("on_connect") == 2)
    finally:
        ws.stop()
        if server is not None:
            server.stop()
    failures = log.events.index("on_connect")
    assert failures >= 8
    assert all(code is None and reason for code, reason in log.events[:failures])
    assert log.events[failures:] == ["on_connect", (1012, "resync"), "on_connect"]
    assert ceilings[:failures] == below_cap + [30.0] * (failures - len(below_cap))
    assert ceilings[failures:] == [after]


def test_a_server_that_closes_each_connection_at_once_keeps_the_wait_growing(
    ceilings: list[float],
):
    # Each connection goes live (on_connect) and closes: none lasted, so each wait doubles, as
    # after a failed attempt, and every fresh login token goes out ever more slowly.
    server = _Server(*[_close_resync] * 4).start()
    log = _Log()
    ws = _client(server.port, log, TRADING_KEY)
    ws.start()
    try:
        log.wait_for(lambda events: events.count("on_connect") == 5)
    finally:
        ws.stop()
        server.stop()
    assert log.events == ["on_connect", (1012, "resync")] * 4 + ["on_connect"]
    assert ceilings == [1.0, 2.0, 4.0, 8.0]


def test_a_failed_reconnect_after_a_connection_reports_no_code():
    # A stopped server stops listening before it closes its connections, so every attempt after
    # the close is refused: each reports code None, not the code of the connection before it.
    server = _Server().start()
    log = _Log()
    ws = _client(server.port, log)
    ws.start()
    try:
        log.wait_for(lambda events: "on_connect" in events)
        server.stop()
        log.wait_for(lambda events: len(events) >= 4)
    finally:
        ws.stop()
    assert log.events[:2] == ["on_connect", (1001, "")]
    assert all(code is None and reason for code, reason in log.events[2:])


def test_eleven_hundred_failed_connects_leave_the_thread_alive(ceilings: list[float]):
    # A ceiling computed as a power overflows a float at the 1,024th failure, outside the try.
    log = _Log()
    ws = _client(_closed_port(), log)
    ws.start()
    try:
        log.wait_for(lambda events: len(events) >= 1_100, timeout=30)
        assert ws._thread is not None and ws._thread.is_alive()
    finally:
        ws.stop()
    assert all(code is None for code, _ in log.events)
    assert max(ceilings) == 30.0


def test_each_wait_is_drawn_below_a_float_ceiling(ceilings: list[float]):
    # Whatever the delay's type: a numpy float32 stays float32 through the draw, and from Python
    # 3.10 macOS's selector (kqueue) refuses it as a timeout, which ends the loop thread.
    log = _Log()
    ws = _client(_closed_port(), log, reconnect_delay=Fraction(1, 2))
    ws.start()
    try:
        log.wait_for(lambda events: len(events) >= 3)
    finally:
        ws.stop()
    assert ceilings[:3] == [0.5, 1.0, 2.0]
    assert {type(ceiling) for ceiling in ceilings} == {float}


class _TimesOut:
    """What ``websockets.connect`` 12 and 13 give when the handshake times out: a bare
    ``TimeoutError``, with no text."""

    async def __aenter__(self) -> Any:
        raise asyncio.TimeoutError

    async def __aexit__(self, *exc: object) -> None:
        pass


def test_a_failed_connect_with_no_error_text_reports_its_type(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(websockets, "connect", lambda *args, **kwargs: _TimesOut())
    log = _Log()
    ws = _client(_closed_port(), log, auto_reconnect=False)
    ws.start()
    try:
        log.wait_for(lambda events: len(events) == 1)
    finally:
        ws.stop()
    assert log.events == [(None, "TimeoutError")]


def test_the_websocket_trusts_what_rest_trusts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    # websockets gets the TLS httpx builds for REST from the same verify, never its own default
    # (the system store): certifi's CAs, your own SSLContext, or no checks.
    for name in ("SSL_CERT_FILE", "SSL_CERT_DIR"):  # httpx reads either in place of certifi
        monkeypatch.delenv(name, raising=False)
    certifi_cas = ssl.create_default_context(cafile=certifi.where()).get_ca_certs(binary_form=True)
    bundle = tmp_path / "ca.pem"  # a CA bundle of your own: one of certifi's CAs
    bundle.write_text(ssl.DER_cert_to_PEM_cert(certifi_cas[0]))
    context = ssl.create_default_context(cafile=str(bundle))
    connects: list[dict[str, Any]] = []

    def connect(url: str, **kwargs: Any) -> _TimesOut:
        connects.append(kwargs)
        return _TimesOut()

    monkeypatch.setattr(websockets, "connect", connect)

    def tls(base_url: str = "https://127.0.0.1/api", **kwargs: Any) -> dict[str, Any]:
        ws = LoafClient(base_url=base_url, **kwargs).websocket(auto_reconnect=False)
        asyncio.run(ws._run(ws._stop_event))  # one attempt, which the stand-in fails
        return connects.pop()

    default = tls()["ssl"]
    assert (default.verify_mode, default.check_hostname) == (ssl.CERT_REQUIRED, True)
    assert default.get_ca_certs(binary_form=True) == certifi_cas
    assert tls(verify=context)["ssl"] is context
    off = tls(verify=False)["ssl"]
    assert (off.verify_mode, off.check_hostname) == (ssl.CERT_NONE, False)
    # websockets refuses ssl for ws://
    assert "ssl" not in tls("http://127.0.0.1/api", verify=context)
    # The WebSocket's own verify wins over the LoafClient's, and needs no LoafClient.
    own = LoafClient(base_url="https://127.0.0.1/api", verify=context).websocket(verify=False)._ssl
    assert own is not None and (own.verify_mode, own.check_hostname) == (ssl.CERT_NONE, False)
    assert LoafWebSocketClient(ws_url="wss://127.0.0.1/ws", verify=context)._ssl is context
    alone = LoafWebSocketClient(ws_url="wss://127.0.0.1/ws")._ssl  # the default: certifi's CAs
    assert alone is not None and alone.verify_mode == ssl.CERT_REQUIRED
    assert alone.get_ca_certs(binary_form=True) == certifi_cas


def test_stop_fires_no_on_disconnect():
    # stop() sets the stop event, then cancels the connection's task, so a close can land in
    # between: it must not reach on_disconnect. The handler sets only the event, so it does.
    server = _Server(_close_resync).start()
    log = _Log()
    ws = _client(server.port, log)
    ws.on_connect(lambda _: ws._stop_event.set())
    ws.start()
    try:
        assert ws._thread is not None
        ws._thread.join(timeout=5)
        assert not ws._thread.is_alive()
    finally:
        ws.stop()
        server.stop()
    assert log.events == ["on_connect"]


def test_run_forever_returns_once_stopped():
    # The server keeps the connection open: only stop() (here from a handler) ends the run.
    server = _Server().start()
    log = _Log()
    ws = _client(server.port, log)
    ws.on_connect(lambda _: ws.stop())
    runner = threading.Thread(target=ws.run_forever, daemon=True)
    runner.start()
    try:
        runner.join(timeout=5)
        assert not runner.is_alive()
    finally:
        ws.stop()
        server.stop()
    assert log.events == ["on_connect"]


def test_a_run_ending_late_leaves_the_next_runs_connection_alone():
    # stop() stops waiting after 5 s while a close can take longer, so start() can begin the next
    # run before the old one ends. The handler stands in for that next run, live by the time the
    # old connection closes: the old run must leave its connection and live flag alone.
    server = _Server(_close_resync).start()
    log = _Log()
    ws = _client(server.port, log, auto_reconnect=False)
    newer = object()
    ws.on_connect(lambda _: setattr(ws, "_ws", newer))
    ws.start()
    try:
        assert ws._thread is not None
        ws._thread.join(timeout=5)
        assert not ws._thread.is_alive()
        assert ws._ws is newer and ws.wait_until_connected(timeout=0)
    finally:
        ws.stop()
        server.stop()
    assert log.events == ["on_connect", (1012, "resync")]


def test_stop_without_start_is_safe():
    ws = LoafWebSocketClient(ws_url="ws://127.0.0.1:1/ws")
    ws.stop()  # no-op, must not raise


def test_a_refused_subscription_reaches_on_error_before_on_connect():
    server = _Server().start()
    log = _Log()
    ws = _client(server.port, log)  # no trading key: no auth frame, and portfolio is refused
    ws.on_error(lambda m: log.add(m.code))
    ws.subscribe_portfolio()
    ws.subscribe_orderbook("OPRA")
    ws.start()
    try:
        log.wait_for(lambda events: "on_connect" in events)
    finally:
        ws.stop()
        server.stop()
    assert log.events == ["UNAUTHORIZED", "on_connect"]
    assert [frame["type"] for frame in server.received[0]] == ["subscribe", "ping"]


def test_a_refused_login_token_is_retried_while_portfolio_is_subscribed(ceilings: list[float]):
    # A refused auth, even a transient one, leaves the socket anonymous: the client drops the
    # connection, once handlers have had the refusal, and retries with backoff until a token is
    # accepted, each attempt with a fresh one: a refused token is never sent again.
    server = _Server(refused={1, 2, 3}).start()
    log = _Log()
    ws = _client(server.port, log, TRADING_KEY)
    ws.on("auth_result", lambda m: log.add(m.success))
    ws.subscribe_portfolio()
    ws.start()
    try:
        log.wait_for(lambda events: "on_connect" in events)
    finally:
        ws.stop()
        server.stop()
    assert log.events == [False, REFUSED, False, REFUSED, False, REFUSED, True, "on_connect"]
    assert ceilings == [1.0, 2.0, 4.0]
    assert [frames[0]["type"] for frames in server.received] == ["auth"] * 4
    tokens = [frames[0]["token"] for frames in server.received]
    assert len(set(tokens)) == 4


def test_a_refused_login_token_is_retried_once_portfolio_is_subscribed_later(
    ceilings: list[float]
):
    # The server refuses portfolio on the anonymous socket the refusal left, however late it is
    # subscribed: the client drops that live connection at the refusal, once the subscribe has
    # gone out (here from a handler, on the socket's thread), and authenticates again.
    server = _Server(refused={1}).start()
    log = _Log()
    ws = _client(server.port, log, TRADING_KEY)
    ws.on_error(lambda m: log.add(m.code))

    def subscribe_late(_: Any) -> None:
        if log.events == ["on_connect"]:  # the first connection, live without portfolio
            ws.subscribe_portfolio()

    ws.on_connect(subscribe_late)
    ws.subscribe_orderbook("OPRA")
    ws.start()
    try:
        log.wait_for(lambda events: events.count("on_connect") == 2)
    finally:
        ws.stop()
        server.stop()
    assert log.events == ["on_connect", "UNAUTHORIZED", REFUSED, "on_connect"]
    assert ceilings == [1.0]
    assert [[(f["type"], f.get("channels")) for f in frames] for frames in server.received] == [
        [("auth", None), ("subscribe", ["orderbook:OPRA"]), ("ping", None),
         ("subscribe", ["portfolio"])],
        [("auth", None), ("subscribe", ["orderbook:OPRA", "portfolio"]), ("ping", None)],
    ]


def test_a_refused_login_token_is_retried_once_portfolio_is_subscribed_from_another_thread():
    # The same with portfolio subscribed from the bot's own thread, where the docs say to act.
    server = _Server(refused={1}).start()
    log = _Log()
    ws = _client(server.port, log, TRADING_KEY)
    ws.on_error(lambda m: log.add(m.code))
    ws.subscribe_orderbook("OPRA")
    ws.start()
    try:
        assert ws.wait_until_connected(timeout=5)
        ws.subscribe_portfolio()
        log.wait_for(lambda events: events.count("on_connect") == 2)
    finally:
        ws.stop()
        server.stop()
    assert log.events == ["on_connect", "UNAUTHORIZED", REFUSED, "on_connect"]


def test_a_refused_login_token_keeps_a_public_only_connection():
    # Without portfolio nothing is lost: the server keeps the socket open for public channels.
    server = _Server(refused={1}).start()
    log = _Log()
    ws = _client(server.port, log, TRADING_KEY)
    ws.on("auth_result", lambda m: log.add(m.success))
    ws.subscribe_orderbook("OPRA")
    ws.start()
    try:
        log.wait_for(lambda events: "on_connect" in events)
    finally:
        ws.stop()
        server.stop()
    assert log.events == [False, "on_connect"]


def test_the_connection_sends_a_fresh_login_token_at_each_half_life(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    # In band, on the one connection: each token the connection holds is replaced once half its
    # life has passed, well before it expires, and portfolio stays subscribed throughout.
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr("loaf._l2.L2_LOGIN_TOKEN_LIFE_MS", 1_200)
    server = _Server().start()
    log = _Log()
    ws = _client(server.port, log, TRADING_KEY)
    ws.on("auth_result", lambda m: log.add(m.success))
    ws.subscribe_portfolio()
    ws.start()
    try:
        log.wait_for(lambda events: events.count(True) == 4)
    finally:
        ws.stop()
        server.stop()
    assert log.events == [True, "on_connect", True, True, True]
    (frames,) = server.received
    assert [f["type"] for f in frames] == ["auth", "subscribe", "ping", "auth", "auth", "auth"]
    tokens = [f["token"] for f in frames if f["type"] == "auth"]
    assert len(set(tokens)) == 4
    expiries = [verify_login(token)["l2ExpiryMs"] for token in tokens]
    for expiry, sent_at in zip(expiries, server.auth_at_ms[1:]):
        assert expiry - 600 <= sent_at < expiry  # half its life gone, never all of it
    assert not any(token in caplog.text for token in tokens)  # a credential: never logged


def test_a_refused_fresh_login_token_ends_the_connection_and_signs_another(
    monkeypatch: pytest.MonkeyPatch, ceilings: list[float]
):
    # The server refuses the token a live connection sent at half-life (it could not check it at
    # that moment, say): with portfolio subscribed, the attempt ends there, and the next one sends
    # a fresh token.
    monkeypatch.setattr("loaf._l2.L2_LOGIN_TOKEN_LIFE_MS", 1_200)
    server = _Server(refused={2}).start()
    log = _Log()
    ws = _client(server.port, log, TRADING_KEY)
    ws.on("auth_result", lambda m: log.add(m.success))
    ws.subscribe_portfolio()
    ws.start()
    try:
        log.wait_for(lambda events: events.count("on_connect") == 2)
    finally:
        ws.stop()
        server.stop()
    assert log.events[:6] == [True, "on_connect", False, REFUSED, True, "on_connect"]
    assert ceilings == [1.0]
    first, second = server.received[:2]
    tokens = [f["token"] for f in first if f["type"] == "auth"] + [second[0]["token"]]
    assert len(tokens) == 3 and len(set(tokens)) == 3


def test_after_a_key_reset_the_next_fresh_login_token_ends_the_private_stream(
    monkeypatch: pytest.MonkeyPatch, ceilings: list[float]
):
    # The server checks the key in force only when a token arrives: after a reset in the web app
    # the live connection streams on until it sends its next fresh token, which is refused, and
    # with portfolio subscribed the client ends the attempt there. Every reconnect's token, signed
    # by the same key, is refused too, each attempt waiting longer.
    monkeypatch.setattr("loaf._l2.L2_LOGIN_TOKEN_LIFE_MS", 2_000)  # the reset lands before 1 s
    server = _Server().start()
    log = _Log()
    ws = _client(server.port, log, TRADING_KEY)
    ws.on("auth_result", lambda m: log.add(m.success))
    ws.subscribe_portfolio()
    ws.start()
    try:
        assert ws.wait_until_connected(timeout=5)
        server.key_in_force = OTHER_KEY  # reset in the web app
        log.wait_for(lambda events: events.count(REFUSED) == 3)
    finally:
        ws.stop()
        server.stop()
    assert log.events[:8] == [True, "on_connect", False, REFUSED, False, REFUSED, False, REFUSED]
    assert ceilings[:3] == [1.0, 2.0, 4.0]
    assert [f["type"] for f in server.received[0]] == ["auth", "subscribe", "ping", "auth"]
    tokens = [f["token"] for frames in server.received[:3] for f in frames if f["type"] == "auth"]
    assert len(set(tokens)) == 4
    for token in tokens:
        verify_login(token)  # each a good signature, by the key no longer in force


@pytest.mark.parametrize(
    "serve, events",
    [
        (lambda: _Server(_close_resync), ["on_connect", (1012, "resync"), "on_connect"]),
        (lambda: _Server(_go_away), ["on_connect", (1001, "going away"), "on_connect"]),
        (lambda: _Server(refused={1}), [REFUSED, "on_connect"]),
    ],
    ids=["resync", "going-away", "refused"],
)
def test_the_token_refresh_ends_with_its_connection(
    serve: Callable[[], _Server], events: list[Any]
):
    # Each connection's refresh sleeps until the half-life of the client's one login token: it
    # ends with its connection, however that ends. Left running, it wakes with the next
    # connection's and, waking first, signs the fresh token and records it as sent: that
    # connection then skips its refresh and is closed 4001 expired.
    server = serve().start()
    log = _Log()
    ws = _client(server.port, log, TRADING_KEY)
    ws.subscribe_portfolio()
    ws.start()

    async def refreshes() -> int:  # run on the client's loop: its refresh tasks still running
        name = LoafWebSocketClient._keep_authenticated.__qualname__
        return sum(
            not task.done() and getattr(task.get_coro(), "__qualname__", None) == name
            for task in asyncio.all_tasks()
        )

    try:
        log.wait_for(lambda seen: seen == events)
        assert ws._loop is not None
        running = asyncio.run_coroutine_threadsafe(refreshes(), ws._loop).result(5)
    finally:
        ws.stop()
        server.stop()
    assert running == 1  # the live connection's alone


# ---------------------------------------------------------------------------
# The beat on a live connection: fresh, stale, and what ends a watch
# ---------------------------------------------------------------------------

ENGINE_MS = 1_800_000_000_000


def _snapshot(engine_ms: int) -> Action:
    """Sends the perps account snapshot the exchange beats with, about once a second."""

    async def send(ws: Any) -> None:
        await ws.send(json.dumps({
            "type": "perps_account_update", "snapshot": True, "engineMs": engine_ms,
            "batchSeq": 7, "perps": {"l2AccountIndex": 5, "pools": []}, "timestamp": 0,
        }))

    return send


def _then(*steps: Action) -> Action:
    async def run(ws: Any) -> None:
        for step in steps:
            await step(ws)

    return run


def _beat_client(port: int, log: _Log, **kwargs: Any) -> LoafWebSocketClient:
    ws = _client(port, log, TRADING_KEY, **kwargs)
    ws.on_fresh(lambda m: log.add(("fresh", m.engineMs)))
    ws.on_stale(lambda m: log.add(("stale", m.reason, m.engineMs, m.silentSeconds)))
    ws.subscribe_portfolio()
    return ws


def _stale_seen(seen: list[Any]) -> bool:
    return any(isinstance(event, tuple) and event[0] == "stale" for event in seen)


def test_a_silent_private_stream_turns_stale_after_stale_after():
    server = _Server(_snapshot(ENGINE_MS)).start()  # then nothing: the exchange's engine stalled
    log = _Log()
    ws = _beat_client(server.port, log, auto_reconnect=False, stale_after=1)
    ws.start()
    try:
        log.wait_for(_stale_seen)
        assert ws.fresh is False
    finally:
        ws.stop()
        server.stop()
    assert log.events[:2] == ["on_connect", ("fresh", ENGINE_MS)] and len(log.events) == 3
    _, reason, engine_ms, silent = log.events[2]
    assert (reason, engine_ms) == ("silent", ENGINE_MS) and 1.0 <= silent < 5
    assert ws.fresh is None  # stopped: nothing is watched


def test_stop_leaves_a_fresh_stream_unwatched_and_fires_nothing():
    server = _Server(_snapshot(ENGINE_MS)).start()
    log = _Log()
    ws = _beat_client(server.port, log)
    ws.start()
    try:
        log.wait_for(lambda seen: ("fresh", ENGINE_MS) in seen)
        assert ws.fresh is True and ws.engine_ms == ENGINE_MS
    finally:
        ws.stop()
        server.stop()
    # A stop is yours: no on_stale and no on_disconnect, and nothing left claiming a live stream.
    assert log.events == ["on_connect", ("fresh", ENGINE_MS)]
    assert ws.fresh is None and ws.engine_ms == ENGINE_MS


def test_a_dropped_connection_turns_the_stream_stale_and_the_next_waits_for_its_own_snapshot():
    server = _Server(_then(_snapshot(ENGINE_MS), _close_resync), _snapshot(ENGINE_MS + 5000)).start()
    log = _Log()
    ws = _beat_client(server.port, log)
    ws.on_connect(lambda _: log.add(("fresh at connect", ws.fresh)))

    async def watches() -> int:  # run on the client's loop: its beat watches still running
        name = LoafWebSocketClient._watch_beats.__qualname__
        return sum(
            not task.done() and getattr(task.get_coro(), "__qualname__", None) == name
            for task in asyncio.all_tasks()
        )

    ws.start()
    try:
        log.wait_for(lambda seen: ("fresh", ENGINE_MS + 5000) in seen)
        assert ws._loop is not None
        running = asyncio.run_coroutine_threadsafe(watches(), ws._loop).result(5)
    finally:
        ws.stop()
        server.stop()
    assert log.events[:3] == ["on_connect", ("fresh at connect", None), ("fresh", ENGINE_MS)]
    # Stale before on_disconnect, so a bot pulls its quotes before it hears why.
    _, reason, engine_ms, silent = log.events[3]
    assert (reason, engine_ms) == ("disconnected", ENGINE_MS) and 0 <= silent < 5
    assert log.events[4:] == [
        (1012, "resync"), "on_connect", ("fresh at connect", None), ("fresh", ENGINE_MS + 5000),
    ]
    assert running == 1  # the old connection's watch ended with it


def test_a_run_ending_late_leaves_the_next_runs_beat_alone():
    # As test_a_run_ending_late_leaves_the_next_runs_connection_alone: the on_fresh handler stands
    # in for the next run, live and fresh by the time the old connection closes. The old run's end
    # is not news about the next run's stream: no stale turn, and the stream stays fresh.
    server = _Server(_then(_snapshot(ENGINE_MS), _close_resync)).start()
    log = _Log()
    ws = _beat_client(server.port, log, auto_reconnect=False)
    newer = object()
    ws.on_fresh(lambda _: setattr(ws, "_ws", newer))
    ws.start()
    try:
        assert ws._thread is not None
        ws._thread.join(timeout=5)
        assert not ws._thread.is_alive()
        assert ws._ws is newer and ws.fresh is True
    finally:
        ws.stop()
        server.stop()
    assert log.events == ["on_connect", ("fresh", ENGINE_MS), (1012, "resync")]


def test_unsubscribing_portfolio_from_another_thread_turns_the_stream_stale():
    server = _Server(_snapshot(ENGINE_MS)).start()
    log = _Log()
    ws = _beat_client(server.port, log)
    ws.start()
    try:
        log.wait_for(lambda seen: ("fresh", ENGINE_MS) in seen)
        ws.unsubscribe("portfolio")  # this thread, while the loop runs
        log.wait_for(_stale_seen)
        assert ws.fresh is False
    finally:
        ws.stop()
        server.stop()
    assert len(log.events) == 3
    _, reason, engine_ms, silent = log.events[2]
    assert (reason, engine_ms) == ("unsubscribed", ENGINE_MS) and 0 <= silent < 5


def test_each_connection_measures_its_own_lag():
    # The first connection's snapshots show the engine's clock stepping back 5 s: behind, as far
    # as that connection can tell. The next connection starts its baseline afresh, so the same
    # engine clock is on time there.
    server = _Server(
        _then(_snapshot(ENGINE_MS), _snapshot(ENGINE_MS - 5000), _close_resync),
        _snapshot(ENGINE_MS - 5000),
    ).start()
    log = _Log()
    ws = _beat_client(server.port, log)
    ws.start()
    try:
        log.wait_for(lambda seen: len(seen) == 6)
    finally:
        ws.stop()
        server.stop()
    assert log.events[:2] == ["on_connect", ("fresh", ENGINE_MS)]
    assert log.events[2][:3] == ("stale", "behind", ENGINE_MS)  # engineMs: the newest seen
    # Already stale: the drop fires no second stale turn.
    assert log.events[3:] == [(1012, "resync"), "on_connect", ("fresh", ENGINE_MS)]


# ---------------------------------------------------------------------------
# Frame size: a frame can outgrow websockets' 1 MiB default
# ---------------------------------------------------------------------------


@pytest.fixture(params=["asyncio", "legacy"])
def ws_library(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    """Runs a test with each client the declared websockets range ships: the asyncio one (14 and
    later) and the legacy one (``websockets.connect`` in 12 and 13, still inside later ones)."""
    if request.param == "legacy":
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            legacy = pytest.importorskip("websockets.legacy.client")
        monkeypatch.setattr(
            "loaf.ws.client.websockets",
            SimpleNamespace(connect=legacy.connect, ConnectionClosed=websockets.ConnectionClosed),
        )
    return request.param


def _push_big_book(size: int) -> Action:
    async def send(ws: Any) -> None:
        await ws.send(json.dumps({
            "type": "orderbook_update", "ticker": "OPRA", "bids": [], "asks": [],
            "padding": "x" * size, "timestamp": 0,
        }))

    return send


def test_a_book_over_1_mib_reaches_its_handler(ws_library: str):
    server = _Server(_push_big_book(2 * 1024 * 1024)).start()
    log = _Log()
    ws = _client(server.port, log)
    ws.on_orderbook(lambda m: log.add(("book", len(m.padding))))
    ws.subscribe_orderbook("OPRA")
    ws.start()
    try:
        log.wait_for(lambda events: len(events) == 2)
    finally:
        ws.stop()
        server.stop()
    assert log.events == ["on_connect", ("book", 2 * 1024 * 1024)]


def test_the_client_takes_frames_up_to_16_mib(monkeypatch: pytest.MonkeyPatch):
    connects: list[dict[str, Any]] = []

    def connect(url: str, **kwargs: Any) -> _TimesOut:
        connects.append(kwargs)
        return _TimesOut()

    monkeypatch.setattr(websockets, "connect", connect)
    for url in ("ws://127.0.0.1:1/ws", "wss://127.0.0.1:1/ws"):
        ws = LoafWebSocketClient(ws_url=url, auto_reconnect=False)
        asyncio.run(ws._run(ws._stop_event))
    assert [kwargs["max_size"] for kwargs in connects] == [16 * 1024 * 1024] * 2


def test_a_frame_over_the_limit_reports_the_close_the_client_sent(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, ws_library: str
):
    # The client refuses the frame and closes 1009; the server drops the connection without
    # answering that close. on_disconnect reports the close the client sent, not 1006.
    monkeypatch.setattr("loaf.ws.client._MAX_FRAME_SIZE", 4096)
    server = _Server(_then(_push_big_book(8192), _drop)).start()
    log = _Log()
    ws = _client(server.port, log, auto_reconnect=False)
    ws.start()
    try:
        log.wait_for(lambda events: len(events) == 2)
    finally:
        ws.stop()
        server.stop()
    code, reason = log.events[1]  # the legacy client sends no reason
    assert log.events[0] == "on_connect" and code == 1009 and type(code) is int
    assert f"Loaf WebSocket closed: 1009 {reason}" in caplog.text


# ---------------------------------------------------------------------------
# What a handler's SystemExit, KeyboardInterrupt or a stray cancellation does
# ---------------------------------------------------------------------------


def _exit(_: Any) -> None:
    sys.exit(3)


def _interrupt(_: Any) -> None:
    raise KeyboardInterrupt


def _cancel_the_run(_: Any) -> None:
    # Cancels the client's own task: the cancellation lands at its next await, not in here.
    task = asyncio.current_task()
    assert task is not None
    task.cancel()


def _ending_client(port: int, log: _Log, handler: Callable[[Any], None]) -> LoafWebSocketClient:
    ws = _beat_client(port, log)
    ws.on_orderbook(handler)  # the book after the snapshot: the stream is fresh by then
    return ws


def _assert_reported(
    ws: LoafWebSocketClient, log: _Log, caplog: pytest.LogCaptureFixture, name: str
) -> None:
    assert log.events[:2] == ["on_connect", ("fresh", ENGINE_MS)]
    _, reason, engine_ms, _silent = log.events[2]
    assert (reason, engine_ms) == ("disconnected", ENGINE_MS)
    assert log.events[3:] == [(None, name)]  # and no reconnect
    assert ws.fresh is False and not ws.wait_until_connected(timeout=0)
    assert f"Loaf WebSocket feed has stopped: {name} ended the client's run" in caplog.text


@pytest.mark.parametrize(
    "handler, name",
    [(_exit, "SystemExit"), (_interrupt, "KeyboardInterrupt"),
     (_cancel_the_run, "CancelledError")],
    ids=["SystemExit", "KeyboardInterrupt", "cancelled"],
)
def test_a_run_a_handler_ends_says_so_and_stays_stale(
    caplog: pytest.LogCaptureFixture, handler: Callable[[Any], None], name: str
):
    server = _Server(_then(_snapshot(ENGINE_MS), _push_book)).start()
    log = _Log()
    ws = _ending_client(server.port, log, handler)
    ws.start()
    try:
        assert ws._thread is not None
        ws._thread.join(timeout=5)
        assert not ws._thread.is_alive()
        _assert_reported(ws, log, caplog, name)
        assert len(server.received) == 1  # it did not reconnect
    finally:
        ws.stop()
        server.stop()
    assert ws.fresh is None  # stop() resets it


def test_after_a_run_a_handler_ended_start_runs_again():
    server = _Server(_then(_snapshot(ENGINE_MS), _push_book)).start()
    log = _Log()
    ws = _ending_client(server.port, log, _exit)
    ws.start()
    try:
        assert ws._thread is not None
        ws._thread.join(timeout=5)
        assert ws.fresh is False
        ws.start()  # the next connection idles: no snapshot yet
        assert ws.fresh is None
        log.wait_for(lambda events: events.count("on_connect") == 2)
    finally:
        ws.stop()
        server.stop()


def test_run_forever_raises_a_handlers_system_exit_once_it_has_said_so(
    caplog: pytest.LogCaptureFixture,
):
    server = _Server(_then(_snapshot(ENGINE_MS), _push_book)).start()
    log = _Log()
    ws = _ending_client(server.port, log, _exit)
    raised: list[BaseException] = []

    def run() -> None:
        try:
            ws.run_forever()
        except BaseException as exc:  # noqa: BLE001
            raised.append(exc)

    runner = threading.Thread(target=run, daemon=True)
    runner.start()
    try:
        runner.join(timeout=5)
        assert not runner.is_alive()
        assert len(raised) == 1 and isinstance(raised[0], SystemExit) and raised[0].code == 3
        _assert_reported(ws, log, caplog, "SystemExit")
    finally:
        ws.stop()
        server.stop()


def test_run_forever_stops_at_a_handlers_keyboard_interrupt_as_at_ctrl_c(
    caplog: pytest.LogCaptureFixture,
):
    # run_forever runs on the caller's own thread, so a KeyboardInterrupt in a handler's code is
    # Ctrl-C landing there: the client stops and run_forever returns, and as after stop(),
    # neither on_stale nor on_disconnect fires and no error is logged.
    server = _Server(_then(_snapshot(ENGINE_MS), _push_book)).start()
    log = _Log()
    ws = _ending_client(server.port, log, _interrupt)
    runner = threading.Thread(target=ws.run_forever, daemon=True)
    runner.start()
    try:
        runner.join(timeout=5)
        assert not runner.is_alive()
        assert log.events == ["on_connect", ("fresh", ENGINE_MS)]
        assert ws.fresh is None and not ws.wait_until_connected(timeout=0) and not ws._halted
        assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []
        assert len(server.received) == 1  # it did not reconnect
    finally:
        ws.stop()
        server.stop()


def test_a_system_exit_in_on_stale_still_reports_the_end(caplog: pytest.LogCaptureFixture):
    # Raised from the beat's watch, a task of its own, not from the connection's run.
    server = _Server(_snapshot(ENGINE_MS)).start()  # then silence
    log = _Log()
    ws = _beat_client(server.port, log, stale_after=1)
    ws.on_stale(_exit)
    ws.start()
    try:
        assert ws._thread is not None
        ws._thread.join(timeout=5)
        assert not ws._thread.is_alive()
    finally:
        ws.stop()
        server.stop()
    assert log.events[:2] == ["on_connect", ("fresh", ENGINE_MS)]
    assert log.events[2][:2] == ("stale", "silent") and log.events[3:] == [(None, "SystemExit")]
    assert "Loaf WebSocket feed has stopped: SystemExit" in caplog.text


# ---------------------------------------------------------------------------
# stop() that cannot wait for the thread
# ---------------------------------------------------------------------------


def test_stop_warns_when_the_thread_outlives_its_wait(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    # A handler holds the loop's thread through stop()'s wait: stop() says so and returns. Once
    # the handler returns the thread ends by itself, and the frames queued behind it reach no
    # handler.
    server = _Server(_then(_push_book, _push_book, _push_book)).start()
    log = _Log()
    ws = _client(server.port, log)
    holding, release = threading.Event(), threading.Event()
    books: list[Any] = []

    def hold(m: Any) -> None:
        books.append(m)
        holding.set()
        release.wait(5)

    ws.on_orderbook(hold)
    ws.subscribe_orderbook("OPRA")
    joins: list[Any] = []
    real_join = threading.Thread.join

    def join(self: threading.Thread, timeout: Any = None) -> None:
        joins.append(timeout)
        real_join(self, 0.05)  # stands in for the 5 s wait

    ws.start()
    thread = ws._thread
    try:
        assert holding.wait(5)
        time.sleep(0.05)  # the other two books arrive behind it
        with monkeypatch.context() as patch:
            patch.setattr(threading.Thread, "join", join)
            ws.stop()
        assert joins == [5] and thread is not None and thread.is_alive()
        assert "Loaf WebSocket thread is still running 5 s after stop()" in caplog.text
    finally:
        release.set()
        if thread is not None:
            thread.join(timeout=5)
        server.stop()
    assert thread is not None and not thread.is_alive()
    assert len(books) == 1 and log.events == ["on_connect"]


def _stop_that_gives_up(ws: LoafWebSocketClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """stop(), its 5 s wait for the client's thread cut to 50 ms: it gives up while the thread
    runs on."""
    real_join = threading.Thread.join

    def join(self: threading.Thread, timeout: Any = None) -> None:
        real_join(self, 0.05)

    with monkeypatch.context() as patch:
        patch.setattr(threading.Thread, "join", join)
        ws.stop()


def _when(event: threading.Event, step: Action) -> Action:
    """Runs ``step`` once the test sets ``event``."""

    async def run(ws: Any) -> None:
        while not event.is_set():
            await asyncio.sleep(0.01)
        await step(ws)

    return run


def test_a_run_cancelled_after_stop_gave_up_leaves_the_next_run_alone(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    # A handler holds the old run's loop through stop()'s wait, and start() begins the next run.
    # Once the handler returns, the old run meets the cancellation stop() asked for: it reports
    # nothing and changes nothing of the next run, whose own halt is still reported.
    halt = threading.Event()
    server = _Server(
        _then(_snapshot(ENGINE_MS), _push_book),
        _then(_snapshot(ENGINE_MS + 5000), _when(halt, _push_book)),
    ).start()
    log = _Log()
    ws = _beat_client(server.port, log)
    holding, release = threading.Event(), threading.Event()
    books: list[Any] = []

    def book(m: Any) -> None:
        books.append(m)
        if len(books) == 1:  # the old run's
            holding.set()
            release.wait(5)
        else:  # the next run's, once the old one has ended
            sys.exit(3)

    ws.on_orderbook(book)
    ws.start()
    old = ws._thread
    try:
        assert holding.wait(5)
        _stop_that_gives_up(ws, monkeypatch)
        assert old is not None and old.is_alive()
        ws.start()
        log.wait_for(lambda seen: ("fresh", ENGINE_MS + 5000) in seen)
        release.set()
        old.join(timeout=5)
        assert not old.is_alive()
        assert log.events == [
            "on_connect", ("fresh", ENGINE_MS), "on_connect", ("fresh", ENGINE_MS + 5000),
        ]
        assert ws.wait_until_connected(timeout=0) and ws.fresh is True and not ws._halted
        assert "feed has stopped" not in caplog.text and len(books) == 1
        new = ws._thread
        halt.set()
        assert new is not None
        new.join(timeout=5)
        assert not new.is_alive()
    finally:
        release.set()
        ws.stop()
        server.stop()
    assert log.events[4][:3] == ("stale", "disconnected", ENGINE_MS + 5000)
    assert log.events[5:] == [(None, "SystemExit")]
    assert caplog.text.count("Loaf WebSocket feed has stopped: SystemExit ended") == 1


def test_a_run_ending_by_itself_after_stop_gave_up_leaves_the_next_run_alone(
    monkeypatch: pytest.MonkeyPatch,
):
    # The server closed the old run's connection, and its on_stale handler holds the loop through
    # stop()'s wait; start() begins the next run. Once the handler returns, the old run ends by
    # itself, with no cancellation to meet (it does not reconnect): it calls no on_disconnect,
    # and leaves the next run live and fresh.
    server = _Server(_then(_snapshot(ENGINE_MS), _close_resync), _snapshot(ENGINE_MS + 5000))
    server.start()
    log = _Log()
    ws = _beat_client(server.port, log, auto_reconnect=False)
    holding, release = threading.Event(), threading.Event()

    def hold(_: Any) -> None:
        if not holding.is_set():
            holding.set()
            release.wait(5)

    ws.on_stale(hold)
    ws.start()
    old = ws._thread
    try:
        assert holding.wait(5)
        _stop_that_gives_up(ws, monkeypatch)
        assert old is not None and old.is_alive()
        ws.start()
        log.wait_for(lambda seen: ("fresh", ENGINE_MS + 5000) in seen)
        release.set()
        old.join(timeout=5)
        assert not old.is_alive()
        assert ws.wait_until_connected(timeout=0) and ws.fresh is True and not ws._halted
    finally:
        release.set()
        ws.stop()
        server.stop()
    assert log.events[:2] == ["on_connect", ("fresh", ENGINE_MS)]
    assert log.events[2][:3] == ("stale", "disconnected", ENGINE_MS)
    assert log.events[3:] == ["on_connect", ("fresh", ENGINE_MS + 5000)]


def _replaced_client(port: int, log: _Log, **kwargs: Any) -> LoafWebSocketClient:
    """A client whose handlers record which thread called them."""
    ws = LoafClient(TRADING_KEY, base_url=f"http://127.0.0.1:{port}/api").websocket(**kwargs)
    me = threading.get_ident
    ws.on_connect(lambda _: log.add(("connect", me())))
    ws.on_disconnect(lambda m: log.add(("disconnect", m.reason, me())))
    ws.on_fresh(lambda m: log.add(("fresh", m.engineMs, me())))
    ws.subscribe_portfolio()
    return ws


def _next_runs_state(ws: LoafWebSocketClient) -> tuple[Any, ...]:
    return (
        ws._fresh, ws._last_beat, ws._engine_ms, ws._connected_event.is_set(), ws._halted,
        ws._ws, ws._auth_token, ws._auth_refused, ws._lag_base, ws._behind_since, ws._live_at,
        ws.fresh,
    )


def _book_exits(m: Any) -> None:
    sys.exit(3)


@pytest.mark.parametrize(
    "path, first, reason, stale_after",
    [
        # A frame after a gap reports it: a handler moved this machine's clock on 31 s (the
        # engine's too), so the next snapshot is on time but past stale_after.
        ("frame", _then(_snapshot(ENGINE_MS), _snapshot(ENGINE_MS + 31_000)), "silent", 30),
        ("watch", _snapshot(ENGINE_MS), "silent", 1),  # then silence: the beat's watch
        ("halt", _then(_snapshot(ENGINE_MS), _push_book), "disconnected", 3),
        ("closed", _then(_snapshot(ENGINE_MS), _close_resync), "disconnected", 3),
    ],
)
def test_a_run_whose_on_stale_outlasts_stop_changes_nothing_of_the_next_run(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, ahead: _Ahead,
    path: str, first: Action, reason: str, stale_after: float,
):
    # The first on_stale handler holds the old run's loop through stop()'s wait (pulling quotes
    # over REST, say), and start() begins the next run. Once it returns, the old run calls no
    # other handler (the second on_stale, on_fresh, on_disconnect) and leaves the next run's
    # state as it was: its beat, its connection, and no "feed has stopped" error.
    server = _Server(first, _snapshot(ENGINE_MS + 60_000)).start()
    log = _Log()
    ws = _replaced_client(server.port, log, stale_after=stale_after)
    holding, release = threading.Event(), threading.Event()

    def hold(m: Any) -> None:
        log.add(("stale", m.reason, threading.get_ident()))
        if not holding.is_set():
            holding.set()
            release.wait(5)

    def jump(m: Any) -> None:
        if m.engineMs == ENGINE_MS and path in ("frame", "watch"):
            ahead.ahead += 31

    ws.on_stale(hold)
    ws.on_stale(lambda m: log.add(("stale#2", m.reason, threading.get_ident())))
    ws.on_perps_account(jump)
    if path == "halt":
        ws.on_orderbook(_book_exits)
    ws.start()
    old = ws._thread
    assert old is not None
    try:
        assert holding.wait(5)
        _stop_that_gives_up(ws, monkeypatch)
        assert old.is_alive()
        ws.stale_after = 30.0  # the next run's own watch stays quiet through the test
        ws.start()
        new = ws._thread
        assert new is not None and new is not old
        log.wait_for(lambda seen: any(e[:2] == ("fresh", ENGINE_MS + 60_000) for e in seen))
        before = _next_runs_state(ws)
        assert before[0] is True and before[3] and not before[4]
        release.set()
        old.join(timeout=5)
        assert not old.is_alive()
        assert _next_runs_state(ws) == before
    finally:
        release.set()
        ws.stop()
        server.stop()
    assert [e[:-1] for e in log.events if e[-1] == old.ident] == [
        ("connect",), ("fresh", ENGINE_MS), ("stale", reason),
    ]
    assert [e[:-1] for e in log.events if e[-1] == new.ident] == [
        ("connect",), ("fresh", ENGINE_MS + 60_000),
    ]
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []
    assert len(server.received) == 2  # the old run did not reconnect


def test_start_while_run_forever_runs_raises_and_stop_ends_that_run():
    server = _Server().start()
    log = _Log()
    ws = _client(server.port, log)
    runner = threading.Thread(target=ws.run_forever, daemon=True)
    runner.start()
    try:
        assert ws.wait_until_connected(timeout=5)
        with pytest.raises(RuntimeError, match=r"^start\(\) was called while run_forever\(\)"):
            ws.start()
        with pytest.raises(RuntimeError, match=r"^run_forever\(\) was called while another run"):
            ws.run_forever()  # a second run_forever, here from this thread
        assert ws._thread is None and runner.is_alive()
        ws.stop()
        runner.join(timeout=5)
        assert not runner.is_alive()
        ws.start()  # nothing is live now
        log.wait_for(lambda seen: seen.count("on_connect") == 2)
    finally:
        ws.stop()
        server.stop()
    assert len(server.received) == 2


def test_run_forever_while_the_background_thread_runs_raises_and_stop_ends_that_run():
    server = _Server().start()
    log = _Log()
    ws = _client(server.port, log)
    ws.on_connect(lambda _: ws.stop() if log.events.count("on_connect") == 2 else None)
    ws.start()
    thread = ws._thread
    try:
        assert ws.wait_until_connected(timeout=5)
        with pytest.raises(RuntimeError, match=r"^run_forever\(\) was called while another run"):
            ws.run_forever()
        assert ws.start() is ws and ws._thread is thread  # its own run: nothing changes
        assert thread is not None and thread.is_alive() and ws.wait_until_connected(timeout=0)
        ws.stop()
        assert not thread.is_alive()
        ws.run_forever()  # nothing is live now: it runs, until its on_connect stops it
    finally:
        ws.stop()
        server.stop()
    assert log.events == ["on_connect", "on_connect"] and len(server.received) == 2


@pytest.mark.parametrize("stable_after, after", [(10.0, 1.0), (20.0, 2.0)], ids=["rule", "20s"])
def test_a_connection_behind_for_15_s_is_ended_and_the_next_is_fresh_at_its_first_snapshot(
    monkeypatch: pytest.MonkeyPatch, ceilings: list[float], ahead: _Ahead, stable_after: float,
    after: float,
):
    # The engine's clock steps back 5 s: the connection reads behind, and once 15 s have passed
    # on this machine's clock (moved on here by a handler) the client ends it, as it does for a
    # refused login token, and reconnects. The next connection measures from a baseline of its
    # own: fresh at its first snapshot. The wait before it starts over only as the rule says: the
    # connection was live 16 s, against the rule's 10 s (or 20 s here).
    monkeypatch.setattr("loaf.ws.client._STABLE_CONNECTION", stable_after)
    server = _Server(
        _close_resync,  # not live long enough: the wait after it doubles
        _then(_snapshot(ENGINE_MS), _snapshot(ENGINE_MS - 5000), _snapshot(ENGINE_MS + 11_000)),
        _snapshot(ENGINE_MS + 12_000),
    ).start()
    log = _Log()
    ws = _beat_client(server.port, log)

    def later(m: Any) -> None:
        if m.engineMs == ENGINE_MS - 5000:
            ahead.ahead += 16

    ws.on_perps_account(later)
    ws.start()
    try:
        log.wait_for(lambda seen: ("fresh", ENGINE_MS + 12_000) in seen)
    finally:
        ws.stop()
        server.stop()
    assert log.events[:4] == ["on_connect", (1012, "resync"), "on_connect", ("fresh", ENGINE_MS)]
    assert log.events[4][:3] == ("stale", "behind", ENGINE_MS)
    assert log.events[5:] == [
        (1006, "the handlers stayed behind the feed"), "on_connect", ("fresh", ENGINE_MS + 12_000),
    ]
    assert ceilings == [1.0, after]

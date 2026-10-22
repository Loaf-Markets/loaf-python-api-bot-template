"""Offline lifecycle tests for the WebSocket client against a real local server.

``_Server`` answers like the exchange's: it greets, answers ``auth``, confirms
subscriptions (refusing ``portfolio`` without an accepted auth) and echoes any
other frame, then runs the connection's scripted action. Backoff waits come from
a stand-in for ``loaf.ws.client.random`` that returns 0, so nothing really
sleeps. Also guards the bug where stop() raised "Event loop is closed" after a
successful connection.
"""

from __future__ import annotations

import asyncio
import json
import socket
import ssl
import threading
from fractions import Fraction
from pathlib import Path
from typing import Any, Awaitable, Callable

import certifi
import pytest
import websockets

from loaf import LoafClient, LoafWebSocketClient

Action = Callable[[Any], Awaitable[None]]


async def _push_book(ws: Any) -> None:
    await ws.send(json.dumps({
        "type": "orderbook_update", "propertyId": 1,
        "bids": [{"price": 100.0, "quantity": 1.0}], "asks": [], "timestamp": 0,
    }))


async def _close_venue(ws: Any) -> None:
    await ws.close(1012, "venue")


async def _go_away(ws: Any) -> None:
    await ws.close(1001, "going away")


async def _drop(ws: Any) -> None:
    ws.transport.close()  # the TCP connection, after what was sent, with no close frame


class _Server:
    """A local Loaf-like WS server. ``actions`` run one per connection, in order,
    after the echo of the connection's first ping; later connections just idle.
    It refuses the first ``refusals`` auth frames (``auth_result`` false)."""

    def __init__(self, *actions: Action, refusals: int = 0, port: int = 0) -> None:
        self.actions = list(actions)
        self.refusals = refusals
        self.port = port
        self.received: list[list[dict[str, Any]]] = []  # the client's frames, per connection
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
                    authed = self.refusals == 0
                    if not authed:
                        self.refusals -= 1
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
    port: int, log: _Log, api_key: str | None = None, **kwargs: Any
) -> LoafWebSocketClient:
    # Built as bot.py builds it: the API key and the URL come from the LoafClient.
    ws = LoafClient(api_key=api_key, base_url=f"http://127.0.0.1:{port}/api").websocket(**kwargs)
    ws.on_connect(lambda _: log.add("on_connect"))
    ws.on_disconnect(lambda m: log.add((m.code, m.reason)))
    return ws


def test_connect_subscribe_stop_is_clean():
    server = _Server(_push_book).start()
    log = _Log()
    ws = _client(server.port, log, api_key="key", auto_reconnect=False)
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
    assert server.received[0][0]["token"] == "key"  # the LoafClient's API key
    # The loop thread has ended (stop() clears _thread only when it still had to wait for it).
    assert thread is not None and not thread.is_alive()


def test_a_handler_that_raises_leaves_the_connection_up(caplog: pytest.LogCaptureFixture):
    # A handler's exception is logged and swallowed: only the client's own refused-key error,
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


def test_a_1012_close_reconnects_and_resubscribes(ceilings: list[float]):
    server = _Server(_close_venue).start()
    log = _Log()
    ws = _client(server.port, log, api_key="key")
    ws.subscribe_portfolio()
    ws.start()
    try:
        log.wait_for(lambda events: events.count("on_connect") == 2)
    finally:
        ws.stop()
        server.stop()
    assert log.events == ["on_connect", (1012, "venue"), "on_connect"]
    subscribes = [[f["channels"] for f in frames if f["type"] == "subscribe"]
                  for frames in server.received]
    assert subscribes == [[["portfolio"]], [["portfolio"]]]
    assert ceilings == [1.0]


def test_subscribe_and_unsubscribe_from_another_thread_while_connected():
    # From the bot's own thread a live connection's (un)subscribe goes out at once
    # (run_coroutine_threadsafe), and the reconnect re-sends only the channels still held.
    async def close_after_unsubscribe(ws: Any) -> None:
        async def close() -> None:
            while not any(f["type"] == "unsubscribe" for f in server.received[0]):
                await asyncio.sleep(0.01)
            await ws.close(1012, "venue")

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
    assert log.events == ["on_connect", (1012, "venue"), "on_connect"]
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


@pytest.mark.parametrize("delay, below_cap", [(3.0, [3.0, 6.0, 12.0, 24.0]), (45.0, [])])
def test_backoff_doubles_to_30_s_and_resets_once_connected(
    ceilings: list[float], delay: float, below_cap: list[float]
):
    port = _closed_port()
    log = _Log()
    ws = _client(port, log, reconnect_delay=delay)
    ws.start()
    server = None
    try:
        log.wait_for(lambda events: len(events) >= 8)
        server = _Server(_close_venue, port=port).start()
        log.wait_for(lambda events: events.count("on_connect") == 2)
    finally:
        ws.stop()
        if server is not None:
            server.stop()
    failures = log.events.index("on_connect")
    assert failures >= 8
    assert all(code is None and reason for code, reason in log.events[:failures])
    assert log.events[failures:] == ["on_connect", (1012, "venue"), "on_connect"]
    assert ceilings[:failures] == below_cap + [30.0] * (failures - len(below_cap))
    assert ceilings[failures:] == [min(delay, 30.0)]


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
        asyncio.run(ws._run())  # one attempt, which the stand-in fails
        return connects.pop()

    default = tls()["ssl"]
    assert (default.verify_mode, default.check_hostname) == (ssl.CERT_REQUIRED, True)
    assert default.get_ca_certs(binary_form=True) == certifi_cas
    assert tls(verify=context)["ssl"] is context
    off = tls(verify=False)["ssl"]
    assert (off.verify_mode, off.check_hostname) == (ssl.CERT_NONE, False)
    assert tls("http://127.0.0.1/api", verify=context) == {}  # websockets refuses ssl for ws://
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
    server = _Server(_close_venue).start()
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
    server = _Server(_close_venue).start()
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
    assert log.events == ["on_connect", (1012, "venue")]


def test_stop_without_start_is_safe():
    ws = LoafWebSocketClient(ws_url="ws://127.0.0.1:1/ws")
    ws.stop()  # no-op, must not raise


def test_a_refused_subscription_reaches_on_error_before_on_connect():
    server = _Server().start()
    log = _Log()
    ws = _client(server.port, log)  # no API key: portfolio is refused
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


def test_a_refused_api_key_is_retried_while_portfolio_is_subscribed(ceilings: list[float]):
    # A refused auth, even a transient one, leaves the socket anonymous: the client drops the
    # connection, once handlers have had the refusal, and retries with backoff until the key is
    # accepted.
    server = _Server(refusals=3).start()
    log = _Log()
    ws = _client(server.port, log, api_key="key")
    ws.on("auth_result", lambda m: log.add(m.success))
    ws.subscribe_portfolio()
    ws.start()
    try:
        log.wait_for(lambda events: "on_connect" in events)
    finally:
        ws.stop()
        server.stop()
    refused = (1006, "the server did not accept the API key")
    assert log.events == [False, refused, False, refused, False, refused, True, "on_connect"]
    assert ceilings == [1.0, 2.0, 4.0]
    assert [frames[0]["type"] for frames in server.received] == ["auth"] * 4


def test_a_refused_api_key_is_retried_once_portfolio_is_subscribed_later(ceilings: list[float]):
    # The server refuses portfolio on the anonymous socket the refusal left, however late it is
    # subscribed: the client drops that live connection at the refusal, once the subscribe has
    # gone out (here from a handler, on the socket's thread), and authenticates again.
    server = _Server(refusals=1).start()
    log = _Log()
    ws = _client(server.port, log, api_key="key")
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
    refused = (1006, "the server did not accept the API key")
    assert log.events == ["on_connect", "UNAUTHORIZED", refused, "on_connect"]
    assert ceilings == [1.0]
    assert [[(f["type"], f.get("channels")) for f in frames] for frames in server.received] == [
        [("auth", None), ("subscribe", ["orderbook:OPRA"]), ("ping", None),
         ("subscribe", ["portfolio"])],
        [("auth", None), ("subscribe", ["orderbook:OPRA", "portfolio"]), ("ping", None)],
    ]


def test_a_refused_api_key_is_retried_once_portfolio_is_subscribed_from_another_thread():
    # The same with portfolio subscribed from the bot's own thread, where the docs say to act.
    server = _Server(refusals=1).start()
    log = _Log()
    ws = _client(server.port, log, api_key="key")
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
    refused = (1006, "the server did not accept the API key")
    assert log.events == ["on_connect", "UNAUTHORIZED", refused, "on_connect"]


def test_a_refused_api_key_keeps_a_public_only_connection():
    # Without portfolio nothing is lost: the server keeps the socket open for public channels.
    server = _Server(refusals=1).start()
    log = _Log()
    ws = _client(server.port, log, api_key="key")
    ws.on("auth_result", lambda m: log.add(m.success))
    ws.subscribe_orderbook("OPRA")
    ws.start()
    try:
        log.wait_for(lambda events: "on_connect" in events)
    finally:
        ws.stop()
        server.stop()
    assert log.events == [False, "on_connect"]

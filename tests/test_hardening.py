"""Regression tests for the audit findings of PR #3 (mertcano, PY-01 to PY-13) that still applied to
0.5.0, each named for the defect it pins, and for two handler behaviours PR #3 tests that 0.5.0 already
had. Offline: the mock transport, or the client's own parts.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import itertools
import json
import logging
import threading
import time
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest

import loaf
from loaf import LoafClient, LoafWebSocketClient
from loaf.exceptions import L2SignerError
from loaf.l2.trading import L2Trader, parse_key_state, parse_l2_market
from loaf.resources.orders import _order_id
from loaf.ws.client import _log_send_failure

from conftest import BASE, MARKET, PK0, SIGNER, SK0


def make_client(handler, **kwargs) -> LoafClient:
    http = httpx.Client(transport=httpx.MockTransport(handler), base_url=BASE)
    return LoafClient(api_key="testkey", base_url=BASE, http_client=http, **kwargs)


def recording(reply=None):
    """``(requests, handler)``: the handler records each request and answers ``reply(request)``,
    else an empty 200."""
    seen: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return reply(request) if reply else httpx.Response(200, json={})

    return seen, handler


# --------------------------------------------------------------------------- #
# A ticker is checked before it is put into a URL path (PY-03)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("bad", ["../x", "OPRA/../../auth", "opra", "OPRA\n", "OPRA?x=1", "OPRAX", "", None])
@pytest.mark.parametrize(
    "call",
    [
        lambda c, t: c.market.property(t),
        lambda c, t: c.market.candles(t, "1h"),
        lambda c, t: list(c.market.iter_candles(t, "1h")),
        lambda c, t: c.market.info_header(t),
        lambda c, t: c.market.info_overview(t),
        lambda c, t: c.market.info_documents(t),
        lambda c, t: c.offerings.get(t),
    ],
)
def test_a_ticker_never_reaches_a_path_unchecked(call, bad):
    seen, handler = recording()
    with pytest.raises(loaf.LoafValidationError) as info:
        call(make_client(handler), bad)
    assert info.value.status_code == 0 and seen == []


def test_l2_shapes_match_the_whole_value(trading_client):
    # A trailing newline passed a ^...$ re.match: each shape now matches the whole value.
    with pytest.raises(loaf.LoafL2Error, match=r"80 lowercase hex \(loaf.l2.keys derives it\)") as info:
        L2Trader(None, SK0 + "\n", signer=SIGNER)
    assert not isinstance(info.value, L2SignerError)  # refused by the SDK, before the native signer
    with pytest.raises(loaf.LoafValidationError):
        trading_client().l2.market("OPRA\n")
    with pytest.raises(loaf.LoafValidationError):
        trading_client().l2.mark_price("OPRA\n")
    with pytest.raises(loaf.LoafL2Error, match="proxyAddress"):
        parse_l2_market("OPRA", {**MARKET, "proxyAddress": MARKET["proxyAddress"] + "\n"})
    with pytest.raises(loaf.LoafL2Error, match="journaledKey"):
        parse_key_state({"accountIndex": 1, "epoch": 0, "journaledKey": PK0 + "\n"})
    assert parse_key_state({"accountIndex": 1, "epoch": 0, "journaledKey": PK0}).journaled_key == PK0


# --------------------------------------------------------------------------- #
# An order id is never truncated (PY-05)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "bad",
    [3.9, 2.5, True, False, 0, -5, float("nan"), float("inf"), "abc", "3.9", "-1", "", "٣", None,
     Decimal("3.5"), Decimal("NaN"), Decimal("Infinity")],
)
def test_an_order_id_is_never_truncated(bad):
    with pytest.raises(loaf.LoafValidationError):
        _order_id(bad)


def test_an_order_id_in_its_exact_forms():
    assert [_order_id(v) for v in (42, 7.0, "42", Decimal("9"))] == [42, 7, 42, 9]


@pytest.mark.parametrize(
    "cancel",
    [
        lambda c: c.orders.cancel(3.9),
        lambda c: c.orders.cancel_conditional(True),
        lambda c: c.orders.cancel_row({"id": 2.5, "type": "LIMIT"}),
        lambda c: c.orders.cancel_row({"id": 2.5, "type": "STOP_MARKET", "status": "ARMED"}),
    ],
)
def test_a_cancel_with_an_inexact_id_sends_nothing(cancel):
    seen, handler = recording()
    with pytest.raises(loaf.LoafValidationError):
        cancel(make_client(handler))
    assert seen == []


def test_a_cancel_sends_the_exact_id():
    seen, handler = recording()
    make_client(handler).orders.cancel("42")
    assert seen[0].content == b'{"orderId":42}'


# --------------------------------------------------------------------------- #
# A 2xx labelled JSON that does not parse is a LoafAPIError (PY-08)
# --------------------------------------------------------------------------- #


def test_a_2xx_that_is_not_json_raises_an_api_error():
    headers = {"content-type": "application/json", "x-request-id": "r1"}
    seen, handler = recording(lambda r: httpx.Response(200, content=b"{not json", headers=headers))
    with pytest.raises(loaf.LoafAPIError) as info:
        make_client(handler).market.properties()
    error = info.value
    assert (error.status_code, error.request_id, error.body) == (200, "r1", "{not json")
    assert "GET /trade response did not parse as JSON" in error.message
    assert isinstance(error.__cause__, ValueError)
    assert len(seen) == 1  # a parsed 2xx is final: not retried


# --------------------------------------------------------------------------- #
# A zero or negative rate-limit hint does not collapse the backoff (PY-09)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("headers", [{"Retry-After": "0"}, {"RateLimit-Reset": "0"}, {"Retry-After": "-3"}, {}])
def test_a_zero_rate_limit_hint_still_backs_off(monkeypatch, headers):
    slept: list = []
    monkeypatch.setattr("loaf.client.time", SimpleNamespace(sleep=slept.append))
    monkeypatch.setattr("loaf.client.random", SimpleNamespace(uniform=lambda low, high: high))
    make_client(recording()[1])._sleep_backoff(0, httpx.Response(429, headers=headers))
    assert slept == [0.5]  # the first exponential step, as with no hint at all


@pytest.mark.parametrize("value, expected", [("3", 3.0), ("120", 60.0)])
def test_a_positive_rate_limit_hint_is_honoured(monkeypatch, value, expected):
    slept: list = []
    monkeypatch.setattr("loaf.client.time", SimpleNamespace(sleep=slept.append))
    make_client(recording()[1])._sleep_backoff(0, httpx.Response(429, headers={"Retry-After": value}))
    assert slept == [expected]


# --------------------------------------------------------------------------- #
# WebSocket: reconnect delay, channel set, sends on a closed loop (PY-01, PY-02, PY-11, PY-12)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("delay", [0, -1, float("nan"), float("inf"), True, "2", None])
def test_a_reconnect_delay_must_be_positive(delay):
    with pytest.raises(ValueError, match="reconnect_delay"):
        LoafWebSocketClient(ws_url="ws://127.0.0.1:9/ws", reconnect_delay=delay)


def test_a_positive_reconnect_delay_is_kept():
    assert LoafWebSocketClient(ws_url="ws://127.0.0.1:9/ws", reconnect_delay=0.25).reconnect_delay == 0.25


def test_a_send_on_a_closed_loop_neither_raises_nor_leaks_the_coroutine():
    ws = LoafWebSocketClient(ws_url="ws://127.0.0.1:9/ws")
    loop = asyncio.new_event_loop()
    loop.close()
    sent: list = []

    class Socket:
        coro = None

        def send(self, frame):
            async def deliver():
                sent.append(frame)

            self.coro = deliver()
            return self.coro

    socket = Socket()
    ws._loop, ws._ws = loop, socket
    ws.subscribe("orderbook:OPRA")  # raised "Event loop is closed"
    assert socket.coro.cr_frame is None  # closed, never left un-awaited
    assert sent == [] and "orderbook:OPRA" in ws._channels  # the next connect subscribes it


def test_a_failed_send_is_logged(caplog):
    ws = LoafWebSocketClient(ws_url="ws://127.0.0.1:9/ws")
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()

    class Socket:
        async def send(self, frame):
            raise ConnectionError("socket gone")

    ws._loop, ws._ws = loop, Socket()
    try:
        with caplog.at_level(logging.WARNING, logger="loaf.ws"):
            ws.subscribe("orderbook:OPRA")
            deadline = time.monotonic() + 5
            while "send failed: socket gone" not in caplog.text and time.monotonic() < deadline:
                time.sleep(0.01)
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(5)
        loop.close()
    assert "Loaf WebSocket send failed: socket gone" in caplog.text


def test_a_cancelled_send_is_not_logged(caplog):
    future: concurrent.futures.Future = concurrent.futures.Future()
    future.cancel()  # a send the loop's shutdown cancelled
    with caplog.at_level(logging.DEBUG, logger="loaf.ws"):
        _log_send_failure(future)  # future.exception() would raise CancelledError
    assert caplog.records == []


@pytest.mark.parametrize(
    "late, after",
    [
        (lambda ws: ws.subscribe("trades:LATE"), {"trades:OPRA", "trades:LATE"}),
        (lambda ws: ws.unsubscribe("trades:OPRA"), set()),
    ],
)
def test_a_connect_reads_the_channels_while_no_call_can_change_them(late, after):
    ws = LoafWebSocketClient(ws_url="ws://127.0.0.1:9/ws")
    ws.subscribe("trades:OPRA")
    landed = threading.Event()
    changed: list = []

    class Channels(set):
        def __iter__(self):
            before = set(set.__iter__(self))
            self.late = threading.Thread(target=lambda: (late(ws), landed.set()))
            self.late.start()
            landed.wait(0.5)  # a call that takes no lock lands here, in the middle of the read
            changed.append(set(set.__iter__(self)) != before)
            return iter(before)

    ws._channels = Channels(ws._channels)
    frames: list = []

    class Socket:
        async def send(self, frame):
            frames.append(json.loads(frame))

    asyncio.run(ws._on_open(Socket()))
    ws._channels.late.join(5)
    assert changed == [False]  # the late call waited for the read
    assert [(frame["type"], frame["channels"]) for frame in frames] == [("subscribe", ["trades:OPRA"])]
    assert ws._channels == after  # then landed; its own frame carries it


def test_a_raising_handler_does_not_stop_the_others(caplog):
    ws = LoafWebSocketClient(ws_url="ws://127.0.0.1:9/ws")
    seen: list = []
    ws.on("test_event", lambda message: 1 / 0)
    ws.on("test_event", seen.append)
    with caplog.at_level(logging.ERROR, logger="loaf.ws"):
        ws._dispatch(json.dumps({"type": "test_event"}))
    assert len(seen) == 1
    assert "handler for 'test_event' raised" in caplog.text


def test_a_handler_added_by_a_handler_runs_from_the_next_message():
    ws = LoafWebSocketClient(ws_url="ws://127.0.0.1:9/ws")
    calls: list = []

    def first(message):
        calls.append("first")
        ws.on("test_event", lambda message: calls.append("added"))

    ws.on("test_event", first)
    ws._dispatch(json.dumps({"type": "test_event"}))
    assert calls == ["first"]  # each message is handed to the handlers registered when it arrived
    ws._dispatch(json.dumps({"type": "test_event"}))
    assert calls == ["first", "first", "added"]


def test_a_connect_sends_the_key_then_every_channel():
    ws = LoafWebSocketClient(ws_url="ws://127.0.0.1:9/ws", api_key="testkey")
    ws.subscribe("trades:OPRA", "orderbook:OPRA")
    frames: list = []

    class Socket:
        async def send(self, frame):
            frames.append(json.loads(frame))

    asyncio.run(ws._on_open(Socket()))
    assert [frame["type"] for frame in frames] == ["auth", "subscribe"]
    assert frames[1]["channels"] == ["orderbook:OPRA", "trades:OPRA"]


# --------------------------------------------------------------------------- #
# Paging stops when the server hands back a cursor it already gave (PY-13)
# --------------------------------------------------------------------------- #


def test_a_repeated_cursor_stops_paging():
    seen, handler = recording(
        lambda r: httpx.Response(200, json={"orders": [{"id": 1}], "nextCursor": "same"})
    )
    pages = make_client(handler).history.iter_orders(page_size=1)
    assert [next(pages).id, next(pages).id] == [1, 1]
    with pytest.raises(loaf.LoafError, match="already given"):
        next(pages)
    assert [r.url.params.get("cursor") for r in seen] == [None, "same"]


def test_a_candle_page_that_does_not_move_back_stops_paging():
    pages = {
        None: {"resolution": "1m", "candles": [{"time": 100}], "oldestTs": 100, "hasMore": True},
        "100": {"resolution": "1m", "candles": [{"time": 100}], "oldestTs": 100, "hasMore": True},
    }
    seen, handler = recording(lambda r: httpx.Response(200, json=pages[r.url.params.get("to")]))
    candles = make_client(handler).market.iter_candles("OPRA", "1m")
    assert [c.time for c in itertools.islice(candles, 2)] == [100, 100]
    with pytest.raises(loaf.LoafError, match="stopped paging"):
        next(candles)
    assert len(seen) == 2

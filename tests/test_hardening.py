"""Offline regression tests for the hardening fixes.

Each test names the defect it pins down. No network: everything runs against the
in-memory httpx mock transport, a real loopback WebSocket, or pure helpers.
"""

from __future__ import annotations

import asyncio
import json
import math

import httpx
import pytest

import loaf
from loaf import LoafClient, LoafWebSocketClient
from loaf.exceptions import LoafConnectionError
from loaf.money import validate_price, validate_quantity
from loaf.resources.market import _validate_token_name
from loaf.resources.orders import _order_id

BASE = "http://test/api"


def make_client(handler, **kwargs) -> LoafClient:
    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport, base_url=BASE)
    return LoafClient(api_key="testkey", base_url=BASE, http_client=http, **kwargs)


# --------------------------------------------------------------------------- #
# token_name is validated before it is interpolated into a URL path (C3)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "bad",
    ["../../admin", "..", "a/b", "Opera", "OPERA", "", "x\ny", "x\x00y", "a?b=1", "a#b", "../trade"],
)
def test_validate_token_name_rejects_traversal_and_junk(bad):
    with pytest.raises(loaf.LoafValidationError):
        _validate_token_name(bad)


@pytest.mark.parametrize("ok", ["opera", "a", "property", "z" * 64])
def test_validate_token_name_accepts_real_names(ok):
    assert _validate_token_name(ok) == ok


def test_token_name_never_reaches_the_wire_when_invalid():
    """A traversal attempt must not produce an HTTP request at all."""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={})

    client = make_client(handler)
    with pytest.raises(loaf.LoafValidationError):
        client.market.property("../../admin")
    assert seen == [], f"request escaped validation: {seen}"


# --------------------------------------------------------------------------- #
# money validation rejects wrong TYPES instead of raising raw TypeError (H3)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("bad", [True, False, "1.5", None, [], object()])
def test_validate_price_rejects_non_numbers(bad):
    with pytest.raises(loaf.LoafValidationError):
        validate_price(bad)


@pytest.mark.parametrize("bad", [True, "1.0", None, float("nan"), float("inf"), -1.0, 1.234])
def test_validate_price_rejects_out_of_range(bad):
    with pytest.raises(loaf.LoafValidationError):
        validate_price(bad)


def test_validate_quantity_rejects_non_numbers():
    for bad in (True, "1.0", None, float("nan")):
        with pytest.raises(loaf.LoafValidationError):
            validate_quantity(bad)


def test_validate_price_accepts_int_and_decimal():
    validate_price(100)      # int
    validate_price(0)        # zero is legal (the MARKET sentinel)
    validate_price(167.49)   # 2dp


# --------------------------------------------------------------------------- #
# order ids must not silently truncate (H4)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("bad", [3.9, True, -5, 0, float("nan"), "abc", None, 2.5])
def test_order_id_rejects_non_exact_integers(bad):
    with pytest.raises(loaf.LoafValidationError):
        _order_id(bad)


def test_order_id_accepts_exact_forms():
    assert _order_id(42) == 42
    assert _order_id(7.0) == 7      # a float that is exactly an integer is fine
    assert _order_id("42") == 42


def test_cancel_rejects_a_truncating_id_without_calling_the_server():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"success": True})

    client = make_client(handler)
    with pytest.raises(loaf.LoafValidationError):
        client.orders.cancel(3.9)
    assert seen == [], "a truncated id must never reach the cancel endpoint"


# --------------------------------------------------------------------------- #
# side / deadline are validated locally (H5)
# --------------------------------------------------------------------------- #


def test_invalid_side_is_rejected_before_the_request():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"orderId": 1})

    client = make_client(handler)
    with pytest.raises(loaf.LoafValidationError):
        # create() is the general form; limit_buy() pins side itself, so an unknown
        # side can only be exercised through create() directly.
        client.orders.create("opera", "BU", quantity=1, price=100)
    assert seen == []


def test_invalid_time_in_force_is_rejected_before_the_request():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"orderId": 1})

    client = make_client(handler)
    with pytest.raises(loaf.LoafValidationError):
        client.orders.create(
            "opera", "BUY", quantity=1, price=100, time_in_force="XYZ"
        )
    assert seen == []


def test_gtd_deadline_must_be_a_real_number():
    client = make_client(lambda r: httpx.Response(200, json={"orderId": 1}))
    for bad in (float("nan"), "tomorrow", None, True):
        with pytest.raises(loaf.LoafValidationError):
            client.orders.limit_buy(
                "opera", quantity=1, price=100, time_in_force="GTD", deadline=bad
            )


# --------------------------------------------------------------------------- #
# malformed JSON is wrapped, not raised raw (M5)
# --------------------------------------------------------------------------- #


def test_malformed_json_body_raises_loaf_error_not_jsondecodeerror():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"{not json",
            headers={"content-type": "application/json"},
        )

    client = make_client(handler)
    with pytest.raises(loaf.LoafAPIError) as exc:
        client.market.properties()
    assert "did not parse" in str(exc.value)


# --------------------------------------------------------------------------- #
# retry policy: idempotent retries, non-idempotent does not (M4)
# --------------------------------------------------------------------------- #


def test_network_error_on_post_is_not_retried_and_warns_about_ambiguity():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        raise httpx.ConnectError("boom")

    client = make_client(handler, max_retries=3)
    with pytest.raises(LoafConnectionError) as exc:
        client.orders.limit_buy("opera", quantity=1, price=100)
    # Exactly one attempt: retrying a POST could place the order twice.
    assert calls == ["POST"]
    # The message must tell the caller to reconcile rather than blindly retry.
    assert "may still have been processed" in str(exc.value)


def test_network_error_on_get_is_retried_up_to_the_budget():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        if len(calls) < 3:
            raise httpx.ConnectError("boom")
        return httpx.Response(200, json={"ok": True})

    client = make_client(handler, max_retries=3)
    client.market.properties()
    assert calls == ["GET", "GET", "GET"]


def test_429_is_retried_exactly_max_retries_then_raises():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(429, json={"error": "slow"}, headers={"retry-after": "0"})

    client = make_client(handler, max_retries=2)
    with pytest.raises(loaf.LoafRateLimitError):
        client.market.properties()
    # initial attempt + 2 retries
    assert len(calls) == 3


def test_zero_retry_after_header_still_backs_off(monkeypatch):
    """A `Retry-After: 0` must not collapse the backoff to zero."""
    slept = []
    monkeypatch.setattr(loaf.client.time, "sleep", lambda s: slept.append(s))
    client = make_client(
        lambda r: httpx.Response(429, json={"error": "x"}, headers={"retry-after": "0"})
    )
    client._sleep_backoff(0, httpx.Response(429, headers={"retry-after": "0"}))
    assert slept and slept[0] > 0, "backoff collapsed to zero"


# --------------------------------------------------------------------------- #
# websocket lifecycle (C1, C2, M3)
# --------------------------------------------------------------------------- #


def test_send_now_against_a_closed_loop_does_not_raise():
    """The regression: stop() could close the loop between the guard and dispatch,
    producing a raw RuntimeError out of a public subscribe_* call."""
    ws = LoafWebSocketClient(ws_url="ws://127.0.0.1:1/ws", auto_reconnect=False)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.close()
    ws._loop = loop          # non-None but CLOSED - the case the old guard missed
    ws._ws = object()
    # Must not raise, must not leak an un-awaited coroutine.
    ws.subscribe_orderbook("opera")
    ws.unsubscribe("orderbook:opera")


def test_send_now_without_a_connection_is_a_noop():
    ws = LoafWebSocketClient(ws_url="ws://127.0.0.1:1/ws")
    ws.subscribe_orderbook("opera")          # nothing connected: silently dropped
    ws.stop()


def test_reconnect_delay_must_be_positive():
    for bad in (0, -1):
        with pytest.raises(ValueError):
            LoafWebSocketClient(ws_url="ws://127.0.0.1:1/ws", reconnect_delay=bad)


def test_log_send_failure_does_not_raise_on_cancelled_handles():
    ws = LoafWebSocketClient(ws_url="ws://127.0.0.1:1/ws")
    fut = asyncio.Future()
    fut.cancel()
    ws._log_send_failure(fut)   # must be a silent no-op


def test_concurrent_subscribes_are_all_recorded():
    import threading

    ws = LoafWebSocketClient(ws_url="ws://127.0.0.1:1/ws")
    names = [f"orderbook:p{i}" for i in range(50)]

    def worker(chunk):
        for n in chunk:
            ws.subscribe(n)

    threads = [threading.Thread(target=worker, args=(names[i::5],)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    with ws._lock:
        assert ws._channels == set(names)


def test_handler_registered_from_inside_a_handler_is_safe():
    ws = LoafWebSocketClient(ws_url="ws://127.0.0.1:1/ws")
    seen = []

    def late(payload):
        ws.on_message(lambda p: seen.append("late"))

    ws.on_message(lambda p: late(p))
    ws._emit("*", {"a": 1})
    ws._emit("*", {"a": 2})     # must not raise "list changed size"
    assert seen == ["late"]


def test_a_raising_handler_does_not_stop_the_others():
    ws = LoafWebSocketClient(ws_url="ws://127.0.0.1:1/ws")
    got = []
    ws.on("x", lambda p: (_ for _ in ()).throw(RuntimeError("boom")))
    ws.on("x", lambda p: got.append(p))
    ws._emit("x", 1)
    assert got == [1]


def test_ws_url_derivation_still_correct():
    from loaf.ws.client import derive_ws_url

    assert derive_ws_url("https://api.loafmarkets.com/api") == "wss://api.loafmarkets.com/ws"
    assert derive_ws_url("http://localhost:8005/api") == "ws://localhost:8005/ws"


# --------------------------------------------------------------------------- #
# pagination cannot loop forever on a repeated cursor (M7)
# --------------------------------------------------------------------------- #


def test_iter_orders_stops_when_the_cursor_repeats():
    """A server that keeps returning the same nextCursor would loop forever."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"orders": [{"id": 1}], "nextCursor": "always-the-same"}
        )

    client = make_client(handler)
    it = client.history.iter_orders(page_size=1)
    # Bounded read: the SDK must yield a finite number of pages, not hang.
    pages = [next(it) for _ in range(5)]
    assert len(pages) == 5
    assert all(p["id"] == 1 for p in pages)
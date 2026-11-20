"""How the client reads an answer: what a signed op's answer settles, what a read's body must be,
the waits it takes on rate-limit hints, what an answer may cost, and the login token kept out of
every error it raises. Offline: an in-memory transport, and a socket on 127.0.0.1."""

from __future__ import annotations

import gzip
import json
import math
import socket
import threading
import time
import zlib
from typing import Any, Callable, Iterator

import httpx
import pytest

import loaf
from loaf import LoafClient
from loaf._l2 import LoginTokens
from loaf.exceptions import error_from_response

from conftest import BASE, TRADING_KEY, bearer_requests, chained, secret_locals

# JSON nested past any recursion limit: refused by json on Python 3.9, read on 3.14 (whose repr of
# it overflows instead), and as an error object's message.
DEEP = b"[" * 100_000 + b"]" * 100_000
DEEP_ERROR = b'{"error": ' + DEEP + b', "code": "TRADING_PAUSED"}'


def make_client(handler: Callable[[httpx.Request], httpx.Response], **kwargs: Any) -> LoafClient:
    http = httpx.Client(transport=httpx.MockTransport(handler), base_url=BASE, timeout=30.0)
    return LoafClient(TRADING_KEY, base_url=BASE, http_client=http, **kwargs)


def tokens_sent(requests: list[httpx.Request]) -> list[str]:
    return [
        r.headers["authorization"][len("Bearer ") :]
        for r in requests
        if "authorization" in r.headers
    ]


def op_posts(exchange) -> list[httpx.Request]:
    signed = ("/api/orders", "/api/l2/allocations", "/api/l2/withdrawals")
    return [r for r in exchange.requests if r.method == "POST" and r.url.path in signed]


# The three signed ops, each placed once by a client of the fake exchange.
SIGNED = {
    "order": (
        "/orders",
        loaf.OrderOutcomeUnknownError,
        lambda c: c.orders.limit_buy("OPRA", 10, 160),
    ),
    "perps": ("/l2/allocations", loaf.PerpsOutcomeUnknownError, lambda c: c.perps.add_margin(5)),
    "withdrawal": (
        "/l2/withdrawals",
        loaf.WithdrawalOutcomeUnknownError,
        lambda c: c.funds.withdraw(5),
    ),
}


# --------------------------------------------------------------------------- #
# A signed op's answer
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("op", SIGNED)
@pytest.mark.parametrize("code", [["TRADING_PAUSED"], {"code": "NOT_PROCESSED"}, 503, True])
def test_a_5xx_whose_code_is_no_string_is_an_unknown_outcome(exchange, trading_client, op, code):
    path, unknown, place = SIGNED[op]
    exchange.queue(path, httpx.Response(503, json={"error": "x", "code": code}))
    with pytest.raises(unknown) as info:
        place(trading_client(max_retries=0))
    assert info.value.signed_body == json.loads(op_posts(exchange)[-1].content)
    # A code is a string: anything else names no reason, nor does its text off a code.
    last = info.value.last_error
    assert isinstance(last, loaf.LoafServiceUnavailableError) and last.code is None


def test_a_code_that_is_no_string_is_no_code():
    for code in (["TRADING_HALTED"], {"a": 1}, 7, None):
        error = error_from_response(403, {"error": "x", "code": code})
        assert type(error) is loaf.LoafForbiddenError and error.code is None
        assert "code=" not in str(error)


def json_reply(status: int, content: bytes) -> httpx.Response:
    return httpx.Response(status, content=content, headers={"content-type": "application/json"})


@pytest.mark.parametrize("op", SIGNED)
@pytest.mark.parametrize("status", [200, 202, 500, 503])
@pytest.mark.parametrize("content", [DEEP, DEEP_ERROR], ids=["deep", "deep error"])
def test_a_body_too_deeply_nested_to_read_is_an_unknown_outcome(
    exchange, trading_client, op, status, content
):
    path, unknown, place = SIGNED[op]
    exchange.queue(path, json_reply(status, content))
    with pytest.raises(unknown) as info:
        place(trading_client(max_retries=0))
    assert info.value.attempts == 1 and info.value.signed_body["l2SigS"]


@pytest.mark.parametrize("content", [DEEP, DEEP_ERROR], ids=["deep", "deep error"])
def test_a_4xx_too_deeply_nested_to_read_is_still_a_refusal(exchange, trading_client, content):
    exchange.queue("/orders", json_reply(400, content))
    with pytest.raises(loaf.LoafValidationError) as info:
        trading_client().orders.limit_buy("OPRA", 10, 160)
    assert info.value.status_code == 400 and len(op_posts(exchange)) == 1


@pytest.mark.parametrize("status", [400, 404, 500, 503])
def test_a_read_refused_with_an_error_too_deeply_nested_to_show_keeps_its_status(status):
    client = make_client(lambda request: json_reply(status, DEEP_ERROR), max_retries=0)
    with pytest.raises(loaf.LoafAPIError) as info:
        client.portfolio.summary()
    assert info.value.status_code == status and info.value.body == DEEP_ERROR.decode()


@pytest.mark.parametrize(
    "status, reader", [(200, "_settles"), (500, "_definitely_not_done"), (400, "_never_journaled")]
)
def test_whatever_fails_reading_an_answer_is_an_unknown_outcome_unless_its_status_refuses(
    exchange, trading_client, monkeypatch, status, reader
):
    def broken(*args: Any) -> bool:
        raise RuntimeError("cannot read it")

    monkeypatch.setattr(f"loaf.client.{reader}", broken)
    exchange.queue("/orders", httpx.Response(status, json={"error": "x", "orderId": 1}))
    client = trading_client(max_retries=3)
    if status == 400:
        with pytest.raises(loaf.LoafValidationError) as refused:
            client.orders.limit_buy("OPRA", 10, 160)
        assert isinstance(refused.value.__cause__, RuntimeError)
    else:
        with pytest.raises(loaf.OrderOutcomeUnknownError) as info:
            client.orders.limit_buy("OPRA", 10, 160)
        assert info.value.attempts == 1  # never re-sent
        assert isinstance(info.value.last_error.__cause__, RuntimeError)
    assert len(op_posts(exchange)) == 1


@pytest.mark.parametrize(
    "answer",
    [
        {"success": False, "orderId": None},
        {"success": False, "orderId": 7},
        {"success": True, "orderId": None},
        {"orderId": True},
        {"orderId": "7"},
        {"orderId": 7.0},
        {"orderId": -1},
        {"success": True},
        [{"orderId": 7}],
        "placed",
    ],
    ids=repr,
)
def test_only_a_whole_order_id_settles_an_order(exchange, trading_client, answer):
    exchange.queue("/orders", httpx.Response(200, json=answer))
    with pytest.raises(loaf.OrderOutcomeUnknownError) as info:
        trading_client().orders.limit_buy("OPRA", 10, 160)
    assert info.value.attempts == 1
    assert isinstance(info.value.last_error, loaf.LoafServerError)
    assert info.value.last_error.body == answer


@pytest.mark.parametrize("op", ["perps", "withdrawal"])
@pytest.mark.parametrize(
    "answer",
    [
        {"journalSeq": None},
        {"journalSeq": True},
        {"journalSeq": "1001"},
        {"journalSeq": 1.5},
        {"success": False, "journalSeq": 1001},
        {"duplicate": True},
    ],
    ids=repr,
)
def test_only_a_whole_journal_seq_settles_an_op(exchange, trading_client, op, answer):
    path, unknown, place = SIGNED[op]
    exchange.queue(path, httpx.Response(202, json=answer))
    with pytest.raises(unknown):
        place(trading_client())


def test_a_settled_answer_is_returned(exchange, trading_client):
    client = trading_client()
    assert client.orders.limit_buy("OPRA", 10, 160).orderId == 1
    assert client.perps.add_margin(5).journalSeq == 1001
    exchange.queue("/orders", httpx.Response(200, json={"orderId": 0, "status": "OPEN"}))
    assert client.orders.limit_buy("OPRA", 10, 160).orderId == 0


@pytest.mark.parametrize("op", ["order", "perps", "withdrawal"])
def test_a_503_page_saying_nothing_was_done_is_no_answer(exchange, trading_client, op):
    # A gateway's page that happens to say so is not the exchange's refusal: the op may have
    # landed behind it.
    path, unknown, place = SIGNED[op]
    page = (
        "<html><body>Sorry: your order was not placed, nothing was done or journaled</body></html>"
    )
    exchange.queue(path, httpx.Response(503, html=page))
    with pytest.raises(unknown) as info:
        place(trading_client(max_retries=0))
    assert info.value.last_error.body == page


def test_the_exchanges_own_503_saying_nothing_was_done_is_still_a_refusal(exchange, trading_client):
    exchange.queue("/orders", httpx.Response(503, json={"error": "Your order was not placed"}))
    with pytest.raises(loaf.LoafServiceUnavailableError) as info:
        trading_client(max_retries=0).orders.limit_buy("OPRA", 10, 160)
    assert type(info.value) is loaf.LoafServiceUnavailableError


# --------------------------------------------------------------------------- #
# A read's body
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "content, ctype",
    [
        (b"<html>Just a moment...</html>", "text/html"),
        (b"Welcome to the hotel Wi-Fi", "text/plain"),
        (b'{"markets": [', "application/json"),
        (DEEP, "application/json"),
        (DEEP, "text/plain"),
        (b"\xff\xfe\x00", None),
    ],
)
def test_a_2xx_read_that_is_not_json_is_a_server_error(content, ctype):
    headers = (
        {"x-request-id": "r1"} if ctype is None else {"content-type": ctype, "x-request-id": "r1"}
    )
    client = make_client(lambda request: httpx.Response(200, content=content, headers=headers))
    for call in (lambda: client.market.list(), lambda: client.orders.cancel(1)):
        with pytest.raises(loaf.LoafServerError, match="answered a body that is not JSON") as info:
            call()
        assert (info.value.status_code, info.value.request_id) == (200, "r1")
        assert isinstance(info.value.body, str) and len(info.value.body) == len(
            content.decode("utf-8", errors="replace")
        )


def test_json_under_another_content_type_is_read_as_json():
    client = make_client(
        lambda request: httpx.Response(
            200, content=b'{"markets": []}', headers={"content-type": "text/plain"}
        )
    )
    assert client.market.list().markets == []


@pytest.mark.parametrize(
    "reply",
    [
        httpx.Response(204),
        httpx.Response(200),
        httpx.Response(200, headers={"content-type": "text/html"}),
    ],
)
def test_no_body_is_none(reply):
    assert make_client(lambda request: reply).orders.cancel(1) is None


ITERATORS = {
    "orders": lambda c: c.history.iter_orders(),
    "trades": lambda c: c.history.iter_trades(),
    "funding": lambda c: c.history.iter_funding(),
}


@pytest.mark.parametrize("key", ITERATORS)
@pytest.mark.parametrize(
    "page, error",
    [
        ([], "a page that is not an object"),
        ("page", "a body that is not JSON"),
        ({}, "is not a list"),
        ({"ROWS": {"id": 1}}, "is not a list"),
        ({"ROWS": [{"id": 1}], "nextCursor": 7}, "a nextCursor that is not one"),
        ({"ROWS": [], "nextCursor": "next"}, "an empty page that points to another"),
        ({"ROWS": [], "nextCursor": None, "hasMore": True}, "an empty page with hasMore"),
    ],
)
def test_a_history_page_that_cannot_be_paged_on_raises(key, page, error):
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        body = json.loads(json.dumps(page).replace("ROWS", key))
        if body == "page":
            return httpx.Response(200, html="<html>page</html>")
        return httpx.Response(200, json=body)

    with pytest.raises(loaf.LoafServerError, match=error) as info:
        list(ITERATORS[key](make_client(handler)))
    assert info.value.status_code == 200 and len(sent) == 1


@pytest.mark.parametrize(
    "page, error",
    [
        ([], "a page that is not an object"),
        ({}, "is not a list"),
        ({"cancelledOrders": {"orderId": 1}}, "is not a list"),
        ({"cancelledOrders": [], "nextCursor": 7}, "a nextCursor that is not one"),
    ],
)
def test_a_cancelled_orders_page_that_cannot_be_paged_on_raises(page, error):
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json=page)

    with pytest.raises(loaf.LoafServerError, match=error) as info:
        list(make_client(handler).history.iter_cancelled_orders())
    assert info.value.status_code == 200 and len(sent) == 1


def test_empty_cancelled_orders_pages_with_a_repeated_cursor_stop_paging():
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.url.params.get("cursor"))
        assert len(sent) < 10, "paged forever"
        return httpx.Response(200, json={"cancelledOrders": [], "nextCursor": "same"})

    with pytest.raises(loaf.LoafError, match="already given"):
        list(make_client(handler).history.iter_cancelled_orders())
    assert sent == [None, "same"]


@pytest.mark.parametrize("last", [None, ""])
def test_an_empty_first_cancelled_orders_page_with_no_cursor_ends_the_walk(last):
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"cancelledOrders": [], "nextCursor": last})

    assert list(make_client(handler).history.iter_cancelled_orders()) == []
    assert len(sent) == 1


@pytest.mark.parametrize("key", ITERATORS)
@pytest.mark.parametrize("last", [None, ""])
def test_a_null_or_empty_next_cursor_ends_the_walk(key, last):
    pages = [{key: [{"id": 1}], "nextCursor": "next"}, {key: [{"id": 2}], "nextCursor": last}]
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json=pages[len(sent) - 1])

    assert [row["id"] for row in ITERATORS[key](make_client(handler))] == [1, 2]
    assert len(sent) == 2


@pytest.mark.parametrize("key", ITERATORS)
def test_empty_pages_with_fresh_cursors_never_page_forever(key):
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        assert len(sent) < 10, "paged forever"
        return httpx.Response(200, json={key: [], "nextCursor": f"cursor-{len(sent)}"})

    with pytest.raises(loaf.LoafError, match="stopped paging"):
        list(ITERATORS[key](make_client(handler)))
    assert len(sent) == 1


@pytest.mark.parametrize(
    "page, error",
    [
        ([], "a page that is not an object"),
        ({"hasMore": False}, "candles is not a list"),
        ({"candles": {"time": 1}, "hasMore": False}, "candles is not a list"),
        (
            {"candles": [{"time": 1}], "hasMore": "yes", "oldestTs": 1},
            "a hasMore that is not a bool",
        ),
        ({"candles": [{"time": 1}], "hasMore": True, "oldestTs": None}, "an oldestTs that is not"),
        ({"candles": [{"time": 1}], "hasMore": True, "oldestTs": "1"}, "an oldestTs that is not"),
        ({"candles": [{"time": 1}], "hasMore": True, "oldestTs": True}, "an oldestTs that is not"),
        ({"candles": [{"time": 1}], "hasMore": True, "oldestTs": 1.5}, "an oldestTs that is not"),
        ({"candles": [], "hasMore": True, "oldestTs": 100}, "an empty page with hasMore"),
    ],
)
def test_a_candle_page_that_cannot_be_paged_on_raises(page, error):
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json=page)

    with pytest.raises(loaf.LoafServerError, match=error) as info:
        list(make_client(handler).market.iter_candles("OPRA", "1m"))
    assert info.value.status_code == 200 and len(sent) == 1


def test_the_last_candle_page_may_have_no_oldest_ts():
    page = {"resolution": "1m", "candles": [], "oldestTs": None, "hasMore": False}
    assert (
        list(
            make_client(lambda r: httpx.Response(200, json=page)).market.iter_candles("OPRA", "1m")
        )
        == []
    )


# --------------------------------------------------------------------------- #
# Rate-limit hints
# --------------------------------------------------------------------------- #


def slept_by(monkeypatch) -> list[float]:
    slept: list[float] = []
    monkeypatch.setattr(
        "loaf.client.time", type("Clock", (), {"sleep": staticmethod(slept.append)})
    )
    monkeypatch.setattr(
        "loaf.client.random", type("Jitter", (), {"uniform": staticmethod(lambda a, b: b)})
    )
    return slept


def answers(*replies: httpx.Response) -> tuple[Callable[[httpx.Request], httpx.Response], list]:
    sent: list[httpx.Request] = []
    pending = list(replies)

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return pending.pop(0) if pending else httpx.Response(200, json={"markets": []})

    return handler, sent


@pytest.mark.parametrize(
    "status, headers, wait",
    [
        (429, {"Retry-After": "3"}, 3.0),
        (429, {"Retry-After": "10"}, 10.0),
        (429, {"RateLimit-Reset": "2.5"}, 2.5),
        (503, {"Retry-After": "10"}, 10.0),
        # No usable hint: the jittered backoff's first step.
        (429, {"Retry-After": "0"}, 0.5),
        (429, {"Retry-After": "-3"}, 0.5),
        (429, {"Retry-After": "nan"}, 0.5),
        (429, {"Retry-After": "inf"}, 0.5),
        (429, {"Retry-After": "1e400"}, 0.5),
        (429, {}, 0.5),
        (503, {"RateLimit-Reset": "57"}, 0.5),  # the window's time left says nothing of a 503
    ],
)
def test_a_read_waits_out_a_short_hint(monkeypatch, status, headers, wait):
    slept = slept_by(monkeypatch)
    handler, sent = answers(httpx.Response(status, json={"error": "slow"}, headers=headers))
    assert make_client(handler, max_retries=3).market.list().markets == []
    assert slept == [wait] and len(sent) == 2


@pytest.mark.parametrize(
    "status, headers, error, retry_after",
    [
        (429, {"Retry-After": "10.5"}, loaf.LoafRateLimitError, 10.5),
        (429, {"Retry-After": "120"}, loaf.LoafRateLimitError, 120.0),
        (429, {"RateLimit-Reset": "900"}, loaf.LoafRateLimitError, 900.0),
        (429, {"Retry-After": "1e9"}, loaf.LoafRateLimitError, 86_400.0),
        (429, {"Retry-After": "inf", "RateLimit-Reset": "900"}, loaf.LoafRateLimitError, 900.0),
        (503, {"Retry-After": "30"}, loaf.LoafServiceUnavailableError, 30.0),
        (503, {"Retry-After": "1e9"}, loaf.LoafServiceUnavailableError, 86_400.0),
        (503, {"Retry-After": "30"}, loaf.TradingPausedError, 30.0),
    ],
)
def test_a_read_raises_a_long_hint_at_once(monkeypatch, status, headers, error, retry_after):
    slept = slept_by(monkeypatch)
    body = (
        {"error": "x", "code": "TRADING_PAUSED"}
        if error is loaf.TradingPausedError
        else {"error": "x"}
    )
    handler, sent = answers(httpx.Response(status, json=body, headers=headers))
    with pytest.raises(error) as info:
        make_client(handler, max_retries=3).market.list()
    assert slept == [] and len(sent) == 1
    assert info.value.retry_after == retry_after


def test_busy_candle_history_with_a_long_hint_carries_it(monkeypatch):
    slept = slept_by(monkeypatch)
    busy = {"error": "Candle history is busy. Please try again shortly."}
    handler, sent = answers(httpx.Response(503, json=busy, headers={"Retry-After": "15"}))
    with pytest.raises(loaf.LoafServiceUnavailableError) as info:
        make_client(handler, max_retries=3).market.candles("OPRA", "1h")
    assert slept == [] and len(sent) == 1
    assert info.value.retry_after == 15.0
    # A 503 without a usable hint carries none, after its retries.
    handler, sent = answers(*(httpx.Response(503, json=busy) for _ in range(2)))
    with pytest.raises(loaf.LoafServiceUnavailableError) as info:
        list(make_client(handler, max_retries=1).market.iter_candles("OPRA", "1h"))
    assert len(sent) == 2 and info.value.retry_after is None


@pytest.mark.parametrize("value", ["inf", "-inf", "nan", "1e400"])
def test_a_retry_after_that_is_no_finite_number_is_none(value):
    # time.sleep(error.retry_after) must never overflow.
    handler, _ = answers(
        httpx.Response(429, json={"error": "slow"}, headers={"Retry-After": value}),
        httpx.Response(
            503, json={"error": "x", "code": "TRADING_PAUSED"}, headers={"Retry-After": value}
        ),
        httpx.Response(503, json={"error": "x"}, headers={"Retry-After": value}),
    )
    client = make_client(handler)
    with pytest.raises(loaf.LoafRateLimitError) as limited:
        client.orders.cancel(1)
    with pytest.raises(loaf.TradingPausedError) as paused:
        client.orders.cancel(1)
    with pytest.raises(loaf.LoafServiceUnavailableError) as unavailable:
        client.orders.cancel(1)
    assert limited.value.retry_after is None and paused.value.retry_after is None
    assert unavailable.value.retry_after is None


def test_retry_after_is_never_negative():
    handler, _ = answers(httpx.Response(429, json={"error": "slow"}, headers={"Retry-After": "-5"}))
    with pytest.raises(loaf.LoafRateLimitError) as info:
        make_client(handler).orders.cancel(1)
    assert info.value.retry_after == 0.0


def test_only_finite_rate_limit_figures_are_recorded():
    headers = {"RateLimit-Limit": "inf", "RateLimit-Remaining": "-1", "RateLimit-Reset": "nan"}
    client = make_client(lambda r: httpx.Response(200, json={}, headers=headers))
    client.market.list()
    assert client.last_rate_limit == {}
    headers = {"RateLimit-Limit": "100", "RateLimit-Remaining": "0", "RateLimit-Reset": "1e400"}
    client = make_client(lambda r: httpx.Response(200, json={}, headers=headers))
    client.market.list()
    assert client.last_rate_limit == {"limit": 100.0, "remaining": 0.0}
    assert all(math.isfinite(v) and v >= 0 for v in client.last_rate_limit.values())


# --------------------------------------------------------------------------- #
# What an answer may cost
# --------------------------------------------------------------------------- #


class Trickle(httpx.SyncByteStream):
    """A body that arrives in ``chunks``, the client's clock moving ``step`` seconds a chunk."""

    def __init__(self, chunks: list[bytes], clock: list[float], step: float) -> None:
        self.chunks, self.clock, self.step = chunks, clock, step

    def __iter__(self) -> Iterator[bytes]:
        for chunk in self.chunks:
            self.clock[0] += self.step
            yield chunk


@pytest.fixture
def clock(monkeypatch) -> list[float]:
    now = [1000.0]
    monkeypatch.setattr("loaf.client._monotonic", lambda: now[0])
    return now


def test_a_body_past_the_size_cap_is_not_read(monkeypatch):
    monkeypatch.setattr("loaf.client.MAX_RESPONSE_BYTES", 1000)
    handler, sent = answers(
        *(httpx.Response(200, json={"markets": ["x" * 2000]}) for _ in range(4))
    )
    with pytest.raises(loaf.LoafConnectionError, match="passed 1000 bytes"):
        make_client(handler, max_retries=3).market.list()
    assert len(sent) == 1  # never retried: the same answer would cost as much
    # At the cap, it is read.
    monkeypatch.setattr("loaf.client.MAX_RESPONSE_BYTES", len(b'{"markets": []}'))
    assert (
        make_client(lambda r: httpx.Response(200, content=b'{"markets": []}')).market.list().markets
        == []
    )


_PACKED: list[bytes] = []


def bomb() -> httpx.Response:
    """A 200 of 40 MiB of JSON (spaces, then an empty object), gzipped to about 40 KiB."""
    if not _PACKED:
        _PACKED.append(gzip.compress(b" " * (40 * 1024 * 1024) + b"{}", 1))
    (packed,) = _PACKED
    return httpx.Response(
        200, stream=httpx.ByteStream(packed), headers={"content-encoding": "gzip"}
    )


def test_a_compressed_body_is_capped_decompressed():
    response = bomb()
    assert len(_PACKED[0]) < loaf.constants.MAX_RESPONSE_BYTES // 100
    handler, sent = answers(response)
    with pytest.raises(loaf.LoafConnectionError, match=f"passed {32 * 1024 * 1024} bytes"):
        make_client(handler, max_retries=3).market.list()
    assert len(sent) == 1


class _Watched(httpx.SyncByteStream):
    """A body that records whether anything read it."""

    def __init__(self, content: bytes) -> None:
        self.content, self.read = content, False

    def __iter__(self) -> Iterator[bytes]:
        self.read = True
        yield self.content


_ANSWER = b'{"markets": []}'


@pytest.mark.parametrize(
    "headers, shown",
    [
        ([("content-encoding", "gzip, gzip")], "gzip, gzip"),
        ([("content-encoding", "gzip"), ("content-encoding", "gzip")], "gzip, gzip"),
        ([("content-encoding", "identity, gzip")], "identity, gzip"),
        ([("content-encoding", "br")], "br"),
        ([("content-encoding", " ZSTD ")], "zstd"),
        ([("content-encoding", "compress")], "compress"),
    ],
    ids=["stacked", "two-headers", "identity-then-gzip", "brotli", "zstd", "compress"],
)
def test_an_answer_in_any_other_encoding_is_refused_before_its_body_is_read(
    headers: list[tuple[str, str]], shown: str
):
    # httpx decodes every encoding a list names, each multiplying the last: 979 bytes of gzip
    # inside gzip decode to 512 MiB in one read. Brotli and zstd are decoded when installed.
    body = _Watched(gzip.compress(gzip.compress(b" " * (8 * 1024 * 1024) + _ANSWER)))
    handler, sent = answers(httpx.Response(200, stream=body, headers=headers))
    with pytest.raises(loaf.LoafConnectionError) as info:
        make_client(handler, max_retries=3).market.list()
    assert str(info.value) == (
        f"GET /trade failed: its answer's body is encoded {shown!r}, which the client does not "
        "read (only gzip or deflate, once)"
    )
    assert not body.read and len(sent) == 1  # never retried: the same answer would come again


def test_a_refused_encoding_is_cut_short_in_the_error():
    handler, _ = answers(
        httpx.Response(200, content=_ANSWER, headers={"content-encoding": "x" * 500})
    )
    with pytest.raises(loaf.LoafConnectionError) as info:
        make_client(handler).market.list()
    assert f"encoded {'x' * 100 + '...'!r}, which" in str(info.value)


@pytest.mark.parametrize("path, unknown, place", SIGNED.values(), ids=SIGNED.keys())
def test_a_signed_ops_answer_in_stacked_gzip_is_an_unknown_outcome(
    exchange, trading_client, path: str, unknown: type, place: Callable[[LoafClient], Any]
):
    # It was answered, but what the answer says was not read: it may have landed.
    body = _Watched(gzip.compress(gzip.compress(b'{"orderId": 1, "journalSeq": 1}')))
    stacked = {"content-encoding": "gzip, gzip"}
    exchange.queue(path, httpx.Response(200, stream=body, headers=stacked))
    with pytest.raises(unknown) as info:
        place(trading_client(max_retries=3))
    assert isinstance(info.value.last_error, loaf.LoafConnectionError)
    assert "encoded 'gzip, gzip'" in str(info.value.last_error)
    assert info.value.attempts == 1 and len(op_posts(exchange)) == 1 and not body.read


def _deflated(data: bytes) -> bytes:
    packer = zlib.compressobj(wbits=zlib.MAX_WBITS)
    return packer.compress(data) + packer.flush()


@pytest.mark.parametrize(
    "encoding, content",
    [
        (None, _ANSWER),
        ("identity", _ANSWER),
        (" Identity ", _ANSWER),
        ("", _ANSWER),
        ("gzip", gzip.compress(_ANSWER)),
        ("GZIP", gzip.compress(_ANSWER)),
        (" gzip ", gzip.compress(_ANSWER)),
        ("deflate", _deflated(_ANSWER)),
        ("Deflate", _deflated(_ANSWER)),
    ],
)
def test_a_plain_gzip_or_deflate_answer_is_read(encoding: str | None, content: bytes):
    headers = {} if encoding is None else {"content-encoding": encoding}
    client = make_client(lambda r: httpx.Response(200, content=content, headers=headers))
    assert client.market.list().markets == []


def test_an_x_gzip_answer_is_not_refused_for_its_encoding():
    # Read as httpx reads it: one encoding at most, so a read's expansion stays bounded.
    packed = gzip.compress(_ANSWER)
    client = make_client(
        lambda r: httpx.Response(200, content=packed, headers={"content-encoding": "x-gzip"})
    )
    _, content = client._receive(client._http.build_request("GET", "/markets"))
    assert content in (packed, _ANSWER)


def test_a_body_slower_than_the_read_timeout_is_not_read(clock):
    # Every chunk arrives within the socket's read timeout (30 s), the whole body does not.
    chunks = [b'{"markets": [', b" " * 10, b" " * 10, b"]}"]
    handler, sent = answers(httpx.Response(200, stream=Trickle(chunks, clock, 12.0)))
    with pytest.raises(loaf.LoafConnectionError, match=r"past the read timeout \(30.0 s\)"):
        make_client(handler, max_retries=3).market.list()
    assert len(sent) == 1
    # Within it, it is read.
    handler, _ = answers(httpx.Response(200, stream=Trickle(chunks, clock, 7.0)))
    assert make_client(handler).market.list().markets == []
    # And with no read timeout, nothing bounds it.
    http = httpx.Client(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, stream=Trickle(chunks, clock, 1e6))
        ),
        base_url=BASE,
        timeout=None,
    )
    assert LoafClient(base_url=BASE, http_client=http).market.list().markets == []


@pytest.mark.parametrize("op", SIGNED)
def test_a_signed_ops_answer_past_a_bound_is_an_unknown_outcome(
    exchange, trading_client, clock, op
):
    path, unknown, place = SIGNED[op]
    exchange.queue(path, bomb())
    with pytest.raises(unknown) as big:
        place(trading_client(max_retries=3))
    assert big.value.attempts == 1 and "bytes" in str(big.value.last_error)
    assert isinstance(big.value.last_error, loaf.LoafConnectionError)
    # The placement timeout (45 s) bounds the body's arrival too.
    exchange.queue(path, httpx.Response(200, stream=Trickle([b"{", b" ", b"}"], clock, 30.0)))
    with pytest.raises(unknown) as slow:
        place(trading_client(max_retries=3))
    assert slow.value.attempts == 1 and "past the read timeout (45.0 s)" in str(
        slow.value.last_error
    )


def test_a_body_that_breaks_off_is_still_retried_and_classified(exchange, trading_client):
    class Broken(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            yield b'{"markets": '
            raise httpx.ReadError("connection reset")

    handler, sent = answers(httpx.Response(200, stream=Broken()))
    assert make_client(handler, max_retries=1).market.list().markets == []
    assert len(sent) == 2
    # A placement's answer that broke off may follow a placed order: re-sent, then settled.
    exchange.queue("/orders", httpx.Response(200, stream=Broken()))
    assert trading_client().orders.limit_buy("OPRA", 10, 160).orderId == 1
    assert len(op_posts(exchange)) == 2


def test_a_pre_block_is_found_in_linear_time():
    started = time.perf_counter()
    error = error_from_response(404, "<pre>" * 20_480)  # 100 KiB of openings, no close
    assert time.perf_counter() - started < 1.0
    assert error.message.startswith("<pre><pre>")
    assert error_from_response(404, "x <PRE>a &amp; <pre>b</Pre> c").message == "a & <pre>b"
    assert error_from_response(404, "<pre>  only opened").message == "<pre>  only opened"


def test_a_long_page_is_cut_in_the_message_and_kept_in_the_body():
    page = "<html><body>" + "gateway trouble " * 20_000 + "</body></html>"
    error = error_from_response(502, page)
    assert len(error.message) == 500 and error.message.endswith("…")
    assert len(str(error)) < 600 and error.body == page
    assert error_from_response(502, "short").message == "short"


# --------------------------------------------------------------------------- #
# The login token in no error
# --------------------------------------------------------------------------- #


def timing_out(sent: list[httpx.Request]) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        headers = request.headers  # a local of this frame, as a transport's would be
        raise httpx.ReadTimeout(f"slow ({len(headers)} headers)", request=request)

    return handler


@pytest.mark.parametrize(
    "call",
    [lambda c: c.portfolio.summary(), lambda c: c.history.orders(), lambda c: c.orders.cancel(1)],
    ids=["GET summary", "GET history", "POST cancel"],
)
def test_a_network_error_holds_no_login_token(call):
    sent: list[httpx.Request] = []
    with pytest.raises(loaf.LoafConnectionError) as info:
        call(make_client(timing_out(sent), max_retries=2))
    tokens = tokens_sent(sent)
    assert tokens and all(t.startswith("loaf_lt_") for t in tokens)
    error = info.value
    assert isinstance(error.__cause__, httpx.ReadTimeout)
    assert secret_locals(error, *tokens) == []
    # Nor on the request the httpx error carries: a copy of it, without the header.
    assert bearer_requests(error) == []
    request = error.__cause__.request
    assert request.url == sent[-1].url and "authorization" not in request.headers
    assert "authorization" in sent[-1].headers  # what was sent is left as it was


def test_a_perps_ops_network_error_holds_no_login_token(exchange, trading_client):
    exchange.queue("/l2/allocations", *(httpx.ReadTimeout("slow") for _ in range(4)))
    with pytest.raises(loaf.PerpsOutcomeUnknownError) as info:
        trading_client().perps.add_margin(5)
    tokens = tokens_sent(op_posts(exchange))
    assert len(tokens) == 4
    assert secret_locals(info.value, *tokens) == []
    assert bearer_requests(info.value) == []
    cause = info.value.last_error.__cause__
    assert (
        isinstance(cause, httpx.ReadTimeout)
        and cause.request.content == op_posts(exchange)[-1].content
    )


def test_a_raising_response_hook_holds_no_login_token(exchange):
    def check(response: httpx.Response) -> None:
        response.raise_for_status()

    http = httpx.Client(
        transport=httpx.MockTransport(exchange.handler),
        base_url=BASE,
        event_hooks={"response": [check]},
    )
    client = LoafClient(TRADING_KEY, base_url=BASE, http_client=http)
    exchange.queue("/l2/allocations", httpx.Response(500, json={"error": "boom"}))
    with pytest.raises(loaf.PerpsOutcomeUnknownError) as op:
        client.perps.add_margin(5)
    exchange.queue("/portfolio/summary", httpx.Response(500, json={"error": "boom"}))
    with pytest.raises(httpx.HTTPStatusError) as read:
        client.portfolio.summary()
    tokens = tokens_sent(exchange.requests)
    for error in (op.value, read.value):
        assert secret_locals(error, *tokens) == []
        assert bearer_requests(error) == []


def test_a_real_network_error_holds_no_login_token():
    # A server on this machine that takes the request and never answers: httpcore's own frames,
    # which hold the request's headers, are in the error's chain.
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    received: list[bytes] = []

    def serve() -> None:
        connection, _ = server.accept()
        with connection:
            received.append(connection.recv(65536))
            connection.recv(65536)  # until the client gives up

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.getsockname()[1]}/api"
    try:
        with LoafClient(TRADING_KEY, base_url=base, timeout=0.3, max_retries=0) as client:
            with pytest.raises(loaf.LoafConnectionError) as info:
                client.portfolio.summary()
    finally:
        thread.join(5)
        server.close()
    (sent,) = received
    token = sent.split(b"Bearer ", 1)[1].split(b"\r\n", 1)[0].decode()
    assert isinstance(info.value.__cause__, httpx.ReadTimeout)
    assert secret_locals(info.value, token) == []
    assert bearer_requests(info.value) == []


def finished_sdk_frames(exc: BaseException, name: str) -> list[Any]:
    """The frames of ``loaf.client``'s ``name`` in the tracebacks of ``exc`` and what it chains."""
    frames = []
    for error in chained(exc):
        tb = error.__traceback__
        while tb is not None:
            code = tb.tb_frame.f_code
            if code.co_name == name and tb.tb_frame.f_globals.get("__name__") == "loaf.client":
                frames.append(tb.tb_frame)
            tb = tb.tb_next
    return frames


def test_no_finished_frame_keeps_the_login_tokens(exchange, trading_client):
    # The client object holds its current token: a finished frame keeps no local that holds it.
    exchange.queue("/l2/allocations", httpx.ReadTimeout("slow"))
    with pytest.raises(loaf.PerpsOutcomeUnknownError) as op:
        trading_client(max_retries=0).perps.add_margin(5)
    exchange.queue("/portfolio/summary", httpx.Response(404, json={"error": "gone"}))
    with pytest.raises(loaf.LoafNotFoundError) as read:
        trading_client().portfolio.summary()
    for error, name in ((op.value, "_send_signed"), (read.value, "_request")):
        frames = finished_sdk_frames(error, name)
        assert frames
        for frame in frames:
            assert not isinstance(frame.f_locals.get("tokens"), LoginTokens)


# --------------------------------------------------------------------------- #
# The caller's own errors
# --------------------------------------------------------------------------- #


def failing_lookup() -> None:
    caller_value = "the caller's own"
    raise KeyError(caller_value)


def last_frame_locals(exc: BaseException) -> dict[str, Any]:
    tb = exc.__traceback__
    assert tb is not None
    while tb.tb_next is not None:
        tb = tb.tb_next
    return dict(tb.tb_frame.f_locals)


@pytest.mark.parametrize("call", ["read", "signed"])
def test_an_error_being_handled_is_left_as_it_was(exchange, trading_client, call):
    client = trading_client()
    exchange.queue("/portfolio/summary", httpx.Response(404, json={"error": "gone"}))
    exchange.queue("/orders", httpx.Response(400, json={"error": "x", "code": "INVALID_PRICE"}))
    theirs = httpx.Request("GET", "https://other.test/x", headers={"Authorization": "Bearer X"})

    def sdk_call() -> None:
        if call == "read":
            client.portfolio.summary()
        else:
            client.orders.limit_buy("OPRA", 10, 160)

    saved = error = None
    try:
        failing_lookup()
    except KeyError as mine:
        saved = mine
        with pytest.raises(loaf.LoafAPIError) as info:
            sdk_call()
        error = info.value
    assert saved is not None and error is not None
    assert error.__context__ is saved
    assert last_frame_locals(saved) == {"caller_value": "the caller's own"}
    # The caller's own httpx error keeps its request, Authorization header and all.
    try:
        raise httpx.ConnectError("theirs", request=theirs)
    except httpx.ConnectError as mine:
        exchange.queue("/portfolio/summary", httpx.Response(404, json={"error": "gone"}))
        exchange.queue("/orders", httpx.Response(400, json={"error": "x", "code": "INVALID_PRICE"}))
        with pytest.raises(loaf.LoafAPIError):
            sdk_call()
        assert mine.request is theirs and mine.request.headers["authorization"] == "Bearer X"


def test_an_sdk_error_raised_while_handling_one_is_still_scrubbed():
    sent: list[httpx.Request] = []
    client = make_client(timing_out(sent), max_retries=0)
    error = None
    try:
        failing_lookup()
    except KeyError:
        with pytest.raises(loaf.LoafConnectionError) as info:
            client.portfolio.summary()
        error = info.value
    assert error is not None and isinstance(error.__cause__, httpx.ReadTimeout)
    assert secret_locals(error, *tokens_sent(sent)) == []
    assert bearer_requests(error) == []


# --------------------------------------------------------------------------- #
# What every request asks for
# --------------------------------------------------------------------------- #


def test_every_request_asks_only_for_gzip_and_deflate(monkeypatch):
    # As httpx asks when brotli and zstandard are installed.
    monkeypatch.setattr(httpx._client, "ACCEPT_ENCODING", "gzip, deflate, br, zstd")
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    received: list[bytes] = []

    def serve() -> None:
        connection, _ = server.accept()
        with connection:
            received.append(connection.recv(65536))
            connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}")

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.getsockname()[1]}/api"
    try:
        with LoafClient(base_url=base, max_retries=0) as client:
            client.get("/markets", auth=False)
    finally:
        thread.join(5)
        server.close()
    (request,) = received
    headers = dict(
        line.split(b": ", 1) for line in request.split(b"\r\n\r\n")[0].split(b"\r\n")[1:]
    )
    assert headers[b"Accept-Encoding"] == b"gzip, deflate"

    # Through a client of your own too, whatever it asks for itself: a read, a cancel, a signed
    # order and a signed perps op.
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"orderId": 1, "journalSeq": 1})

    http = httpx.Client(
        transport=httpx.MockTransport(handler), base_url=BASE, headers={"Accept-Encoding": "br"}
    )
    client = LoafClient(TRADING_KEY, base_url=BASE, http_client=http, max_retries=0)
    client.get("/trade", auth=False)
    client.post("/orders/cancel", json={"orderId": 1})
    client._send_signed("/orders", {"l2OrderIndex": 1})
    client._send_signed("/l2/exits", {"l2Nonce": 1}, op="perps")
    assert [r.headers.get_list("accept-encoding") for r in sent] == [["gzip, deflate"]] * 4


def test_a_signed_ops_4xx_past_the_size_cap_is_an_unknown_outcome(
    exchange, trading_client, monkeypatch
):
    # Its body was not read to its end: what it says is unknown, whatever its status.
    receive = LoafClient._receive

    def capped(self: LoafClient, request: httpx.Request) -> Any:
        if request.url.path == "/api/orders":  # the order's answer alone, not the reads before it
            monkeypatch.setattr("loaf.client.MAX_RESPONSE_BYTES", 100)
        return receive(self, request)

    monkeypatch.setattr(LoafClient, "_receive", capped)
    refusal = {"error": "x" * 200, "code": "INVALID_PRICE"}
    exchange.queue("/orders", httpx.Response(400, json=refusal))
    with pytest.raises(loaf.OrderOutcomeUnknownError) as info:
        trading_client().orders.limit_buy("OPRA", 10, 160)
    assert isinstance(info.value.last_error, loaf.LoafConnectionError)
    assert info.value.attempts == 1

"""Offline tests — no network. Run with: pytest

They exercise the client against an in-memory httpx mock transport, plus the
pure helpers (basis points, object wrapping, URL derivation, error mapping).
"""

from __future__ import annotations

import copy
import itertools
import json
import os
import pickle
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Optional, get_type_hints

import httpx
import pytest

import loaf
from loaf import LoafClient, models
from loaf._object import LoafObject, parse
from loaf.exceptions import error_from_response
from loaf.money import bps_to_fraction, fraction_to_bps
from loaf.ws.client import derive_ws_url

BASE = "http://test/api"


def make_client(handler, *, api_key: str | None = "testkey", **kwargs) -> LoafClient:
    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport, base_url=BASE)
    return LoafClient(api_key=api_key, base_url=BASE, http_client=http, **kwargs)


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #


def test_bps_helpers():
    assert bps_to_fraction(30) == 0.003
    assert fraction_to_bps(0.003) == 30


def test_loaf_object_access():
    obj = parse({"a": 1, "nested": {"b": 2}, "list": [{"c": 3}]})
    assert isinstance(obj, LoafObject)
    assert obj.a == 1 and obj["a"] == 1
    assert obj.nested.b == 2
    assert obj.list[0].c == 3
    assert json.loads(json.dumps(obj)) == {"a": 1, "nested": {"b": 2}, "list": [{"c": 3}]}
    with pytest.raises(AttributeError):
        _ = obj.missing


def test_derive_ws_url():
    assert derive_ws_url("https://api.loafmarkets.com/api") == "wss://api.loafmarkets.com/ws"
    assert derive_ws_url("http://localhost:8005/api") == "ws://localhost:8005/ws"
    assert derive_ws_url("http://localhost:8005/api/") == "ws://localhost:8005/ws"


def test_error_mapping():
    assert isinstance(error_from_response(401, {"error": "no"}), loaf.LoafAuthError)
    # The emergency kill switch is a 403 with no machine code — matched on message.
    assert isinstance(
        error_from_response(403, {"error": "Trading is currently halted"}),
        loaf.TradingHaltedError,
    )
    err = error_from_response(400, {"error": "Validation failed", "details": ["price: bad"]})
    assert isinstance(err, loaf.LoafValidationError) and err.details == ["price: bad"]
    assert isinstance(error_from_response(429, {"error": "slow"}), loaf.LoafRateLimitError)
    assert type(error_from_response(503, {"error": "down"})) is loaf.LoafServiceUnavailableError

    assert isinstance(
        error_from_response(403, {"error": "Trading halted for this market"}),
        loaf.TradingHaltedError,
    )
    delisted = error_from_response(
        403, {"error": "This market has been delisted and is no longer available for trading"}
    )
    assert type(delisted) is loaf.LoafForbiddenError  # not a halt
    # The daily price band is a 400.
    band = error_from_response(
        400,
        {
            "error": "Order price $170 is above upper daily price band limit $160 "
            "(reference: $150, band: 667bps)"
        },
    )
    assert isinstance(band, loaf.LoafValidationError)
    used = "l2 order_index: already used by the account; re-sign with the next index"
    assert isinstance(error_from_response(409, {"error": used}), loaf.LoafConflictError)
    # The exchange engine's own halts arrive as 400s with its text.
    for message in (
        "trading halted: …",
        "L1_RECHECK_PENDING: …",
        "E_L2_ORDERS_WITHOUT_JOURNAL: …",
    ):
        halt = error_from_response(400, {"error": message})
        assert type(halt) is loaf.TradingHaltedError and halt.status_code == 400
    # "halted" anywhere else in a 400 is an ordinary refusal.
    assert type(
        error_from_response(400, {"error": "Order refused: the market was halted earlier"})
    ) is loaf.LoafValidationError
    # A route that does not exist answers with an HTML page; keep just its message.
    missing = error_from_response(
        404,
        '<!DOCTYPE html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        "<title>Error</title>\n</head>\n<body>\n<pre>Cannot GET /trade</pre>\n"
        "</body>\n</html>\n",
    )
    assert isinstance(missing, loaf.LoafNotFoundError)
    assert missing.message == "Cannot GET /trade"


def test_errors_survive_pickle_and_copy():
    # A process pool pickles a worker's exception: signed_body must reach the parent.
    body = {
        "l2AccountIndex": 5,
        "l2OrderIndex": 4097,
        "l2ExpiryMs": 1_800_000_000_000,
        "l2SigS": "ab" * 40,
        "l2SigE": "cd" * 40,
    }
    error = loaf.OrderOutcomeUnknownError(
        signed_body=body, attempts=2, last_error=error_from_response(502, {"error": "x"})
    )
    assert str(error) == (
        "Could not confirm whether order 4097 of account 5 was placed after 2 attempt(s) (last "
        "error: HTTP 502: x). It may be live, and may already have filled. Never place it afresh: "
        "while it can still rest, re-send it unchanged with orders.resubmit(error.signed_body) "
        "(the same order if it landed; if not, it is placed now at the signed price), or find "
        "it in history.orders() by its expiryMs 1800000000000 (every status; openOrders shows "
        "only resting orders). A history miss proves nothing while it can still rest (until its "
        "expiryMs minus 72 hours)."
    )
    for clone in (pickle.loads(pickle.dumps(error)), copy.copy(error), copy.deepcopy(error)):
        assert type(clone) is loaf.OrderOutcomeUnknownError
        assert (clone.signed_body, clone.attempts) == (body, 2)
        assert str(clone) == str(error)
        assert type(clone.last_error) is loaf.LoafServerError
        assert (clone.last_error.status_code, clone.last_error.message) == (502, "x")
    coded = error_from_response(403, {"error": "x", "code": "COMPETITION_NOT_ENABLED"})
    for clone in (pickle.loads(pickle.dumps(coded)), copy.copy(coded), copy.deepcopy(coded)):
        assert type(clone) is loaf.LoafForbiddenError
        assert (clone.status_code, clone.code) == (403, "COMPETITION_NOT_ENABLED")
        assert str(clone) == str(coded)
    limited = error_from_response(429, {"error": "slow"}, retry_after=3.0, request_id="r1")
    clone = pickle.loads(pickle.dumps(limited))
    assert type(clone) is loaf.LoafRateLimitError
    assert (clone.retry_after, clone.status_code, clone.request_id) == (3.0, 429, "r1")
    assert str(clone) == str(limited)


def test_is_conditional_order():
    # `type` is the discriminator: `status` is not (CANCELLED belongs to both spaces).
    for t in ("STOP_LIMIT", "STOP_MARKET", "TAKE_LIMIT", "TAKE_MARKET"):
        assert loaf.is_conditional_order({"type": t}) is True
    assert loaf.is_conditional_order({"type": "LIMIT"}) is False
    assert loaf.is_conditional_order({"type": "MARKET"}) is False
    assert loaf.is_conditional_order({}) is False


def test_models_list_every_shape():
    # __all__ names every TypedDict the module defines.
    shapes = {
        name for name, value in vars(models).items()
        if isinstance(value, type) and issubclass(value, dict)
        and value.__module__ == models.__name__
    }
    assert set(models.__all__) == shapes
    assert models.OrderQueueEntry.__required_keys__ == {
        "orderId", "side", "price", "quantity", "quantityAhead", "ordersAhead"
    }
    assert {"orderId", "role"} <= models.TradeHistoryItem.__optional_keys__
    # A holding names its spot market as mark_price frames do; None: the asset has none.
    assert get_type_hints(models.Holding)["marketIndex"] == Optional[int]


# --------------------------------------------------------------------------- #
# Client behaviour
# --------------------------------------------------------------------------- #


def test_auth_header_and_config_error():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"userId": 7})

    client = make_client(handler)
    client.portfolio.component()
    assert seen["auth"] == "Bearer testkey"

    # A key read from a file or pasted with surrounding whitespace is sent stripped (httpx
    # refuses a header value ending in a newline, quoting the whole token in the error).
    padded = make_client(handler, api_key=" testkey\n")
    assert padded.api_key == "testkey"
    padded.portfolio.component()
    assert seen["auth"] == "Bearer testkey"

    # An anonymous client must refuse an authenticated call; a blank key counts as none.
    for anon in (
        LoafClient(base_url=BASE, http_client=httpx.Client(base_url=BASE)),
        LoafClient(api_key=" \n", base_url=BASE, http_client=httpx.Client(base_url=BASE)),
    ):
        assert anon.api_key is None
        with pytest.raises(loaf.LoafConfigError):
            anon.portfolio.component()


def test_public_endpoint_sends_no_auth():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"markets": []})

    make_client(handler).market.list()
    assert seen["auth"] is None


def test_active_orders_passthrough():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"activeOrders": [{"orderId": 1, "quantityLeft": 47.3, "createdAt": 0}]}
        )

    client = make_client(handler)
    result = client.history.active_orders()
    assert result.activeOrders[0].quantityLeft == 47.3


def test_params_drop_none():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={})

    client = make_client(handler)
    client.history.orders()  # no filters -> no query params
    assert seen["params"] == {}
    client.history.orders(page_size=5)
    assert seen["params"] == {"pageSize": "5"}


def test_rate_limit_retry_then_success():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                429, json={"error": "slow"}, headers={"RateLimit-Reset": "0"}
            )
        return httpx.Response(200, json={"markets": []})

    client = make_client(handler, max_retries=2)
    res = client.market.list()  # GET is idempotent -> 429 is retried
    assert res["markets"] == []
    assert calls["n"] == 2  # retried once


def test_rate_limit_not_retried_for_non_idempotent_post():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429, json={"error": "slow"}, headers={"RateLimit-Reset": "0"})

    # A 429 on a POST must surface, not silently retry (a retry could place the order twice).
    client = make_client(handler, max_retries=3)
    with pytest.raises(loaf.LoafRateLimitError):
        client.orders.cancel(1)
    assert calls["n"] == 1


@pytest.mark.parametrize(
    "headers",
    [{"Retry-After": "0"}, {"RateLimit-Reset": "0"}, {"Retry-After": "-3"}, {"Retry-After": "nan"}, {}],
)
def test_a_rate_limit_hint_of_zero_or_less_still_backs_off(monkeypatch, headers):
    slept: list = []
    monkeypatch.setattr("loaf.client.time", SimpleNamespace(sleep=slept.append))
    monkeypatch.setattr("loaf.client.random", SimpleNamespace(uniform=lambda low, high: high))
    make_client(lambda r: httpx.Response(200, json={}))._sleep_backoff(0, httpx.Response(429, headers=headers))
    assert slept == [0.5]  # the first exponential step, as with no hint at all


@pytest.mark.parametrize("value, expected", [("3", 3.0), ("120", 60.0)])
def test_a_positive_rate_limit_hint_is_honoured(monkeypatch, value, expected):
    slept: list = []
    monkeypatch.setattr("loaf.client.time", SimpleNamespace(sleep=slept.append))
    response = httpx.Response(429, headers={"Retry-After": value})
    make_client(lambda r: httpx.Response(200, json={}))._sleep_backoff(0, response)
    assert slept == [expected]


def test_candles_params_and_no_auth():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            json={"resolution": "1h", "candles": [], "oldestTs": None, "hasMore": False},
        )

    client = make_client(handler)
    res = client.market.candles("OPRA", "1h", count_back=24)
    assert seen["path"].endswith("/trade/OPRA/candles")
    assert seen["params"] == {"resolution": "1h", "countBack": "24"}  # `to` omitted
    assert seen["auth"] is None  # public endpoint
    assert res.hasMore is False

    client.market.candles("OPRA", loaf.CandleResolution.ONE_DAY, to=1_700_000_000)
    assert seen["params"] == {"resolution": "1d", "to": "1700000000"}


def test_trades_params_and_no_auth():
    sent = []
    tape = [{"tradeId": 41, "price": 160.5}, {"tradeId": 44, "price": 161}]  # oldest first

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(
            (request.url.path, dict(request.url.params), request.headers.get("authorization"))
        )
        return httpx.Response(200, json={"trades": tape})

    client = make_client(handler)
    assert [t.tradeId for t in client.market.trades("OPRA").trades] == [41, 44]
    client.market.trades("OPRA", after_trade_id=44, limit=1000)  # the next page
    assert sent == [
        ("/api/trade/OPRA/trades", {"afterTradeId": "0", "limit": "100"}, None),  # the defaults
        ("/api/trade/OPRA/trades", {"afterTradeId": "44", "limit": "1000"}, None),
    ]


# Refused before anything is sent: "" or "OPRA#" would fetch another route, and a control
# character would raise an httpx error, not a LoafError.
_MALFORMED_TICKERS = ("opra", "", "OPRA#", "../trade", "OPRA\n", "OPERAHOUSES", None)


@pytest.mark.parametrize(
    "call, route",
    [
        (lambda c, t: c.market.get(t), "/api/trade/{}"),
        (lambda c, t: c.market.candles(t, "1h"), "/api/trade/{}/candles"),
        (lambda c, t: list(c.market.iter_candles(t, "1h")), "/api/trade/{}/candles"),
        (lambda c, t: c.market.trades(t), "/api/trade/{}/trades"),
    ],
)
def test_market_routes_take_only_a_market_ticker(call, route):
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append((request.url.path, request.headers.get("authorization")))
        return httpx.Response(200, json={})

    client = make_client(handler)
    for ticker in ("OPRA", "OPERAHOUSE", "H100-PERP"):
        call(client, ticker)
    # Sent as given, and public.
    assert sent == [(route.format(t), None) for t in ("OPRA", "OPERAHOUSE", "H100-PERP")]
    # Refused as orders refuse it, before anything is sent.
    for ticker in (*_MALFORMED_TICKERS, "-PERP", "OPRA-perp", "OPRA-SPOT"):
        with pytest.raises(loaf.LoafValidationError, match="a market's ticker") as info:
            call(client, ticker)
        assert info.value.status_code == 0
    assert len(sent) == 3


@pytest.mark.parametrize(
    "call, route",
    [
        (lambda c, t: c.market.info_header(t), "/api/info/{}/header"),
        (lambda c, t: c.market.info_overview(t), "/api/info/{}/overview"),
        (lambda c, t: c.market.info_documents(t), "/api/info/{}/documents"),
        (lambda c, t: c.offerings.get(t), "/api/offerings/{}"),
    ],
)
def test_asset_routes_take_only_an_asset_ticker(call, route):
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append((request.url.path, request.headers.get("authorization")))
        return httpx.Response(200, json={})

    client = make_client(handler)
    for ticker in ("OPRA", "OPERAHOUSE"):
        call(client, ticker)
    assert sent == [(route.format(t), None) for t in ("OPRA", "OPERAHOUSE")]
    # An asset has no -PERP: that is a market's ticker.
    for ticker in (*_MALFORMED_TICKERS, "H100-PERP"):
        with pytest.raises(loaf.LoafValidationError, match="an asset's ticker") as info:
            call(client, ticker)
        assert info.value.status_code == 0
    assert len(sent) == 2


def test_candles_busy_503_retried_then_raises():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            503,
            json={"error": "Candle history is busy. Please try again shortly."},
            headers={"Retry-After": "1"},
        )

    client = make_client(handler, max_retries=2)
    with pytest.raises(loaf.LoafServiceUnavailableError) as info:
        client.market.candles("OPRA", "1h")
    assert info.value.message == "Candle history is busy. Please try again shortly."
    assert calls["n"] == 3  # the first try + 2 retries


def test_iter_candles_pages_backwards():
    pages = {
        # First call: no `to` -> newest page; then page back from oldestTs=100.
        None: {"resolution": "1m", "candles": [{"time": 100}, {"time": 160}], "oldestTs": 100, "hasMore": True},
        "100": {"resolution": "1m", "candles": [{"time": 40}], "oldestTs": 40, "hasMore": False},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=pages[request.url.params.get("to")])

    client = make_client(handler)
    times = [c.time for c in client.market.iter_candles("OPRA", "1m")]
    assert times == [160, 100, 40]  # newest -> oldest across pages


@pytest.mark.parametrize(
    "key, iterate",
    [
        ("orders", lambda c: c.history.iter_orders(page_size=2)),
        ("trades", lambda c: c.history.iter_trades(page_size=2)),
    ],
    ids=["orders", "trades"],
)
def test_iter_history_follows_the_cursor(key, iterate):
    pages = {
        # First call: no cursor; then each page's nextCursor, until a page has none.
        None: {key: [{"id": 3}, {"id": 2}], "nextCursor": "MTAwOjI"},
        "MTAwOjI": {key: [{"id": 1}], "nextCursor": None},
    }
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append((request.url.path, dict(request.url.params)))
        assert len(sent) <= len(pages), "paged past the last page"
        return httpx.Response(200, json=pages[request.url.params.get("cursor")])

    assert [row.id for row in iterate(make_client(handler))] == [3, 2, 1]
    assert sent == [
        (f"/api/history/{key}", {"pageSize": "2"}),
        (f"/api/history/{key}", {"pageSize": "2", "cursor": "MTAwOjI"}),
    ]


def test_a_repeated_cursor_stops_paging():
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.url.params.get("cursor"))
        return httpx.Response(200, json={"orders": [{"id": 1}], "nextCursor": "same"})

    pages = make_client(handler).history.iter_orders(page_size=1)
    assert [next(pages).id, next(pages).id] == [1, 1]
    with pytest.raises(loaf.LoafError, match="already given"):
        next(pages)
    assert sent == [None, "same"]


def test_a_candle_page_that_does_not_move_back_stops_paging():
    pages = {
        None: {"resolution": "1m", "candles": [{"time": 100}], "oldestTs": 100, "hasMore": True},
        "100": {"resolution": "1m", "candles": [{"time": 100}], "oldestTs": 100, "hasMore": True},
    }
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.url.params.get("to"))
        return httpx.Response(200, json=pages[request.url.params.get("to")])

    candles = make_client(handler).market.iter_candles("OPRA", "1m")
    assert [c.time for c in itertools.islice(candles, 2)] == [100, 100]
    with pytest.raises(loaf.LoafError, match="stopped paging"):
        next(candles)
    assert sent == [None, "100"]


def test_competition_endpoints():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["auth"] = request.headers.get("authorization")
        if request.content:
            seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True})

    client = make_client(handler)

    client.competition.info()
    assert seen["path"].endswith("/competition") and seen["auth"] is None

    client.competition.queue_position()
    assert seen["path"].endswith("/competition/queue-position")
    assert seen["auth"] == "Bearer testkey"

    client.competition.payout_details()
    assert seen["path"].endswith("/competition/payout-details")
    assert seen["auth"] == "Bearer testkey"

    client.competition.submit_payout_details(email="win@example.com")
    assert seen["path"].endswith("/competition/payout-details")
    assert seen["body"] == {"email": "win@example.com"}  # None wallet dropped

    # Exactly one destination is enforced locally.
    with pytest.raises(loaf.LoafValidationError):
        client.competition.submit_payout_details()
    with pytest.raises(loaf.LoafValidationError):
        client.competition.submit_payout_details(wallet_address="0xabc", email="a@b.c")


def test_rate_limit_headers_recorded():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"markets": []},
            headers={"RateLimit-Limit": "100", "RateLimit-Remaining": "97", "RateLimit-Reset": "873"},
        )

    client = make_client(handler)
    client.market.list()
    assert client.last_rate_limit == {"limit": 100.0, "remaining": 97.0, "reset": 873.0}


def test_conditional_cancel_routing():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"success": True})

    client = make_client(handler)

    client.orders.cancel_conditional(1)
    assert seen["path"].endswith("/orders/conditional/cancel")
    assert seen["body"] == {"orderId": 1}

    client.orders.cancel_row({"id": 7, "type": "LIMIT", "status": "OPEN"})
    assert seen["path"].endswith("/orders/cancel")
    assert seen["body"] == {"orderId": 7}

    # An active_orders() row: no `type` to narrow on, and its id lives under `orderId`.
    client.orders.cancel_row({"orderId": 11, "quantityLeft": 2})
    assert seen["path"].endswith("/orders/cancel")
    assert seen["body"] == {"orderId": 11}

    client.orders.cancel_row({"id": 8, "type": "STOP_MARKET", "status": "ARMED"})
    assert seen["path"].endswith("/orders/conditional/cancel")
    assert seen["body"] == {"orderId": 8}

    # Once PLACED the row is an ordinary order in the book, under a different id.
    client.orders.cancel_row(
        {"id": 9, "type": "STOP_MARKET", "status": "PLACED", "placedOrderId": 42}
    )
    assert seen["path"].endswith("/orders/cancel")
    assert seen["body"] == {"orderId": 42}

    # A terminal row is forwarded — the server, not the snapshot, decides.
    client.orders.cancel_row({"id": 10, "type": "STOP_MARKET", "status": "FAILED"})
    assert seen["path"].endswith("/orders/conditional/cancel")
    assert seen["body"] == {"orderId": 10}

    with pytest.raises(loaf.LoafValidationError):
        client.orders.cancel_row({"type": "LIMIT"})  # no id anywhere in the row


def test_cancel_all_route_and_auth():
    answer = {"requestedCount": 2, "cancelledOrderIds": [8, 7], "failedOrders": []}
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json=answer)

    assert make_client(handler).orders.cancel_all() == answer
    (request,) = sent  # exactly one request
    assert (request.method, request.url.path) == ("POST", "/api/orders/cancel-all")
    assert request.headers.get("authorization") == "Bearer testkey"
    assert request.content == b""  # no body


@pytest.mark.parametrize("order_id", [3.9, True, "7", 0, -1, None])
@pytest.mark.parametrize(
    "cancel",
    [
        lambda orders, order_id: orders.cancel(order_id),
        lambda orders, order_id: orders.cancel_conditional(order_id),
        lambda orders, order_id: orders.cancel_row({"id": order_id, "type": "LIMIT"}),
    ],
    ids=["cancel", "cancel_conditional", "cancel_row"],
)
def test_cancel_refuses_what_is_not_an_order_id(cancel, order_id):
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"success": True})

    orders = make_client(handler).orders
    # Never rounded or read from text: 3.9 or True would name another order.
    with pytest.raises(loaf.LoafValidationError) as info:
        cancel(orders, order_id)
    assert info.value.status_code == 0
    assert sent == []


def test_a_signed_body_passed_as_an_order_id_stays_out_of_the_refusal():
    orders = make_client(lambda request: httpx.Response(200)).orders
    signed_body = {"l2OrderIndex": 4097, "l2SigS": "ab" * 40, "l2SigE": "cd" * 40}
    for cancel in (orders.cancel, orders.cancel_conditional):
        with pytest.raises(loaf.LoafValidationError) as info:
            cancel(signed_body)  # type: ignore[arg-type]
        # Anyone holding its signatures can place the order: the refusal only names its type.
        assert str(info.value).endswith("(got dict)")


@pytest.mark.parametrize(
    "call", [lambda c: c.market.list(), lambda c: c.orders.cancel(1)], ids=["GET", "POST"]
)
def test_a_2xx_that_is_not_json_is_a_server_error(call):
    body = '{"markets": ['
    client = make_client(
        lambda request: httpx.Response(
            200,
            content=body.encode(),
            headers={"content-type": "application/json", "x-request-id": "r1"},
        )
    )
    with pytest.raises(loaf.LoafServerError) as info:
        call(client)
    assert (info.value.status_code, info.value.request_id, info.value.body) == (200, "r1", body)


def test_an_undecodable_body_is_a_connection_error():
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(
            200, stream=httpx.ByteStream(b"this is not gzip"), headers={"content-encoding": "gzip"}
        )

    with pytest.raises(loaf.LoafConnectionError) as info:
        make_client(handler, max_retries=1).market.list()
    assert isinstance(info.value.__cause__, httpx.DecodingError)
    assert len(sent) == 2  # a read is sent again


def test_version():
    assert loaf.constants.USER_AGENT == f"loaf-python-sdk/{loaf.__version__}"


def test_trading_needs_no_wallet_extra():
    # eth-account and eth-utils are the `wallet` extra, for python -m loaf.secret_key alone: the SDK
    # imports, signs and places orders without them. A fresh interpreter, where neither imports.
    code = """
import sys
sys.modules["eth_account"] = sys.modules["eth_utils"] = None
import httpx
from conftest import BASE, L2_SECRET, FakeExchange
from loaf import LoafClient
exchange = FakeExchange()
http = httpx.Client(transport=httpx.MockTransport(exchange.handler), base_url=BASE)
client = LoafClient(api_key="testkey", secret_key=L2_SECRET, base_url=BASE, http_client=http)
assert client.orders.limit_buy("OPRA", 1, 160).orderId == 1
assert [name for name in sys.modules if name.startswith("eth_") and sys.modules[name]] == []
"""
    tests = Path(__file__).parent
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(tests.parent), str(tests)]))
    subprocess.run([sys.executable, "-c", code], env=env, check=True)

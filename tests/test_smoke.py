"""Offline tests — no network. Run with: pytest

They exercise the client against an in-memory httpx mock transport, plus the
pure helpers (basis points, object wrapping, URL derivation, error mapping).
"""

from __future__ import annotations

import copy
import json
import pickle

import httpx
import pytest

import loaf
from loaf import LoafClient
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
    assert isinstance(
        error_from_response(403, {"error": "x", "code": "NOT_COMPETITION_PARTICIPANT"}),
        loaf.CompetitionEligibilityError,
    )
    # The emergency kill switch is a 403 with no machine code — matched on message.
    assert isinstance(
        error_from_response(403, {"error": "Trading is currently halted"}),
        loaf.TradingHaltedError,
    )
    err = error_from_response(400, {"error": "Validation failed", "details": ["price: bad"]})
    assert isinstance(err, loaf.LoafValidationError) and err.details == ["price: bad"]
    assert isinstance(error_from_response(429, {"error": "slow"}), loaf.LoafRateLimitError)
    assert isinstance(error_from_response(503, {"error": "down"}), loaf.LoafServiceUnavailableError)

    assert isinstance(
        error_from_response(403, {"error": "Trading halted for this property"}),
        loaf.TradingHaltedError,
    )
    closed = error_from_response(403, {"error": "Trading is currently closed"})
    assert type(closed) is loaf.LoafForbiddenError  # not a halt
    # The daily price band is a 400.
    band = error_from_response(
        400,
        {
            "error": "Order price $170 is above upper daily price band limit $160 "
            "(reference: $150, band: 667bps)"
        },
    )
    assert isinstance(band, loaf.LoafValidationError)
    assert isinstance(
        error_from_response(409, {"error": "nonce already used by a different order"}),
        loaf.LoafConflictError,
    )
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
    body = {"nonce": "0192a3b4c5d600112233445566778899", "signature": "0x" + "ab" * 65}
    error = loaf.OrderOutcomeUnknownError(
        signed_body=body, attempts=2, last_error=error_from_response(502, {"error": "x"})
    )
    for clone in (pickle.loads(pickle.dumps(error)), copy.copy(error), copy.deepcopy(error)):
        assert type(clone) is loaf.OrderOutcomeUnknownError
        assert (clone.signed_body, clone.attempts, clone.nonce) == (body, 2, body["nonce"])
        assert str(clone) == str(error)
        assert type(clone.last_error) is loaf.LoafServerError
        assert (clone.last_error.status_code, clone.last_error.message) == (502, "x")
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
        return httpx.Response(200, json={"properties": []})

    make_client(handler).market.properties()
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
        return httpx.Response(200, json={"properties": []})

    client = make_client(handler, max_retries=2)
    res = client.market.properties()  # GET is idempotent -> 429 is retried
    assert res["properties"] == []
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


def test_error_surfaces_after_retries_exhausted():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": "x", "code": "NOT_COMPETITION_PARTICIPANT"})

    client = make_client(handler)
    with pytest.raises(loaf.CompetitionEligibilityError):
        client.orders.cancel(1)


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
    res = client.market.candles("opera", "1h", count_back=24)
    assert seen["path"].endswith("/trade/opera/candles")
    assert seen["params"] == {"resolution": "1h", "countBack": "24"}  # `to` omitted
    assert seen["auth"] is None  # public endpoint
    assert res.hasMore is False

    client.market.candles("opera", loaf.CandleResolution.ONE_DAY, to=1_700_000_000)
    assert seen["params"] == {"resolution": "1d", "to": "1700000000"}


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
        client.market.candles("opera", "1h")
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
    times = [c.time for c in client.market.iter_candles("x", "1m")]
    assert times == [160, 100, 40]  # newest -> oldest across pages


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


def test_ws_new_channel_helpers():
    ws = loaf.LoafWebSocketClient(ws_url="ws://test/ws")
    ws.subscribe_volume("opera")
    ws.subscribe_leaderboard()
    ws.subscribe_property_status("opera")
    ws.subscribe_portfolio()
    assert ws._channels == {"volume:opera", "leaderboard", "property:opera", "portfolio"}


def test_ws_property_halt_dispatch():
    ws = loaf.LoafWebSocketClient(ws_url="ws://test/ws")
    seen = []
    ws.on_property_halt(seen.append)
    ws._dispatch(json.dumps({
        "type": "property_halt", "propertyId": 1, "tokenName": "opera",
        "isHalted": True, "timestamp": 0,
    }))
    assert seen[0].tokenName == "opera" and seen[0].isHalted is True


def test_trade_new_dispatches():
    ws = loaf.LoafWebSocketClient(ws_url="ws://test/ws")
    seen = []
    # A fill is final when it matches; txHash is "" until a batch proof.
    ws.on_trade(lambda m: seen.append((m.trade.tradeId, m.trade.txHash, m.trade.fee)))
    ws._dispatch(json.dumps({
        "type": "trade_new",
        "trade": {
            "tradeId": 1, "propertyId": 1, "tokenName": "opera", "txHash": "", "side": "BUY",
            "quantity": 1, "price": 100, "executedAt": 0, "fee": 0.1,
        },
        "timestamp": 0,
    }))
    assert seen == [(1, "", 0.1)]


def test_rate_limit_headers_recorded():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"properties": []},
            headers={"RateLimit-Limit": "100", "RateLimit-Remaining": "97", "RateLimit-Reset": "873"},
        )

    client = make_client(handler)
    client.market.properties()
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


def test_version():
    assert loaf.constants.USER_AGENT == f"loaf-python-sdk/{loaf.__version__}"

"""Offline tests — no network. Run with: pytest

They exercise the client against an in-memory httpx mock transport, plus the
pure helpers (validation, object wrapping, URL derivation, error mapping).
"""

from __future__ import annotations

import json

import httpx
import pytest

import loaf
from loaf import LoafClient
from loaf._object import LoafObject, parse
from loaf.exceptions import error_from_response
from loaf.money import bps_to_fraction, fraction_to_bps, validate_price, validate_quantity
from loaf.ws.client import derive_ws_url

BASE = "http://test/api"


def make_client(handler, **kwargs) -> LoafClient:
    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport, base_url=BASE)
    return LoafClient(api_key="testkey", base_url=BASE, http_client=http, **kwargs)


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #


def test_bps_helpers():
    assert bps_to_fraction(30) == 0.003
    assert fraction_to_bps(0.003) == 30


def test_validate_price_quantity():
    validate_price(167.49)  # ok
    validate_quantity(47.3)  # ok
    with pytest.raises(loaf.LoafValidationError):
        validate_price(1.234)  # 3 dp
    with pytest.raises(loaf.LoafValidationError):
        validate_quantity(1.23)  # 2 dp
    with pytest.raises(loaf.LoafValidationError):
        validate_quantity(0)


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
    assert isinstance(
        error_from_response(403, {"error": "KYC verification required"}), loaf.KycRequiredError
    )
    err = error_from_response(400, {"error": "Validation failed", "details": ["price: bad"]})
    assert isinstance(err, loaf.LoafValidationError) and err.details == ["price: bad"]
    assert isinstance(error_from_response(429, {"error": "slow"}), loaf.LoafRateLimitError)
    assert isinstance(error_from_response(503, {"error": "down"}), loaf.LoafServiceUnavailableError)


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

    # An anonymous client must refuse an authenticated call.
    anon = LoafClient(base_url=BASE, http_client=httpx.Client(base_url=BASE))
    with pytest.raises(loaf.LoafConfigError):
        anon.portfolio.component()


def test_public_endpoint_sends_no_auth():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"properties": []})

    make_client(handler).market.properties()
    assert seen["auth"] is None


def test_order_create_flow():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/orders/nonce"):
            return httpx.Response(200, json={"nonce": "a" * 32, "deadline": 123})
        if request.url.path.endswith("/orders"):
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json={"success": True, "orderId": 99})
        return httpx.Response(404, json={"error": "nope"})

    client = make_client(handler)
    res = client.orders.limit_buy("opera", quantity=10, price=167.49)
    assert res.orderId == 99
    body = captured["body"]
    # Sent as plain human units, correct enum strings, auto-fetched nonce.
    assert body == {
        "tokenName": "opera",
        "price": 167.49,
        "quantity": 10,
        "side": "BUY",
        "type": "LIMIT",
        "timeInForce": "GTC",
        "deadline": 0,
        "nonce": "a" * 32,
    }


def test_market_order_forces_zero_price():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/orders/nonce"):
            return httpx.Response(200, json={"nonce": "b" * 32, "deadline": 1})
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"success": True, "orderId": 1})

    client = make_client(handler)
    client.orders.market_sell("opera", quantity=2.5)
    assert captured["body"]["type"] == "MARKET"
    assert captured["body"]["price"] == 0


def test_order_validation_local():
    client = make_client(lambda r: httpx.Response(200, json={}))
    with pytest.raises(loaf.LoafValidationError):
        client.orders.limit_buy("opera", quantity=1, price=1.234)  # too many price dp
    with pytest.raises(loaf.LoafValidationError):
        client.orders.create("opera", "BUY", quantity=1, type="LIMIT")  # missing price


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

    # A 429 on a POST must surface, not silently retry with a stale single-use nonce.
    client = make_client(handler, max_retries=3)
    with pytest.raises(loaf.LoafRateLimitError):
        client.orders.cancel(1)
    assert calls["n"] == 1


def test_non_finite_price_quantity_rejected():
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(loaf.LoafValidationError):
            validate_price(bad)
        with pytest.raises(loaf.LoafValidationError):
            validate_quantity(bad)


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
    ws.subscribe_portfolio()
    assert ws._channels == {"volume:opera", "leaderboard", "portfolio"}


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

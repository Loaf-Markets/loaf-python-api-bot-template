"""Offline tests — no network. Run with: pytest

They exercise the client against an in-memory httpx mock transport, plus the
pure helpers (basis points, object wrapping, URL derivation, error mapping).
"""

from __future__ import annotations

import copy
import json
import os
import pickle
import subprocess
import sys
from pathlib import Path

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
    # The market mode's refusals: one class for the three 403 codes, the code kept.
    for code, message in (
        ("NOT_TRADEABLE_IN_MODE", "Property is not tradeable in the current market mode"),
        ("COMPETITION_NOT_STARTED", "The competition round has not started yet"),
        ("NOT_COMPETITION_PARTICIPANT", "Not a competition participant for the current round"),
    ):
        venue = error_from_response(403, {"error": message, "code": code})
        assert type(venue) is loaf.VenueError
        assert (venue.status_code, venue.code, venue.message) == (403, code, message)
    switching = error_from_response(
        503, {"error": "Market mode is switching; try again in a moment", "code": "VENUE_SWITCHING"}
    )
    assert type(switching) is loaf.VenueSwitchingError and switching.code == "VENUE_SWITCHING"
    assert isinstance(switching, loaf.LoafServiceUnavailableError)
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
    used = "l2 order_index: already used by the account; re-sign with the next index"
    assert isinstance(error_from_response(409, {"error": used}), loaf.LoafConflictError)
    # The exchange engine's own halts arrive as 400s with its text.
    for message in (
        "trading halted: L1 request 7 reversed at block 9; see the operator runbook",
        "L1_RECHECK_PENDING: 3 journaled L1 credits are not final and not re-checked since the "
        "start; orders and withdrawals are refused (cancels admitted)",
        "E_L2_ORDERS_WITHOUT_JOURNAL: every order is L2-signed and the L2 journal is not read "
        "back yet, so no order is admitted",
    ):
        halt = error_from_response(400, {"error": message})
        assert type(halt) is loaf.TradingHaltedError and halt.status_code == 400
    # "halted" anywhere else in a 400 is an ordinary refusal.
    assert type(
        error_from_response(400, {"error": "Order refused: the property was halted earlier"})
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
        "ticker": "OPRA",
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
        "expiryMs minus 24 hours)."
    )
    for clone in (pickle.loads(pickle.dumps(error)), copy.copy(error), copy.deepcopy(error)):
        assert type(clone) is loaf.OrderOutcomeUnknownError
        assert (clone.signed_body, clone.attempts) == (body, 2)
        assert str(clone) == str(error)
        assert type(clone.last_error) is loaf.LoafServerError
        assert (clone.last_error.status_code, clone.last_error.message) == (502, "x")
    venue = error_from_response(403, {"error": "x", "code": "NOT_COMPETITION_PARTICIPANT"})
    for clone in (pickle.loads(pickle.dumps(venue)), copy.copy(venue), copy.deepcopy(venue)):
        assert type(clone) is loaf.VenueError
        assert (clone.status_code, clone.code) == (403, "NOT_COMPETITION_PARTICIPANT")
        assert str(clone) == str(venue)
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


@pytest.mark.parametrize(
    "call, path",
    [
        (lambda c: c.market.property("OPRA"), "/api/trade/OPRA"),
        (lambda c: c.market.info_header("OPRA"), "/api/info/OPRA/header"),
        (lambda c: c.market.info_overview("OPRA"), "/api/info/OPRA/overview"),
        (lambda c: c.market.info_documents("OPRA"), "/api/info/OPRA/documents"),
        (lambda c: c.offerings.get("OPRA"), "/api/offerings/OPRA"),
    ],
)
def test_property_routes_take_the_ticker(call, path):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={})

    call(make_client(handler))
    assert seen == {"path": path, "auth": None}  # sent as given, and public


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

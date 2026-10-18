"""Order placement against an in-memory exchange: L2-signed bodies, the market
read, MARKET pricing, TP/SL legs, conditionals, max slippage, credentials,
retries, resubmit and the client's construction.

Every signature is checked with ``conftest.assert_signed``, which rebuilds the
signed terms from the body by the backend's rules and signs them again.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import time
from decimal import Context, Decimal, Inexact, InvalidOperation, Rounded, localcontext
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

import loaf
from loaf import LoafClient, OrderOutcomeUnknownError
from loaf.l2.signer import KAT
from loaf.l2.trading import DEFAULT_L2_CHAIN_ID, L2Trader

from conftest import (
    ACCOUNT,
    BASE,
    CONDITIONAL_KEYS,
    EPOCH,
    EXPIRY,
    LEG_KEYS,
    MARKET,
    ORDER_KEYS,
    PK0,
    SIGNER,
    SK0,
    assert_signed,
    detail,
    signed_terms,
)

NOT_CONFIRMED = (
    "Trading service did not confirm the order — it may still be processing. "
    "Check your open orders before retrying."
)
NOT_PLACED = (
    "Trading service is temporarily unavailable. Your order was not placed — "
    "please try again in a moment."
)
OPEN_ORDER_CAP = (
    "You have reached the limit of 100 open orders on this property. Cancel some open "
    "orders on it before placing a new one."
)
SIGNATURE_REFUSED = "l2 signature: the Schnorr signature does not verify"

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def error_reply(status: int, message: str, *, code: str | None = None, **kwargs) -> httpx.Response:
    body = {"error": message} if code is None else {"error": message, "code": code}
    return httpx.Response(status, json=body, **kwargs)


def duplicate_reply(order_id: int = 7) -> httpx.Response:
    """The exchange's answer to a re-send of an order that already landed."""
    return httpx.Response(
        200,
        json={
            "success": True,
            "orderId": order_id,
            "status": "OPEN",
            "quantityLeft": 10.0,
            "duplicate": True,
            "attachedLegIds": [],
        },
    )


def plain(body: dict) -> dict:
    """A body without its L2 fields: what the order says, not who signed it or with which index."""
    return {k: v for k, v in body.items() if not k.startswith("l2")}


# --------------------------------------------------------------------------- #
# Body shape
# --------------------------------------------------------------------------- #


def test_limit_body_signed_no_bearer(exchange, trading_client):
    res = trading_client().orders.limit_buy("OPRA", quantity=10, price=167.49)
    request = exchange.requests[-1]
    body = json.loads(request.content)
    assert list(body) == ORDER_KEYS
    assert body == {
        **body,
        "ticker": "OPRA",
        "price": 167.49,
        "quantity": 10.0,
        "side": "BUY",
        "type": "LIMIT",
        "timeInForce": "GTC",
        "l2ChainId": DEFAULT_L2_CHAIN_ID,
        "l2AccountIndex": ACCOUNT,
        "l2MarketIndex": MARKET["marketIndex"],
        "l2OrderIndex": 1000,
        "l2Epoch": EPOCH,
        "l2FeeRateCap": 7000,
        "l2ExpiryMs": EXPIRY,
        "l2BaseAssetId": MARKET["baseAssetId"],
        "l2QuoteAssetId": MARKET["quoteAssetId"],
    }
    # The signature authenticates a placement: the API key is set but not sent.
    assert "authorization" not in request.headers
    assert request.headers["content-type"] == "application/json"
    assert_signed(body)
    assert (res.orderId, res.status, res.quantityLeft, res.duplicate) == (1, "OPEN", 10.0, False)
    assert res.attachedLegIds == []
    # The account and its epoch for this client's key, and one block of the web app's 64 indices.
    (keys,) = exchange.gets("/l2/keys")
    assert dict(keys.url.params) == {"apiKeyIndex": "0", "publicKey": PK0}
    assert exchange.bodies("/l2/order-indices") == [{"count": 64}]


# --------------------------------------------------------------------------- #
# The property's L2 market
# --------------------------------------------------------------------------- #


def test_market_read_once_per_ticker(exchange, trading_client):
    exchange.details["MUS"] = detail("MUS", market={**MARKET, "marketIndex": 6, "baseAssetId": 8})
    client = trading_client()
    for ticker in ("OPRA", "MUS", "OPRA", "MUS"):
        client.orders.limit_buy(ticker, 10, 100)
    assert len(exchange.gets("/trade/OPRA")) == len(exchange.gets("/trade/MUS")) == 1
    assert len(exchange.gets("/l2/keys")) == 1
    bodies = exchange.bodies("/orders")
    markets = [(b["ticker"], b["l2MarketIndex"], b["l2BaseAssetId"]) for b in bodies]
    assert markets == [("OPRA", 5, 7), ("MUS", 6, 8)] * 2
    # One account, one block: the indices run on across the properties.
    assert [b["l2OrderIndex"] for b in bodies] == [1000, 1001, 1002, 1003]
    for body in bodies:
        assert_signed(body)


def test_market_read_again_after_30_s(exchange, trading_client, clock):
    client = trading_client()
    client.orders.limit_buy("OPRA", 1, 100)
    clock.t += 29
    client.orders.limit_buy("OPRA", 1, 100)
    assert len(exchange.gets("/trade/OPRA")) == 1
    clock.t += 1
    client.orders.limit_buy("OPRA", 1, 100)
    assert len(exchange.gets("/trade/OPRA")) == 2
    assert len(exchange.gets("/l2/keys")) == 1  # the key state keeps for 60 s


def test_no_l2_market_never_cached(exchange, trading_client):
    exchange.details["OPRA"] = detail(market=None)
    client = trading_client()
    for _ in range(2):
        with pytest.raises(loaf.LoafL2Error, match="no L2 market is listed for OPRA"):
            client.orders.limit_buy("OPRA", 10, 167.49)
    assert exchange.posts("/orders") == []
    assert len(exchange.gets("/trade/OPRA")) == 2  # a missing market is never cached


def test_unknown_ticker_404(exchange, trading_client):
    with pytest.raises(loaf.LoafNotFoundError, match="Property not found"):
        trading_client().orders.limit_buy("ATLS", 10, 167.49)
    assert exchange.posts("/orders") == []


# --------------------------------------------------------------------------- #
# MARKET orders
# --------------------------------------------------------------------------- #


def test_market_buy_reads_the_reference(exchange, trading_client):
    client = trading_client()
    client.orders.market_buy("OPRA", 2)  # reference 160, signs 160 x 1.02
    body = exchange.bodies("/orders")[-1]
    assert (body["type"], body["price"], body["quantity"]) == ("MARKET", 163.2, 2.0)
    assert_signed(body)
    assert len(exchange.gets("/trade/OPRA")) == 1  # the reference read refreshes the market too

    client.orders.market_sell("OPRA", 2)  # 160 x 0.98
    body = exchange.bodies("/orders")[-1]
    assert (body["side"], body["price"]) == ("SELL", 156.8)
    assert_signed(body)
    assert len(exchange.gets("/trade/OPRA")) == 2  # the reference is read for every order


def test_market_reference_price_no_reference_get(exchange, trading_client):
    client = trading_client()
    client.orders.limit_buy("OPRA", 10, 167.49)  # reads the market
    client.orders.market_sell("OPRA", 2, reference_price=100.005, max_slippage_bps=50)
    body = exchange.bodies("/orders")[-1]
    assert body["price"] == 99.51  # 99.504975, up to the cent: never below the tolerance
    assert_signed(body)
    client.orders.market_buy("OPRA", 2, reference_price=100.005, max_slippage_bps=50)
    assert exchange.bodies("/orders")[-1]["price"] == 100.5  # 100.505025, down to the cent
    assert len(exchange.gets("/trade/OPRA")) == 1


@pytest.mark.parametrize(
    "method, args, kwargs, message",
    [
        ("create", ("OPRA", "BUY", 1), {"type": "MARKET", "price": 167}, "take no price"),
        ("create", ("OPRA", "BUY", 1), {"type": "MARKET", "price": 0}, "take no price"),
        ("market_buy", ("OPRA", 1), {"reference_price": 0}, "reference_price must be positive"),
        (
            "create",
            ("OPRA", "BUY", 1),
            {"type": "LIMIT", "price": 1, "reference_price": 1},
            "reference_price only applies to MARKET orders",
        ),
        (
            "create",
            ("OPRA", "BUY", 1),
            {"type": "LIMIT", "price": 1, "max_slippage_bps": 10},
            "max_slippage_bps only applies to MARKET orders",
        ),
        (
            "market_buy",
            ("OPRA", 1),
            {"max_slippage_bps": 10_000},
            "max_slippage_bps must be a whole number",
        ),
        (
            "market_buy",
            ("OPRA", 1),
            {"reference_price": 100, "sl_price": 90, "leg_max_slippage_bps": 2.5},
            "leg_max_slippage_bps must be a whole number",
        ),
        ("create", ("OPRA", "BUY", 1), {"type": "LIMIT"}, "LIMIT orders require a price"),
        ("create", ("OPRA", "BUY", 1), {"type": "STOP_MARKET", "price": 1}, "Unknown order type"),
    ],
)
def test_pricing_refusals(exchange, trading_client, method, args, kwargs, message):
    with pytest.raises(loaf.LoafValidationError, match=message) as info:
        getattr(trading_client().orders, method)(*args, **kwargs)
    assert info.value.status_code == 0
    assert exchange.requests == []


@pytest.mark.parametrize(
    "served",
    [
        detail(mark=0),  # the exchange has no reference yet
        {**detail(), "propertyList": [{"ticker": "MUS", "tokenName": "Musgrave", "marketPrice": 50}]},
        # Matched on the ticker: an entry whose display name happens to read OPRA is not it.
        {**detail(), "propertyList": [{"ticker": "MUS", "tokenName": "OPRA", "marketPrice": 50}]},
    ],
)
def test_market_without_reference_refused(exchange, trading_client, served):
    exchange.details["OPRA"] = served
    with pytest.raises(loaf.LoafValidationError, match="No market reference price") as info:
        trading_client().orders.market_buy("OPRA", 1)
    assert info.value.status_code == 0
    assert exchange.posts("/orders") == []


# --------------------------------------------------------------------------- #
# Take-profit / stop-loss legs
# --------------------------------------------------------------------------- #


def test_limit_buy_with_legs(exchange, trading_client):
    client = trading_client()
    res = client.orders.limit_buy("OPRA", 10, 100, tp_price=120, sl_price=90)
    body = exchange.bodies("/orders")[-1]
    assert list(body) == ORDER_KEYS + ["tp", "sl"]
    assert list(body["tp"]) == list(body["sl"]) == LEG_KEYS
    # The triggers go as given; each leg signs a SELL 2% below its trigger, with the next indices.
    assert (body["tp"]["triggerPrice"], body["tp"]["price"]) == (120.0, 117.6)
    assert (body["sl"]["triggerPrice"], body["sl"]["price"]) == (90.0, 88.2)
    indices = (body["l2OrderIndex"], body["tp"]["l2OrderIndex"], body["sl"]["l2OrderIndex"])
    assert indices == (1000, 1001, 1002)
    assert body["tp"]["l2ExpiryMs"] == body["sl"]["l2ExpiryMs"] == body["l2ExpiryMs"]
    assert_signed(body)  # the order and both legs
    assert res.attachedLegIds == [901, 902]

    client.orders.limit_buy("OPRA", 10, 100, tp_price=120, sl_price=90, leg_max_slippage_bps=500)
    body = exchange.bodies("/orders")[-1]
    assert (body["tp"]["price"], body["sl"]["price"]) == (114.0, 85.5)
    assert_signed(body)

    # A lone stop-loss takes the index after its order; the next order follows its legs.
    client.orders.limit_buy("OPRA", 10, 100, sl_price=90)
    body = exchange.bodies("/orders")[-1]
    assert list(body) == ORDER_KEYS + ["sl"]
    assert (body["l2OrderIndex"], body["sl"]["l2OrderIndex"]) == (1006, 1007)
    client.orders.limit_buy("OPRA", 10, 100)
    body = exchange.bodies("/orders")[-1]
    assert list(body) == ORDER_KEYS  # strict schema: no empty legs
    assert body["l2OrderIndex"] == 1008


@pytest.mark.parametrize(
    "legs, message",
    [
        ({"tp_price": 90, "sl_price": 95}, "tp_price must be above sl_price"),
        ({"tp_price": 95, "sl_price": 95}, "tp_price must be above sl_price"),
        ({"sl_price": 100}, "sl_price must be below the order price"),
        ({"sl_price": 110}, "sl_price must be below the order price"),
        ({"tp_price": 100}, "tp_price must be above the order price"),
        ({"tp_price": 90}, "tp_price must be above the order price"),
        ({"tp_price": 120.001}, "tp_price must have at most 2"),
        ({"leg_max_slippage_bps": 100}, "only applies when tp_price"),
        (
            {"tp_price": 120, "leg_max_slippage_bps": 10_000},
            "leg_max_slippage_bps must be a whole number",
        ),
    ],
)
def test_leg_rules(exchange, trading_client, legs, message):
    with pytest.raises(loaf.LoafValidationError, match=message) as info:
        trading_client().orders.limit_buy("OPRA", 1, 100, **legs)
    assert info.value.status_code == 0
    assert exchange.requests == []


def test_leg_rules_side_and_market_parent(exchange, trading_client):
    orders = trading_client().orders
    with pytest.raises(loaf.LoafValidationError, match="only be attached to a BUY"):
        orders.create("OPRA", "SELL", 1, price=100, sl_price=90)
    with pytest.raises(TypeError):
        orders.limit_sell("OPRA", 1, 100, sl_price=90)  # legs protect a BUY only
    with pytest.raises(loaf.LoafValidationError, match="tp_price must be above sl_price"):
        orders.market_buy("OPRA", 1, tp_price=90, sl_price=95)
    assert exchange.requests == []

    # On a MARKET parent the exchange checks the triggers against its own reference. The legs
    # keep the client's max slippage (2% here) whatever the entry's max_slippage_bps is.
    orders.market_buy("OPRA", 1, max_slippage_bps=50, tp_price=120, sl_price=90)
    body = exchange.bodies("/orders")[-1]
    assert (body["price"], body["tp"]["price"], body["sl"]["price"]) == (160.8, 117.6, 88.2)
    assert_signed(body)


# --------------------------------------------------------------------------- #
# Conditional (stop / take) orders
# --------------------------------------------------------------------------- #


def test_conditional_market_price_derived(exchange, trading_client):
    orders = trading_client().orders
    res = orders.create_conditional("OPRA", "SELL", 5, type="STOP_MARKET", trigger_price=90)
    assert res.orderId == 1
    body = exchange.bodies("/orders/conditional")[-1]
    assert list(body) == CONDITIONAL_KEYS
    assert plain(body) == {
        "ticker": "OPRA",
        "price": 88.2,  # books at up to 2% below the trigger
        "quantity": 5.0,
        "side": "SELL",
        "type": "STOP_MARKET",
        "timeInForce": "GTC",
        "triggerPrice": 90.0,
    }
    assert_signed(body)  # the order the trigger books, signed now
    assert exchange.posts("/orders") == []

    orders.create_conditional("OPRA", "BUY", 2, type="STOP_MARKET", trigger_price=130)
    body = exchange.bodies("/orders/conditional")[-1]
    assert body["price"] == 132.6  # a BUY books above its trigger
    assert_signed(body)

    orders.stop_loss("OPRA", 5, 90)
    stop = exchange.bodies("/orders/conditional")[-1]
    assert plain(stop) == plain(exchange.bodies("/orders/conditional")[0])

    orders.take_profit("OPRA", 5, 120, max_slippage_bps=100)
    take = exchange.bodies("/orders/conditional")[-1]
    assert (take["type"], take["side"]) == ("TAKE_MARKET", "SELL")
    assert (take["triggerPrice"], take["price"]) == (120.0, 118.8)  # 1% below the trigger
    assert_signed(take)
    indices = [b["l2OrderIndex"] for b in exchange.bodies("/orders/conditional")]
    assert indices == [1000, 1001, 1002, 1003]


def test_conditional_limit(exchange, trading_client):
    trading_client().orders.create_conditional(
        "OPRA", "BUY", 2, type="TAKE_LIMIT", trigger_price=70, price=70.5
    )
    (body,) = exchange.bodies("/orders/conditional")
    assert (body["type"], body["price"], body["triggerPrice"]) == ("TAKE_LIMIT", 70.5, 70.0)
    assert_signed(body)


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"type": "STOP_LIMIT", "trigger_price": 90}, "STOP_LIMIT orders require a price"),
        (
            {"type": "STOP_MARKET", "trigger_price": 90, "price": 89.5},
            "STOP_MARKET orders take no price",
        ),
        (
            {"type": "TAKE_LIMIT", "trigger_price": 90, "price": 89.5, "max_slippage_bps": 50},
            "max_slippage_bps only applies to",
        ),
        ({"type": "STOP_MARKET", "trigger_price": 90.123}, "trigger_price must have at most 2"),
        ({"type": "STOP_MARKET", "trigger_price": 0}, "trigger_price must be positive"),
        ({"type": "LIMIT", "trigger_price": 90}, "Unknown conditional order type"),
    ],
)
def test_conditional_refusals(exchange, trading_client, kwargs, message):
    with pytest.raises(loaf.LoafValidationError, match=message) as info:
        trading_client().orders.create_conditional("OPRA", "SELL", 5, **kwargs)
    assert info.value.status_code == 0
    assert exchange.requests == []


def test_stop_loss_takes_no_price(trading_client):
    with pytest.raises(TypeError):
        trading_client().orders.stop_loss("OPRA", quantity=5, trigger_price=90, price=89.5)


# --------------------------------------------------------------------------- #
# Max slippage: the client's default ($LOAF_MAX_SLIPPAGE_BPS / max_slippage_bps=)
# --------------------------------------------------------------------------- #


def test_max_slippage_resolution(monkeypatch):
    assert LoafClient(base_url=BASE).max_slippage_bps == 200  # DEFAULT_MAX_SLIPPAGE_BPS
    monkeypatch.setenv("LOAF_MAX_SLIPPAGE_BPS", " 150\n")
    assert LoafClient(base_url=BASE).max_slippage_bps == 150  # the env var, when no argument
    assert LoafClient(base_url=BASE, max_slippage_bps=75).max_slippage_bps == 75  # the argument wins
    assert LoafClient(base_url=BASE, max_slippage_bps=0).max_slippage_bps == 0  # 0 is not "unset"
    for text, expected in (("0", 0), ("9999", 9999), ("0150", 150)):
        monkeypatch.setenv("LOAF_MAX_SLIPPAGE_BPS", text)
        assert LoafClient(base_url=BASE).max_slippage_bps == expected
    for blank in ("", "   ", "\t\n"):  # blank counts as unset
        monkeypatch.setenv("LOAF_MAX_SLIPPAGE_BPS", blank)
        client = LoafClient(base_url=BASE)
        assert client.max_slippage_bps == 200 and type(client.max_slippage_bps) is int


@pytest.mark.parametrize(
    "value", ["abc", "2.5", "1e2", "-1", "10000", "+150", "2%", "1 50", "True", "1.5"]
)
def test_invalid_max_slippage_env(monkeypatch, value):
    monkeypatch.setenv("LOAF_MAX_SLIPPAGE_BPS", value)
    # Checked before anything that could fail later (here, a CA bundle that does not exist).
    with pytest.raises(loaf.LoafConfigError) as info:
        LoafClient(base_url=BASE, verify="/nonexistent/ca.pem")
    message = str(info.value)
    assert message.startswith("$LOAF_MAX_SLIPPAGE_BPS must be a whole number of basis points")
    assert repr(value) in message


@pytest.mark.parametrize(
    "value", ["abc", "2.5", "1e2", "-1", "10000", "150", -1, 10_000, True, 1.5, Decimal("150")]
)
def test_invalid_max_slippage_argument(monkeypatch, value):
    monkeypatch.setenv("LOAF_MAX_SLIPPAGE_BPS", "150")  # valid, but the argument decides
    with pytest.raises(loaf.LoafConfigError) as info:
        LoafClient(base_url=BASE, max_slippage_bps=value)
    message = str(info.value)
    assert message.startswith("max_slippage_bps must be a whole number of basis points")
    assert repr(value) in message and "LOAF_MAX_SLIPPAGE_BPS" not in message


def test_client_max_slippage_prices_every_derived_price(exchange, trading_client):
    orders = trading_client(max_slippage_bps=100).orders

    def prices(path="/orders"):  # the last body's price, then its legs' prices
        body = exchange.bodies(path)[-1]
        return [body["price"]] + [body[leg]["price"] for leg in ("tp", "sl") if leg in body]

    orders.market_buy("OPRA", 2)  # reference 160 x 1.01
    assert prices() == [161.6]
    orders.market_sell("OPRA", 2)
    assert prices() == [158.4]
    orders.market_sell("OPRA", 2, reference_price=100)  # your own reference, same tolerance
    assert prices() == [99.0]
    orders.market_buy("OPRA", 2, max_slippage_bps=0)  # this order only
    assert prices() == [160.0]
    orders.limit_buy("OPRA", 10, 100, tp_price=120, sl_price=90)  # each leg 1% below its trigger
    assert prices() == [100.0, 118.8, 89.1]
    # An entry's max_slippage_bps moves only the entry; leg_max_slippage_bps only the legs.
    orders.market_buy("OPRA", 1, max_slippage_bps=0, tp_price=200, sl_price=90)
    assert prices() == [160.0, 198.0, 89.1]
    orders.market_buy("OPRA", 1, leg_max_slippage_bps=500, tp_price=200, sl_price=90)
    assert prices() == [161.6, 190.0, 85.5]

    orders.stop_loss("OPRA", 5, 90)
    orders.take_profit("OPRA", 5, 120)
    orders.create_conditional("OPRA", "BUY", 2, type="STOP_MARKET", trigger_price=130)
    orders.create_conditional("OPRA", "BUY", 2, type="TAKE_MARKET", trigger_price=70)
    orders.stop_loss("OPRA", 5, 90, max_slippage_bps=500)  # this order only
    orders.take_profit("OPRA", 5, 120)
    sent = [body["price"] for body in exchange.bodies("/orders/conditional")]
    assert sent == [89.1, 118.8, 131.3, 70.7, 85.5, 118.8]


def test_client_max_slippage_read_per_order(exchange, trading_client):
    client = trading_client()
    client.max_slippage_bps = 100  # a public attribute: later orders use the new value
    client.orders.market_buy("OPRA", 1)
    assert exchange.bodies("/orders")[-1]["price"] == 161.6
    sent = len(exchange.requests)
    client.max_slippage_bps = 2.5
    with pytest.raises(loaf.LoafValidationError, match="LoafClient.max_slippage_bps must be"):
        client.orders.market_buy("OPRA", 1)
    assert len(exchange.requests) == sent


# --------------------------------------------------------------------------- #
# Credentials
# --------------------------------------------------------------------------- #


def test_no_trading_key(exchange, trading_client):
    client = trading_client(l2_key=None)
    assert client.l2_public_key is None
    with pytest.raises(loaf.LoafConfigError, match="LOAF_L2_SECRET_KEY"):
        client.orders.limit_buy("OPRA", 10, 167.49)
    with pytest.raises(loaf.LoafConfigError, match="LOAF_L2_SECRET_KEY"):
        client.orders.stop_loss("OPRA", 5, 90)
    assert exchange.requests == []


def test_trading_key_alone_cannot_place(exchange, trading_client):
    # The order goes without the API key, but the account's key state and indices need it.
    client = trading_client(api_key=None)
    with pytest.raises(loaf.LoafConfigError, match="no API key is set"):
        client.orders.limit_buy("OPRA", 10, 167.49)
    assert [r.url.path for r in exchange.requests] == ["/api/trade/OPRA"]  # the public read only
    with pytest.raises(loaf.LoafConfigError):
        client.orders.cancel(1)  # cancels need the API key too


def test_api_key_on_the_account_reads_only(exchange):
    # The public construction: the trader is made on first use from l2_secret_key.
    http = httpx.Client(transport=httpx.MockTransport(exchange.handler), base_url=BASE)
    client = LoafClient(
        api_key="testkey", base_url=BASE, http_client=http, l2_secret_key=SK0, l2_signer=SIGNER
    )
    client.orders.limit_sell("OPRA", 1, 200)
    auth = {r.url.path: r.headers.get("authorization") for r in exchange.requests}
    assert auth == {
        "/api/trade/OPRA": None,  # a public read
        "/api/l2/keys": "Bearer testkey",
        "/api/l2/order-indices": "Bearer testkey",
        "/api/orders": None,  # the signature authenticates the order
    }
    assert_signed(exchange.bodies("/orders")[0])


# --------------------------------------------------------------------------- #
# Retries: identical re-sends, and an unknown outcome when nothing settles it
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "failure",
    [
        lambda: httpx.ReadTimeout("timed out"),
        lambda: error_reply(503, NOT_CONFIRMED),
        lambda: httpx.Response(500, json={"error": "boom"}),
        lambda: httpx.Response(502, html="<html><body>Bad Gateway</body></html>"),
        lambda: httpx.Response(504, html="<html><body>Gateway Timeout</body></html>"),
    ],
    ids=["read-timeout", "503-unconfirmed", "500", "502", "504"],
)
def test_transient_failure_resent_identically(exchange, trading_client, failure):
    exchange.queue("/orders", failure(), duplicate_reply())
    res = trading_client().orders.limit_buy("OPRA", 10, 167.49)
    first, second = exchange.posts("/orders")
    assert second == first
    assert res.orderId == 7 and res.duplicate is True


# The 503s Loaf-Backend sends only when nothing was placed (K1 head): ordersService.ts, the
# eligibility read (auth.ts), l2OrderFields.ts and the market mode (competitionVenue.ts).
@pytest.mark.parametrize(
    "message, code",
    [
        (NOT_PLACED, None),
        (
            "Trading is temporarily unavailable for this property. Your order was not placed — "
            "please try again in a moment.",
            None,
        ),
        ("Trading temporarily unavailable. Please try again shortly.", None),
        ("Trading is temporarily unavailable; your order was not placed, try again in a moment", None),
        (
            "l2 signature: the order signature could not be checked; your order was not placed, "
            "try again",
            None,
        ),
        (
            "l2 account: the trading account could not be read; nothing was done, try again in a "
            "moment",
            None,
        ),
        (
            "The market mode could not be read; your order was not placed, try again in a moment",
            None,
        ),
        ("Market mode is switching; try again in a moment", "VENUE_SWITCHING"),
    ],
)
def test_503_not_placed_exhausted(exchange, trading_client, message, code):
    exchange.queue("/orders", *(error_reply(503, message, code=code) for _ in range(3)))
    with pytest.raises(loaf.LoafServiceUnavailableError) as info:
        trading_client(max_retries=2).orders.limit_buy("OPRA", 10, 167.49)
    assert info.value.message == message
    posts = exchange.posts("/orders")
    assert len(posts) == 3 and len(set(posts)) == 1


@pytest.mark.parametrize(
    "failure",
    [
        httpx.ReadTimeout,
        httpx.WriteTimeout,
        httpx.ReadError,
        httpx.WriteError,
        httpx.RemoteProtocolError,
    ],
)
def test_timeouts_exhausted_unknown(exchange, trading_client, failure):
    # Anything past connecting may have reached the exchange.
    exchange.queue("/orders", *(failure("timed out") for _ in range(4)))
    with pytest.raises(OrderOutcomeUnknownError) as info:
        trading_client().orders.limit_buy("OPRA", 10, 167.49)
    error = info.value
    posts = exchange.posts("/orders")
    assert len(posts) == 4 and len(set(posts)) == 1
    assert error.attempts == 4
    assert error.signed_body == json.loads(posts[0])
    assert json.dumps(error.signed_body, separators=(",", ":")).encode() == posts[0]
    # The exchange holds at most one order per account and index: they name the order.
    assert (error.l2_account_index, error.l2_order_index) == (ACCOUNT, 1000)
    assert "order index 1000 of account 42" in str(error)
    assert isinstance(error.last_error, loaf.LoafConnectionError)
    assert error.__cause__ is error.last_error
    # Code that re-places an order on a connection error must not catch this one.
    assert not isinstance(error, loaf.LoafConnectionError)


def test_ambiguous_then_4xx_is_unknown(exchange, trading_client):
    exchange.queue("/orders", httpx.ReadTimeout("timed out"), error_reply(400, OPEN_ORDER_CAP))
    with pytest.raises(OrderOutcomeUnknownError) as info:
        trading_client().orders.limit_buy("OPRA", 10, 167.49)
    # The cap may be counting the first attempt itself: the refusal settles nothing.
    assert isinstance(info.value.last_error, loaf.LoafValidationError)
    assert info.value.last_error.message == OPEN_ORDER_CAP
    assert len(exchange.posts("/orders")) == 2


def test_ambiguous_then_not_placed_503s_is_unknown(exchange, trading_client):
    exchange.queue(
        "/orders",
        httpx.ReadTimeout("timed out"),
        *(error_reply(503, NOT_PLACED) for _ in range(3)),
    )
    with pytest.raises(OrderOutcomeUnknownError) as info:
        trading_client().orders.limit_buy("OPRA", 10, 167.49)
    # "Not placed" describes the re-send, not the first attempt.
    assert info.value.attempts == 4
    assert isinstance(info.value.last_error, loaf.LoafServiceUnavailableError)


@pytest.mark.parametrize("failure", [httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout])
def test_connect_errors_exhausted(exchange, trading_client, failure):
    exchange.queue("/orders", *(failure("unreachable") for _ in range(4)))
    with pytest.raises(loaf.LoafConnectionError):  # never sent, so never placed
        trading_client().orders.limit_buy("OPRA", 10, 167.49)
    assert len(exchange.posts("/orders")) == 4


def test_429_not_retried(exchange, trading_client):
    too_many = "Too many requests for this account. Please slow down."
    exchange.queue("/orders", error_reply(429, too_many, headers={"Retry-After": "3"}))
    with pytest.raises(loaf.LoafRateLimitError) as info:
        trading_client().orders.limit_buy("OPRA", 10, 167.49)
    assert info.value.retry_after == 3.0
    assert len(exchange.posts("/orders")) == 1


def test_400_not_retried(exchange, trading_client):
    details = ["price: Price must have at most 2 decimal places"]
    exchange.queue(
        "/orders", httpx.Response(400, json={"error": "Validation failed", "details": details})
    )
    with pytest.raises(loaf.LoafValidationError) as info:
        trading_client().orders.limit_buy("OPRA", 10, 167.49)
    assert info.value.status_code == 400 and info.value.details == details
    assert len(exchange.posts("/orders")) == 1


@pytest.mark.parametrize(
    "status, message, error",
    [
        (500, "boom", loaf.LoafServerError),
        (503, NOT_CONFIRMED, loaf.LoafServiceUnavailableError),
    ],
)
def test_5xx_exhausted_unknown(exchange, trading_client, status, message, error):
    exchange.queue("/orders", *(error_reply(status, message) for _ in range(4)))
    with pytest.raises(OrderOutcomeUnknownError) as info:
        trading_client().orders.limit_buy("OPRA", 10, 167.49)
    assert info.value.attempts == 4
    assert type(info.value.last_error) is error


@pytest.mark.parametrize(
    "reply",
    [
        lambda: httpx.Response(200, html="<html>maintenance</html>"),
        lambda: httpx.Response(200, json={"success": True}),
        # Labelled JSON but cut short: the order may be committed all the same.
        lambda: httpx.Response(
            200, content=b'{"success":true,"orderId":', headers={"content-type": "application/json"}
        ),
    ],
)
def test_200_without_order_id_is_unknown(exchange, trading_client, reply):
    exchange.queue("/orders", reply())
    with pytest.raises(OrderOutcomeUnknownError) as info:
        trading_client().orders.limit_buy("OPRA", 10, 167.49)
    assert info.value.attempts == 1
    assert info.value.__cause__ is info.value.last_error
    assert isinstance(info.value.last_error, loaf.LoafServerError)
    assert info.value.signed_body == exchange.bodies("/orders")[0]
    assert len(exchange.posts("/orders")) == 1


@pytest.mark.parametrize("status", [200, 502])
def test_undecodable_reply_is_unknown(exchange, trading_client, status):
    # A reply arrived, so the order may have landed, but its body cannot be decoded.
    exchange.queue(
        "/orders",
        httpx.Response(
            status,
            stream=httpx.ByteStream(b"this is not gzip"),
            headers={"content-encoding": "gzip"},
        ),
    )
    with pytest.raises(OrderOutcomeUnknownError) as info:
        trading_client(max_retries=0).orders.limit_buy("OPRA", 10, 167.49)
    assert isinstance(info.value.last_error.__cause__, httpx.DecodingError)
    assert len(exchange.posts("/orders")) == 1


def test_max_retries_zero(exchange, trading_client):
    client = trading_client(max_retries=0)
    exchange.queue("/orders", httpx.ReadTimeout("timed out"))
    with pytest.raises(OrderOutcomeUnknownError) as info:
        client.orders.limit_buy("OPRA", 10, 167.49)
    assert info.value.attempts == 1

    exchange.queue("/orders", error_reply(503, NOT_PLACED))
    with pytest.raises(loaf.LoafServiceUnavailableError):
        client.orders.limit_buy("OPRA", 10, 167.49)
    assert len(exchange.posts("/orders")) == 2  # one attempt each


def test_conditional_timeout_resent_identically(exchange, trading_client):
    exchange.queue("/orders/conditional", httpx.ReadTimeout("timed out"))
    assert trading_client().orders.stop_loss("OPRA", 5, 90).orderId == 1
    first, second = exchange.posts("/orders/conditional")
    assert second == first


def test_placement_timeout(exchange, trading_client):
    trading_client().orders.limit_buy("OPRA", 10, 167.49)
    *reads, post = exchange.requests
    # The market and key-state reads and the index reservation keep the client's timeout.
    assert [r.extensions["timeout"]["read"] for r in reads] == [30.0, 30.0, 30.0]
    assert post.extensions["timeout"]["read"] == 45.0  # placements wait out the engine

    trading_client(timeout=60.0).orders.limit_buy("OPRA", 10, 167.49)
    assert exchange.requests[-1].extensions["timeout"]["read"] == 60.0


def test_placement_timeout_none_and_object(exchange, trading_client):
    trading_client(timeout=None).orders.limit_buy("OPRA", 10, 167.49)
    assert exchange.requests[-1].extensions["timeout"]["read"] is None
    # Any form httpx accepts; only the read (answer) wait is raised, never connect/write/pool.
    for timeout, expected in (
        (httpx.Timeout(10.0, connect=5.0), (5.0, 45.0, 10.0, 10.0)),
        ((5.0, 30.0, 30.0, 5.0), (5.0, 45.0, 30.0, 5.0)),
        (5, (5, 45.0, 5, 5)),
    ):
        trading_client(timeout=timeout).orders.limit_buy("OPRA", 10, 167.49)
        t = exchange.requests[-1].extensions["timeout"]
        assert (t["connect"], t["read"], t["write"], t["pool"]) == expected


def test_placement_timeout_ignores_injected_clients_timeout(exchange):
    # Placements follow LoafClient's own timeout argument, as documented, not the
    # timeouts of an injected http_client (which reads keep).
    http = httpx.Client(
        transport=httpx.MockTransport(exchange.handler),
        base_url=BASE,
        timeout=httpx.Timeout(120.0, connect=20.0),
    )
    client = LoafClient(
        api_key="testkey", base_url=BASE, l2_secret_key=SK0, l2_signer=SIGNER, http_client=http
    )
    client.orders.limit_buy("OPRA", 10, 167.49)
    *reads, post = exchange.requests
    for read in reads:
        read_timeout = read.extensions["timeout"]
        assert (read_timeout["connect"], read_timeout["read"]) == (20.0, 120.0)
    placement = post.extensions["timeout"]
    phases = tuple(placement[k] for k in ("connect", "read", "write", "pool"))
    assert phases == (30.0, 45.0, 30.0, 30.0)  # the default 30 s, with only the read raised


def test_placement_backoff_ignores_rate_limit_headers(exchange, trading_client, monkeypatch):
    sleeps: list = []
    monkeypatch.setattr("loaf.client.time", SimpleNamespace(sleep=sleeps.append))
    # Every reply carries RateLimit-Reset (seconds left in the window), 5xx included.
    exchange.queue(
        "/orders",
        error_reply(503, NOT_PLACED, headers={"RateLimit-Reset": "57"}),
        error_reply(500, "boom", headers={"RateLimit-Reset": "60"}),
        error_reply(503, NOT_CONFIRMED, headers={"RateLimit-Reset": "59"}),
    )
    assert trading_client().orders.limit_buy("OPRA", 10, 167.49).orderId == 1
    # A signed price goes stale: re-sends wait only the jittered 0.5 / 1 / 2 s.
    assert len(sleeps) == 3 and all(0 <= s <= 2.0 for s in sleeps)

    # Reads still wait out the hint.
    del sleeps[:]
    replies = [
        httpx.Response(503, json={"error": "busy"}, headers={"RateLimit-Reset": "57"}),
        httpx.Response(200, json={"properties": []}),
    ]
    http = httpx.Client(transport=httpx.MockTransport(lambda r: replies.pop(0)), base_url=BASE)
    LoafClient(base_url=BASE, http_client=http).market.properties()
    assert sleeps == [57.0]


@pytest.mark.parametrize("read", ["/trade/OPRA", "/l2/keys"])
@pytest.mark.parametrize(
    "place",
    [
        lambda orders: orders.limit_buy("OPRA", 10, 167.49),
        lambda orders: orders.market_buy("OPRA", 2),  # the reference read
        lambda orders: orders.market_buy("OPRA", 2, reference_price=160),
        lambda orders: orders.stop_loss("OPRA", 5, 90),
    ],
)
def test_pre_signing_read_429_not_waited_out(exchange, trading_client, monkeypatch, place, read):
    sleeps: list = []
    monkeypatch.setattr("loaf.client.time", SimpleNamespace(sleep=sleeps.append))
    limited = error_reply(429, "Too many requests", headers={"RateLimit-Reset": "58"})
    exchange.queue(read, limited)
    # Waiting out the window would place the order a minute late, at a stale price.
    with pytest.raises(loaf.LoafRateLimitError) as info:
        place(trading_client().orders)
    assert info.value.retry_after == 58.0
    assert sleeps == []
    assert len(exchange.gets(read)) == 1
    assert exchange.posts("/orders") == [] and exchange.posts("/orders/conditional") == []

    # A 503 there is retried, after the short jittered pause only (every reply carries the hint).
    exchange.queue(read, error_reply(503, "busy", headers={"RateLimit-Reset": "57"}))
    assert place(trading_client().orders).orderId == 1
    assert len(sleeps) == 1 and 0 <= sleeps[0] <= 0.5


# --------------------------------------------------------------------------- #
# resubmit
# --------------------------------------------------------------------------- #


def test_resubmit(exchange, trading_client):
    exchange.queue("/orders", httpx.ReadTimeout("timed out"))
    with pytest.raises(OrderOutcomeUnknownError) as order:
        trading_client(max_retries=0).orders.limit_buy("OPRA", 10, 100, tp_price=120, sl_price=90)
    exchange.queue("/orders/conditional", httpx.ReadTimeout("timed out"))
    with pytest.raises(OrderOutcomeUnknownError) as stop:
        trading_client(max_retries=0).orders.stop_loss("OPRA", 5, 90)

    # The body carries its own signature: neither key is needed to re-send it.
    bare = trading_client(api_key=None, l2_key=None)
    exchange.queue("/orders", duplicate_reply())
    assert bare.orders.resubmit(order.value.signed_body).duplicate is True
    first, again = exchange.posts("/orders")
    assert again == first
    bare.orders.resubmit(stop.value.signed_body)  # routed by its triggerPrice
    first, again = exchange.posts("/orders/conditional")
    assert again == first

    sent = len(exchange.requests)
    body = order.value.signed_body
    now_ms = time.time_ns() // 1_000_000
    hour = 3_600_000
    for bad, message in (
        ({k: v for k, v in body.items() if k != "l2SigS"}, "needs a signed placement body"),
        ({**body, "l2OrderIndex": "1000"}, "needs a signed placement body"),
        ({**body, "l2AccountIndex": True}, "needs a signed placement body"),
        ({**body, "l2ExpiryMs": float(body["l2ExpiryMs"])}, "needs a signed placement body"),
        (json.dumps(body), "needs a signed placement body"),
        # The exchange refuses an order within a day of its expiry: it could not confirm it either.
        ({**body, "l2ExpiryMs": now_ms + 23 * hour}, "expires within a day"),
        ({**body, "l2ExpiryMs": now_ms - hour}, "expires within a day"),
    ):
        with pytest.raises(loaf.LoafValidationError, match=message) as info:
            bare.orders.resubmit(bad)
        assert info.value.status_code == 0
    assert len(exchange.requests) == sent

    # A body signed days ago (e.g. saved across a restart) still goes out unchanged.
    old = {**body, "l2ExpiryMs": now_ms + 25 * hour}
    bare.orders.resubmit(old)
    assert json.loads(exchange.posts("/orders")[-1]) == old


@pytest.mark.parametrize(
    "replies, reason",
    [
        (lambda: [httpx.ConnectError("unreachable") for _ in range(4)], loaf.LoafConnectionError),
        (lambda: [error_reply(429, "Too many requests")], loaf.LoafRateLimitError),
        (
            lambda: [error_reply(503, NOT_PLACED) for _ in range(4)],
            loaf.LoafServiceUnavailableError,
        ),
        (lambda: [error_reply(400, OPEN_ORDER_CAP)], loaf.LoafValidationError),
        (lambda: [error_reply(401, SIGNATURE_REFUSED)], loaf.LoafAuthError),
    ],
)
def test_resubmit_failure_stays_unknown(exchange, trading_client, replies, reason):
    exchange.queue("/orders", httpx.ReadTimeout("timed out"))
    with pytest.raises(OrderOutcomeUnknownError) as first:
        trading_client(max_retries=0).orders.limit_buy("OPRA", 10, 167.49)
    queued = replies()
    exchange.queue("/orders", *queued)
    # The first attempt may be live, so nothing but a 200 settles the re-send either.
    with pytest.raises(OrderOutcomeUnknownError) as again:
        trading_client().orders.resubmit(first.value.signed_body)
    assert type(again.value.last_error) is reason
    assert again.value.signed_body == first.value.signed_body
    assert again.value.attempts == len(queued)
    posts = exchange.posts("/orders")
    assert len(posts) == 1 + len(queued) and len(set(posts)) == 1


# --------------------------------------------------------------------------- #
# Local guards
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "ticker, side",
    [
        ("opra", "BUY"),  # sent exactly as given: never upper-cased for you
        ("Opra", "BUY"),
        ("OPERA", "BUY"),  # at most 4
        ("", "BUY"),
        ("OP-1", "BUY"),
        ("OP/R", "BUY"),
        (" OPR", "BUY"),
        ("OPRA\n", "BUY"),
        ("ＯＰＲ", "BUY"),  # fullwidth letters are not A-Z
        (None, "BUY"),
        ("OPRA", "buy"),
    ],
)
def test_ticker_and_side_validation(exchange, trading_client, ticker, side):
    orders = trading_client().orders
    with pytest.raises(loaf.LoafValidationError) as info:
        orders.create(ticker, side, 1, price=100)
    assert info.value.status_code == 0
    with pytest.raises(loaf.LoafValidationError):
        orders.create_conditional(
            ticker, side, 1, type="STOP_LIMIT", trigger_price=90, price=89
        )
    assert exchange.requests == []


@pytest.mark.parametrize("ticker", ["A", "MUS", "OPRA", "X1", "1234"])
def test_ticker_accepted(exchange, trading_client, ticker):
    exchange.details[ticker] = detail(ticker)
    trading_client().orders.limit_buy(ticker, 1, 100)
    (body,) = exchange.bodies("/orders")
    assert body["ticker"] == ticker
    assert len(exchange.gets(f"/trade/{ticker}")) == 1


def test_wire_normalization(exchange, trading_client):
    client = trading_client()
    client.orders.limit_buy("OPRA", Decimal("2.0"), Decimal("167.490"))
    (content,) = exchange.posts("/orders")
    body = json.loads(content)
    assert (body["price"], body["quantity"]) == (167.49, 2.0)
    assert b'"price":167.49,"quantity":2.0,' in content
    assert_signed(body)

    # 2.01 * 1000 == 2009.9999999999998 in floating point: the scaling must stay exact.
    client.orders.limit_buy("OPRA", 10, 2.01)
    body = exchange.bodies("/orders")[-1]
    assert body["price"] == 2.01 and signed_terms(body)["price"] == 2010
    assert_signed(body)


# --------------------------------------------------------------------------- #
# Client construction
# --------------------------------------------------------------------------- #


def test_client_construction(monkeypatch):
    monkeypatch.setenv("LOAF_L2_SECRET_KEY", SK0)
    with LoafClient(base_url=BASE) as client:
        assert "l2_key=set" in repr(client)
        assert client.l2_public_key == PK0  # the trader is made on first use
        text = repr(client)
        assert f"l2_key={PK0}" in text and SK0 not in text
        assert client._l2_secret_key is None  # the trader holds the one copy

    monkeypatch.setenv("LOAF_L2_SECRET_KEY", "  ")  # blank counts as unset
    with LoafClient(base_url=BASE) as client:
        assert client.l2_public_key is None
        assert "l2_key" not in repr(client)

    # 79 hex, upper case, 0x-prefixed, an API token (64 hex): refused, and never echoed.
    for bad in (SK0[:-1], SK0.upper(), "0x" + SK0, "ab" * 32):
        with pytest.raises(loaf.LoafConfigError, match="80 lowercase") as info:
            LoafClient(l2_secret_key=bad)
        assert bad not in str(info.value)

    # The trading key where the API token goes, in any letter case.
    for swapped in (SK0, SK0.upper(), " " + SK0 + "\n"):
        with pytest.raises(loaf.LoafConfigError, match="looks like an L2 trading key") as info:
            LoafClient(api_key=swapped)
        assert SK0 not in str(info.value).lower()


def _key_locals(exc: BaseException, secret: str) -> list:
    """Where ``secret`` sits in the locals of the SDK's frames in ``exc``'s traceback."""
    found = []
    tb = exc.__traceback__
    while tb is not None:
        frame = tb.tb_frame
        if frame.f_globals.get("__name__", "").startswith("loaf"):
            for name, value in frame.f_locals.items():
                for v in value if isinstance(value, (tuple, list)) else (value,):
                    if (isinstance(v, str) and secret in v) or (
                        isinstance(v, bytes) and secret.encode() in v
                    ):
                        found.append(f"{frame.f_code.co_name}:{name}")
        tb = tb.tb_next
    return found


@pytest.mark.filterwarnings("ignore::DeprecationWarning")  # httpx 0.28 deprecates verify=<str>
def test_keys_not_in_traceback_locals(monkeypatch):
    # Crash reporters and debuggers record every traceback frame's locals.
    monkeypatch.setenv("LOAF_L2_SECRET_KEY", SK0)
    # Failures after the key was resolved: a CA bundle that does not exist, a bad max_retries.
    kwargs: dict[str, Any]
    for failure, kwargs in (
        (OSError, {"verify": "/nonexistent/ca.pem"}),
        (ValueError, {"max_retries": "x"}),
    ):
        with pytest.raises(failure) as info:
            LoafClient(base_url=BASE, **kwargs)
        assert _key_locals(info.value, SK0) == []
    monkeypatch.delenv("LOAF_L2_SECRET_KEY")
    with pytest.raises(OSError) as info:  # the key passed in
        LoafClient(base_url=BASE, l2_secret_key=SK0, verify="/nonexistent/ca.pem")
    assert _key_locals(info.value, SK0) == []

    monkeypatch.setenv("LOAF_API_KEY", SK0)  # the trading key in the API token's variable
    with pytest.raises(loaf.LoafConfigError, match="looks like an L2 trading key") as info:
        LoafClient(base_url=BASE)
    assert _key_locals(info.value, SK0) == []
    monkeypatch.delenv("LOAF_API_KEY")

    near_miss = SK0[:-1]  # one hex digit short: nearly the key itself
    monkeypatch.setenv("LOAF_L2_SECRET_KEY", near_miss)
    with pytest.raises(loaf.LoafConfigError) as info:
        LoafClient(base_url=BASE)
    assert _key_locals(info.value, near_miss) == []
    with pytest.raises(loaf.LoafL2Error) as info:
        L2Trader(None, near_miss, signer=SIGNER)
    assert _key_locals(info.value, near_miss) == []

    # The signer's frames, refusing while the key is in hand (an order field it does not know).
    kat_order = json.loads(KAT.read_text())["orders"][0]
    order = {k: v for k, v in kat_order.items() if k not in ("m", "kind", "code")}
    with pytest.raises(loaf.L2SignerError, match="unknown field") as info:
        SIGNER.sign_order(SK0, {**order, "l2ApiKeyIndex": 0})
    assert _key_locals(info.value, SK0) == []
    with pytest.raises(loaf.L2SignerError) as info:
        SIGNER.sign_batch(SK0, [{**order, "l2ApiKeyIndex": 0}])
    assert _key_locals(info.value, SK0) == []


def test_order_math_ignores_callers_decimal_context(exchange, trading_client):
    client = trading_client()
    with localcontext(Context(prec=6, traps=[Inexact, Rounded, InvalidOperation])):
        client.orders.limit_buy("OPRA", 10, 12345.67)
        client.orders.market_sell("OPRA", 2, reference_price=100.005, max_slippage_bps=50)
        assert loaf.worst_price(167.49, "BUY", 200) == 170.83
        with pytest.raises(loaf.LoafValidationError, match="at most 2 decimal places"):
            loaf.money.validate_price(1.234)
    limit, market = exchange.bodies("/orders")
    assert (limit["price"], market["price"]) == (12345.67, 99.51)
    assert_signed(limit)
    assert_signed(market)


def test_reads_and_orders_need_no_eth_account():
    # eth-account is only the l2keys extra (the wallet flows). A fresh interpreter, so that
    # nothing imported it before: the client and its trading key never import it.
    code = f"""
import sys
sys.modules["eth_account"] = None
import loaf
with loaf.LoafClient(l2_secret_key={SK0!r}) as client:
    assert client.l2_public_key == {PK0!r}
for name in ("loaf.evm", "loaf.ipo", "loaf.l2.keys"):
    assert name not in sys.modules, name
try:
    import loaf.evm
except ImportError as exc:
    assert "loaf-bot[l2keys]" in str(exc), exc
else:
    raise AssertionError("no ImportError")
"""
    subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, check=True)

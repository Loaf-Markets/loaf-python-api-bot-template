"""Order placement against an in-memory exchange: signed bodies, the contract
cache, MARKET pricing, TP/SL legs, conditionals, retries, resubmit and the
contract heal.

Every signature is checked with ``conftest.recover``, which rebuilds the typed
data without :mod:`loaf.signing`.
"""

from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys
import time
from decimal import Context, Decimal, Inexact, InvalidOperation, Rounded, localcontext
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

import loaf
import loaf.signing
from loaf import LoafClient, OrderOutcomeUnknownError
from loaf.signing import new_order_nonce

from conftest import (
    BASE,
    CONTRACT_B,
    HARDHAT_ADDRESS,
    HARDHAT_KEY,
    VECTOR_NONCE,
    VECTOR_SIGNATURE,
    opera_detail,
    recover,
)

# The SDK sends the keys in this fixed order.
ORDER_KEYS = "tokenName price quantity side type timeInForce deadline nonce signature".split()
CONDITIONAL_KEYS = ORDER_KEYS[:5] + ["triggerPrice"] + ORDER_KEYS[5:]

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
STALE_SIGNER = "Signer is not authorized to trade for any account"
MIXED_SIGNERS = "Every signature in the request must be by the same signer"

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def error_reply(status: int, message: str, **kwargs) -> httpx.Response:
    return httpx.Response(status, json={"error": message}, **kwargs)


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
        },
    )


def leg_terms(body: dict, leg: str) -> dict:
    """What a TP/SL leg signs: a SELL of the parent's quantity at the leg's price."""
    return {
        "side": "SELL",
        "price": body[leg]["price"],
        "quantity": body["quantity"],
        "nonce": body[leg]["nonce"],
    }


def unsigned(body: dict) -> dict:
    return {k: v for k, v in body.items() if k not in ("nonce", "signature")}


# --------------------------------------------------------------------------- #
# Body shape
# --------------------------------------------------------------------------- #


def test_limit_body_signed_no_bearer(exchange, trading_client):
    res = trading_client().orders.limit_buy("opera", quantity=10, price=167.49)
    request = exchange.requests[-1]
    body = json.loads(request.content)
    assert list(body) == ORDER_KEYS
    assert [body[k] for k in ORDER_KEYS[:7]] == ["opera", 167.49, 10.0, "BUY", "LIMIT", "GTC", 0]
    assert re.fullmatch(r"[0-9a-f]{32}", body["nonce"])
    # The signature authenticates a placement: the API key is set but not sent.
    assert "authorization" not in request.headers
    assert request.headers["content-type"] == "application/json"
    assert recover(body, body["signature"]) == HARDHAT_ADDRESS
    assert (res.orderId, res.status, res.quantityLeft, res.duplicate) == (1, "OPEN", 10.0, False)


def test_vector_end_to_end(exchange, trading_client, monkeypatch):
    monkeypatch.setattr(loaf.signing, "new_order_nonce", lambda now_ms=None: VECTOR_NONCE)
    trading_client().orders.limit_buy("opera", 1.5, 160)
    (body,) = exchange.bodies("/orders")
    assert body["nonce"] == VECTOR_NONCE
    assert body["signature"] == VECTOR_SIGNATURE


# --------------------------------------------------------------------------- #
# Contract cache
# --------------------------------------------------------------------------- #


def test_contract_cached_once(exchange, trading_client):
    client = trading_client()
    client.orders.limit_buy("opera", 10, 167.49)
    client.orders.limit_buy("opera", 10, 167.49)
    (get,) = exchange.gets("/trade/opera")
    assert "authorization" not in get.headers  # a public read
    assert len(exchange.posts("/orders")) == 2


def test_null_contract_local_error(exchange, trading_client):
    exchange.details["opera"] = opera_detail(contract=None)
    client = trading_client()
    for _ in range(2):
        with pytest.raises(loaf.LoafValidationError, match="no deployed token contract") as info:
            client.orders.limit_buy("opera", 10, 167.49)
        assert info.value.status_code == 0
    assert exchange.posts("/orders") == []
    assert len(exchange.gets("/trade/opera")) == 2  # a missing contract is never cached


def test_unknown_token_404(exchange, trading_client):
    with pytest.raises(loaf.LoafNotFoundError, match="Property not found"):
        trading_client().orders.limit_buy("atlantis", 10, 167.49)
    assert exchange.posts("/orders") == []


# --------------------------------------------------------------------------- #
# MARKET orders
# --------------------------------------------------------------------------- #


def test_market_buy_fetches_reference(exchange, trading_client):
    client = trading_client()
    client.orders.market_buy("opera", 2)  # reference 160, signs 160 x 1.02
    body = exchange.bodies("/orders")[-1]
    assert (body["type"], body["price"], body["quantity"]) == ("MARKET", 163.2, 2.0)
    assert recover(body, body["signature"]) == HARDHAT_ADDRESS
    assert len(exchange.gets("/trade/opera")) == 1

    client.orders.market_sell("opera", 2)  # 160 x 0.98
    body = exchange.bodies("/orders")[-1]
    assert (body["side"], body["price"]) == ("SELL", 156.8)
    assert recover(body, body["signature"]) == HARDHAT_ADDRESS
    assert len(exchange.gets("/trade/opera")) == 2  # the reference is read for every order


def test_market_reference_price_no_reference_get(exchange, trading_client):
    client = trading_client()
    client.orders.limit_buy("opera", 10, 167.49)  # warms the contract cache
    client.orders.market_sell("opera", 2, reference_price=100.005, max_slippage_bps=50)
    body = exchange.bodies("/orders")[-1]
    assert body["price"] == 99.5  # 99.504975, half-up to the cent
    assert recover(body, body["signature"]) == HARDHAT_ADDRESS
    client.orders.market_buy("opera", 2, reference_price=100.005, max_slippage_bps=50)
    assert exchange.bodies("/orders")[-1]["price"] == 100.51  # 100.505025
    assert len(exchange.gets("/trade/opera")) == 1


@pytest.mark.parametrize(
    "method, args, kwargs, message",
    [
        ("create", ("opera", "BUY", 1), {"type": "MARKET", "price": 167}, "take no price"),
        ("create", ("opera", "BUY", 1), {"type": "MARKET", "price": 0}, "take no price"),
        ("market_buy", ("opera", 1), {"reference_price": 0}, "reference_price must be positive"),
        (
            "create",
            ("opera", "BUY", 1),
            {"type": "LIMIT", "price": 1, "reference_price": 1},
            "reference_price only applies to MARKET orders",
        ),
        (
            "create",
            ("opera", "BUY", 1),
            {"type": "LIMIT", "price": 1, "max_slippage_bps": 10},
            "max_slippage_bps only applies to MARKET orders",
        ),
        (
            "market_buy",
            ("opera", 1),
            {"max_slippage_bps": 10_000},
            "max_slippage_bps must be a whole number",
        ),
        (
            "market_buy",
            ("opera", 1),
            {"reference_price": 100, "sl_price": 90, "leg_max_slippage_bps": 2.5},
            "leg_max_slippage_bps must be a whole number",
        ),
        ("create", ("opera", "BUY", 1), {"type": "LIMIT"}, "LIMIT orders require a price"),
        ("create", ("opera", "BUY", 1), {"type": "STOP_MARKET", "price": 1}, "Unknown order type"),
    ],
)
def test_pricing_refusals(exchange, trading_client, method, args, kwargs, message):
    with pytest.raises(loaf.LoafValidationError, match=message) as info:
        getattr(trading_client().orders, method)(*args, **kwargs)
    assert info.value.status_code == 0
    assert exchange.requests == []


@pytest.mark.parametrize(
    "detail",
    [
        opera_detail(market_price=0),  # the exchange has no reference yet
        {**opera_detail(), "propertyList": [{"tokenName": "musgrave", "marketPrice": 50}]},
    ],
)
def test_market_without_reference_refused(exchange, trading_client, detail):
    exchange.details["opera"] = detail
    with pytest.raises(loaf.LoafValidationError, match="No market reference price") as info:
        trading_client().orders.market_buy("opera", 1)
    assert info.value.status_code == 0
    assert exchange.posts("/orders") == []


# --------------------------------------------------------------------------- #
# Take-profit / stop-loss legs
# --------------------------------------------------------------------------- #


def test_limit_buy_with_legs(exchange, trading_client):
    client = trading_client()
    client.orders.limit_buy("opera", 10, 100, tp_price=120, sl_price=90)
    body = exchange.bodies("/orders")[-1]
    assert list(body) == ORDER_KEYS + ["tp", "sl"]
    assert list(body["tp"]) == ["triggerPrice", "price", "nonce", "signature"]
    # The triggers go as given; each leg signs a SELL 2% below its trigger.
    assert (body["tp"]["triggerPrice"], body["tp"]["price"]) == (120.0, 117.6)
    assert (body["sl"]["triggerPrice"], body["sl"]["price"]) == (90.0, 88.2)
    assert len({body["nonce"], body["tp"]["nonce"], body["sl"]["nonce"]}) == 3
    assert recover(body, body["signature"]) == HARDHAT_ADDRESS
    for leg in ("tp", "sl"):
        assert recover(leg_terms(body, leg), body[leg]["signature"]) == HARDHAT_ADDRESS

    client.orders.limit_buy("opera", 10, 100, tp_price=120, sl_price=90, leg_max_slippage_bps=500)
    body = exchange.bodies("/orders")[-1]
    assert (body["tp"]["price"], body["sl"]["price"]) == (114.0, 85.5)
    assert recover(leg_terms(body, "sl"), body["sl"]["signature"]) == HARDHAT_ADDRESS

    client.orders.limit_buy("opera", 10, 100, sl_price=90)
    assert list(exchange.bodies("/orders")[-1]) == ORDER_KEYS + ["sl"]
    client.orders.limit_buy("opera", 10, 100)
    assert list(exchange.bodies("/orders")[-1]) == ORDER_KEYS  # strict schema: no empty legs


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
        trading_client().orders.limit_buy("opera", 1, 100, **legs)
    assert info.value.status_code == 0
    assert exchange.requests == []


def test_leg_rules_side_and_market_parent(exchange, trading_client):
    orders = trading_client().orders
    with pytest.raises(loaf.LoafValidationError, match="only be attached to a BUY"):
        orders.create("opera", "SELL", 1, price=100, sl_price=90)
    with pytest.raises(TypeError):
        orders.limit_sell("opera", 1, 100, sl_price=90)  # legs protect a BUY only
    with pytest.raises(loaf.LoafValidationError, match="tp_price must be above sl_price"):
        orders.market_buy("opera", 1, tp_price=90, sl_price=95)
    assert exchange.requests == []

    # On a MARKET parent the exchange checks the triggers against its own reference. The legs
    # keep the client's max slippage (2% here) whatever the entry's max_slippage_bps is.
    orders.market_buy("opera", 1, max_slippage_bps=50, tp_price=120, sl_price=90)
    body = exchange.bodies("/orders")[-1]
    assert (body["price"], body["tp"]["price"], body["sl"]["price"]) == (160.8, 117.6, 88.2)
    assert recover(body, body["signature"]) == HARDHAT_ADDRESS
    for leg in ("tp", "sl"):
        assert recover(leg_terms(body, leg), body[leg]["signature"]) == HARDHAT_ADDRESS


# --------------------------------------------------------------------------- #
# Conditional (stop / take) orders
# --------------------------------------------------------------------------- #


def test_conditional_market_price_derived(exchange, trading_client):
    orders = trading_client().orders
    res = orders.create_conditional("opera", "SELL", 5, type="STOP_MARKET", trigger_price=90)
    assert res.orderId == 1
    body = exchange.bodies("/orders/conditional")[-1]
    assert list(body) == CONDITIONAL_KEYS
    assert unsigned(body) == {
        "tokenName": "opera",
        "price": 88.2,  # books at up to 2% below the trigger
        "quantity": 5.0,
        "side": "SELL",
        "type": "STOP_MARKET",
        "triggerPrice": 90.0,
        "timeInForce": "GTC",
        "deadline": 0,
    }
    assert recover(body, body["signature"]) == HARDHAT_ADDRESS
    assert exchange.posts("/orders") == []

    orders.create_conditional("opera", "BUY", 2, type="STOP_MARKET", trigger_price=130)
    body = exchange.bodies("/orders/conditional")[-1]
    assert body["price"] == 132.6  # a BUY books above its trigger
    assert recover(body, body["signature"]) == HARDHAT_ADDRESS

    orders.stop_loss("opera", 5, 90)
    stop = exchange.bodies("/orders/conditional")[-1]
    assert unsigned(stop) == unsigned(exchange.bodies("/orders/conditional")[0])

    orders.take_profit("opera", 5, 120, max_slippage_bps=100)
    take = exchange.bodies("/orders/conditional")[-1]
    assert (take["type"], take["side"]) == ("TAKE_MARKET", "SELL")
    assert (take["triggerPrice"], take["price"]) == (120.0, 118.8)  # 1% below the trigger
    assert recover(take, take["signature"]) == HARDHAT_ADDRESS


def test_conditional_limit(exchange, trading_client):
    trading_client().orders.create_conditional(
        "opera", "BUY", 2, type="TAKE_LIMIT", trigger_price=70, price=70.5
    )
    (body,) = exchange.bodies("/orders/conditional")
    assert (body["type"], body["price"], body["triggerPrice"]) == ("TAKE_LIMIT", 70.5, 70.0)
    assert recover(body, body["signature"]) == HARDHAT_ADDRESS


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
        trading_client().orders.create_conditional("opera", "SELL", 5, **kwargs)
    assert info.value.status_code == 0
    assert exchange.requests == []


def test_stop_loss_takes_no_price(trading_client):
    with pytest.raises(TypeError):
        trading_client().orders.stop_loss("opera", quantity=5, trigger_price=90, price=89.5)


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

    orders.market_buy("opera", 2)  # reference 160 x 1.01
    assert prices() == [161.6]
    orders.market_sell("opera", 2)
    assert prices() == [158.4]
    orders.market_sell("opera", 2, reference_price=100)  # your own reference, same tolerance
    assert prices() == [99.0]
    orders.market_buy("opera", 2, max_slippage_bps=0)  # this order only
    assert prices() == [160.0]
    orders.limit_buy("opera", 10, 100, tp_price=120, sl_price=90)  # each leg 1% below its trigger
    assert prices() == [100.0, 118.8, 89.1]
    # An entry's max_slippage_bps moves only the entry; leg_max_slippage_bps only the legs.
    orders.market_buy("opera", 1, max_slippage_bps=0, tp_price=200, sl_price=90)
    assert prices() == [160.0, 198.0, 89.1]
    orders.market_buy("opera", 1, leg_max_slippage_bps=500, tp_price=200, sl_price=90)
    assert prices() == [161.6, 190.0, 85.5]

    orders.stop_loss("opera", 5, 90)
    orders.take_profit("opera", 5, 120)
    orders.create_conditional("opera", "BUY", 2, type="STOP_MARKET", trigger_price=130)
    orders.create_conditional("opera", "BUY", 2, type="TAKE_MARKET", trigger_price=70)
    orders.stop_loss("opera", 5, 90, max_slippage_bps=500)  # this order only
    orders.take_profit("opera", 5, 120)
    sent = [body["price"] for body in exchange.bodies("/orders/conditional")]
    assert sent == [89.1, 118.8, 131.3, 70.7, 85.5, 118.8]


def test_client_max_slippage_read_per_order(exchange, trading_client):
    client = trading_client()
    client.max_slippage_bps = 100  # a public attribute: later orders use the new value
    client.orders.market_buy("opera", 1)
    assert exchange.bodies("/orders")[-1]["price"] == 161.6
    sent = len(exchange.requests)
    client.max_slippage_bps = 2.5
    with pytest.raises(loaf.LoafValidationError, match="LoafClient.max_slippage_bps must be"):
        client.orders.market_buy("opera", 1)
    assert len(exchange.requests) == sent


# --------------------------------------------------------------------------- #
# Credentials
# --------------------------------------------------------------------------- #


def test_no_agent_key(exchange, trading_client):
    client = trading_client(agent_key=None)
    assert client.agent_address is None
    with pytest.raises(loaf.LoafConfigError, match="LOAF_AGENT_PRIVATE_KEY"):
        client.orders.limit_buy("opera", 10, 167.49)
    assert exchange.requests == []


def test_agent_only_client(trading_client):
    client = trading_client(api_key=None)
    assert client.orders.limit_buy("opera", 10, 167.49).orderId == 1
    with pytest.raises(loaf.LoafConfigError):
        client.orders.cancel(1)  # cancels still need the API key


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
    res = trading_client().orders.limit_buy("opera", 10, 167.49)
    first, second = exchange.posts("/orders")
    assert second == first
    assert res.orderId == 7 and res.duplicate is True


@pytest.mark.parametrize(
    "message",
    [
        NOT_PLACED,
        "Trading is temporarily unavailable for this property. Your order was not placed — "
        "please try again in a moment.",
        "Trading temporarily unavailable. Please try again shortly.",
    ],
)
def test_503_not_placed_exhausted(exchange, trading_client, message):
    exchange.queue("/orders", *(error_reply(503, message) for _ in range(3)))
    with pytest.raises(loaf.LoafServiceUnavailableError) as info:
        trading_client(max_retries=2).orders.limit_buy("opera", 10, 167.49)
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
        trading_client().orders.limit_buy("opera", 10, 167.49)
    error = info.value
    posts = exchange.posts("/orders")
    assert len(posts) == 4 and len(set(posts)) == 1
    assert error.attempts == 4
    assert error.signed_body == json.loads(posts[0])
    assert json.dumps(error.signed_body, separators=(",", ":")).encode() == posts[0]
    assert error.nonce == error.signed_body["nonce"] and error.nonce in str(error)
    assert isinstance(error.last_error, loaf.LoafConnectionError)
    assert error.__cause__ is error.last_error
    # Code that re-places an order on a connection error must not catch this one.
    assert not isinstance(error, loaf.LoafConnectionError)


def test_ambiguous_then_4xx_is_unknown(exchange, trading_client):
    exchange.queue("/orders", httpx.ReadTimeout("timed out"), error_reply(400, OPEN_ORDER_CAP))
    with pytest.raises(OrderOutcomeUnknownError) as info:
        trading_client().orders.limit_buy("opera", 10, 167.49)
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
        trading_client().orders.limit_buy("opera", 10, 167.49)
    # "Not placed" describes the re-send, not the first attempt.
    assert info.value.attempts == 4
    assert isinstance(info.value.last_error, loaf.LoafServiceUnavailableError)


@pytest.mark.parametrize("failure", [httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout])
def test_connect_errors_exhausted(exchange, trading_client, failure):
    exchange.queue("/orders", *(failure("unreachable") for _ in range(4)))
    with pytest.raises(loaf.LoafConnectionError):  # never sent, so never placed
        trading_client().orders.limit_buy("opera", 10, 167.49)
    assert len(exchange.posts("/orders")) == 4


def test_429_not_retried(exchange, trading_client):
    too_many = "Too many requests for this account. Please slow down."
    exchange.queue("/orders", error_reply(429, too_many, headers={"Retry-After": "3"}))
    with pytest.raises(loaf.LoafRateLimitError) as info:
        trading_client().orders.limit_buy("opera", 10, 167.49)
    assert info.value.retry_after == 3.0
    assert len(exchange.posts("/orders")) == 1


def test_400_not_retried(exchange, trading_client):
    details = ["price: Price must have at most 2 decimal places"]
    exchange.queue(
        "/orders", httpx.Response(400, json={"error": "Validation failed", "details": details})
    )
    with pytest.raises(loaf.LoafValidationError) as info:
        trading_client().orders.limit_buy("opera", 10, 167.49)
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
        trading_client().orders.limit_buy("opera", 10, 167.49)
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
        trading_client().orders.limit_buy("opera", 10, 167.49)
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
        trading_client(max_retries=0).orders.limit_buy("opera", 10, 167.49)
    assert isinstance(info.value.last_error.__cause__, httpx.DecodingError)
    assert len(exchange.posts("/orders")) == 1


def test_max_retries_zero(exchange, trading_client):
    client = trading_client(max_retries=0)
    exchange.queue("/orders", httpx.ReadTimeout("timed out"))
    with pytest.raises(OrderOutcomeUnknownError) as info:
        client.orders.limit_buy("opera", 10, 167.49)
    assert info.value.attempts == 1

    exchange.queue("/orders", error_reply(503, NOT_PLACED))
    with pytest.raises(loaf.LoafServiceUnavailableError):
        client.orders.limit_buy("opera", 10, 167.49)
    assert len(exchange.posts("/orders")) == 2  # one attempt each


def test_conditional_timeout_resent_identically(exchange, trading_client):
    exchange.queue("/orders/conditional", httpx.ReadTimeout("timed out"))
    assert trading_client().orders.stop_loss("opera", 5, 90).orderId == 1
    first, second = exchange.posts("/orders/conditional")
    assert second == first


def test_placement_timeout(exchange, trading_client):
    trading_client().orders.limit_buy("opera", 10, 167.49)
    get, post = exchange.requests
    assert get.extensions["timeout"]["read"] == 30.0  # reads keep the client's timeout
    assert post.extensions["timeout"]["read"] == 45.0  # placements wait out the engine

    trading_client(timeout=60.0).orders.limit_buy("opera", 10, 167.49)
    assert exchange.requests[-1].extensions["timeout"]["read"] == 60.0


def test_placement_timeout_none_and_object(exchange, trading_client):
    trading_client(timeout=None).orders.limit_buy("opera", 10, 167.49)
    assert exchange.requests[-1].extensions["timeout"]["read"] is None
    # Any form httpx accepts; only the read (answer) wait is raised, never connect/write/pool.
    for timeout, expected in (
        (httpx.Timeout(10.0, connect=5.0), (5.0, 45.0, 10.0, 10.0)),
        ((5.0, 30.0, 30.0, 5.0), (5.0, 45.0, 30.0, 5.0)),
        (5, (5, 45.0, 5, 5)),
    ):
        trading_client(timeout=timeout).orders.limit_buy("opera", 10, 167.49)
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
    client = LoafClient(base_url=BASE, agent_private_key=HARDHAT_KEY, http_client=http)
    client.orders.limit_buy("opera", 10, 167.49)
    get, post = exchange.requests
    read_timeout = get.extensions["timeout"]
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
    assert trading_client().orders.limit_buy("opera", 10, 167.49).orderId == 1
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


@pytest.mark.parametrize(
    "place",
    [
        lambda orders: orders.limit_buy("opera", 10, 167.49),  # the contract read
        lambda orders: orders.market_buy("opera", 2),  # the reference read
        lambda orders: orders.market_buy("opera", 2, reference_price=160),
        lambda orders: orders.stop_loss("opera", 5, 90),
    ],
)
def test_pre_signing_read_429_not_waited_out(exchange, trading_client, monkeypatch, place):
    sleeps: list = []
    monkeypatch.setattr("loaf.client.time", SimpleNamespace(sleep=sleeps.append))
    limited = error_reply(429, "Too many requests", headers={"RateLimit-Reset": "58"})
    exchange.queue("/trade/opera", limited)
    # Waiting out the window would place the order a minute late, at a stale price.
    with pytest.raises(loaf.LoafRateLimitError) as info:
        place(trading_client().orders)
    assert info.value.retry_after == 58.0
    assert sleeps == []
    assert len(exchange.gets("/trade/opera")) == 1
    assert exchange.posts("/orders") == [] and exchange.posts("/orders/conditional") == []

    # A 503 there is retried, after the short jittered pause only (every reply carries the hint).
    exchange.queue("/trade/opera", error_reply(503, "busy", headers={"RateLimit-Reset": "57"}))
    assert place(trading_client().orders).orderId == 1
    assert len(sleeps) == 1 and 0 <= sleeps[0] <= 0.5


# --------------------------------------------------------------------------- #
# resubmit
# --------------------------------------------------------------------------- #


def test_resubmit(exchange, trading_client):
    exchange.queue("/orders", httpx.ReadTimeout("timed out"))
    with pytest.raises(OrderOutcomeUnknownError) as order:
        trading_client(max_retries=0).orders.limit_buy("opera", 10, 100, tp_price=120, sl_price=90)
    exchange.queue("/orders/conditional", httpx.ReadTimeout("timed out"))
    with pytest.raises(OrderOutcomeUnknownError) as stop:
        trading_client(max_retries=0).orders.stop_loss("opera", 5, 90)

    # The body carries its own signature: neither key is needed to re-send it.
    bare = trading_client(api_key=None, agent_key=None)
    exchange.queue("/orders", duplicate_reply())
    assert bare.orders.resubmit(order.value.signed_body).duplicate is True
    first, again = exchange.posts("/orders")
    assert again == first
    bare.orders.resubmit(stop.value.signed_body)  # routed by its triggerPrice
    first, again = exchange.posts("/orders/conditional")
    assert again == first

    sent = len(exchange.requests)
    body = order.value.signed_body
    day_and_an_hour_ago = time.time_ns() // 1_000_000 - 25 * 3600 * 1000
    a_day_and_an_hour_ahead = time.time_ns() // 1_000_000 + 25 * 3600 * 1000
    for bad, message in (
        ({k: v for k, v in body.items() if k != "signature"}, "needs a signed placement body"),
        (json.dumps(body), "needs a signed placement body"),
        ({**body, "nonce": new_order_nonce(day_and_an_hour_ago)}, "more than 24 hours ago"),
        ({**body, "nonce": new_order_nonce(a_day_and_an_hour_ahead)}, "more than 24 hours ago"),
    ):
        with pytest.raises(loaf.LoafValidationError, match=message) as info:
            bare.orders.resubmit(bad)
        assert info.value.status_code == 0
    assert len(exchange.requests) == sent

    # A body signed hours ago (e.g. saved across a restart) still goes out unchanged.
    twenty_three_hours_ago = time.time_ns() // 1_000_000 - 23 * 3600 * 1000
    old = {**body, "nonce": new_order_nonce(twenty_three_hours_ago)}
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
        (lambda: [error_reply(401, STALE_SIGNER)], loaf.LoafAuthError),
    ],
)
def test_resubmit_failure_stays_unknown(exchange, trading_client, replies, reason):
    exchange.queue("/orders", httpx.ReadTimeout("timed out"))
    with pytest.raises(OrderOutcomeUnknownError) as first:
        trading_client(max_retries=0).orders.limit_buy("opera", 10, 167.49)
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
# Contract heal (a competition round redeployed the property's token)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "path, place",
    [
        ("/orders", lambda orders: orders.limit_buy("opera", 10, 167.49)),
        ("/orders/conditional", lambda orders: orders.stop_loss("opera", 5, 90)),
    ],
)
def test_contract_heal(exchange, trading_client, path, place):
    client = trading_client()
    place(client.orders)  # caches CONTRACT
    exchange.details["opera"] = opera_detail(contract=CONTRACT_B)
    exchange.queue(path, error_reply(401, STALE_SIGNER))
    assert place(client.orders).orderId == 2
    stale, healed = exchange.bodies(path)[1:]
    assert healed["nonce"] != stale["nonce"]  # re-signed with fresh nonces, not re-sent
    assert recover(healed, healed["signature"], CONTRACT_B) == HARDHAT_ADDRESS
    assert len(exchange.gets("/trade/opera")) == 2

    place(client.orders)  # the new contract is cached
    latest = exchange.bodies(path)[-1]
    assert recover(latest, latest["signature"], CONTRACT_B) == HARDHAT_ADDRESS
    assert len(exchange.gets("/trade/opera")) == 2


def test_contract_heal_with_legs(exchange, trading_client):
    client = trading_client()
    client.orders.limit_buy("opera", 10, 167.49)
    exchange.details["opera"] = opera_detail(contract=CONTRACT_B)
    exchange.queue("/orders", error_reply(401, MIXED_SIGNERS))
    client.orders.limit_buy("opera", 10, 100, tp_price=120, sl_price=90)
    healed = exchange.bodies("/orders")[-1]
    assert len(exchange.posts("/orders")) == 3
    assert recover(healed, healed["signature"], CONTRACT_B) == HARDHAT_ADDRESS
    for leg in ("tp", "sl"):
        signer = recover(leg_terms(healed, leg), healed[leg]["signature"], CONTRACT_B)
        assert signer == HARDHAT_ADDRESS


def test_contract_heal_only_once(exchange, trading_client):
    client = trading_client()
    client.orders.limit_buy("opera", 10, 167.49)
    exchange.details["opera"] = opera_detail(contract=CONTRACT_B)
    exchange.queue("/orders", error_reply(401, STALE_SIGNER), error_reply(401, STALE_SIGNER))
    with pytest.raises(loaf.LoafAuthError, match=STALE_SIGNER):
        client.orders.limit_buy("opera", 10, 167.49)
    assert len(exchange.posts("/orders")) == 3


def test_no_heal_when_contract_unchanged(exchange, trading_client):
    client = trading_client()
    client.orders.limit_buy("opera", 10, 167.49)
    exchange.queue("/orders", error_reply(401, STALE_SIGNER))
    with pytest.raises(loaf.LoafAuthError, match=STALE_SIGNER):  # the agent key itself is refused
        client.orders.limit_buy("opera", 10, 167.49)
    assert len(exchange.posts("/orders")) == 2
    assert len(exchange.gets("/trade/opera")) == 2  # re-read once, found unchanged


def test_no_heal_when_contract_differs_only_in_case(exchange, trading_client):
    mixed_case = "0x" + "aB" * 20
    exchange.details["opera"] = opera_detail(contract=mixed_case)
    client = trading_client()
    client.orders.limit_buy("opera", 10, 167.49)
    exchange.details["opera"] = opera_detail(contract=mixed_case.lower())  # the same address
    exchange.queue("/orders", error_reply(401, STALE_SIGNER))
    with pytest.raises(loaf.LoafAuthError, match=STALE_SIGNER):
        client.orders.limit_buy("opera", 10, 167.49)
    assert len(exchange.posts("/orders")) == 2  # nothing to re-sign


def test_no_heal_for_other_401s(exchange, trading_client):
    client = trading_client()
    client.orders.limit_buy("opera", 10, 167.49)
    exchange.details["opera"] = opera_detail(contract=CONTRACT_B)
    exchange.queue("/orders", error_reply(401, "Invalid signature"))
    with pytest.raises(loaf.LoafAuthError, match="Invalid signature"):
        client.orders.limit_buy("opera", 10, 167.49)
    assert len(exchange.posts("/orders")) == 2
    assert len(exchange.gets("/trade/opera")) == 1


def test_no_heal_on_freshly_read_contract(exchange, trading_client):
    client = trading_client()
    exchange.queue("/orders", error_reply(401, STALE_SIGNER))
    with pytest.raises(loaf.LoafAuthError, match=STALE_SIGNER):
        client.orders.limit_buy("opera", 10, 167.49)
    assert len(exchange.posts("/orders")) == 1
    assert len(exchange.gets("/trade/opera")) == 1

    # A MARKET order's reference read is as fresh, even with the contract already cached.
    exchange.queue("/orders", error_reply(401, STALE_SIGNER))
    with pytest.raises(loaf.LoafAuthError, match=STALE_SIGNER):
        client.orders.market_buy("opera", 2)
    assert len(exchange.posts("/orders")) == 2
    assert len(exchange.gets("/trade/opera")) == 2  # the reference read, no re-read


@pytest.mark.parametrize(
    "path, place, stale",
    [
        ("/orders", lambda orders: orders.limit_buy("opera", 10, 167.49), STALE_SIGNER),
        (
            "/orders",
            lambda orders: orders.limit_buy("opera", 10, 100, tp_price=120, sl_price=90),
            MIXED_SIGNERS,
        ),
        ("/orders/conditional", lambda orders: orders.stop_loss("opera", 5, 90), STALE_SIGNER),
    ],
)
@pytest.mark.parametrize(
    "ambiguous",
    [
        lambda: httpx.ReadTimeout("timed out"),
        lambda: error_reply(503, NOT_CONFIRMED),
        lambda: error_reply(500, "Internal server error"),
    ],
)
def test_no_heal_after_ambiguous_attempt(exchange, trading_client, path, place, stale, ambiguous):
    client = trading_client()
    place(client.orders)  # caches CONTRACT
    exchange.details["opera"] = opera_detail(contract=CONTRACT_B)
    exchange.queue(path, ambiguous(), error_reply(401, stale))
    # The first attempt may be live: re-signing now would place a second order.
    with pytest.raises(OrderOutcomeUnknownError) as info:
        place(client.orders)
    assert type(info.value.last_error) is loaf.LoafAuthError
    assert info.value.last_error.message == stale
    first, again = exchange.posts(path)[1:]
    assert again == first  # re-sent unchanged: one nonce
    assert len(exchange.gets("/trade/opera")) == 1  # no heal re-read


def test_heal_after_definite_503(exchange, trading_client):
    # "Your order was not placed" settles the first attempt, so the heal may still run.
    client = trading_client()
    client.orders.limit_buy("opera", 10, 167.49)
    exchange.details["opera"] = opera_detail(contract=CONTRACT_B)
    exchange.queue("/orders", error_reply(503, NOT_PLACED), error_reply(401, STALE_SIGNER))
    assert client.orders.limit_buy("opera", 10, 167.49).orderId == 2
    _, not_placed, stale, healed = exchange.bodies("/orders")
    assert stale == not_placed and healed["nonce"] != stale["nonce"]
    assert recover(healed, healed["signature"], CONTRACT_B) == HARDHAT_ADDRESS
    assert len(exchange.gets("/trade/opera")) == 2


# --------------------------------------------------------------------------- #
# Local guards
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "token_name, side",
    [
        ("Opera", "BUY"),
        ("op3ra", "BUY"),
        ("", "BUY"),
        ("a" * 21, "BUY"),
        ("op/era", "BUY"),
        ("opera\n", "BUY"),
        (None, "BUY"),
        ("opera", "buy"),
    ],
)
def test_token_and_side_validation(exchange, trading_client, token_name, side):
    orders = trading_client().orders
    with pytest.raises(loaf.LoafValidationError) as info:
        orders.create(token_name, side, 1, price=100)
    assert info.value.status_code == 0
    with pytest.raises(loaf.LoafValidationError):
        orders.create_conditional(
            token_name, side, 1, type="STOP_LIMIT", trigger_price=90, price=89
        )
    assert exchange.requests == []


def test_wire_normalization(exchange, trading_client):
    client = trading_client()
    client.orders.limit_buy("opera", Decimal("2.0"), Decimal("167.490"))
    (content,) = exchange.posts("/orders")
    body = json.loads(content)
    assert (body["price"], body["quantity"]) == (167.49, 2.0)
    assert b'"price":167.49,' in content and b'"quantity":2.0,' in content
    assert recover(body, body["signature"]) == HARDHAT_ADDRESS

    # 2.01 * 1000 == 2009.9999999999998 in floating point: the scaling must stay exact.
    client.orders.limit_buy("opera", 10, 2.01)
    body = exchange.bodies("/orders")[-1]
    assert body["price"] == 2.01
    assert recover(body, body["signature"]) == HARDHAT_ADDRESS


# --------------------------------------------------------------------------- #
# Client construction
# --------------------------------------------------------------------------- #


def test_client_construction(monkeypatch):
    monkeypatch.setenv("LOAF_AGENT_PRIVATE_KEY", HARDHAT_KEY)
    with LoafClient(base_url=BASE) as client:
        assert client.agent_address == HARDHAT_ADDRESS
        text = repr(client)
        assert HARDHAT_ADDRESS in text and HARDHAT_KEY[2:] not in text

    monkeypatch.setenv("LOAF_AGENT_PRIVATE_KEY", "  ")  # blank counts as unset
    with LoafClient(base_url=BASE) as client:
        assert client.agent_address is None
        assert "agent=" not in repr(client)

    bad = "0x" + "ab" * 31  # 62 hex: not a key
    with pytest.raises(loaf.LoafConfigError) as info:
        LoafClient(agent_private_key=bad)
    assert bad[2:] not in str(info.value)

    swapped_keys = ("0x" + "ab" * 32, " " + HARDHAT_KEY, HARDHAT_KEY + "\n", "0X" + HARDHAT_KEY[2:])
    for swapped in swapped_keys:
        with pytest.raises(loaf.LoafConfigError, match="looks like a private key"):
            LoafClient(api_key=swapped)
    for api_key in (HARDHAT_KEY[2:], HARDHAT_KEY[2:].upper()):  # whatever the letter case
        with pytest.raises(loaf.LoafConfigError, match="same value") as info:
            LoafClient(api_key=api_key, agent_private_key=HARDHAT_KEY)
        assert HARDHAT_KEY[2:] not in str(info.value)


def _key_locals(exc: BaseException, secret: str) -> list:
    """Where ``secret`` sits in the locals of the SDK's frames in ``exc``'s traceback."""
    found = []
    tb = exc.__traceback__
    while tb is not None:
        frame = tb.tb_frame
        if frame.f_globals.get("__name__", "").startswith("loaf"):
            for name, value in frame.f_locals.items():
                if isinstance(value, str) and secret in value:
                    found.append(f"{frame.f_code.co_name}:{name}")
        tb = tb.tb_next
    return found


@pytest.mark.filterwarnings("ignore::DeprecationWarning")  # httpx 0.28 deprecates verify=<str>
def test_env_key_not_in_traceback_locals(monkeypatch):
    # Crash reporters and debuggers record every traceback frame's locals.
    monkeypatch.setenv("LOAF_AGENT_PRIVATE_KEY", HARDHAT_KEY)
    # Failures after the key was resolved: a CA bundle that does not exist, a bad max_retries.
    kwargs: dict[str, Any]
    for failure, kwargs in (
        (OSError, {"verify": "/nonexistent/ca.pem"}),
        (ValueError, {"max_retries": "x"}),
    ):
        with pytest.raises(failure) as info:
            LoafClient(base_url=BASE, **kwargs)
        assert _key_locals(info.value, HARDHAT_KEY[2:]) == []

    with monkeypatch.context() as m:  # installed without eth-account
        m.setitem(sys.modules, "loaf.signing", None)
        with pytest.raises(loaf.LoafConfigError, match="eth-account") as info:
            LoafClient(base_url=BASE)
    assert _key_locals(info.value, HARDHAT_KEY[2:]) == []

    monkeypatch.setenv("LOAF_API_KEY", HARDHAT_KEY[2:])  # the same secret in both variables
    with pytest.raises(loaf.LoafConfigError, match="same value") as info:
        LoafClient(base_url=BASE)
    assert _key_locals(info.value, HARDHAT_KEY[2:]) == []
    monkeypatch.delenv("LOAF_API_KEY")

    near_miss = HARDHAT_KEY[:-1]  # one hex digit short: nearly the key itself
    monkeypatch.setenv("LOAF_AGENT_PRIVATE_KEY", near_miss)
    with pytest.raises(loaf.LoafConfigError) as info:
        LoafClient(base_url=BASE)
    assert _key_locals(info.value, near_miss[2:]) == []


def test_order_math_ignores_callers_decimal_context(exchange, trading_client):
    client = trading_client()
    with localcontext(Context(prec=6, traps=[Inexact, Rounded, InvalidOperation])):
        client.orders.limit_buy("opera", 10, 12345.67)
        client.orders.market_sell("opera", 2, reference_price=100.005, max_slippage_bps=50)
        assert loaf.worst_price(167.49, "BUY", 200) == 170.84
        with pytest.raises(loaf.LoafValidationError, match="at most 2 decimal places"):
            loaf.money.validate_price(1.234)
    limit, market = exchange.bodies("/orders")
    assert (limit["price"], market["price"]) == (12345.67, 99.5)
    assert recover(limit, limit["signature"]) == HARDHAT_ADDRESS
    assert recover(market, market["signature"]) == HARDHAT_ADDRESS


def test_client_without_eth_account():
    # Someone who pulls 0.4 without re-running `pip install -e .` can still read data, and
    # is told what to install once they configure a key. A fresh interpreter, so that
    # loaf.signing (and with it eth-account) is imported only when a key needs it.
    code = f"""
import sys
sys.modules["eth_account"] = None
import loaf
with loaf.LoafClient() as client:
    assert client.agent_address is None
try:
    loaf.LoafClient(agent_private_key={HARDHAT_KEY!r})
except loaf.LoafConfigError as exc:
    assert "pip install -e ." in str(exc), exc
    assert "eth_account" in str(exc), exc  # the real cause, in case it is not eth-account itself
else:
    raise AssertionError("no LoafConfigError")
assert "loaf.signing" not in sys.modules
"""
    subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, check=True)

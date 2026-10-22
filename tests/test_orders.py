"""Order placement against an in-memory exchange: the signed L2 bodies, what the
SDK reads for them (the property's market, your trading account, order numbers),
MARKET pricing, TP/SL legs, conditionals, the order life, retries, resubmit, what
a refusal invalidates, the order numbers a definite refusal gives back, and the
trading key's handling.

Signatures are checked with ``conftest.verify_order``, which rebuilds the signed
terms from the posted JSON as the exchange reads it.
"""

from __future__ import annotations

import copy
import json
import pickle
import re
import threading
import time
from decimal import Context, Decimal, Inexact, InvalidOperation, Rounded, localcontext
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

import loaf
from loaf import LoafClient, OrderOutcomeUnknownError, k1
from loaf._l2 import (
    AccountState,
    L2State,
    Market,
    TradingKey,
    market_of,
    read_account,
)
from loaf.constants import (
    DEFAULT_ORDER_TTL_MS,
    L2_BUFFER_MS,
    L2_ORDER_LIFE_MARGIN_MS,
    MAX_ORDER_TTL_MS,
)
from loaf.exceptions import error_from_response

from conftest import (
    BASE,
    KAT,
    KEYS,
    L2_PUBLIC_KEY,
    L2_SECRET,
    MARKET,
    NO_ACCOUNT,
    FakeExchange,
    property_detail,
    routes,
    secret_locals,
    verify_order,
)

# The SDK sends the keys in this fixed order.
ORDER_KEYS = (
    "ticker price quantity side type timeInForce l2ChainId l2AccountIndex l2MarketIndex "
    "l2OrderIndex l2Epoch l2FeeRateCap l2ExpiryMs l2BaseAssetId l2QuoteAssetId l2SigS l2SigE"
).split()
CONDITIONAL_KEYS = ORDER_KEYS[:5] + ["triggerPrice"] + ORDER_KEYS[5:]
LEG_KEYS = "triggerPrice price l2OrderIndex l2ExpiryMs l2SigS l2SigE".split()
# What every order on OPRA signs besides its own terms: MARKET and the account in KEYS.
CONTEXT = {
    "l2ChainId": 4242,
    "l2AccountIndex": 5,
    "l2MarketIndex": 2050,
    "l2Epoch": 2,
    "l2FeeRateCap": 7000,
    "l2BaseAssetId": 6,
    "l2QuoteAssetId": 3,
}

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
BALANCE_UNAVAILABLE = "The L2 balance is temporarily unavailable; try again in a moment"
NOT_TRADEABLE = "Property is not tradeable in the current market mode"
SWITCHING = "Market mode is switching; try again in a moment"
INDEX_USED = "l2 order_index: already used by the account; re-sign with the next index"
INDEX_NOT_RESERVED = (
    "l2 order_index: not inside a range reserved to the account; reserve one with "
    "ReserveOrderIndices"
)
INDEX_TOO_OLD = (
    "l2 order_index: the range was reserved more than K1's longest order life before the "
    "expiry; reserve a new one"
)
RESERVED_TOO_SOON = "l2 order_index: the account's last reservation is less than 1 s old"
STALE_EPOCH = "l2 epoch: not the account's current settle epoch"
WRONG_CHAIN = "l2 chain id: not the engine's L2 chain"
BAD_SIGNATURE = "l2 signature: the Schnorr signature does not verify"
K1_BOUND = "l2 k1 bound: quantity x price / 10 is not below 2^48 (K1's 48-bit fill quote)"
CLOSED = "Cannot send a request, as the client has been closed."

# Another account's trading key (K1's second known-answer key).
OTHER_KEY = "0x" + KAT["keys"][1]["pk"]
# K1's group order n: 40 bytes little-endian, not a secret key.
N = 0x7FFFFFFD80000007_7FFFFFF100000016_7FFFFFE6CFB80639_E8885C39D724A09C_E80FD996948BFFE1

HOUR_MS = 3_600_000
DAY_MS = 86_400_000


def error_reply(status: int, message: str, **kwargs: Any) -> httpx.Response:
    return httpx.Response(status, json={"error": message}, **kwargs)


def coded_reply(status: int, message: str, code: str) -> httpx.Response:
    return httpx.Response(status, json={"error": message, "code": code})


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


def counts(exchange: FakeExchange) -> list[int]:
    """The count each order-number reservation asked for, in order."""
    return [body["count"] for body in exchange.bodies("/l2/order-indices")]


def indices(exchange: FakeExchange, path: str = "/orders") -> list[int]:
    """The order number of each body posted to ``path``, in order."""
    return [body["l2OrderIndex"] for body in exchange.bodies(path)]


def numbers(body: dict) -> list[int]:
    """The order numbers a body signs: the order's, then its legs'."""
    legs = [body[leg]["l2OrderIndex"] for leg in ("tp", "sl") if leg in body]
    return [body["l2OrderIndex"]] + legs


def unsigned(body: dict) -> dict:
    return {k: v for k, v in body.items() if k not in ("l2ExpiryMs", "l2SigS", "l2SigE")}


def freeze(l2_clock: SimpleNamespace, at_ms: int | None = None) -> int:
    """Stop the SDK's order clock at ``at_ms`` (default: now), in unix ms; returns it."""
    now_ms = time.time_ns() // 1_000_000 if at_ms is None else at_ms
    l2_clock.time_ns = lambda: now_ms * 1_000_000
    return now_ms


def pool(client: LoafClient) -> tuple[L2State, Market, AccountState]:
    """The client's L2 state with the account read, as a first placement on OPRA leaves it."""
    l2 = client.orders._l2
    market = market_of(property_detail(), "OPRA")
    return l2, market, l2.account(market, L2_PUBLIC_KEY, "OPRA")


# --------------------------------------------------------------------------- #
# Body shape
# --------------------------------------------------------------------------- #


def test_limit_body_signed_no_bearer(exchange, trading_client):
    res = trading_client().orders.limit_buy("OPRA", quantity=10, price=167.49)
    request = exchange.requests[-1]
    body = json.loads(request.content)
    assert list(body) == ORDER_KEYS
    assert unsigned(body) == {
        "ticker": "OPRA",
        "price": 167.49,
        "quantity": 10.0,
        "side": "BUY",
        "type": "LIMIT",
        "timeInForce": "GTC",
        "l2OrderIndex": 4097,
        **CONTEXT,
    }
    assert all(re.fullmatch("[0-9a-f]{80}", body[k]) for k in ("l2SigS", "l2SigE"))
    # The signature authenticates a placement: the API key is set but not sent.
    assert "authorization" not in request.headers
    assert request.headers["content-type"] == "application/json"
    verify_order(body)
    assert res == {
        "success": True,
        "orderId": 1,
        "status": "OPEN",
        "quantityLeft": 10.0,
        "duplicate": False,
        "attachedLegIds": [],
    }


# --------------------------------------------------------------------------- #
# What an order reads: the property's market, the account, order numbers
# --------------------------------------------------------------------------- #


def test_context_reads(exchange, trading_client):
    orders = trading_client().orders
    orders.limit_buy("OPRA", 10, 167.49)
    assert routes(exchange.requests) == [
        "GET /trade/OPRA",
        "GET /l2/keys",
        "POST /l2/order-indices",
        "POST /orders",
    ]
    detail, keys, reserve, _ = exchange.requests
    assert "authorization" not in detail.headers  # a public read
    # The account: apiKeyIndex only (the SDK compares the key in force with its own).
    assert keys.headers["authorization"] == "Bearer testkey"
    assert keys.url.params.multi_items() == [("apiKeyIndex", "0")]
    assert reserve.headers["authorization"] == "Bearer testkey"
    assert json.loads(reserve.content) == {"count": 1}

    # The market and the account are kept; the next order only needs order numbers.
    orders.limit_buy("OPRA", 10, 167.49)
    assert routes(exchange.requests[4:]) == ["POST /l2/order-indices", "POST /orders"]
    assert counts(exchange) == [1, 2]
    assert indices(exchange) == [4097, 4098]


def test_reservations_double_up_to_64(exchange, trading_client):
    l2, market, account = pool(trading_client())
    reserved_for = []  # the order (1-based) each reservation was made for
    for order in range(1, 129):  # single orders: one number each
        before = len(exchange.posts("/l2/order-indices"))
        first, _, _ = l2.take(market, account, 1, DEFAULT_ORDER_TTL_MS)
        assert first == 4096 + order
        if len(exchange.posts("/l2/order-indices")) > before:
            reserved_for.append(order)
    # The n-th reservation comes at order 2**(n - 1), then one every 64 orders.
    assert counts(exchange) == [1, 2, 4, 8, 16, 32, 64, 64]
    assert reserved_for == [1, 2, 4, 8, 16, 32, 64, 128]


def test_legs_reserve_their_span(exchange, trading_client):
    orders = trading_client().orders
    for _ in range(2):
        orders.limit_buy("OPRA", 10, 100, tp_price=120, sl_price=90)
    assert counts(exchange) == [3, 6]
    assert [numbers(body) for body in exchange.bodies("/orders")] == [
        [4097, 4098, 4099],
        [4100, 4101, 4102],
    ]


def test_short_tail_is_abandoned(exchange, trading_client):
    orders = trading_client().orders
    orders.limit_buy("OPRA", 10, 167.49)
    orders.limit_buy("OPRA", 10, 167.49)  # reserves 2: one number left
    orders.limit_buy("OPRA", 10, 100, tp_price=120, sl_price=90)  # needs 3: a new block
    orders.limit_buy("OPRA", 10, 167.49)
    assert counts(exchange) == [1, 2, 4]
    assert [numbers(body) for body in exchange.bodies("/orders")] == [
        [4097],
        [4098],
        [4100, 4101, 4102],
        [4103],
    ]


def test_tickers_on_one_vault_share_the_account_and_the_block(exchange, trading_client):
    # Another market on the same vault (proxy and L2 chain): its own market index and asset.
    exchange.details["MUS"] = property_detail(
        "MUS", l2_market={**MARKET, "marketIndex": 2051, "baseAssetId": 7}
    )
    orders = trading_client().orders
    orders.limit_buy("OPRA", 10, 167.49)
    orders.limit_buy("OPRA", 10, 167.49)  # reserves 2: one number left
    sent = len(exchange.requests)
    orders.limit_buy("MUS", 10, 167.49)
    # Its own market read; the account and the order numbers are the vault's.
    assert routes(exchange.requests[sent:]) == ["GET /trade/MUS", "POST /orders"]
    assert len(exchange.gets("/l2/keys")) == 1
    body = exchange.bodies("/orders")[-1]
    assert (body["l2MarketIndex"], body["l2BaseAssetId"], body["l2OrderIndex"]) == (2051, 7, 4099)
    assert (body["l2AccountIndex"], body["l2Epoch"]) == (5, 2)


def test_an_account_on_another_vault_replaces_the_block(exchange, trading_client):
    # A market on another vault (the fake answers the same key there; the exchange would not).
    exchange.details["MUS"] = property_detail(
        "MUS", l2_market={**MARKET, "proxyAddress": "0x" + "22" * 20}
    )
    orders = trading_client().orders
    orders.limit_buy("OPRA", 10, 167.49)
    orders.limit_buy("OPRA", 10, 167.49)  # reserves 2: one number left on OPRA's vault
    for ticker in ("MUS", "OPRA"):
        sent = len(exchange.requests)
        orders.limit_buy(ticker, 10, 167.49)
        # The account is read on each vault, and no block is ever used on another.
        assert routes(exchange.requests[sent:]) == [
            f"GET /trade/{ticker}",
            "GET /l2/keys",
            "POST /l2/order-indices",
            "POST /orders",
        ]
    assert counts(exchange) == [1, 2, 4, 8]
    assert indices(exchange) == [4097, 4098, 4100, 4104]


def test_a_refused_read_on_another_vault_keeps_the_account_and_the_block(
    exchange, trading_client
):
    # A market on another vault, where another trading key is in force (a round's market).
    exchange.details["MUS"] = property_detail(
        "MUS", l2_market={**MARKET, "proxyAddress": "0x" + "22" * 20}
    )
    orders = trading_client().orders
    orders.limit_buy("OPRA", 10, 167.49)
    orders.limit_buy("OPRA", 10, 167.49)  # reserves 2: one number left on OPRA's vault
    exchange.keys = {**KEYS, "journaledKey": OTHER_KEY}  # what MUS's vault would answer
    for _ in range(2):  # nothing is kept for MUS's vault: each attempt reads the account
        sent = len(exchange.requests)
        with pytest.raises(loaf.LoafConfigError, match="not the trading key in force on MUS's"):
            orders.limit_buy("MUS", 10, 167.49)
        assert routes(exchange.requests[sent:]) == ["GET /trade/MUS", "GET /l2/keys"]
    exchange.keys = dict(KEYS)
    sent = len(exchange.requests)
    orders.limit_buy("OPRA", 10, 167.49)
    # OPRA's vault keeps its account and the block's last number.
    assert routes(exchange.requests[sent:]) == ["POST /orders"]
    assert indices(exchange) == [4097, 4098, 4099]


def test_unknown_ticker_404(exchange, trading_client):
    with pytest.raises(loaf.LoafNotFoundError, match="Property not found"):
        trading_client().orders.limit_buy("ATLS", 10, 167.49)
    assert routes(exchange.requests) == ["GET /trade/ATLS"]


# --------------------------------------------------------------------------- #
# Market refusals: nothing follows the property read
# --------------------------------------------------------------------------- #


def test_null_l2_market_refused_locally(exchange, trading_client):
    exchange.details["OPRA"] = property_detail(l2_market=None)
    orders = trading_client().orders
    for _ in range(2):
        with pytest.raises(
            loaf.LoafValidationError, match="OPRA has no L2 market right now"
        ) as info:
            orders.limit_buy("OPRA", 10, 167.49)
        assert info.value.status_code == 0
    assert routes(exchange.requests) == ["GET /trade/OPRA"] * 2  # never cached


def test_server_without_the_l2_exchange(exchange, trading_client):
    exchange.details["OPRA"] = property_detail(l2_market=...)
    with pytest.raises(loaf.LoafConfigError, match="LOAF_API_BASE_URL"):
        trading_client().orders.limit_buy("OPRA", 10, 167.49)
    assert routes(exchange.requests) == ["GET /trade/OPRA"]


@pytest.mark.parametrize(
    "l2_market, field",
    [
        ({**MARKET, "chainId": 0}, "chainId"),
        ({**MARKET, "chainId": True}, "chainId"),
        ({**MARKET, "chainId": "421614"}, "chainId"),
        ({**MARKET, "proxyAddress": MARKET["proxyAddress"][:-1]}, "proxyAddress"),
        ({**MARKET, "l2ChainId": 0}, "l2ChainId"),
        ({**MARKET, "l2ChainId": 2130706433}, "l2ChainId"),
        ({**MARKET, "marketIndex": 4096}, "marketIndex"),
        ({k: v for k, v in MARKET.items() if k != "marketIndex"}, "marketIndex"),
        ({**MARKET, "baseAssetId": 0}, "baseAssetId"),
        ({**MARKET, "baseAssetId": 63}, "baseAssetId"),
        ({**MARKET, "quoteAssetId": 0}, "quoteAssetId"),
        ({**MARKET, "quoteAssetId": 63}, "quoteAssetId"),
        ({**MARKET, "quoteAssetId": 6}, "quoteAssetId"),  # the base asset's id
        (MARKET["proxyAddress"], "l2Market"),
    ],
)
def test_unusable_l2_market(exchange, trading_client, l2_market, field):
    exchange.details["OPRA"] = property_detail(l2_market=l2_market)
    message = f"GET /trade/OPRA answered an unusable property.l2Market ({field})"
    with pytest.raises(loaf.LoafServerError, match=re.escape(message)):
        trading_client().orders.limit_buy("OPRA", 10, 167.49)
    assert routes(exchange.requests) == ["GET /trade/OPRA"]


@pytest.mark.parametrize(
    "competition, mode, l2_market, why",
    [
        (False, True, MARKET, "a competition round is on, and only its market trades"),
        (True, False, MARKET, "it is a competition market, which trades only during its round"),
        # Another round's market: during a round only the round's own has an l2Market.
        (True, True, None, "a competition round is on, and only its market trades"),
    ],
)
def test_market_mode_mismatch_refused_locally(
    exchange, trading_client, competition, mode, l2_market, why
):
    exchange.details["OPRA"] = property_detail(
        competition=competition, mode=mode, l2_market=l2_market
    )
    with pytest.raises(loaf.VenueError) as info:
        trading_client().orders.limit_buy("OPRA", 10, 167.49)
    assert (info.value.status_code, info.value.code) == (0, "NOT_TRADEABLE_IN_MODE")
    assert info.value.message == f"OPRA is not tradeable in the current market mode: {why}"
    # The account is never read for a market the current mode does not trade.
    assert routes(exchange.requests) == ["GET /trade/OPRA"]


def test_local_venue_error_drops_the_cached_market_and_account(exchange, trading_client):
    orders = trading_client().orders
    orders.limit_buy("OPRA", 10, 167.49)  # caches the market and the account
    exchange.details["OPRA"] = property_detail(mode=True)  # a round started
    with pytest.raises(loaf.VenueError):
        orders.market_buy("OPRA", 1)  # a MARKET order reads the property every time
    exchange.details["OPRA"] = property_detail()  # the round ended
    sent = len(exchange.requests)
    orders.limit_buy("OPRA", 10, 167.49)
    assert routes(exchange.requests[sent:]) == [
        "GET /trade/OPRA",
        "GET /l2/keys",
        "POST /l2/order-indices",
        "POST /orders",
    ]


# --------------------------------------------------------------------------- #
# Account refusals: nothing kept, reserved or posted
# --------------------------------------------------------------------------- #

NOT_ENABLED = (
    "Trading is not enabled on your account on OPRA's market (no trading key is in force): open "
    "OPRA on the Loaf web app's Trade page, enable trading there and wait for it to take effect, "
    "then set LOAF_SECRET_KEY to the key in force: the web app's Show trading key (README §2), or "
    "on a competition round's market python -m loaf.secret_key --ticker OPRA (README, "
    "Competition rounds)."
)
NOT_IN_FORCE = "LOAF_SECRET_KEY is not the trading key in force on OPRA's market"


@pytest.mark.parametrize(
    "keys, error, message",
    [
        (
            None,
            loaf.LoafConfigError,
            "You have no trading account on OPRA's market yet: on a real market, deposit",
        ),
        ({"journaledKey": None}, loaf.LoafConfigError, NOT_ENABLED),
        ({"epoch": None}, loaf.LoafConfigError, NOT_ENABLED),
        ({"journaledKey": "0x" + "0" * 80}, loaf.LoafConfigError, NOT_ENABLED),  # disabled on L1
        ({"journaledKey": OTHER_KEY}, loaf.LoafConfigError, NOT_IN_FORCE),
        ({"journaledKey": OTHER_KEY, "pending": True}, loaf.LoafConfigError, NOT_IN_FORCE),
        ({"accountIndex": -1}, loaf.LoafServerError, "unusable accountIndex"),
        ({"accountIndex": 2**48}, loaf.LoafServerError, "unusable accountIndex"),
        ({"accountIndex": True}, loaf.LoafServerError, "unusable accountIndex"),
        ({"accountIndex": None}, loaf.LoafServerError, "unusable accountIndex"),
        ({"epoch": "2"}, loaf.LoafServerError, "unusable epoch"),
        ({"epoch": 2**48}, loaf.LoafServerError, "unusable epoch"),
        ({"journaledKey": L2_PUBLIC_KEY[:-1]}, loaf.LoafServerError, "unusable journaledKey"),
        (
            {"journaledKey": "0x" + L2_PUBLIC_KEY[2:].upper()},
            loaf.LoafServerError,
            "unusable journaledKey",
        ),
    ],
)
def test_account_refusals(exchange, trading_client, keys, error, message):
    exchange.keys = None if keys is None else {**KEYS, **keys}
    orders = trading_client().orders
    for _ in range(2):  # nothing is kept: the next order reads the account again
        with pytest.raises(error, match=re.escape(message)) as info:
            orders.limit_buy("OPRA", 10, 167.49)
        assert type(info.value) is error
    assert routes(exchange.requests) == ["GET /trade/OPRA", "GET /l2/keys"] * 2


def test_no_seat_on_a_rounds_market(exchange, trading_client):
    # Only the round's participants have an account on its market: the account read refuses
    # before anything is reserved or sent.
    exchange.details["ARNA"] = property_detail("ARNA", competition=True, mode=True)
    exchange.keys = None
    with pytest.raises(loaf.LoafConfigError, match=re.escape("queue_position().participant")):
        trading_client().orders.limit_buy("ARNA", 10, 167.49)
    assert routes(exchange.requests) == ["GET /trade/ARNA", "GET /l2/keys"]


def test_key_not_in_force_names_the_remedy(exchange, trading_client):
    exchange.keys = {**KEYS, "journaledKey": OTHER_KEY}
    with pytest.raises(loaf.LoafConfigError) as info:
        trading_client().orders.limit_buy("OPRA", 10, 167.49)
    assert "the web app's Show trading key" in str(info.value)
    assert "python -m loaf.secret_key --ticker OPRA" in str(info.value)
    assert "re-arm your stops and takes" in str(info.value)


def test_pending_is_not_read(exchange, trading_client):
    # The SDK compares the key in force itself: "pending" decides nothing.
    exchange.keys = {**KEYS, "pending": True}
    assert trading_client().orders.limit_buy("OPRA", 10, 167.49).orderId == 1


def test_other_keys_404_propagates(exchange, trading_client):
    exchange.queue("/l2/keys", error_reply(404, "Not found"))
    orders = trading_client().orders
    with pytest.raises(loaf.LoafNotFoundError, match="Not found") as info:
        orders.limit_buy("OPRA", 10, 167.49)
    assert type(info.value) is loaf.LoafNotFoundError
    assert orders.limit_buy("OPRA", 10, 167.49).orderId == 1
    assert len(exchange.gets("/l2/keys")) == 2


def test_read_account(exchange, trading_client):
    client = trading_client()
    assert read_account(client, "OPRA") == (5, 2, L2_PUBLIC_KEY)
    # The caller compares the key in force with its own.
    exchange.keys = {**KEYS, "journaledKey": OTHER_KEY}
    assert read_account(client, "OPRA") == (5, 2, OTHER_KEY)
    for keys, error, message in (
        (None, loaf.LoafConfigError, "You have no trading account on OPRA's market yet"),
        ({**KEYS, "epoch": None}, loaf.LoafConfigError, NOT_ENABLED),
        ({**KEYS, "journaledKey": "0x" + "0" * 80}, loaf.LoafConfigError, NOT_ENABLED),
        ({**KEYS, "accountIndex": True}, loaf.LoafServerError, "unusable accountIndex"),
    ):
        exchange.keys = keys
        with pytest.raises(error, match=re.escape(message)):
            read_account(client, "OPRA")
    assert [r.url.params.multi_items() for r in exchange.gets("/l2/keys")] == [
        [("apiKeyIndex", "0")]
    ] * 6


# --------------------------------------------------------------------------- #
# Order-number reservations
# --------------------------------------------------------------------------- #


def test_reservation_less_than_a_second_old_waits_once(exchange, trading_client, l2_clock):
    exchange.queue("/l2/order-indices", error_reply(422, RESERVED_TOO_SOON))
    assert trading_client().orders.limit_buy("OPRA", 10, 167.49).orderId == 1
    assert l2_clock.slept == [1.0]
    assert counts(exchange) == [1, 1]


def test_reservation_too_soon_twice_raises(exchange, trading_client, l2_clock):
    exchange.queue(
        "/l2/order-indices",
        error_reply(422, RESERVED_TOO_SOON),
        error_reply(422, RESERVED_TOO_SOON),
    )
    with pytest.raises(loaf.LoafAPIError) as info:
        trading_client().orders.limit_buy("OPRA", 10, 167.49)
    assert (info.value.status_code, info.value.message) == (422, RESERVED_TOO_SOON)
    assert l2_clock.slept == [1.0]
    assert counts(exchange) == [1, 1]
    assert exchange.posts("/orders") == []


@pytest.mark.parametrize(
    "answer",
    [
        {"accountIndex": True},
        {"firstIndex": 0},
        {"count": 0},
        {"firstIndex": 2**40 - 1, "count": 2},  # its last index does not fit 40 bits
        {"reservedAtMs": 0},
        {"reservedAtMs": 2**53},
        {"reservedAtMs": True},
    ],
)
def test_unusable_reservation(exchange, trading_client, answer):
    now_ms = time.time_ns() // 1_000_000
    usable = {"accountIndex": 5, "firstIndex": 4097, "count": 1, "reservedAtMs": now_ms}
    exchange.queue("/l2/order-indices", httpx.Response(201, json={**usable, **answer}))
    orders = trading_client().orders
    with pytest.raises(loaf.LoafServerError) as info:
        orders.limit_buy("OPRA", 10, 167.49)
    assert info.value.status_code == 201
    assert info.value.message.startswith(
        "POST /l2/order-indices answered a range no order can be signed in: "
    )
    assert exchange.posts("/orders") == []
    sent = len(exchange.requests)
    orders.limit_buy("OPRA", 10, 167.49)
    # The account is kept (a reservation naming another account is a switch: test_venue_window).
    assert routes(exchange.requests[sent:]) == ["POST /l2/order-indices", "POST /orders"]


@pytest.mark.parametrize(
    "path, message",
    [
        ("/trade/OPRA", "GET /trade/OPRA answered no property"),
        ("/l2/keys", "GET /l2/keys answered an unusable accountIndex"),
        ("/l2/order-indices", "POST /l2/order-indices answered a range no order can be signed in"),
    ],
)
def test_a_web_page_answering_a_read_is_refused(exchange, trading_client, path, message):
    # A host answering with a web page (the web app's, an edge challenge): read as text.
    exchange.queue(path, httpx.Response(200, html="<!doctype html><html>app</html>"))
    with pytest.raises(loaf.LoafServerError, match=re.escape(message)):
        trading_client().orders.limit_buy("OPRA", 10, 167.49)
    assert exchange.posts("/orders") == []


def test_block_too_old_for_the_expiry_is_replaced(exchange, trading_client, l2_clock):
    now = freeze(l2_clock)
    exchange.reserved_at_ms = now
    orders = trading_client().orders
    orders.limit_buy("OPRA", 10, 167.49)
    orders.limit_buy("OPRA", 10, 167.49)  # reserves 2: one number left
    # 42 days on, a 7-day order expires 50 days after that block's reservation: later than
    # the exchange admits on it (K1's longest order life).
    later = now + 42 * DAY_MS
    freeze(l2_clock, later)
    exchange.reserved_at_ms = later
    orders.limit_buy("OPRA", 10, 167.49)
    assert counts(exchange) == [1, 2, 4]
    assert indices(exchange) == [4097, 4098, 4100]


def test_a_block_serves_expiries_up_to_one_margin_short_of_k1s_longest_life(
    exchange, trading_client, l2_clock
):
    now = freeze(l2_clock)
    orders = trading_client().orders
    # A max-life order expires two margins short of K1's longest life: a range reserved one
    # margin ago (this machine's clock may be that far ahead) still admits it...
    exchange.reserved_at_ms = now - L2_ORDER_LIFE_MARGIN_MS
    orders.limit_buy("OPRA", 10, 167.49, ttl_ms=MAX_ORDER_TTL_MS)
    # ...one reserved a millisecond earlier does not.
    exchange.reserved_at_ms -= 1
    with pytest.raises(loaf.LoafConfigError, match="clock is hours ahead"):
        orders.limit_buy("OPRA", 10, 167.49, ttl_ms=MAX_ORDER_TTL_MS)
    assert counts(exchange) == [1, 2]
    assert indices(exchange) == [4097]


def test_clock_hours_ahead(exchange, trading_client, l2_clock):
    now = freeze(l2_clock)
    # This machine's clock is 50 hours ahead of the exchange's: a max-life order signed now
    # would expire later than any range the exchange reserves now admits.
    exchange.reserved_at_ms = now - 50 * HOUR_MS
    orders = trading_client().orders
    with pytest.raises(loaf.LoafConfigError, match="clock is hours ahead") as info:
        orders.limit_buy("OPRA", 10, 100, tp_price=120, sl_price=90, ttl_ms=MAX_ORDER_TTL_MS)
    assert f"expire at {now + L2_BUFFER_MS + MAX_ORDER_TTL_MS} (unix ms)" in str(info.value)
    assert exchange.posts("/orders") == []
    # The block is kept: it serves shorter lives.
    orders.limit_buy("OPRA", 10, 167.49, ttl_ms=DAY_MS)
    assert counts(exchange) == [3]
    assert indices(exchange) == [4097]
    # Nothing estimates the exchange's clock: a block with room is too old for another max-life
    # order, so it reserves again, and the fresh range is refused the same way.
    with pytest.raises(loaf.LoafConfigError, match="clock is hours ahead"):
        orders.limit_buy("OPRA", 10, 167.49, ttl_ms=MAX_ORDER_TTL_MS)
    assert counts(exchange) == [3, 6]
    assert len(exchange.posts("/orders")) == 1


@pytest.mark.parametrize(
    "status, message, error",
    [
        (
            422,
            "l2 order_index: the account would hold more than 65536 reserved indices no order uses",
            loaf.LoafAPIError,
        ),
        (
            422,
            "l2 order_index: the counter came round too fast (the lap trail); try again later",
            loaf.LoafAPIError,
        ),
        (
            503,
            "l2 order indices: the engine did not confirm the reservation; reserve again",
            loaf.LoafServiceUnavailableError,
        ),
        (404, "Not found", loaf.LoafNotFoundError),  # only the no-account 404 is a switch
    ],
)
def test_other_reservation_refusals_propagate(
    exchange, trading_client, l2_clock, status, message, error
):
    exchange.queue("/l2/order-indices", error_reply(status, message))
    with pytest.raises(error) as info:
        trading_client().orders.limit_buy("OPRA", 10, 167.49)
    assert (type(info.value), info.value.message) == (error, message)
    assert l2_clock.slept == [] and counts(exchange) == [1]  # no wait, no second reservation
    assert exchange.posts("/orders") == []


def test_venue_refusal_of_a_reservation(exchange, trading_client):
    participants_only = "Not a competition participant for the current round"
    exchange.queue(
        "/l2/order-indices", coded_reply(403, participants_only, "NOT_COMPETITION_PARTICIPANT")
    )
    orders = trading_client().orders
    with pytest.raises(loaf.VenueError) as info:
        orders.limit_buy("OPRA", 10, 167.49)
    assert (info.value.status_code, info.value.code) == (403, "NOT_COMPETITION_PARTICIPANT")
    assert exchange.posts("/orders") == []
    sent = len(exchange.requests)
    orders.limit_buy("OPRA", 10, 167.49)
    assert routes(exchange.requests[sent:]) == [
        "GET /trade/OPRA",
        "GET /l2/keys",
        "POST /l2/order-indices",
        "POST /orders",
    ]


def test_expiry_read_after_the_reservation(exchange, l2_clock):
    now = freeze(l2_clock)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/l2/order-indices":  # the reservation takes ten minutes
            freeze(l2_clock, now + 600_000)
        return exchange.handler(request)

    http = httpx.Client(transport=httpx.MockTransport(handler), base_url=BASE)
    client = LoafClient(api_key="testkey", secret_key=L2_SECRET, base_url=BASE, http_client=http)
    client.orders.limit_buy("OPRA", 10, 167.49)
    (body,) = exchange.bodies("/orders")
    assert body["l2ExpiryMs"] == now + 600_000 + L2_BUFFER_MS + DEFAULT_ORDER_TTL_MS


# --------------------------------------------------------------------------- #
# Concurrency
# --------------------------------------------------------------------------- #


def test_concurrent_placements_share_one_account_read_and_one_pool(exchange, trading_client):
    orders = trading_client().orders
    start = threading.Barrier(8)
    errors: list[BaseException] = []

    def place(quantity: int) -> None:
        try:
            start.wait()
            for _ in range(10):
                orders.limit_buy("OPRA", quantity, 167.49)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=place, args=(n,)) for n in range(1, 9)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert len(exchange.gets("/l2/keys")) == 1
    assert counts(exchange) == [1, 2, 4, 8, 16, 32, 64]
    # 80 distinct order numbers, every one inside a reserved range.
    assert sorted(indices(exchange)) == list(range(4097, 4097 + 80))
    bodies = exchange.bodies("/orders")
    for quantity in range(1, 9):  # one body per thread (each verification costs milliseconds)
        verify_order(next(body for body in bodies if body["quantity"] == quantity))


# --------------------------------------------------------------------------- #
# Take-profit / stop-loss legs
# --------------------------------------------------------------------------- #


def test_limit_buy_with_legs(exchange, trading_client):
    orders = trading_client().orders
    res = orders.limit_buy("OPRA", 10, 100, tp_price=120, sl_price=90)
    body = exchange.bodies("/orders")[-1]
    assert list(body) == ORDER_KEYS + ["tp", "sl"]
    assert list(body["tp"]) == LEG_KEYS and list(body["sl"]) == LEG_KEYS
    # The triggers go as given; each leg signs a SELL 2% below its trigger, with the next
    # order numbers and the parent's expiry.
    assert (body["tp"]["triggerPrice"], body["tp"]["price"]) == (120.0, 117.6)
    assert (body["sl"]["triggerPrice"], body["sl"]["price"]) == (90.0, 88.2)
    assert numbers(body) == [4097, 4098, 4099]
    assert body["tp"]["l2ExpiryMs"] == body["sl"]["l2ExpiryMs"] == body["l2ExpiryMs"]
    verify_order(body)
    for leg in ("tp", "sl"):
        verify_order(body, leg)
    assert res.attachedLegIds == [2, 3]

    res = orders.limit_buy("OPRA", 10, 100, sl_price=90)
    body = exchange.bodies("/orders")[-1]
    assert list(body) == ORDER_KEYS + ["sl"]
    assert numbers(body) == [body["l2OrderIndex"], body["l2OrderIndex"] + 1]
    assert res.attachedLegIds == [res.orderId + 1]

    orders.limit_buy("OPRA", 10, 100, tp_price=120, sl_price=90, leg_max_slippage_bps=500)
    body = exchange.bodies("/orders")[-1]
    assert (body["tp"]["price"], body["sl"]["price"]) == (114.0, 85.5)
    verify_order(body, "sl")

    orders.limit_buy("OPRA", 10, 100)
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
        ({"sl_price": 89.999}, "sl_price must have at most 2"),
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


# --------------------------------------------------------------------------- #
# Conditional (stop / take) orders
# --------------------------------------------------------------------------- #


def test_conditional_market_price_derived(exchange, trading_client):
    orders = trading_client().orders
    res = orders.create_conditional("OPRA", "SELL", 5, type="STOP_MARKET", trigger_price=90)
    assert res == {"success": True, "orderId": 1}
    body = exchange.bodies("/orders/conditional")[-1]
    assert list(body) == CONDITIONAL_KEYS
    assert unsigned(body) == {
        "ticker": "OPRA",
        "price": 88.2,  # books at up to 2% below the trigger
        "quantity": 5.0,
        "side": "SELL",
        "type": "STOP_MARKET",
        "triggerPrice": 90.0,
        "timeInForce": "GTC",
        "l2OrderIndex": 4097,
        **CONTEXT,
    }
    verify_order(body)
    assert exchange.posts("/orders") == []

    orders.create_conditional("OPRA", "BUY", 2, type="STOP_MARKET", trigger_price=130)
    body = exchange.bodies("/orders/conditional")[-1]
    assert body["price"] == 132.6  # a BUY books above its trigger
    verify_order(body)

    orders.stop_loss("OPRA", 5, 90)
    stop = exchange.bodies("/orders/conditional")[-1]
    assert (stop["type"], stop["side"], stop["triggerPrice"], stop["price"]) == (
        "STOP_MARKET",
        "SELL",
        90.0,
        88.2,
    )

    orders.take_profit("OPRA", 5, 120, max_slippage_bps=100)
    take = exchange.bodies("/orders/conditional")[-1]
    assert (take["type"], take["side"]) == ("TAKE_MARKET", "SELL")
    assert (take["triggerPrice"], take["price"]) == (120.0, 118.8)  # 1% below the trigger
    assert indices(exchange, "/orders/conditional") == [4097, 4098, 4099, 4100]  # one each


def test_conditional_limit(exchange, trading_client):
    trading_client().orders.create_conditional(
        "OPRA", "BUY", 2, type="TAKE_LIMIT", trigger_price=70, price=70.5
    )
    (body,) = exchange.bodies("/orders/conditional")
    assert (body["type"], body["price"], body["triggerPrice"]) == ("TAKE_LIMIT", 70.5, 70.0)
    verify_order(body)


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
        (
            {"type": "STOP_LIMIT", "trigger_price": 90, "price": 89.555},
            ": price must have at most 2",
        ),
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


def test_conditional_order_number_conflict_drops_the_block(exchange, trading_client):
    orders = trading_client().orders
    orders.stop_loss("OPRA", 5, 90)
    orders.stop_loss("OPRA", 5, 90)  # reserves 2: one number left
    # The only order-number refusal a conditional can get: another row holds the number.
    exchange.queue("/orders/conditional", error_reply(409, INDEX_USED))
    with pytest.raises(loaf.LoafConflictError, match=re.escape(INDEX_USED)):
        orders.stop_loss("OPRA", 5, 90)
    orders.stop_loss("OPRA", 5, 90)
    assert counts(exchange) == [1, 2, 4]
    assert indices(exchange, "/orders/conditional") == [4097, 4098, 4099, 4100]


def test_conditional_balance_unavailable_is_not_placed(exchange, trading_client):
    exchange.queue(
        "/orders/conditional", *(error_reply(503, BALANCE_UNAVAILABLE) for _ in range(4))
    )
    orders = trading_client().orders
    with pytest.raises(loaf.LoafServiceUnavailableError) as info:
        orders.stop_loss("OPRA", 5, 90)
    assert type(info.value) is loaf.LoafServiceUnavailableError  # definite: not unknown
    posts = exchange.posts("/orders/conditional")
    assert len(posts) == 4 and len(set(posts)) == 1
    orders.stop_loss("OPRA", 5, 90)
    assert indices(exchange, "/orders/conditional") == [4097] * 5  # its number given back


# --------------------------------------------------------------------------- #
# Order life
# --------------------------------------------------------------------------- #


def test_expiry_is_now_plus_buffer_plus_ttl(exchange, trading_client, l2_clock):
    now = freeze(l2_clock)
    orders = trading_client().orders
    orders.limit_buy("OPRA", 10, 100, tp_price=120, sl_price=90)
    body = exchange.bodies("/orders")[-1]
    expiry = now + 86_400_000 + 604_800_000  # 24 h of buffer, then 7 days of life
    assert body["l2ExpiryMs"] == body["tp"]["l2ExpiryMs"] == body["sl"]["l2ExpiryMs"] == expiry
    for ttl in (60_000, MAX_ORDER_TTL_MS):
        orders.limit_buy("OPRA", 10, 167.49, ttl_ms=ttl)
        assert exchange.bodies("/orders")[-1]["l2ExpiryMs"] == now + L2_BUFFER_MS + ttl


@pytest.mark.parametrize("ttl", [59_999, MAX_ORDER_TTL_MS + 1, 1.5, True, "60000", Decimal(60000)])
def test_ttl_refused_locally(exchange, trading_client, ttl):
    with pytest.raises(loaf.LoafValidationError) as info:
        trading_client().orders.create("OPRA", "BUY", 1, price=100, ttl_ms=ttl)
    assert info.value.status_code == 0
    assert info.value.message == (
        "ttl_ms must be a whole number of milliseconds from 60,000 (one minute) to "
        f"4,141,458,432 (about 47.9 days) (got {ttl!r})"
    )
    assert exchange.requests == []


@pytest.mark.parametrize(
    "place",
    [
        lambda orders, ttl: orders.limit_buy("OPRA", 1, 100, ttl_ms=ttl),
        lambda orders, ttl: orders.limit_sell("OPRA", 1, 100, ttl_ms=ttl),
        lambda orders, ttl: orders.market_buy("OPRA", 1, ttl_ms=ttl),
        lambda orders, ttl: orders.market_sell("OPRA", 1, ttl_ms=ttl),
        lambda orders, ttl: orders.create_conditional(
            "OPRA", "SELL", 1, type="STOP_MARKET", trigger_price=90, ttl_ms=ttl
        ),
        lambda orders, ttl: orders.stop_loss("OPRA", 1, 90, ttl_ms=ttl),
        lambda orders, ttl: orders.take_profit("OPRA", 1, 120, ttl_ms=ttl),
    ],
)
def test_every_placement_takes_ttl(exchange, trading_client, l2_clock, place):
    now = freeze(l2_clock)
    orders = trading_client().orders
    with pytest.raises(loaf.LoafValidationError, match="ttl_ms must be a whole number") as info:
        place(orders, 59_999)
    assert info.value.status_code == 0
    assert exchange.requests == []
    place(orders, HOUR_MS)
    assert json.loads(exchange.requests[-1].content)["l2ExpiryMs"] == now + L2_BUFFER_MS + HOUR_MS


# --------------------------------------------------------------------------- #
# MARKET orders
# --------------------------------------------------------------------------- #


def test_market_orders_read_the_reference(exchange, trading_client):
    orders = trading_client().orders
    orders.market_buy("OPRA", 2)  # reference 160, signs 160 x 1.02
    body = exchange.bodies("/orders")[-1]
    assert (body["type"], body["price"], body["quantity"]) == ("MARKET", 163.2, 2.0)
    orders.market_sell("OPRA", 2)  # 160 x 0.98
    body = exchange.bodies("/orders")[-1]
    assert (body["side"], body["price"]) == ("SELL", 156.8)
    assert len(exchange.gets("/trade/OPRA")) == 2  # the reference is read for every order
    assert len(exchange.gets("/l2/keys")) == 1


def test_market_reference_price_skips_the_read(exchange, trading_client):
    orders = trading_client().orders
    orders.limit_buy("OPRA", 10, 167.49)  # caches the market and the account
    orders.market_sell("OPRA", 2, reference_price=100.005, max_slippage_bps=50)
    assert exchange.bodies("/orders")[-1]["price"] == 99.51  # 99.504975, up to the cent
    orders.market_buy("OPRA", 2, reference_price=100.005, max_slippage_bps=50)
    assert exchange.bodies("/orders")[-1]["price"] == 100.5  # 100.505025, down to the cent
    assert len(exchange.gets("/trade/OPRA")) == 1


def test_market_reference_price_still_reads_an_unknown_market(exchange, trading_client):
    trading_client().orders.market_buy("OPRA", 2, reference_price=100)
    assert routes(exchange.requests) == [
        "GET /trade/OPRA",
        "GET /l2/keys",
        "POST /l2/order-indices",
        "POST /orders",
    ]
    assert exchange.bodies("/orders")[0]["price"] == 102.0


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
        # Off the grid: refused, never rounded (": price" is the order's own price).
        ("limit_buy", ("OPRA", 10, 167.495), {}, ": price must have at most 2"),
        ("limit_buy", ("OPRA", 1.24, 100), {}, "quantity must have at most 1"),
        ("stop_loss", ("OPRA", 1.24, 90), {}, "quantity must have at most 1"),
        ("create", ("OPRA", "BUY", 1), {"type": "STOP_MARKET", "price": 1}, "Unknown order type"),
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
        property_detail(market_price=0),  # the exchange has no reference yet
        {
            **property_detail(),
            "propertyList": [{"ticker": "MUS", "tokenName": "Musgrave", "marketPrice": 50}],
        },
        # Matched on the ticker: an entry whose display name happens to read OPRA is not it.
        {
            **property_detail(),
            "propertyList": [{"ticker": "MUS", "tokenName": "OPRA", "marketPrice": 50}],
        },
    ],
)
def test_market_without_reference_refused(exchange, trading_client, detail):
    exchange.details["OPRA"] = detail
    with pytest.raises(loaf.LoafValidationError, match="No market reference price") as info:
        trading_client().orders.market_buy("OPRA", 1)
    assert info.value.status_code == 0
    assert exchange.posts("/l2/order-indices") == [] and exchange.posts("/orders") == []


# --------------------------------------------------------------------------- #
# Scaling and bounds
# --------------------------------------------------------------------------- #


def test_wire_normalization(exchange, trading_client):
    orders = trading_client().orders
    orders.limit_buy("OPRA", Decimal("2.0"), Decimal("167.490"))
    (content,) = exchange.posts("/orders")
    body = json.loads(content)
    assert (body["price"], body["quantity"]) == (167.49, 2.0)
    assert b'"price":167.49,' in content and b'"quantity":2.0,' in content
    verify_order(body)

    # 2.01 * 1000 == 2009.9999999999998 in floating point: the scaling must stay exact.
    orders.limit_buy("OPRA", 10, 2.01)
    body = exchange.bodies("/orders")[-1]
    assert body["price"] == 2.01
    verify_order(body)


def test_largest_price_and_quantity(exchange, trading_client):
    orders = trading_client().orders
    orders.limit_buy("OPRA", 0.1, 4294967.29)
    body = exchange.bodies("/orders")[-1]
    assert (body["price"], body["quantity"]) == (4294967.29, 0.1)
    verify_order(body)
    orders.limit_sell("OPRA", 28147497671065.5, 0.01)
    body = exchange.bodies("/orders")[-1]
    assert (body["price"], body["quantity"]) == (0.01, 28147497671065.5)
    verify_order(body)

    sent = len(exchange.requests)
    with pytest.raises(
        loaf.LoafValidationError, match="price must be at most 4,294,967.29"
    ) as info:
        orders.limit_buy("OPRA", 0.1, 4294967.3)
    assert info.value.status_code == 0
    assert len(exchange.requests) == sent


def test_k1_bound_refused_locally_and_its_number_given_back(exchange, trading_client):
    orders = trading_client().orders
    # Each fits its width, but the order's whole value does not fit K1's 48-bit fill quote.
    with pytest.raises(loaf.LoafValidationError) as info:
        orders.limit_sell("OPRA", 28147497671065.5, 1.00)
    assert (info.value.status_code, info.value.message) == (0, K1_BOUND)
    assert info.value.__cause__ is None and info.value.__suppress_context__
    assert exchange.posts("/orders") == []
    orders.limit_buy("OPRA", 10, 167.49)
    assert indices(exchange) == [4097]  # the number the refusal took
    assert counts(exchange) == [1]


def test_order_math_ignores_callers_decimal_context(exchange, trading_client):
    orders = trading_client().orders
    with localcontext(Context(prec=6, traps=[Inexact, Rounded, InvalidOperation])):
        orders.limit_buy("OPRA", 10, 12345.67)
        orders.market_sell("OPRA", 2, reference_price=100.005, max_slippage_bps=50)
        assert loaf.worst_price(167.49, "BUY", 200) == 170.83
        with pytest.raises(loaf.LoafValidationError, match="at most 2 decimal places"):
            loaf.money.validate_price(1.234)
    limit, market = exchange.bodies("/orders")
    assert (limit["price"], market["price"]) == (12345.67, 99.51)
    verify_order(limit)
    verify_order(market)


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
    # Checked before anything that could fail later (here, a max_retries that is no number).
    with pytest.raises(loaf.LoafConfigError) as info:
        LoafClient(base_url=BASE, max_retries="x")  # type: ignore[arg-type]
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


def test_placing_needs_both_keys(exchange, trading_client):
    keyless = trading_client(secret_key=None)
    assert keyless.public_key is None
    for place in (
        lambda orders: orders.limit_buy("OPRA", 10, 167.49),
        lambda orders: orders.stop_loss("OPRA", 5, 90),
    ):
        with pytest.raises(loaf.LoafConfigError, match="LOAF_SECRET_KEY") as info:
            place(keyless.orders)
        assert "the web app's Show trading key gives it" in str(info.value)
    # The API key reads the account and reserves order numbers (and cancels).
    readless = trading_client(api_key=None)
    with pytest.raises(loaf.LoafConfigError, match="LOAF_API_KEY"):
        readless.orders.limit_buy("OPRA", 10, 167.49)
    with pytest.raises(loaf.LoafConfigError):
        readless.orders.cancel(1)
    assert exchange.requests == []


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


@pytest.mark.parametrize(
    "message",
    [
        NOT_PLACED,
        "Trading is temporarily unavailable for this property. Your order was not placed — "
        "please try again in a moment.",
        "Trading temporarily unavailable. Please try again shortly.",  # the eligibility check
        "Trading is temporarily unavailable; your order was not placed, try again in a moment",
        "The market mode could not be read; your order was not placed, try again in a moment",
        "l2 account: the trading account could not be read; nothing was done, try again in a "
        "moment",
        "l2 signature: the order signature could not be checked; your order was not placed, "
        "try again",
    ],
)
def test_503_not_placed_exhausted(exchange, trading_client, message):
    orders = trading_client(max_retries=2).orders
    exchange.queue("/orders", *(error_reply(503, message) for _ in range(3)))
    with pytest.raises(loaf.LoafServiceUnavailableError) as info:
        orders.limit_buy("OPRA", 10, 167.49)
    assert type(info.value) is loaf.LoafServiceUnavailableError
    assert info.value.message == message
    posts = exchange.posts("/orders")
    assert len(posts) == 3 and len(set(posts)) == 1
    # A transient failure shows nothing stale, "l2 account:" / "l2 signature:" texts included.
    sent = len(exchange.requests)
    orders.limit_buy("OPRA", 10, 167.49)
    assert routes(exchange.requests[sent:]) == ["POST /orders"]


def test_venue_switching_is_final(exchange, trading_client):
    orders = trading_client().orders
    orders.limit_buy("OPRA", 10, 167.49)  # caches the market and the account
    exchange.queue("/orders", coded_reply(503, SWITCHING, "VENUE_SWITCHING"))
    with pytest.raises(loaf.VenueSwitchingError) as info:
        orders.limit_buy("OPRA", 10, 167.49)
    assert (info.value.status_code, info.value.code) == (503, "VENUE_SWITCHING")
    assert len(exchange.posts("/orders")) == 2  # one POST: the body belongs to the old mode
    sent = len(exchange.requests)
    orders.limit_buy("OPRA", 10, 167.49)
    assert routes(exchange.requests[sent:]) == ["GET /trade/OPRA", "GET /l2/keys", "POST /orders"]
    assert indices(exchange) == [4097, 4098, 4098]  # not placed: its number is signed again


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
    assert str(error).startswith("Could not confirm whether order 4097 of account 5 was placed")
    assert f"expiryMs {error.signed_body['l2ExpiryMs']}" in str(error)
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


@pytest.mark.parametrize(
    "replies, last",
    [
        # "Not placed" describes the re-send, not the first attempt.
        (
            lambda: [httpx.ReadTimeout("timed out")]
            + [error_reply(503, NOT_PLACED) for _ in range(3)],
            loaf.LoafServiceUnavailableError,
        ),
        # The network went down once the order had left: the re-sends never did.
        (
            lambda: [httpx.ReadTimeout("timed out")]
            + [httpx.ConnectError("unreachable") for _ in range(3)],
            loaf.LoafConnectionError,
        ),
        # An attempt that may have landed counts whichever attempt it was.
        (
            lambda: [httpx.ConnectError("unreachable"), httpx.ReadTimeout("timed out")]
            + [httpx.ConnectError("unreachable") for _ in range(2)],
            loaf.LoafConnectionError,
        ),
        (
            lambda: [error_reply(503, NOT_PLACED), error_reply(500, "Failed to submit order")]
            + [error_reply(503, NOT_PLACED) for _ in range(2)],
            loaf.LoafServiceUnavailableError,
        ),
    ],
    ids=["then-not-placed", "then-never-sent", "never-sent-first", "not-placed-first"],
)
def test_one_ambiguous_attempt_makes_the_outcome_unknown(exchange, trading_client, replies, last):
    orders = trading_client().orders
    exchange.queue("/orders", *replies())
    with pytest.raises(OrderOutcomeUnknownError) as info:
        orders.limit_buy("OPRA", 10, 167.49)
    assert info.value.attempts == 4
    assert type(info.value.last_error) is last
    orders.limit_buy("OPRA", 10, 167.49)
    assert indices(exchange) == [4097] * 4 + [4098]  # never the unknown order's number


def test_ambiguous_then_venue_switching_is_unknown(exchange, trading_client):
    orders = trading_client().orders
    exchange.queue(
        "/orders", httpx.ReadTimeout("timed out"), coded_reply(503, SWITCHING, "VENUE_SWITCHING")
    )
    with pytest.raises(OrderOutcomeUnknownError) as info:
        orders.limit_buy("OPRA", 10, 167.49)
    # The switch refused the re-send; the first attempt may have landed on the ledger being left.
    assert isinstance(info.value.last_error, loaf.VenueSwitchingError)
    assert info.value.attempts == 2
    first, second = exchange.posts("/orders")
    assert second == first
    orders.limit_buy("OPRA", 10, 167.49)
    assert indices(exchange) == [4097, 4097, 4098]  # never the unknown order's number


@pytest.mark.parametrize(
    "failure", [httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError]
)
def test_connect_errors_exhausted(exchange, trading_client, failure):
    # ProxyError: a proxy refused the tunnel (its CONNECT answered 407, say) before the order left.
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
    "failure, error",
    [
        (lambda: error_reply(500, "boom"), loaf.LoafServerError),
        (lambda: error_reply(503, NOT_CONFIRMED), loaf.LoafServiceUnavailableError),
        # A gateway's own pages: the server behind it may have committed the order first.
        (
            lambda: httpx.Response(502, html="<html><body>Bad Gateway</body></html>"),
            loaf.LoafServerError,
        ),
        (
            lambda: httpx.Response(504, html="<html><body>Gateway Timeout</body></html>"),
            loaf.LoafServerError,
        ),
    ],
    ids=["500", "503-unconfirmed", "502", "504"],
)
def test_5xx_exhausted_unknown(exchange, trading_client, failure, error):
    exchange.queue("/orders", *(failure() for _ in range(4)))
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
    # The property, the account and the reservation keep the client's timeout; the
    # placement waits out the engine.
    assert [r.extensions["timeout"]["read"] for r in exchange.requests] == [30.0, 30.0, 30.0, 45.0]

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
    client = LoafClient(api_key="testkey", base_url=BASE, secret_key=L2_SECRET, http_client=http)
    client.orders.limit_buy("OPRA", 10, 167.49)
    *reads, post = exchange.requests
    for read in reads:
        timeout = read.extensions["timeout"]
        assert (timeout["connect"], timeout["read"]) == (20.0, 120.0)
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


def test_backoff_stays_capped_through_a_long_outage(exchange, trading_client, monkeypatch):
    sleeps: list = []
    monkeypatch.setattr("loaf.client.time", SimpleNamespace(sleep=sleeps.append))
    exchange.queue("/orders", *(error_reply(503, NOT_CONFIRMED) for _ in range(1_101)))
    # A client told to retry for hours still ends in the typed error, with the body kept.
    with pytest.raises(OrderOutcomeUnknownError) as info:
        trading_client(max_retries=1_100).orders.limit_buy("OPRA", 10, 167.49)
    assert info.value.attempts == 1_101
    assert info.value.signed_body == exchange.bodies("/orders")[0]
    assert len(sleeps) == 1_100 and max(sleeps) <= 30.0


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
    exchange.queue(read, error_reply(429, "Too many requests", headers={"RateLimit-Reset": "58"}))
    # Waiting out the window would place the order a minute late, at a stale price.
    with pytest.raises(loaf.LoafRateLimitError) as info:
        place(trading_client().orders)
    assert info.value.retry_after == 58.0
    assert sleeps == []
    assert len(exchange.gets(read)) == 1
    assert exchange.posts("/l2/order-indices") == []
    assert exchange.posts("/orders") == [] and exchange.posts("/orders/conditional") == []

    # A 503 there is retried, after the short jittered pause only (every reply carries the hint).
    exchange.queue(read, error_reply(503, "busy", headers={"RateLimit-Reset": "57"}))
    assert place(trading_client().orders).orderId == 1
    assert len(sleeps) == 1 and 0 <= sleeps[0] <= 0.5


def test_reservation_429_not_waited_out(exchange, trading_client, monkeypatch, l2_clock):
    sleeps: list = []
    monkeypatch.setattr("loaf.client.time", SimpleNamespace(sleep=sleeps.append))
    exchange.queue(
        "/l2/order-indices",
        error_reply(429, "Too many requests for this account. Please slow down.",
                    headers={"RateLimit-Reset": "58"}),
    )
    with pytest.raises(loaf.LoafRateLimitError):
        trading_client().orders.limit_buy("OPRA", 10, 167.49)
    assert sleeps == [] and l2_clock.slept == []
    assert len(exchange.posts("/l2/order-indices")) == 1  # a POST is never re-sent
    assert exchange.posts("/orders") == []


@pytest.mark.parametrize("read", ["/trade/OPRA", "/l2/keys"])
def test_pre_signing_read_network_errors(exchange, trading_client, read):
    # A read is sent again after a network error...
    exchange.queue(read, httpx.ReadTimeout("timed out"))
    assert trading_client().orders.limit_buy("OPRA", 10, 167.49).orderId == 1
    assert len(exchange.gets(read)) == 2
    # ...up to max_retries times; then the typed error, and nothing placed.
    exchange.queue(read, *(httpx.ConnectError("unreachable") for _ in range(4)))
    with pytest.raises(loaf.LoafConnectionError) as info:
        trading_client().orders.limit_buy("OPRA", 10, 167.49)
    assert isinstance(info.value.__cause__, httpx.ConnectError)
    assert len(exchange.gets(read)) == 6
    assert len(exchange.posts("/l2/order-indices")) == len(exchange.posts("/orders")) == 1


@pytest.mark.parametrize(
    "path, call",
    [
        ("/l2/order-indices", lambda orders: orders.limit_buy("OPRA", 10, 167.49)),
        ("/orders/cancel", lambda orders: orders.cancel(7)),
    ],
    ids=["reservation", "cancel"],
)
def test_post_not_resent_after_a_network_error(exchange, trading_client, path, call):
    # It may have landed: a second reservation would leave more order numbers unused, and a
    # second cancel would be refused although the first worked.
    exchange.queue(path, httpx.ReadTimeout("timed out"))
    with pytest.raises(loaf.LoafConnectionError) as info:
        call(trading_client().orders)
    assert isinstance(info.value.__cause__, httpx.ReadTimeout)
    assert len(exchange.posts(path)) == 1
    assert exchange.posts("/orders") == []


def test_closed_client_after_an_ambiguous_attempt_is_unknown(exchange, trading_client):
    # E.g. another thread closed the client at shutdown while the order was being re-sent.
    exchange.queue("/orders", httpx.ReadTimeout("timed out"), RuntimeError(CLOSED))
    with pytest.raises(OrderOutcomeUnknownError) as info:
        trading_client().orders.limit_buy("OPRA", 10, 167.49)
    first, second = exchange.posts("/orders")
    assert second == first
    assert info.value.signed_body == json.loads(first)
    assert info.value.attempts == 2
    assert type(info.value.last_error) is RuntimeError
    assert info.value.__cause__ is info.value.last_error

    # Nothing sent before it could have landed: the error is passed on as it is.
    exchange.queue("/orders", RuntimeError(CLOSED))
    with pytest.raises(RuntimeError, match="client has been closed"):
        trading_client().orders.limit_buy("OPRA", 10, 167.49)


def test_response_hook_raising_on_a_5xx_is_unknown(exchange):
    # Your own http_client may raise on an answer it received (httpx's raise_for_status hook):
    # a 5xx answer may have placed the order.
    http = httpx.Client(
        transport=httpx.MockTransport(exchange.handler),
        base_url=BASE,
        event_hooks={"response": [lambda response: response.raise_for_status()]},
    )
    client = LoafClient(api_key="testkey", secret_key=L2_SECRET, base_url=BASE, http_client=http)
    exchange.queue("/orders", error_reply(503, NOT_CONFIRMED))
    with pytest.raises(OrderOutcomeUnknownError) as info:
        client.orders.limit_buy("OPRA", 10, 167.49)
    (post,) = exchange.posts("/orders")
    assert info.value.signed_body == json.loads(post)
    assert info.value.attempts == 1
    assert type(info.value.last_error) is httpx.HTTPStatusError

    # A 4xx answer is a refusal: passed on as it is.
    exchange.queue("/orders", error_reply(400, OPEN_ORDER_CAP))
    with pytest.raises(httpx.HTTPStatusError):
        client.orders.limit_buy("OPRA", 10, 167.49)


# --------------------------------------------------------------------------- #
# What a refusal invalidates (the SDK never re-signs: one POST per call)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "status, message, code, error",
    [
        (400, STALE_EPOCH, None, loaf.LoafValidationError),
        (400, WRONG_CHAIN, None, loaf.LoafValidationError),
        (
            400,
            "l2 market: the market index is not the property's market",
            None,
            loaf.LoafValidationError,
        ),
        (
            400,
            "l2 asset: the base asset id is not the market's base asset",
            None,
            loaf.LoafValidationError,
        ),
        # Matched anywhere in the text: a leg's refusal comes prefixed with its key.
        (401, "tp: " + BAD_SIGNATURE, None, loaf.LoafAuthError),
        (401, "l2 key: this trading key has been disabled", None, loaf.LoafAuthError),
        (401, "l2 account: no trading account has this account index", None, loaf.LoafAuthError),
        (403, NOT_TRADEABLE, "NOT_TRADEABLE_IN_MODE", loaf.VenueError),
        (
            403,
            "The competition round has not started yet",
            "COMPETITION_NOT_STARTED",
            loaf.VenueError,
        ),
        (
            403,
            "Not a competition participant for the current round",
            "NOT_COMPETITION_PARTICIPANT",
            loaf.VenueError,
        ),
    ],
)
def test_context_refusal_rereads_market_and_account(
    exchange, trading_client, status, message, code, error
):
    orders = trading_client().orders
    orders.limit_buy("OPRA", 10, 167.49)
    exchange.queue("/orders", httpx.Response(status, json={"error": message, "code": code}))
    with pytest.raises(error) as info:
        orders.limit_buy("OPRA", 10, 167.49)  # reserves 2 and takes 4098
    assert type(info.value) is error
    assert len(exchange.posts("/orders")) == 2  # never re-signed or re-sent
    sent = len(exchange.requests)
    orders.limit_buy("OPRA", 10, 167.49)
    # The market and the account are read again; the refused order's number is signed again.
    assert routes(exchange.requests[sent:]) == ["GET /trade/OPRA", "GET /l2/keys", "POST /orders"]
    assert indices(exchange) == [4097, 4098, 4098]


@pytest.mark.parametrize(
    "message",
    [INDEX_USED, INDEX_NOT_RESERVED, INDEX_TOO_OLD, "attached_legs[0]: " + INDEX_USED],
)
def test_index_refusal_drops_the_block(exchange, trading_client, message):
    orders = trading_client().orders
    orders.limit_buy("OPRA", 10, 167.49)
    exchange.queue("/orders", error_reply(400, message))
    with pytest.raises(loaf.LoafValidationError):
        orders.limit_buy("OPRA", 10, 167.49)  # reserves 2 and takes 4098
    assert len(exchange.posts("/orders")) == 2  # never re-signed or re-sent
    sent = len(exchange.requests)
    orders.limit_buy("OPRA", 10, 167.49)
    # A new block, and nothing given back from the old one; the account is kept.
    assert routes(exchange.requests[sent:]) == ["POST /l2/order-indices", "POST /orders"]
    assert counts(exchange) == [1, 2, 4]
    assert indices(exchange) == [4097, 4098, 4100]


@pytest.mark.parametrize(
    "message, rereads, last",
    [
        (STALE_EPOCH, ["GET /trade/OPRA", "GET /l2/keys", "POST /orders"], 4099),  # the account
        (INDEX_NOT_RESERVED, ["POST /l2/order-indices", "POST /orders"], 4100),  # the block
    ],
    ids=["epoch", "index"],
)
def test_unknown_outcome_still_invalidates(exchange, trading_client, message, rereads, last):
    orders = trading_client().orders
    orders.limit_buy("OPRA", 10, 167.49)
    exchange.queue("/orders", httpx.ReadTimeout("timed out"), error_reply(400, message))
    with pytest.raises(OrderOutcomeUnknownError):
        orders.limit_buy("OPRA", 10, 167.49)  # 4098, which may be live
    sent = len(exchange.requests)
    orders.limit_buy("OPRA", 10, 167.49)
    assert routes(exchange.requests[sent:]) == rereads
    assert indices(exchange) == [4097, 4098, 4098, last]  # never the unknown order's number


def test_refusal_of_an_older_account_keeps_a_fresh_read(exchange, trading_client):
    l2, market, account = pool(trading_client())
    l2.remember("OPRA", market)
    epoch_refusal = error_from_response(400, {"error": STALE_EPOCH})
    # An order signed with the account as it was before another thread read it again.
    l2.after_refusal("OPRA", account._replace(epoch=1), None, epoch_refusal)
    assert l2.market("OPRA") is None  # the market is dropped whatever it was
    assert l2.cached_account(market) == account
    l2.after_refusal("OPRA", account, None, epoch_refusal)
    assert l2.cached_account(market) is None


def test_index_refusal_of_a_replaced_block_keeps_the_new_one(exchange, trading_client):
    l2, market, account = pool(trading_client())
    _, old, _ = l2.take(market, account, 1, DEFAULT_ORDER_TTL_MS)  # reserves 1
    _, new, _ = l2.take(market, account, 1, DEFAULT_ORDER_TTL_MS)  # another thread: reserves 2
    assert new is not old
    # The refusal of an order from the old block arrives after the new one was reserved.
    l2.after_refusal("OPRA", account, old, error_from_response(400, {"error": INDEX_USED}))
    first, block, _ = l2.take(market, account, 1, DEFAULT_ORDER_TTL_MS)
    assert (first, block) == (4099, new)
    assert counts(exchange) == [1, 2]


def test_take_refuses_another_vault(exchange, trading_client):
    l2, market, account = pool(trading_client())
    l2.take(market, account, 1, DEFAULT_ORDER_TTL_MS)  # reserves 1
    _, block, _ = l2.take(market, account, 1, DEFAULT_ORDER_TTL_MS)  # reserves 2: one left
    other = market._replace(proxy_address="0x" + "22" * 20)
    with pytest.raises(loaf.VenueSwitchingError) as info:
        l2.take(other, account, 1, DEFAULT_ORDER_TTL_MS)
    assert (info.value.status_code, info.value.code) == (0, "VENUE_SWITCHING")
    # Nothing handed out, nothing reserved.
    assert block.next == 4099 and counts(exchange) == [1, 2]
    assert l2.take(market, account, 1, DEFAULT_ORDER_TTL_MS)[0] == 4099


# --------------------------------------------------------------------------- #
# Give-back: a definitely refused order's numbers are signed again
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "replies",
    [
        lambda: [error_reply(400, OPEN_ORDER_CAP)],
        lambda: [error_reply(403, "Trading is currently closed")],
        lambda: [error_reply(429, "Too many requests for this account. Please slow down.")],
        lambda: [error_reply(503, NOT_PLACED) for _ in range(4)],
        lambda: [httpx.ConnectError("unreachable") for _ in range(4)],
    ],
    ids=["400-cap", "403-closed", "429", "503-not-placed", "connect-error"],
)
def test_definite_refusal_gives_the_numbers_back(exchange, trading_client, replies):
    orders = trading_client().orders
    exchange.queue("/orders", *replies())
    with pytest.raises((loaf.LoafAPIError, loaf.LoafConnectionError)):
        orders.limit_buy("OPRA", 10, 100, tp_price=120, sl_price=90)
    orders.limit_buy("OPRA", 10, 100, tp_price=120, sl_price=90)
    refused, placed = exchange.bodies("/orders")[0], exchange.bodies("/orders")[-1]
    assert numbers(refused) == numbers(placed) == [4097, 4098, 4099]
    assert counts(exchange) == [3]
    assert placed["l2SigS"] != refused["l2SigS"]  # signed afresh, not the refused body
    verify_order(placed)
    for leg in ("tp", "sl"):
        verify_order(placed, leg)


def test_unknown_outcome_keeps_its_numbers(exchange, trading_client):
    orders = trading_client().orders
    exchange.queue("/orders", *(httpx.ReadTimeout("timed out") for _ in range(4)))
    with pytest.raises(OrderOutcomeUnknownError):
        orders.limit_buy("OPRA", 10, 100, tp_price=120, sl_price=90)
    orders.limit_buy("OPRA", 10, 100, tp_price=120, sl_price=90)
    assert numbers(exchange.bodies("/orders")[-1]) == [4100, 4101, 4102]
    assert counts(exchange) == [3, 6]


def test_give_back_below_the_latest_numbers(exchange, trading_client):
    l2, market, account = pool(trading_client())
    for _ in range(3):  # reserves 1, then 2
        l2.take(market, account, 1, DEFAULT_ORDER_TTL_MS)
    older, block, _ = l2.take(market, account, 1, DEFAULT_ORDER_TTL_MS)  # reserves 4
    newer, same, _ = l2.take(market, account, 1, DEFAULT_ORDER_TTL_MS)
    assert (older, newer, same) == (4100, 4101, block)
    l2.give_back(block, older, 1)  # a later order holds the next number: kept for later
    assert block.next == 4102
    # An order with a leg takes consecutive numbers, never the one given back...
    assert l2.take(market, account, 2, DEFAULT_ORDER_TTL_MS)[:2] == (4102, block)
    # ...which the next single order signs.
    assert l2.take(market, account, 1, DEFAULT_ORDER_TTL_MS)[:2] == (older, block)
    # The block has no number left at its end, and one given back still serves an order.
    assert block.next == block.end
    l2.give_back(block, newer, 1)
    assert l2.take(market, account, 1, DEFAULT_ORDER_TTL_MS)[:2] == (newer, block)
    assert counts(exchange) == [1, 2, 4]


def test_concurrent_refusals_give_their_numbers_back(exchange, trading_client):
    # Four orders in flight at once, each refused after the next took its number: only the last
    # one's is the block's latest.
    l2, market, account = pool(trading_client())
    for _ in range(3):  # reserves 1, then 2
        l2.take(market, account, 1, DEFAULT_ORDER_TTL_MS)
    taken = [l2.take(market, account, 1, DEFAULT_ORDER_TTL_MS) for _ in range(4)]  # reserves 4
    block = taken[0][1]
    for first, _, _ in taken:
        l2.give_back(block, first, 1)
    # The last one's number goes back to the block's end; the others are kept for later.
    assert (block.next, block.returned) == (4103, [(4100, 1), (4101, 1), (4102, 1)])
    again = [l2.take(market, account, 1, DEFAULT_ORDER_TTL_MS)[0] for _ in range(4)]
    assert again == [first for first, _, _ in taken] == [4100, 4101, 4102, 4103]
    assert counts(exchange) == [1, 2, 4]  # nothing reserved for them


def test_a_span_given_back_is_kept_whole(exchange, trading_client):
    l2, market, account = pool(trading_client())
    l2.take(market, account, 3, DEFAULT_ORDER_TTL_MS)  # reserves 3
    l2.take(market, account, 1, DEFAULT_ORDER_TTL_MS)  # reserves 6
    first, block, _ = l2.take(market, account, 3, DEFAULT_ORDER_TTL_MS)
    l2.take(market, account, 1, DEFAULT_ORDER_TTL_MS)  # a later order holds the next number
    l2.give_back(block, first, 3)
    # A single order takes the block's next number, never part of the three...
    assert l2.take(market, account, 1, DEFAULT_ORDER_TTL_MS)[:2] == (4105, block)
    # ...which the next order with two legs signs.
    assert l2.take(market, account, 3, DEFAULT_ORDER_TTL_MS)[:2] == (first, block)
    assert counts(exchange) == [3, 6]


def test_a_number_given_back_serves_only_expiries_its_block_admits(
    exchange, trading_client, l2_clock
):
    now = freeze(l2_clock)
    exchange.reserved_at_ms = now
    l2, market, account = pool(trading_client())
    for _ in range(3):  # reserves 1, then 2
        l2.take(market, account, 1, DEFAULT_ORDER_TTL_MS)
    first, block, _ = l2.take(market, account, 1, DEFAULT_ORDER_TTL_MS)  # reserves 4
    second = l2.take(market, account, 1, DEFAULT_ORDER_TTL_MS)[0]
    l2.take(market, account, 1, DEFAULT_ORDER_TTL_MS)  # a later order holds the next number
    for given_back in (first, second):
        l2.give_back(block, given_back, 1)
    # Ten hours on, the block's range still admits a 7-day order...
    exchange.reserved_at_ms = freeze(l2_clock, now + 10 * HOUR_MS)
    assert l2.take(market, account, 1, DEFAULT_ORDER_TTL_MS)[:2] == (first, block)
    # ...but not a max-life one, which the exchange would refuse when it books it (a stop or take
    # only when it fires, so it would FAIL then): that one signs a number from a fresh range.
    assert l2.take(market, account, 1, MAX_ORDER_TTL_MS)[0] == 4104
    assert counts(exchange) == [1, 2, 4, 8]


# --------------------------------------------------------------------------- #
# The market-mode window: an account read that contradicts the trading key right
# after the SDK saw the market mode switch is the switch still settling
# --------------------------------------------------------------------------- #


def _refused_by_the_exchange(exchange: FakeExchange, orders: Any) -> None:
    orders.limit_buy("OPRA", 10, 167.49)
    exchange.queue("/orders", coded_reply(403, NOT_TRADEABLE, "NOT_TRADEABLE_IN_MODE"))
    with pytest.raises(loaf.VenueError):
        orders.limit_buy("OPRA", 10, 167.49)


def _refused_locally(exchange: FakeExchange, orders: Any) -> None:
    orders.limit_buy("OPRA", 10, 167.49)
    exchange.details["OPRA"] = property_detail(mode=True)  # a round started
    with pytest.raises(loaf.VenueError):
        orders.market_buy("OPRA", 1)  # a MARKET order reads the property every time
    exchange.details["OPRA"] = property_detail()  # a stale detail: still the old mode


def _switching(exchange: FakeExchange, orders: Any) -> None:
    orders.limit_buy("OPRA", 10, 167.49)
    exchange.queue("/orders", coded_reply(503, SWITCHING, "VENUE_SWITCHING"))
    with pytest.raises(loaf.VenueSwitchingError):
        orders.limit_buy("OPRA", 10, 167.49)


def _reserved_on_the_other_ledger(exchange: FakeExchange, orders: Any) -> None:
    orders.limit_buy("OPRA", 10, 167.49)  # its block of one is used up
    now_ms = time.time_ns() // 1_000_000
    other = {"accountIndex": 9, "firstIndex": 1, "count": 2, "reservedAtMs": now_ms}
    exchange.queue("/l2/order-indices", httpx.Response(201, json=other))
    with pytest.raises(loaf.VenueSwitchingError, match="switched while this order") as info:
        orders.limit_buy("OPRA", 10, 167.49)
    assert (info.value.status_code, info.value.code) == (0, "VENUE_SWITCHING")


def _reserved_with_no_account(exchange: FakeExchange, orders: Any) -> None:
    orders.limit_buy("OPRA", 10, 167.49)  # its block of one is used up
    exchange.queue("/l2/order-indices", error_reply(404, NO_ACCOUNT))
    with pytest.raises(loaf.VenueSwitchingError, match="switched while this order") as info:
        orders.limit_buy("OPRA", 10, 167.49)
    assert (info.value.status_code, info.value.code) == (0, "VENUE_SWITCHING")


def _reserved_with_no_account_on_the_retry(exchange: FakeExchange, orders: Any) -> None:
    orders.limit_buy("OPRA", 10, 167.49)  # its block of one is used up
    exchange.queue(
        "/l2/order-indices", error_reply(422, RESERVED_TOO_SOON), error_reply(404, NO_ACCOUNT)
    )
    with pytest.raises(loaf.VenueSwitchingError, match="switched while this order") as info:
        orders.limit_buy("OPRA", 10, 167.49)
    assert (info.value.status_code, info.value.code) == (0, "VENUE_SWITCHING")


@pytest.mark.parametrize(
    "refusal, opened_at",
    [
        (_refused_by_the_exchange, 1_000.0),
        (_switching, 1_000.0),
        (_reserved_on_the_other_ledger, 1_000.0),
        (_reserved_with_no_account, 1_000.0),
        (_reserved_with_no_account_on_the_retry, 1_000.0),
        # The stale read at 1_119 shows the other mode again: the window counts from it.
        (_refused_locally, 1_119.0),
    ],
)
@pytest.mark.parametrize(
    "keys, message",
    [
        (None, "You have no trading account on OPRA's market yet"),
        ({**KEYS, "accountIndex": 9, "journaledKey": OTHER_KEY}, NOT_IN_FORCE),
    ],
)
def test_venue_window(exchange, trading_client, l2_clock, refusal, opened_at, keys, message):
    l2_clock.monotonic = lambda: 1_000.0
    orders = trading_client().orders
    refusal(exchange, orders)
    exchange.keys = keys  # the account is read on the other ledger
    l2_clock.monotonic = lambda: 1_119.0
    sent = len(exchange.requests)
    with pytest.raises(loaf.VenueSwitchingError) as info:
        orders.limit_buy("OPRA", 10, 167.49)
    assert (info.value.status_code, info.value.code) == (0, "VENUE_SWITCHING")
    assert isinstance(info.value.__cause__, loaf.LoafConfigError)
    assert message in str(info.value.__cause__)
    assert routes(exchange.requests[sent:]) == ["GET /trade/OPRA", "GET /l2/keys"]
    l2_clock.monotonic = lambda: opened_at + 119  # the window is still open
    with pytest.raises(loaf.VenueSwitchingError):
        orders.limit_buy("OPRA", 10, 167.49)
    # Past two minutes the same answer is a setup to fix.
    l2_clock.monotonic = lambda: opened_at + 121
    sent = len(exchange.requests)
    with pytest.raises(loaf.LoafConfigError, match=re.escape(message)):
        orders.limit_buy("OPRA", 10, 167.49)
    assert routes(exchange.requests[sent:]) == ["GET /trade/OPRA", "GET /l2/keys"]


def test_own_switching_error_does_not_reopen_the_window(exchange, trading_client, l2_clock):
    l2_clock.monotonic = lambda: 1_000.0
    orders = trading_client().orders
    _refused_by_the_exchange(exchange, orders)
    exchange.keys = None
    l2_clock.monotonic = lambda: 1_060.0
    for _ in range(2):
        with pytest.raises(loaf.VenueSwitchingError):
            orders.limit_buy("OPRA", 10, 167.49)
    l2_clock.monotonic = lambda: 1_121.0  # past the exchange's refusal's window, not the SDK's own
    with pytest.raises(loaf.LoafConfigError, match="deposit"):
        orders.limit_buy("OPRA", 10, 167.49)


def test_refusals_while_a_mode_lasts_do_not_hold_the_window_open(
    exchange, trading_client, l2_clock
):
    l2_clock.monotonic = lambda: 1_000.0
    exchange.details["MUS"] = property_detail("MUS")
    orders = trading_client().orders
    orders.limit_buy("MUS", 10, 167.49)  # before the round
    # A round starts. MUS, a real market, is refused on every tick; the round's market, on its
    # own vault, names the round's trading key, which this client does not hold.
    round_market = {**MARKET, "proxyAddress": "0x" + "22" * 20}
    exchange.details = {
        "MUS": property_detail("MUS", mode=True),
        "ARNA": property_detail("ARNA", l2_market=round_market, competition=True, mode=True),
    }
    exchange.keys = {**KEYS, "accountIndex": 9, "journaledKey": OTHER_KEY}
    raised = []
    for tick in range(8):  # every 30 s
        l2_clock.monotonic = lambda at=1_000.0 + 30 * tick: at
        with pytest.raises(loaf.VenueError):
            orders.market_buy("MUS", 1)  # a MARKET order reads the property every time
        with pytest.raises((loaf.VenueSwitchingError, loaf.LoafConfigError)) as info:
            orders.limit_buy("ARNA", 10, 167.49)
        raised.append(type(info.value))
    # The switch seen at 1_000 opened the window for two minutes, and the refusals since did not
    # hold it open: the configuration error then surfaces.
    assert raised == [loaf.VenueSwitchingError] * 4 + [loaf.LoafConfigError] * 4


def test_a_switch_seen_in_a_property_read_opens_the_window(exchange, trading_client, l2_clock):
    exchange.details["MUS"] = property_detail("MUS", mode=True)  # a round is on
    orders = trading_client().orders
    for tick in range(4):  # MUS, a real market, is refused on every tick of the round
        l2_clock.monotonic = lambda at=1_000.0 + 30 * tick: at
        with pytest.raises(loaf.VenueError):
            orders.limit_buy("MUS", 10, 167.49)
    # The round ends: a fresh read shows the real mode, while the round's ledger, which holds
    # no account of yours, still answers the account read.
    exchange.details["MUS"] = property_detail("MUS")
    exchange.keys = None
    l2_clock.monotonic = lambda: 1_120.0
    with pytest.raises(loaf.VenueSwitchingError):
        orders.limit_buy("MUS", 10, 167.49)
    l2_clock.monotonic = lambda: 1_241.0
    with pytest.raises(loaf.LoafConfigError, match="no trading account"):
        orders.limit_buy("MUS", 10, 167.49)


@pytest.mark.parametrize(
    "seen",
    [
        # Another thread's property read shows the round...
        lambda l2: l2.note_mode(property_detail("MUS", mode=True)),
        # ...or the exchange refuses another thread's order for the market mode.
        lambda l2: l2.after_refusal(
            "MUS",
            None,
            None,
            error_from_response(403, {"error": NOT_TRADEABLE, "code": "NOT_TRADEABLE_IN_MODE"}),
        ),
    ],
    ids=["property-read", "refusal"],
)
def test_a_switch_seen_during_an_account_read_counts_for_it(exchange, seen):
    reading, release = threading.Event(), threading.Event()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/l2/keys" and exchange.keys is None:
            reading.set()  # the account read is in flight until the test releases it
            release.wait(10)
        return exchange.handler(request)

    http = httpx.Client(transport=httpx.MockTransport(handler), base_url=BASE)
    orders = LoafClient(
        api_key="testkey", secret_key=L2_SECRET, base_url=BASE, http_client=http
    ).orders
    orders.limit_buy("OPRA", 10, 167.49)  # the real mode is seen
    exchange.queue("/orders", error_reply(400, STALE_EPOCH))
    with pytest.raises(loaf.LoafValidationError):
        orders.limit_buy("OPRA", 10, 167.49)  # the account is read again next time
    # The round's ledger already answers that read, while the detail, still stale, shows the
    # real mode.
    exchange.keys = None
    raised: list[Exception] = []

    def place() -> None:
        try:
            orders.limit_buy("OPRA", 10, 167.49)
        except loaf.LoafError as exc:
            raised.append(exc)

    placing = threading.Thread(target=place)
    placing.start()
    assert reading.wait(10)
    other = threading.Thread(target=seen, args=(orders._l2,))
    other.start()
    deadline = time.monotonic() + 3  # the switch is noted while the account read is in flight
    while orders._l2._switch_seen_at == float("-inf") and time.monotonic() < deadline:
        time.sleep(0.01)
    release.set()
    placing.join(10)
    other.join(10)
    (error,) = raised
    assert isinstance(error, loaf.VenueSwitchingError)
    assert isinstance(error.__cause__, loaf.LoafConfigError)


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
    bare = trading_client(api_key=None, secret_key=None)
    sent = len(exchange.requests)
    exchange.queue("/orders", duplicate_reply())
    assert bare.orders.resubmit(order.value.signed_body).duplicate is True
    first, again = exchange.posts("/orders")
    assert again == first
    bare.orders.resubmit(stop.value.signed_body)  # routed by its triggerPrice
    first, again = exchange.posts("/orders/conditional")
    assert again == first
    # Stateless: nothing read or reserved, and no Authorization.
    assert routes(exchange.requests[sent:]) == ["POST /orders", "POST /orders/conditional"]
    assert all("authorization" not in r.headers for r in exchange.requests[sent:])

    sent = len(exchange.requests)
    body = order.value.signed_body
    for bad in (
        {k: v for k, v in body.items() if k != "l2SigS"},
        json.dumps(body),
        {**body, "l2OrderIndex": True},
    ):
        with pytest.raises(loaf.LoafValidationError, match="needs a signed placement body") as info:
            bare.orders.resubmit(bad)
        assert info.value.status_code == 0
    assert len(exchange.requests) == sent


def test_resubmit_only_while_the_order_can_rest(exchange, trading_client, l2_clock):
    now = freeze(l2_clock)
    exchange.queue("/orders", httpx.ReadTimeout("timed out"))
    with pytest.raises(OrderOutcomeUnknownError) as unknown:
        trading_client(max_retries=0).orders.limit_buy("OPRA", 10, 167.49, ttl_ms=6 * HOUR_MS)
    body = unknown.value.signed_body
    rests_until = body["l2ExpiryMs"] - L2_BUFFER_MS
    assert rests_until == now + 6 * HOUR_MS

    sent = len(exchange.requests)
    freeze(l2_clock, rests_until)
    # Nothing is sent, and the first attempt may have landed, and filled: still unknown.
    with pytest.raises(OrderOutcomeUnknownError) as info:
        trading_client().orders.resubmit(body)
    assert (info.value.attempts, info.value.signed_body) == (0, body)
    refusal = info.value.last_error
    assert type(refusal) is loaf.LoafValidationError and refusal.status_code == 0
    assert info.value.__cause__ is refusal
    until = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(rests_until // 1000))
    assert f"could rest only until {until}" in refusal.message
    assert f"expiryMs {body['l2ExpiryMs']}" in refusal.message
    # Nothing expires a stop, a take or a leg: one that landed must be cancelled.
    assert "stays ARMED" in refusal.message
    assert len(exchange.requests) == sent

    # Signed six hours ago and still inside its life: sent unchanged.
    freeze(l2_clock, rests_until - 1)
    exchange.queue("/orders", duplicate_reply())
    trading_client().orders.resubmit(body)
    first, again = exchange.posts("/orders")
    assert again == first


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
        (lambda: [error_reply(401, BAD_SIGNATURE)], loaf.LoafAuthError),
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
# The trading key
# --------------------------------------------------------------------------- #


def test_trading_key_from_argument_or_environment(monkeypatch):
    for secret in (L2_SECRET, L2_SECRET.upper()):
        assert LoafClient(base_url=BASE, secret_key=secret).public_key == L2_PUBLIC_KEY

    monkeypatch.setenv("LOAF_SECRET_KEY", f"  {L2_SECRET}\n")
    with LoafClient(api_key="testkey", base_url=BASE) as client:
        assert client.public_key == L2_PUBLIC_KEY
        assert repr(client) == (
            f"<LoafClient base_url='{BASE}' authenticated public_key={L2_PUBLIC_KEY} "
            f"v{loaf.__version__}>"
        )

    monkeypatch.setenv("LOAF_SECRET_KEY", "  ")  # blank counts as unset
    with LoafClient(base_url=BASE) as client:
        assert client.public_key is None
        assert repr(client) == f"<LoafClient base_url='{BASE}' anonymous v{loaf.__version__}>"


@pytest.mark.parametrize(
    "bad",
    [
        L2_SECRET[:-1],  # 79 hex
        L2_SECRET + "0",  # 81 hex
        "0x" + L2_SECRET,  # 0x + 80 hex is the shape of a public key
        L2_SECRET[:-1] + "g",
        "0" * 80,
        N.to_bytes(40, "little").hex(),  # the group order itself
        "ab" * 32,  # an API key
    ],
)
def test_bad_trading_key_refused(monkeypatch, bad):
    def refused(make: Any) -> loaf.LoafConfigError:
        with pytest.raises(loaf.LoafConfigError, match="is not a trading key") as info:
            make()
        assert "the 80 hex characters the web app's Show trading key gives" in str(info.value)
        assert bad not in str(info.value)
        assert info.value.__cause__ is None and info.value.__context__ is None
        return info.value

    refused(lambda: LoafClient(base_url=BASE, secret_key=bad))
    monkeypatch.setenv("LOAF_SECRET_KEY", bad)
    refused(lambda: LoafClient(base_url=BASE))


def test_trading_key_cannot_be_pickled_or_copied():
    key = TradingKey(L2_SECRET)
    assert repr(key) == f"TradingKey(public_key={L2_PUBLIC_KEY!r})"
    # Serializers that would otherwise read the slots (jsonpickle) ask __getstate__ too.
    for leak in (pickle.dumps, copy.copy, copy.deepcopy, TradingKey.__getstate__):
        with pytest.raises(TypeError, match="holds a secret key"):
            leak(key)


def test_trading_key_not_in_traceback_locals(monkeypatch):
    # Crash reporters and debuggers record every traceback frame's locals.
    later: list[tuple[type[BaseException], dict[str, Any]]] = [
        (ValueError, {"max_retries": "x"}),
    ]
    monkeypatch.setenv("LOAF_SECRET_KEY", L2_SECRET.upper())
    for failure, kwargs in later:
        with pytest.raises(failure) as info:
            LoafClient(base_url=BASE, **kwargs)
        assert secret_locals(info.value, L2_SECRET) == []
    monkeypatch.delenv("LOAF_SECRET_KEY")

    # Nor an explicit argument, whichever step of the constructor fails.
    earlier: list[tuple[type[BaseException], dict[str, Any]]] = [
        (loaf.LoafConfigError, {"api_key": "0x" + "ab" * 32}),
        (loaf.LoafConfigError, {"max_slippage_bps": -1}),
    ]
    for failure, kwargs in earlier + later:
        with pytest.raises(failure) as info:
            LoafClient(base_url=BASE, secret_key=L2_SECRET, **kwargs)
        assert secret_locals(info.value, L2_SECRET) == []

    # Nearly the key itself, refused: one hex digit short, or 80 hex that are no scalar below
    # K1's group order (its last byte is the most significant).
    for near_miss, kept in (
        (L2_SECRET[:-1], L2_SECRET[:-1]),
        (L2_SECRET[:-2] + "ff", L2_SECRET[:-2]),
    ):
        monkeypatch.setenv("LOAF_SECRET_KEY", near_miss)
        with pytest.raises(loaf.LoafConfigError) as info:
            LoafClient(base_url=BASE)
        assert secret_locals(info.value, kept) == []
    monkeypatch.delenv("LOAF_SECRET_KEY")

    # The key as bytes (a secret file read in binary mode) is no trading key either.
    for raw in (L2_SECRET.encode(), bytearray(L2_SECRET.encode())):
        with pytest.raises(loaf.LoafConfigError, match="is not a trading key") as info:
            LoafClient(base_url=BASE, secret_key=raw)  # type: ignore[arg-type]
        assert info.value.__context__ is None
        assert secret_locals(info.value, L2_SECRET) == []


def test_interrupted_key_setup_leaves_no_trace(monkeypatch):
    # A Ctrl-C while the first client computes its public key, when K1 builds its table.
    def interrupted() -> Any:
        raise KeyboardInterrupt

    monkeypatch.setattr(k1, "_generator_table", interrupted)
    with pytest.raises(KeyboardInterrupt) as info:
        LoafClient(base_url=BASE, secret_key=L2_SECRET)
    assert secret_locals(info.value, L2_SECRET) == []


def test_half_built_objects_still_repr():
    # Crash reporters repr every local of a failed constructor's frame.
    def selves(exc: BaseException) -> list[Any]:
        found = []
        tb = exc.__traceback__
        while tb is not None:
            if tb.tb_frame.f_code.co_name == "__init__":
                found.append(tb.tb_frame.f_locals["self"])
            tb = tb.tb_next
        return found

    with pytest.raises(ValueError) as failed:
        LoafClient(
            base_url=BASE, secret_key=L2_SECRET, max_retries="x"  # type: ignore[arg-type]
        )
    (client,) = selves(failed.value)
    assert repr(client) == (
        f"<LoafClient base_url='{BASE}' anonymous public_key={L2_PUBLIC_KEY} v{loaf.__version__}>"
    )
    with pytest.raises(loaf.LoafConfigError) as refused:
        LoafClient(base_url=BASE, secret_key="0" * 80)
    client, key = selves(refused.value)
    assert repr(client) == f"<LoafClient base_url=None anonymous v{loaf.__version__}>"
    assert repr(key) == "TradingKey(public_key=None)"
    with pytest.raises(loaf.LoafConfigError) as refused:
        loaf.LoafWebSocketClient(ws_url="ws://test/ws", api_key="0x" + "ab" * 32)
    (ws,) = selves(refused.value)
    assert repr(ws) == "<LoafWebSocketClient None idle channels=0>"
    with pytest.raises(loaf.LoafConfigError) as refused:
        loaf.LoafWebSocketClient(ws_url="ws://test/ws", reconnect_delay=0)
    (ws,) = selves(refused.value)
    assert repr(ws) == "<LoafWebSocketClient 'ws://test/ws' idle channels=0>"


def test_signing_failure_leaves_no_trace(exchange, trading_client, monkeypatch):
    orders = trading_client().orders
    with monkeypatch.context() as m:
        # k1.sign itself refuses (31 random bytes), with the secret scalar in its frame.
        m.setattr("loaf._l2.os", SimpleNamespace(urandom=lambda n: bytes(n - 1)))
        with pytest.raises(loaf.LoafError) as info:
            orders.limit_buy("OPRA", 10, 100, tp_price=120, sl_price=90)
    error = info.value
    assert type(error) is loaf.LoafError
    assert str(error) == (
        "Signing the order failed (K1Error: l2 signer: the random bytes must be 32 bytes); "
        "nothing was sent."
    )
    # Neither a chained error nor the traceback reaches k1.sign's frame.
    assert error.__cause__ is None and error.__context__ is None
    tb, files = error.__traceback__, []
    while tb is not None:
        files.append(tb.tb_frame.f_code.co_filename)
        tb = tb.tb_next
    assert k1.__file__ not in files
    assert secret_locals(error, L2_SECRET) == []
    assert exchange.posts("/orders") == []
    orders.limit_buy("OPRA", 10, 100, tp_price=120, sl_price=90)
    assert numbers(exchange.bodies("/orders")[-1]) == [4097, 4098, 4099]  # given back


def test_wallet_key_shaped_api_key_refused():
    wallet = "0x" + "ab" * 32
    for swapped in (
        wallet,
        f" {wallet}",
        f"{wallet}\n",
        "0X" + "AB" * 32,
        # As a .env loader may keep it: quoted, with a comment, the pasted line's own name, or
        # on a second line.
        f'"{wallet}"',
        f"'{wallet}'",
        f"{wallet} # wallet",
        f"PRIVATE_KEY={wallet}",
        f"{'cd' * 32}\n{wallet}",
    ):
        with pytest.raises(loaf.LoafConfigError, match="looks like a wallet private key") as info:
            LoafClient(api_key=swapped)
        assert "ab" * 32 not in str(info.value).lower()
        # Nor left in the constructor's frame, where crash reporters look.
        assert secret_locals(info.value, "ab" * 32) == []
        tb = info.value.__traceback__
        while tb is not None and tb.tb_frame.f_code.co_name != "__init__":
            tb = tb.tb_next
        assert tb is not None and getattr(tb.tb_frame.f_locals["self"], "api_key", None) is None


@pytest.mark.parametrize(
    "misplaced",
    [
        L2_SECRET,
        L2_SECRET.upper(),
        f" {L2_SECRET}\n",
        f"LOAF_SECRET_KEY={L2_SECRET}",  # the helper's whole line
        f"0x{L2_SECRET}",  # 0x-prefixed: it holds a wallet key's shape too, but is 80 hex
    ],
)
def test_trading_key_shaped_api_key_refused(monkeypatch, misplaced):
    # The API key is sent with every read: the trading key must never go out as one.
    def refused(make: Any) -> None:
        with pytest.raises(loaf.LoafConfigError, match="looks like your trading key") as info:
            make()
        assert L2_SECRET not in str(info.value).lower()
        assert secret_locals(info.value, L2_SECRET) == []
        tb = info.value.__traceback__
        while tb is not None and tb.tb_frame.f_code.co_name != "__init__":
            tb = tb.tb_next
        assert tb is not None and getattr(tb.tb_frame.f_locals["self"], "api_key", None) is None

    refused(lambda: LoafClient(base_url=BASE, api_key=misplaced))
    monkeypatch.setenv("LOAF_API_KEY", misplaced)
    refused(lambda: LoafClient(base_url=BASE))
    monkeypatch.setenv("LOAF_SECRET_KEY", L2_SECRET)  # pasted into both
    refused(lambda: LoafClient(base_url=BASE))


@pytest.mark.parametrize(
    "stray",
    [
        # A .env value written over two lines: a wallet key exported without 0x, or a note, on
        # the second, or a trading key wrapped over both.
        f"{'cd' * 32}\n{'ab' * 32}",
        f"{'cd' * 32}\r\n{'ab' * 32}",
        f"{'cd' * 32}\n# expires 2027-04-01",
        f"{L2_SECRET[:40]}\n{L2_SECRET[40:]}",
        f"{'cd' * 32} {'ab' * 32}",  # or on one line
        f"\u201c{'cd' * 32}\u201d",  # typographic quotes, which no .env loader removes
    ],
    ids=["wallet key", "wallet key crlf", "note", "trading key", "space", "typographic quotes"],
)
def test_api_key_with_a_stray_character_refused(monkeypatch, stray):
    # httpx would quote the whole value in its error, and the WebSocket send it in every auth frame.
    parts = ("cd" * 32, "ab" * 32, L2_SECRET[:40], L2_SECRET[40:])

    def refused(make: Any) -> None:
        with pytest.raises(loaf.LoafConfigError, match="stray character inside it") as info:
            make()
        assert not any(part in str(info.value).lower() for part in parts)
        assert secret_locals(info.value, *parts) == []

    refused(lambda: LoafClient(base_url=BASE, api_key=stray))
    monkeypatch.setenv("LOAF_API_KEY", stray)
    refused(lambda: LoafClient(base_url=BASE))


def test_wallet_key_in_the_environment_refused(monkeypatch):
    wallet = "0x" + "cd" * 32
    monkeypatch.setenv("LOAF_WALLET_PRIVATE_KEY", wallet)
    with pytest.raises(loaf.LoafConfigError, match="LOAF_WALLET_PRIVATE_KEY is set") as info:
        LoafClient(base_url=BASE, secret_key=L2_SECRET)
    assert "cd" * 32 not in str(info.value)
    assert ".env" in str(info.value)  # where a bot's environment usually gets it from
    monkeypatch.setenv("LOAF_WALLET_PRIVATE_KEY", " \n")  # blank counts as unset
    with LoafClient(base_url=BASE) as client:
        assert client.public_key is None


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
        orders.create_conditional(ticker, side, 1, type="STOP_LIMIT", trigger_price=90, price=89)
    assert exchange.requests == []


@pytest.mark.parametrize("ticker", ["A", "MUS", "OPRA", "X1", "1234"])
def test_ticker_accepted(exchange, trading_client, ticker):
    exchange.details[ticker] = property_detail(ticker)
    trading_client().orders.limit_buy(ticker, 1, 100)
    (body,) = exchange.bodies("/orders")
    assert body["ticker"] == ticker
    assert len(exchange.gets(f"/trade/{ticker}")) == 1

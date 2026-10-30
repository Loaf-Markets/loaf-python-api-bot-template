"""Order placement against an in-memory exchange: the signed L2 bodies, what the
SDK reads for them (the market, your trading account, order numbers) and how
they must match the trading key, MARKET pricing, TP/SL legs, conditionals, the
order life, retries, resubmit, what a refusal invalidates, and the order numbers
a definite refusal gives back.

Signatures are checked with ``conftest.verify_order``, which rebuilds the signed
terms from the posted JSON as the exchange reads it.
"""

from __future__ import annotations

import json
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
from loaf._l2 import AccountState, L2State, market_of
from loaf.constants import (
    DEFAULT_ORDER_TTL_MS,
    L2_BUFFER_MS,
    L2_ORDER_LIFE_MARGIN_MS,
    MAX_ORDER_TTL_MS,
)
from loaf.exceptions import error_from_response

from conftest import (
    ACCOUNT_INDEX,
    BAD_SIGNATURE,
    BASE,
    KAT,
    KEY_DISABLED,
    KEYS,
    L2_PUBLIC_KEY,
    L2_SECRET,
    MARKET_INDEX,
    MARKET_L2,
    TRADING_KEY,
    UNKNOWN_KEY,
    FakeExchange,
    freeze,
    market_detail,
    routes,
    secret_locals,
    verify_login,
    verify_order,
)

# The SDK sends the keys in this fixed order: no ticker, the signed l2MarketIndex is the market.
ORDER_KEYS = (
    "price quantity side type timeInForce l2ChainId l2AccountIndex l2MarketIndex l2OrderIndex "
    "l2Epoch l2FeeRateCap l2ExpiryMs l2BaseAssetId l2QuoteAssetId l2SigS l2SigE"
).split()
CONDITIONAL_KEYS = ORDER_KEYS[:4] + ["triggerPrice"] + ORDER_KEYS[4:]
LEG_KEYS = "triggerPrice price l2OrderIndex l2ExpiryMs l2SigS l2SigE".split()
# What every order on OPRA signs besides its own terms: its market (MARKET_INDEX, MARKET_L2) and
# the account in KEYS.
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
    "You have reached the limit of 100 open orders on this market. Cancel some open "
    "orders on it before placing a new one."
)
DELISTED = "This market has been delisted and is no longer available for trading"
BALANCE_UNAVAILABLE = "The L2 balance is temporarily unavailable; try again in a moment"
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
K1_BOUND = "l2 k1 bound: quantity x price / 10 is not below 2^48 (K1's 48-bit fill quote)"
CLOSED = "Cannot send a request, as the client has been closed."

# Another trading key (K1's second known-answer key): in force on the account once yours was reset.
OTHER_KEY = "0x" + KAT["keys"][1]["pk"]

HOUR_MS = 3_600_000
DAY_MS = 86_400_000


def error_reply(status: int, message: str, **kwargs: Any) -> httpx.Response:
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


def pool(client: LoafClient) -> tuple[L2State, AccountState]:
    """The client's L2 state with the account read, as a first placement on OPRA leaves it."""
    l2 = client.orders._l2
    return l2, l2.account(ACCOUNT_INDEX)


# --------------------------------------------------------------------------- #
# Body shape
# --------------------------------------------------------------------------- #


def test_limit_body_signed_no_bearer(exchange, trading_client):
    res = trading_client().orders.limit_buy("OPRA", quantity=10, price=167.49)
    request = exchange.requests[-1]
    body = json.loads(request.content)
    assert list(body) == ORDER_KEYS
    assert unsigned(body) == {
        "price": 167.49,
        "quantity": 10.0,
        "side": "BUY",
        "type": "LIMIT",
        "timeInForce": "GTC",
        "l2OrderIndex": 4097,
        **CONTEXT,
    }
    assert all(re.fullmatch("[0-9a-f]{80}", body[k]) for k in ("l2SigS", "l2SigE"))
    # The signature authenticates a placement: no login token is sent with it.
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
# What an order reads: the market, the account, order numbers
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
    # The account's epoch and its order numbers, both authenticated by one login token the
    # trading key signed for its account: the account is the token's, so only apiKeyIndex is sent.
    assert keys.url.params.multi_items() == [("apiKeyIndex", "0")]
    bearer = keys.headers["authorization"]
    assert reserve.headers["authorization"] == bearer
    assert verify_login(bearer[len("Bearer ") :])["l2AccountIndex"] == ACCOUNT_INDEX
    assert json.loads(reserve.content) == {"count": 1}

    # The market and the account are kept; the next order only needs order numbers.
    orders.limit_buy("OPRA", 10, 167.49)
    assert routes(exchange.requests[4:]) == ["POST /l2/order-indices", "POST /orders"]
    assert counts(exchange) == [1, 2]
    assert indices(exchange) == [4097, 4098]


def test_reservations_double_up_to_64(exchange, trading_client):
    l2, account = pool(trading_client())
    reserved_for = []  # the order (1-based) each reservation was made for
    for order in range(1, 129):  # single orders: one number each
        before = len(exchange.posts("/l2/order-indices"))
        first, _, _ = l2.take(account, 1, DEFAULT_ORDER_TTL_MS)
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


def test_tickers_share_the_account_and_the_block(exchange, trading_client):
    # Another market: its own market index and asset.
    exchange.details["MUS"] = market_detail(
        "MUS", market_index=2051, l2={**MARKET_L2, "baseAssetId": 7}
    )
    orders = trading_client().orders
    orders.limit_buy("OPRA", 10, 167.49)
    orders.limit_buy("OPRA", 10, 167.49)  # reserves 2: one number left
    sent = len(exchange.requests)
    orders.limit_buy("MUS", 10, 167.49)
    # Its own market read; the account and the order numbers are shared.
    assert routes(exchange.requests[sent:]) == ["GET /trade/MUS", "POST /orders"]
    assert len(exchange.gets("/l2/keys")) == 1
    body = exchange.bodies("/orders")[-1]
    assert (body["l2MarketIndex"], body["l2BaseAssetId"], body["l2OrderIndex"]) == (2051, 7, 4099)
    assert (body["l2AccountIndex"], body["l2Epoch"]) == (5, 2)


def test_unknown_ticker_404(exchange, trading_client):
    with pytest.raises(loaf.LoafNotFoundError, match="Market not found"):
        trading_client().orders.limit_buy("ATLS", 10, 167.49)
    assert routes(exchange.requests) == ["GET /trade/ATLS"]


# --------------------------------------------------------------------------- #
# Market refusals: nothing follows the market read
# --------------------------------------------------------------------------- #


def test_null_l2_refused_locally(exchange, trading_client):
    exchange.details["OPRA"] = market_detail(l2=None)
    orders = trading_client().orders
    for _ in range(2):
        with pytest.raises(
            loaf.LoafValidationError, match="OPRA has no L2 market right now"
        ) as info:
            orders.limit_buy("OPRA", 10, 167.49)
        assert info.value.status_code == 0
    assert routes(exchange.requests) == ["GET /trade/OPRA"] * 2  # never cached


def test_server_without_the_exchange(exchange, trading_client):
    # A server from before markets answers a property; another JSON API answers no market either.
    exchange.details["OPRA"] = {"property": {"ticker": "OPRA", "status": "LIVE", "isHalted": False}}
    with pytest.raises(loaf.LoafConfigError) as info:
        trading_client().orders.limit_buy("OPRA", 10, 167.49)
    assert str(info.value) == (
        "GET /trade/OPRA answered no market: this server does not run the exchange this SDK "
        "trades on (check LOAF_API_BASE_URL)."
    )
    assert routes(exchange.requests) == ["GET /trade/OPRA"]


@pytest.mark.parametrize(
    "market_index, l2, field",
    [
        (4096, MARKET_L2, "marketIndex"),  # past K1's 12 bits
        (-1, MARKET_L2, "marketIndex"),
        (True, MARKET_L2, "marketIndex"),
        (..., MARKET_L2, "marketIndex"),
        (MARKET_INDEX, ..., "l2"),
        (MARKET_INDEX, MARKET_L2["proxyAddress"], "l2"),
        (MARKET_INDEX, {**MARKET_L2, "chainId": 0}, "l2.chainId"),
        (MARKET_INDEX, {**MARKET_L2, "chainId": True}, "l2.chainId"),
        (MARKET_INDEX, {**MARKET_L2, "chainId": "421614"}, "l2.chainId"),
        (
            MARKET_INDEX,
            {**MARKET_L2, "proxyAddress": MARKET_L2["proxyAddress"][:-1]},
            "l2.proxyAddress",
        ),
        (MARKET_INDEX, {**MARKET_L2, "l2ChainId": 0}, "l2.l2ChainId"),
        (MARKET_INDEX, {**MARKET_L2, "l2ChainId": 2130706433}, "l2.l2ChainId"),
        (MARKET_INDEX, {**MARKET_L2, "baseAssetId": 0}, "l2.baseAssetId"),
        (MARKET_INDEX, {**MARKET_L2, "baseAssetId": 63}, "l2.baseAssetId"),
        (MARKET_INDEX, {**MARKET_L2, "quoteAssetId": 0}, "l2.quoteAssetId"),
        (MARKET_INDEX, {**MARKET_L2, "quoteAssetId": 63}, "l2.quoteAssetId"),
        (MARKET_INDEX, {**MARKET_L2, "quoteAssetId": 6}, "l2.quoteAssetId"),  # the base's id
    ],
)
def test_unusable_market(exchange, trading_client, market_index, l2, field):
    exchange.details["OPRA"] = market_detail(market_index=market_index, l2=l2)
    message = f"GET /trade/OPRA answered an unusable market.{field}"
    with pytest.raises(loaf.LoafServerError, match=re.escape(message)):
        trading_client().orders.limit_buy("OPRA", 10, 167.49)
    assert routes(exchange.requests) == ["GET /trade/OPRA"]


@pytest.mark.parametrize("kind", [..., None, "FUTURE", "spot", "perp"])
def test_unusable_market_type(exchange, trading_client, kind):
    # The type decides what the l2 holds and what an order sends: never guessed.
    detail = market_detail()
    if kind is ...:
        del detail["market"]["type"]
    else:
        detail["market"]["type"] = kind
    exchange.details["OPRA"] = detail
    message = "GET /trade/OPRA answered an unusable market.type"
    with pytest.raises(loaf.LoafServerError, match=re.escape(message)):
        trading_client().orders.limit_buy("OPRA", 10, 167.49)
    assert routes(exchange.requests) == ["GET /trade/OPRA"]


@pytest.mark.parametrize("market_index", [0, 254, 2048, 4094, 4095])
def test_any_12_bit_market_index_is_signed(exchange, trading_client, market_index):
    # 0 is a market (a perp's), never "none". The SDK checks only K1's 12 bits; the exchange
    # knows its markets.
    exchange.details["OPRA"] = market_detail(market_index=market_index)
    trading_client().orders.limit_buy("OPRA", 10, 167.49)
    (body,) = exchange.bodies("/orders")
    assert body["l2MarketIndex"] == market_index
    verify_order(body)


# --------------------------------------------------------------------------- #
# Account refusals: nothing kept, reserved or posted
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "keys, field",
    [
        ({"accountIndex": 6}, "accountIndex"),  # not your trading key's account
        ({"accountIndex": -1}, "accountIndex"),
        ({"accountIndex": True}, "accountIndex"),
        ({"accountIndex": "5"}, "accountIndex"),
        ({"accountIndex": None}, "accountIndex"),
        ({"epoch": None}, "epoch"),  # the key in force signed the token: it has an epoch
        ({"epoch": "2"}, "epoch"),
        ({"epoch": 2**48}, "epoch"),
        ({"epoch": True}, "epoch"),
    ],
)
def test_unusable_account(exchange, trading_client, keys, field):
    exchange.keys = {**KEYS, **keys}
    orders = trading_client().orders
    for _ in range(2):  # nothing is kept: the next order reads the account again
        with pytest.raises(loaf.LoafServerError) as info:
            orders.limit_buy("OPRA", 10, 167.49)
        assert info.value.message == f"GET /l2/keys answered an unusable {field}"
    # The market is kept, so the second order reads the account alone; nothing is reserved.
    assert routes(exchange.requests) == ["GET /trade/OPRA", "GET /l2/keys", "GET /l2/keys"]


def test_the_journaled_key_and_pending_are_not_read(exchange, trading_client):
    # The login token is checked against the key in force: GET /l2/keys is read for the epoch.
    exchange.keys = {**KEYS, "journaledKey": OTHER_KEY, "pending": True}
    assert trading_client().orders.limit_buy("OPRA", 10, 167.49).orderId == 1


@pytest.mark.parametrize(
    "key_in_force, reason",
    [(OTHER_KEY, BAD_SIGNATURE), (None, UNKNOWN_KEY), ("0x" + "0" * 80, KEY_DISABLED)],
    ids=["reset", "none", "disabled"],
)
def test_a_trading_key_not_in_force_is_refused_with_what_to_do(
    exchange, trading_client, key_in_force, reason
):
    # Reset in the web app (another key in force), no key in force yet, or disabled on L1: the
    # account read's login token is refused, and nothing is reserved or placed.
    exchange.key_in_force = key_in_force
    orders = trading_client().orders
    for _ in range(2):
        with pytest.raises(loaf.LoafAuthError) as info:
            orders.limit_buy("OPRA", 10, 167.49)
        assert info.value.message.startswith(reason + " — LOAF_TRADING_KEY is not the trading key")
        assert info.value.body == {"error": reason}
    # The refusal shows the key stale, not the market: the market is read once.
    assert routes(exchange.requests) == ["GET /trade/OPRA", "GET /l2/keys", "GET /l2/keys"]
    # Each refusal drops its token: the next request signs a fresh one.
    first, second = (r.headers["authorization"] for r in exchange.gets("/l2/keys"))
    assert first != second
    exchange.key_in_force = L2_PUBLIC_KEY
    assert orders.limit_buy("OPRA", 10, 167.49).orderId == 1


def test_a_market_on_another_chain_is_refused_before_anything_is_signed(
    exchange, trading_client
):
    # The trading key is another deployment's: its account and its chain are not this one's.
    exchange.details["OPRA"] = market_detail(l2={**MARKET_L2, "l2ChainId": 4243})
    orders = trading_client().orders
    for _ in range(2):
        with pytest.raises(loaf.LoafConfigError) as info:
            orders.limit_buy("OPRA", 10, 167.49)
        assert str(info.value) == (
            "OPRA's market is on L2 chain 4243 and your trading key is for L2 chain 4242: "
            "LOAF_TRADING_KEY is from another Loaf deployment than the one at http://test/api "
            "(LOAF_API_BASE_URL). Set LOAF_TRADING_KEY to the trading key this deployment's web "
            "app shows (README §2), or LOAF_API_BASE_URL to the key's deployment."
        )
    # The market is kept, read once; no login token is signed or sent.
    assert routes(exchange.requests) == ["GET /trade/OPRA"]


def test_other_keys_404_propagates(exchange, trading_client):
    exchange.queue("/l2/keys", error_reply(404, "Not found"))
    orders = trading_client().orders
    with pytest.raises(loaf.LoafNotFoundError, match="Not found") as info:
        orders.limit_buy("OPRA", 10, 167.49)
    assert type(info.value) is loaf.LoafNotFoundError
    assert orders.limit_buy("OPRA", 10, 167.49).orderId == 1
    # A read's 404 says nothing of the market, which is kept.
    assert routes(exchange.requests) == [
        "GET /trade/OPRA",
        "GET /l2/keys",
        "GET /l2/keys",
        "POST /l2/order-indices",
        "POST /orders",
    ]


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
    "answer, reads",
    [
        ({"accountIndex": True}, []),
        ({"accountIndex": 6}, []),  # another account's range
        ({"firstIndex": 0}, []),
        ({"count": 0}, []),
        ({"firstIndex": 2**40 - 1, "count": 2}, []),  # its last index does not fit 40 bits
        ({"reservedAtMs": 0}, []),
        ({"reservedAtMs": 2**53}, []),
        ({"reservedAtMs": True}, []),
    ],
)
def test_unusable_reservation(exchange, trading_client, answer, reads):
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
    # The account is kept: its index is the trading key's.
    assert routes(exchange.requests[sent:]) == reads + ["POST /l2/order-indices", "POST /orders"]


@pytest.mark.parametrize(
    "path, message",
    [
        ("/trade/OPRA", "GET /trade/OPRA answered no market"),
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
    # 42 days on, a 7-day order expires 52 days after that block's reservation: later than
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
        (404, "Not found", loaf.LoafNotFoundError),
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


def test_expiry_read_after_the_reservation(exchange, l2_clock):
    now = freeze(l2_clock)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/l2/order-indices":  # the reservation takes a minute
            freeze(l2_clock, now + 60_000)
        return exchange.handler(request)

    http = httpx.Client(transport=httpx.MockTransport(handler), base_url=BASE)
    client = LoafClient(TRADING_KEY, base_url=BASE, http_client=http)
    client.orders.limit_buy("OPRA", 10, 167.49)
    (body,) = exchange.bodies("/orders")
    assert body["l2ExpiryMs"] == now + 60_000 + L2_BUFFER_MS + DEFAULT_ORDER_TTL_MS


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
    with pytest.raises(loaf.LoafValidationError, match="tp_price must be above sl_price"):
        orders.market_buy("OPRA", 1, tp_price=90, sl_price=95)
    assert exchange.requests == []
    # Spot has no short to protect: a SELL's legs are refused once its market is read (a perps
    # SELL takes them).
    message = (
        "OPRA is a spot market: take-profit and stop-loss can only be attached to a BUY order "
        "there (spot has no short to protect)"
    )
    for place in (
        lambda: orders.create("OPRA", "SELL", 1, price=100, tp_price=90),
        lambda: orders.limit_sell("OPRA", 1, 100, tp_price=90, sl_price=110),
        lambda: orders.market_sell("OPRA", 1, reference_price=100, sl_price=110),
    ):
        with pytest.raises(loaf.LoafValidationError, match=re.escape(message)) as info:
            place()
        assert info.value.status_code == 0
    assert routes(exchange.requests) == ["GET /trade/OPRA"]  # read once, then remembered

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
    expiry = now + 259_200_000 + 604_800_000  # 72 h of buffer, then 7 days of life
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
        f"3,968,658,432 (about 45.9 days) (got {ttl!r})"
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
# Time in force
# --------------------------------------------------------------------------- #

POST_ONLY_WOULD_TAKE = "Post-only order rejected: it would have traded with a resting order at once"


@pytest.mark.parametrize("name", ["GTC", "IOC", "FOK", "POST_ONLY"])
@pytest.mark.parametrize("member", [False, True], ids=["text", "member"])
def test_a_limit_orders_time_in_force(exchange, trading_client, name, member):
    time_in_force = loaf.TimeInForce[name] if member else name
    orders = trading_client().orders
    orders.limit_buy("OPRA", 10, 167.49, time_in_force=time_in_force)
    orders.limit_sell("OPRA", 10, 170, time_in_force=time_in_force)
    orders.create("OPRA", "BUY", 10, price=167.49, time_in_force=time_in_force)
    bodies = exchange.bodies("/orders")
    assert [body["timeInForce"] for body in bodies] == [name] * 3
    for body in bodies:
        assert list(body) == ORDER_KEYS
        verify_order(body)
    # A stop or take stays GTC: the order it books when it fires rests.
    orders.stop_loss("OPRA", 2, 95)
    assert exchange.bodies("/orders/conditional")[-1]["timeInForce"] == "GTC"


@pytest.mark.parametrize("name", ["GTC", "IOC", "FOK"])
@pytest.mark.parametrize("member", [False, True], ids=["text", "member"])
def test_a_market_orders_time_in_force(exchange, trading_client, name, member):
    time_in_force = loaf.TimeInForce[name] if member else name
    trading_client().orders.create(
        "OPRA", "BUY", 2, type="MARKET", reference_price=160, time_in_force=time_in_force
    )
    (body,) = exchange.bodies("/orders")
    assert (body["type"], body["timeInForce"]) == ("MARKET", name)
    verify_order(body)


def test_an_order_is_gtc_by_default(exchange, trading_client):
    orders = trading_client().orders
    orders.limit_sell("OPRA", 10, 170)
    orders.market_buy("OPRA", 2)  # the exchange runs a MARKET GTC as IOC: it never rests
    orders.create("OPRA", "SELL", 2, type="MARKET", reference_price=160)
    assert [body["timeInForce"] for body in exchange.bodies("/orders")] == ["GTC"] * 3


@pytest.mark.parametrize("member", [False, True], ids=["text", "member"])
def test_post_only_on_a_market_order_is_refused_locally(exchange, trading_client, member):
    time_in_force = loaf.TimeInForce["POST_ONLY"] if member else "POST_ONLY"
    with pytest.raises(loaf.LoafValidationError) as info:
        trading_client().orders.create(
            "OPRA", "BUY", 2, type="MARKET", reference_price=160, time_in_force=time_in_force
        )
    assert (info.value.status_code, info.value.message) == (
        0,
        "time_in_force: POST_ONLY requires a LIMIT order",
    )
    assert exchange.requests == []


@pytest.mark.parametrize("time_in_force", ["GTD", "gtc", "ioc", "", "POST ONLY", None, 0, True])
def test_an_unknown_time_in_force_is_refused_locally(exchange, trading_client, time_in_force):
    orders = trading_client().orders
    message = f"time_in_force must be GTC, IOC, FOK or POST_ONLY (got {time_in_force!r})"
    for place in (
        lambda: orders.limit_buy("OPRA", 10, 167.49, time_in_force=time_in_force),
        lambda: orders.limit_sell("OPRA", 10, 170, time_in_force=time_in_force),
        lambda: orders.create("OPRA", "BUY", 2, type="MARKET", time_in_force=time_in_force),
    ):
        with pytest.raises(loaf.LoafValidationError, match=re.escape(message)) as info:
            place()
        assert info.value.status_code == 0
    assert exchange.requests == []


def test_a_post_only_order_that_would_take_is_its_own_error(exchange, trading_client):
    orders = trading_client().orders
    exchange.queue("/orders", error_reply(400, POST_ONLY_WOULD_TAKE))
    with pytest.raises(loaf.PostOnlyWouldTakeError) as info:
        orders.limit_buy("OPRA", 10, 167.49, time_in_force="POST_ONLY")
    error = info.value
    assert isinstance(error, loaf.LoafValidationError)
    assert (error.status_code, error.message) == (400, POST_ONLY_WOULD_TAKE)
    # Nothing was placed: the next order signs the same order number.
    orders.limit_buy("OPRA", 10, 160, time_in_force="POST_ONLY")
    refused, placed = exchange.bodies("/orders")
    assert refused["l2OrderIndex"] == placed["l2OrderIndex"] == 4097
    verify_order(placed)


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
        market_detail(market_price=0),  # the exchange has no reference yet
        {
            **market_detail(),
            "marketList": [{"ticker": "MUS", "name": "Musgrave", "marketPrice": 50}],
        },
        # Matched on the ticker: an entry whose display name happens to read OPRA is not it.
        {
            **market_detail(),
            "marketList": [{"ticker": "MUS", "name": "OPRA", "marketPrice": 50}],
        },
        # Nor is another market of its asset: the perp's ticker is not the spot market's.
        {
            **market_detail(),
            "marketList": [{"ticker": "OPRA-PERP", "name": "Opera", "marketPrice": 50}],
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


def test_placing_and_cancelling_need_the_trading_key(exchange, trading_client):
    keyless = trading_client(trading_key=None)
    assert keyless.public_key is None
    for place in (
        lambda orders: orders.limit_buy("OPRA", 10, 167.49),
        lambda orders: orders.stop_loss("OPRA", 5, 90),
    ):
        with pytest.raises(loaf.LoafConfigError) as info:
            place(keyless.orders)
        assert str(info.value) == (
            "Placing orders needs your trading key: set $LOAF_TRADING_KEY to the web app's Show "
            "trading key (README §2), or pass trading_key=..."
        )
    # Its login token authenticates a cancel.
    with pytest.raises(loaf.LoafConfigError, match=r"\$LOAF_TRADING_KEY"):
        keyless.orders.cancel(1)
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
        "Trading is temporarily unavailable for this market. Your order was not placed — "
        "please try again in a moment.",
        "Trading is temporarily unavailable; your order was not placed, try again in a moment",
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
    # The market, the account and the reservation keep the client's timeout; the
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
    client = LoafClient(TRADING_KEY, base_url=BASE, http_client=http)
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
        httpx.Response(200, json={"markets": []}),
    ]
    http = httpx.Client(transport=httpx.MockTransport(lambda r: replies.pop(0)), base_url=BASE)
    LoafClient(base_url=BASE, http_client=http).market.list()
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
    client = LoafClient(TRADING_KEY, base_url=BASE, http_client=http)
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
    "status, message, error",
    [
        (400, STALE_EPOCH, loaf.LoafValidationError),
        (400, WRONG_CHAIN, loaf.LoafValidationError),
        (400, "l2 market: the order's L2 market is not active", loaf.LoafValidationError),
        (
            400,
            "l2 asset: the base asset id is not the market's base asset",
            loaf.LoafValidationError,
        ),
        # Matched anywhere in the text: a leg's refusal comes prefixed with its key.
        (401, "tp: " + BAD_SIGNATURE, loaf.LoafAuthError),
        (401, "l2 key: this trading key has been disabled", loaf.LoafAuthError),
        (401, "l2 account: no trading account has this account index", loaf.LoafAuthError),
    ],
)
def test_context_refusal_rereads_market_and_account(
    exchange, trading_client, status, message, error
):
    orders = trading_client().orders
    orders.limit_buy("OPRA", 10, 167.49)
    exchange.queue("/orders", error_reply(status, message))
    with pytest.raises(error) as info:
        orders.limit_buy("OPRA", 10, 167.49)  # reserves 2 and takes 4098
    assert type(info.value) is error
    assert len(exchange.posts("/orders")) == 2  # never re-signed or re-sent
    sent = len(exchange.requests)
    orders.limit_buy("OPRA", 10, 167.49)
    # The market and the account are read again; the refused order's number is signed again.
    assert routes(exchange.requests[sent:]) == ["GET /trade/OPRA", "GET /l2/keys", "POST /orders"]
    assert indices(exchange) == [4097, 4098, 4098]


def test_a_refused_reservation_rereads_the_account_alone(exchange, trading_client):
    # The reservation's login token is refused (the key was reset since the account read): the
    # account is read again, but the market is kept, since the reservation never names it.
    orders = trading_client().orders
    exchange.queue("/l2/order-indices", error_reply(401, BAD_SIGNATURE))
    with pytest.raises(loaf.LoafAuthError):
        orders.limit_buy("OPRA", 10, 167.49)
    sent = len(exchange.requests)
    orders.limit_buy("OPRA", 10, 167.49)
    assert routes(exchange.requests[sent:]) == [
        "GET /l2/keys", "POST /l2/order-indices", "POST /orders"
    ]


@pytest.mark.parametrize(
    "status, message, error",
    [
        (404, "Market not found", loaf.LoafNotFoundError),
        (403, DELISTED, loaf.LoafForbiddenError),
        (403, "This market is not yet available for trading", loaf.LoafForbiddenError),
    ],
    ids=["no-market", "delisted", "pending"],
)
def test_market_refusal_rereads_the_market(exchange, trading_client, status, message, error):
    # The order names its market by the signed index alone: refused as no market, or as one that
    # does not trade, it reads the ticker's market again, which may be another one by now.
    orders = trading_client().orders
    orders.limit_buy("OPRA", 10, 167.49)
    exchange.queue("/orders", error_reply(status, message))
    with pytest.raises(error) as info:
        orders.limit_buy("OPRA", 10, 167.49)  # reserves 2 and takes 4098
    assert type(info.value) is error
    exchange.details["OPRA"] = market_detail(market_index=2052)  # OPRA's market now
    sent = len(exchange.requests)
    orders.limit_buy("OPRA", 10, 167.49)
    # The market alone is read again; the refused order's number is signed again.
    assert routes(exchange.requests[sent:]) == ["GET /trade/OPRA", "POST /orders"]
    assert [body["l2MarketIndex"] for body in exchange.bodies("/orders")] == [2050, 2050, 2052]
    assert indices(exchange) == [4097, 4098, 4098]


@pytest.mark.parametrize(
    "message, error",
    [
        ("Trading is halted on this market", loaf.MarketHaltedError),
        ("Trading is currently halted", loaf.TradingHaltedError),
        ("Trading on this market has not started yet", loaf.LoafForbiddenError),
    ],
)
def test_halt_or_start_not_reached_keeps_the_market(exchange, trading_client, message, error):
    # The market is the ticker's still: an order placed until it trades reads nothing again.
    orders = trading_client().orders
    orders.limit_buy("OPRA", 10, 167.49)
    exchange.queue("/orders", error_reply(403, message))
    with pytest.raises(error) as info:
        orders.limit_buy("OPRA", 10, 167.49)
    assert type(info.value) is error
    sent = len(exchange.requests)
    orders.limit_buy("OPRA", 10, 167.49)
    assert routes(exchange.requests[sent:]) == ["POST /orders"]


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
    l2, account = pool(trading_client())
    l2.remember("OPRA", market_of(market_detail(), "OPRA"))
    _, block, _ = l2.take(account, 1, DEFAULT_ORDER_TTL_MS)
    epoch_refusal = error_from_response(400, {"error": STALE_EPOCH})
    # An order signed with the account as it was before another thread read it again.
    l2.after_refusal("OPRA", account._replace(epoch=1), block, epoch_refusal)
    assert l2.market("OPRA") is None  # the market is dropped whatever it was
    assert l2.account(ACCOUNT_INDEX) == account
    assert len(exchange.gets("/l2/keys")) == 1  # the fresh read is kept
    l2.after_refusal("OPRA", account, block, epoch_refusal)
    l2.account(ACCOUNT_INDEX)
    assert len(exchange.gets("/l2/keys")) == 2  # dropped: read again


def test_index_refusal_of_a_replaced_block_keeps_the_new_one(exchange, trading_client):
    l2, account = pool(trading_client())
    _, old, _ = l2.take(account, 1, DEFAULT_ORDER_TTL_MS)  # reserves 1
    _, new, _ = l2.take(account, 1, DEFAULT_ORDER_TTL_MS)  # another thread: reserves 2
    assert new is not old
    # The refusal of an order from the old block arrives after the new one was reserved.
    l2.after_refusal("OPRA", account, old, error_from_response(400, {"error": INDEX_USED}))
    first, block, _ = l2.take(account, 1, DEFAULT_ORDER_TTL_MS)
    assert (first, block) == (4099, new)
    assert counts(exchange) == [1, 2]


# --------------------------------------------------------------------------- #
# Give-back: a definitely refused order's numbers are signed again
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "replies",
    [
        lambda: [error_reply(400, OPEN_ORDER_CAP)],
        lambda: [error_reply(403, DELISTED)],
        lambda: [error_reply(429, "Too many requests for this account. Please slow down.")],
        lambda: [error_reply(503, NOT_PLACED) for _ in range(4)],
        lambda: [httpx.ConnectError("unreachable") for _ in range(4)],
    ],
    ids=["400-cap", "403-delisted", "429", "503-not-placed", "connect-error"],
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
    l2, account = pool(trading_client())
    for _ in range(3):  # reserves 1, then 2
        l2.take(account, 1, DEFAULT_ORDER_TTL_MS)
    older, block, _ = l2.take(account, 1, DEFAULT_ORDER_TTL_MS)  # reserves 4
    newer, same, _ = l2.take(account, 1, DEFAULT_ORDER_TTL_MS)
    assert (older, newer, same) == (4100, 4101, block)
    l2.give_back(block, older, 1)  # a later order holds the next number: kept for later
    assert block.next == 4102
    # An order with a leg takes consecutive numbers, never the one given back...
    assert l2.take(account, 2, DEFAULT_ORDER_TTL_MS)[:2] == (4102, block)
    # ...which the next single order signs.
    assert l2.take(account, 1, DEFAULT_ORDER_TTL_MS)[:2] == (older, block)
    # The block has no number left at its end, and one given back still serves an order.
    assert block.next == block.end
    l2.give_back(block, newer, 1)
    assert l2.take(account, 1, DEFAULT_ORDER_TTL_MS)[:2] == (newer, block)
    assert counts(exchange) == [1, 2, 4]


def test_concurrent_refusals_give_their_numbers_back(exchange, trading_client):
    # Four orders in flight at once, each refused after the next took its number: only the last
    # one's is the block's latest.
    l2, account = pool(trading_client())
    for _ in range(3):  # reserves 1, then 2
        l2.take(account, 1, DEFAULT_ORDER_TTL_MS)
    taken = [l2.take(account, 1, DEFAULT_ORDER_TTL_MS) for _ in range(4)]  # reserves 4
    block = taken[0][1]
    for first, _, _ in taken:
        l2.give_back(block, first, 1)
    # The last one's number goes back to the block's end; the others are kept for later.
    assert (block.next, block.returned) == (4103, [(4100, 1), (4101, 1), (4102, 1)])
    again = [l2.take(account, 1, DEFAULT_ORDER_TTL_MS)[0] for _ in range(4)]
    assert again == [first for first, _, _ in taken] == [4100, 4101, 4102, 4103]
    assert counts(exchange) == [1, 2, 4]  # nothing reserved for them


def test_a_span_given_back_is_kept_whole(exchange, trading_client):
    l2, account = pool(trading_client())
    l2.take(account, 3, DEFAULT_ORDER_TTL_MS)  # reserves 3
    l2.take(account, 1, DEFAULT_ORDER_TTL_MS)  # reserves 6
    first, block, _ = l2.take(account, 3, DEFAULT_ORDER_TTL_MS)
    l2.take(account, 1, DEFAULT_ORDER_TTL_MS)  # a later order holds the next number
    l2.give_back(block, first, 3)
    # A single order takes the block's next number, never part of the three...
    assert l2.take(account, 1, DEFAULT_ORDER_TTL_MS)[:2] == (4105, block)
    # ...which the next order with two legs signs.
    assert l2.take(account, 3, DEFAULT_ORDER_TTL_MS)[:2] == (first, block)
    assert counts(exchange) == [3, 6]


def test_a_number_given_back_serves_only_expiries_its_block_admits(
    exchange, trading_client, l2_clock
):
    now = freeze(l2_clock)
    exchange.reserved_at_ms = now
    l2, account = pool(trading_client())
    for _ in range(3):  # reserves 1, then 2
        l2.take(account, 1, DEFAULT_ORDER_TTL_MS)
    first, block, _ = l2.take(account, 1, DEFAULT_ORDER_TTL_MS)  # reserves 4
    second = l2.take(account, 1, DEFAULT_ORDER_TTL_MS)[0]
    l2.take(account, 1, DEFAULT_ORDER_TTL_MS)  # a later order holds the next number
    for given_back in (first, second):
        l2.give_back(block, given_back, 1)
    # Ten hours on, the block's range still admits a 7-day order...
    exchange.reserved_at_ms = freeze(l2_clock, now + 10 * HOUR_MS)
    assert l2.take(account, 1, DEFAULT_ORDER_TTL_MS)[:2] == (first, block)
    # ...but not a max-life one, which the exchange would refuse when it books it (a stop or take
    # only when it fires, so it would FAIL then): that one signs a number from a fresh range.
    assert l2.take(account, 1, MAX_ORDER_TTL_MS)[0] == 4104
    assert counts(exchange) == [1, 2, 4, 8]


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

    # The body carries its own signature: no trading key is needed to re-send it.
    bare = trading_client(trading_key=None)
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
    assert "(its l2ExpiryMs minus 72 hours)" in refusal.message  # L2_BUFFER_MS
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
# Signing with the trading key
# --------------------------------------------------------------------------- #


def test_signing_failure_leaves_no_trace(exchange, trading_client, monkeypatch):
    client = trading_client()
    orders = client.orders
    pool(client)  # the account, read with a login token signed before the failure below
    with monkeypatch.context() as m:
        # k1.sign itself refuses (31 random bytes), with the secret scalar in its frame.
        m.setattr("loaf._l2.os", SimpleNamespace(urandom=lambda n: bytes(n - 1)))
        with pytest.raises(loaf.LoafError) as info:
            orders.limit_buy("OPRA", 10, 100, tp_price=120, sl_price=90)
    error = info.value
    assert type(error) is loaf.LoafError
    assert str(error) == (
        "Signing with your trading key failed (K1Error: l2 signer: the random bytes must be 32 "
        "bytes); nothing was sent."
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


# --------------------------------------------------------------------------- #
# Local guards
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "ticker, side",
    [
        ("opra", "BUY"),  # sent exactly as given: never upper-cased for you
        ("Opra", "BUY"),
        ("OPERAHOUSES", "BUY"),  # at most 10, -PERP aside
        ("OPERAHOUSES-PERP", "BUY"),
        ("", "BUY"),
        ("-PERP", "BUY"),
        ("OPR-perp", "BUY"),
        ("OPR-SPOT", "BUY"),
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


@pytest.mark.parametrize("ticker", ["A", "MUS", "OPRA", "X1", "1234", "OPERAHOUSE", "H100-PERP"])
def test_ticker_accepted(exchange, trading_client, ticker):
    exchange.details[ticker] = market_detail(ticker, market_index=7)
    trading_client().orders.limit_buy(ticker, 1, 100)
    (body,) = exchange.bodies("/orders")
    # The ticker names the market to read; the body names it by the index it signs.
    assert "ticker" not in body and body["l2MarketIndex"] == 7
    assert len(exchange.gets(f"/trade/{ticker}")) == 1

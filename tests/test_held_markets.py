"""What an order signs of its market is held: the market's index, its quantity decimals and
its assets, from ``LoafClient(markets=)`` or from the ticker's first read, against every later
answer of the API.

An order signs bare integers. Signed in the units of another answer, the body the SDK posts as
BUY 10 @ 163.2 is the exchange's BUY 0.1 @ 16,320 under the same signature; signed for another
index, it is an order on another market at this one's price. So the tests here assert what
leaves the client: after an answer in other terms, no reservation and no order.
"""

from __future__ import annotations

import json
import pickle
import threading
from types import MappingProxyType
from typing import Any

import httpx
import pytest

import loaf
from loaf._l2 import named_markets

from conftest import (
    MARKET_INDEX,
    MARKET_L2,
    PERP_L2,
    PERP_MARKET_INDEX,
    market_detail,
    routes,
    signed_decimals,
    verify_order,
)

# OPRA and OPRA-PERP as listed (conftest), in the shape LoafClient(markets=) takes.
OPRA = {"marketIndex": MARKET_INDEX, "quantityDecimals": 3, "baseAssetId": 6, "quoteAssetId": 3}
OPRA_PERP = {"marketIndex": PERP_MARKET_INDEX, "quantityDecimals": 3}
CANCELLED = {"requestedCount": 1, "cancelledOrderIds": [9], "failedOrders": []}


def opra(
    *,
    market_index: int = MARKET_INDEX,
    decimals: tuple[int, int] = (2, 3),
    base: int = 6,
    quote: int = 3,
    market_price: float = 160,
) -> dict:
    """OPRA's detail as an API serves it in those terms, consistent in itself (its signed
    decimals are its quantity decimals'), so nothing in the answer alone is unusable."""
    l2 = {**MARKET_L2, "baseAssetId": base, "quoteAssetId": quote, **signed_decimals(decimals[1])}
    return market_detail(
        market_index=market_index, l2=l2, decimals=decimals, market_price=market_price
    )


def not_found() -> httpx.Response:
    return httpx.Response(404, json={"error": "Market not found"})


# One term an order signs, served differently: (the detail's change, the field named, held, served).
CHANGES = [
    pytest.param({"market_index": 2051}, "marketIndex", 2050, 2051, id="index"),
    pytest.param({"decimals": (2, 1)}, "quantityDecimals", 3, 1, id="units"),
    pytest.param({"base": 7}, "l2.baseAssetId", 6, 7, id="base"),
    pytest.param({"quote": 4}, "l2.quoteAssetId", 3, 4, id="quote"),
]


def refused(info: pytest.ExceptionInfo) -> tuple[str, str, int, int]:
    error = info.value
    assert type(error) is loaf.MarketChangedError
    assert error.status_code == 200
    return error.ticker, error.field, error.held, error.served


# --------------------------------------------------------------------------- #
# Held from the first read
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("change, field, held, served", CHANGES)
def test_other_terms_on_a_market_orders_read_sign_nothing(
    exchange, trading_client, change, field, held, served
):
    orders = trading_client().orders
    orders.limit_buy("OPRA", 10, 167.49)
    exchange.details["OPRA"] = opra(**change)
    sent = len(exchange.requests)
    with pytest.raises(loaf.MarketChangedError) as info:
        orders.market_buy("OPRA", 10)  # no reference_price: it reads the detail for its reference
    assert refused(info) == ("OPRA", field, held, served)
    assert routes(exchange.requests[sent:]) == ["GET /trade/OPRA"]  # nothing reserved or sent
    assert len(exchange.posts("/orders")) == 1


@pytest.mark.parametrize("change, field, held, served", CHANGES)
def test_other_terms_after_a_refusal_sign_nothing(
    exchange, trading_client, change, field, held, served
):
    # A refusal that shows the market stale has the next order read it again: the refusal comes
    # through the API, as the answer does.
    orders = trading_client().orders
    orders.limit_buy("OPRA", 10, 167.49)
    exchange.queue("/orders", not_found())
    with pytest.raises(loaf.LoafNotFoundError):
        orders.limit_buy("OPRA", 10, 167.49)
    exchange.details["OPRA"] = opra(**change)
    sent = len(exchange.requests)
    with pytest.raises(loaf.MarketChangedError) as info:
        orders.limit_buy("OPRA", 10, 167.49)
    assert refused(info) == ("OPRA", field, held, served)
    assert routes(exchange.requests[sent:]) == ["GET /trade/OPRA"]
    assert len(exchange.posts("/orders")) == 2


def test_the_first_term_that_differs_is_named(exchange, trading_client):
    orders = trading_client().orders
    orders.warm("OPRA")
    change: dict[str, Any]
    for change, field in (
        ({"market_index": 2051, "decimals": (2, 1), "base": 7, "quote": 4}, "marketIndex"),
        ({"decimals": (2, 1), "base": 7, "quote": 4}, "quantityDecimals"),
        ({"base": 7, "quote": 4}, "l2.baseAssetId"),
    ):
        exchange.details["OPRA"] = opra(**change)
        with pytest.raises(loaf.MarketChangedError) as info:
            orders.warm("OPRA")
        assert info.value.field == field


def test_the_market_held_trades_again_once_it_is_served_again(exchange, trading_client):
    orders = trading_client().orders
    orders.limit_buy("OPRA", 10, 167.49)
    exchange.details["OPRA"] = opra(decimals=(2, 1))
    for _ in range(2):  # refused each time: the answer refused is not kept either
        with pytest.raises(loaf.MarketChangedError):
            orders.market_buy("OPRA", 10)
    orders.limit_sell("OPRA", 10, 167.49)  # reads nothing: the market kept is the one held
    exchange.details["OPRA"] = opra()
    orders.market_buy("OPRA", 10)
    bodies = exchange.bodies("/orders")
    # The refused calls took no order number, and every order is signed in the units first read.
    assert [body["l2OrderIndex"] for body in bodies] == [4097, 4098, 4099]
    assert [body["type"] for body in bodies] == ["LIMIT", "LIMIT", "MARKET"]
    for body in bodies:
        verify_order(body)


def test_warm_holds_the_market_and_is_held_to_it(exchange, trading_client):
    orders = trading_client().orders
    orders.warm("OPRA")
    exchange.details["OPRA"] = opra(decimals=(2, 1))
    with pytest.raises(loaf.MarketChangedError) as info:
        orders.warm("OPRA")
    assert refused(info) == ("OPRA", "quantityDecimals", 3, 1)
    with pytest.raises(loaf.MarketChangedError):
        orders.market_buy("OPRA", 10)
    assert exchange.posts("/orders") == []
    orders.limit_buy("OPRA", 10, 167.49)  # on the market warmed
    verify_order(exchange.bodies("/orders")[0])


def test_what_an_order_does_not_sign_is_read_again(exchange, trading_client):
    # The tick (priceDecimals) and the reference scale nothing signed, and change: a MARKET order
    # prices itself off the answer it just read, on that answer's tick.
    orders = trading_client().orders
    orders.limit_buy("OPRA", 10, 167.49)
    exchange.details["OPRA"] = opra(decimals=(3, 3), market_price=160.125)
    orders.market_buy("OPRA", 2, max_slippage_bps=0)
    body = exchange.bodies("/orders")[1]
    assert body["price"] == 160.125  # off the cent grid the market was first read with
    verify_order(body)
    assert orders.held_markets() == {"OPRA": OPRA}


def test_a_perp_is_held_by_its_index_and_units(exchange, trading_client):
    exchange.details["OPRA-PERP"] = market_detail("OPRA-PERP")
    orders = trading_client().orders
    orders.limit_buy("OPRA-PERP", 2, 101.5)
    change: dict[str, Any]
    for change, field, served in (
        ({"market_index": 5}, "marketIndex", 5),
        ({"decimals": (2, 2)}, "quantityDecimals", 2),
    ):
        exchange.details["OPRA-PERP"] = market_detail("OPRA-PERP", **change)
        sent = len(exchange.requests)
        with pytest.raises(loaf.MarketChangedError) as info:
            orders.market_buy("OPRA-PERP", 2)
        assert refused(info) == ("OPRA-PERP", field, OPRA_PERP[field], served)
        assert routes(exchange.requests[sent:]) == ["GET /trade/OPRA-PERP"]
    # Its band and status are terms that change, and are read again: no refusal.
    exchange.details["OPRA-PERP"] = market_detail("OPRA-PERP", l2={**PERP_L2, "band": 200})
    orders.warm("OPRA-PERP")
    assert len(exchange.posts("/orders")) == 1
    assert orders.held_markets() == {"OPRA-PERP": OPRA_PERP}


def test_a_new_client_reads_afresh(exchange, trading_client):
    # The way out of a hold, when a ticker really was listed again: a new client.
    first = trading_client().orders
    first.limit_buy("OPRA", 10, 167.49)
    exchange.details["OPRA"] = opra(market_index=2052)
    with pytest.raises(loaf.MarketChangedError):
        first.market_buy("OPRA", 10)
    trading_client().orders.limit_buy("OPRA", 10, 167.49)
    assert [body["l2MarketIndex"] for body in exchange.bodies("/orders")] == [2050, 2052]


def test_two_first_reads_at_once_hold_one_market(exchange, trading_client):
    # Both placements read before either is answered, and the answers differ: one market is
    # held, the other answer is refused, whichever thread comes first.
    barrier = threading.Barrier(2)
    answer = exchange.handler

    def together(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/trade/OPRA":
            barrier.wait(timeout=10)
        return answer(request)

    exchange.handler = together
    orders = trading_client().orders
    exchange.queue(
        "/trade/OPRA",
        httpx.Response(200, json=opra()),
        httpx.Response(200, json=opra(decimals=(2, 1))),
    )
    results: list[Any] = []

    def place() -> None:
        try:
            orders.limit_buy("OPRA", 10, 167.49)
            results.append("placed")
        except loaf.MarketChangedError as exc:
            results.append(exc.field)

    threads = [threading.Thread(target=place) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert sorted(results) == ["placed", "quantityDecimals"]
    (body,) = exchange.bodies("/orders")
    units = orders.held_markets()["OPRA"]["quantityDecimals"]
    verify_order(body, l2={**MARKET_L2, **signed_decimals(units)})  # signed in the units held


# --------------------------------------------------------------------------- #
# Held from LoafClient(markets=)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("change, field, held, served", CHANGES)
def test_a_named_market_holds_the_first_answer(
    exchange, trading_client, change, field, held, served
):
    exchange.details["OPRA"] = opra(**change)  # served so from the start
    orders = trading_client(markets={"OPRA": OPRA}).orders
    for place in (
        lambda: orders.limit_buy("OPRA", 10, 167.49),
        lambda: orders.market_buy("OPRA", 10),
        lambda: orders.warm("OPRA"),
    ):
        with pytest.raises(loaf.MarketChangedError) as info:
            place()
        assert refused(info) == ("OPRA", field, held, served)
    # Never an account read, a reservation or an order.
    assert routes(exchange.requests) == ["GET /trade/OPRA"] * 3


def test_a_named_market_served_as_named_trades(exchange, trading_client):
    exchange.details["OPRA-PERP"] = market_detail("OPRA-PERP")
    orders = trading_client(markets={"OPRA": OPRA, "OPRA-PERP": OPRA_PERP}).orders
    orders.limit_buy("OPRA", 10, 167.49)
    orders.market_buy("OPRA", 2)
    orders.limit_buy("OPRA-PERP", 2, 101.5)
    spot, market, perp = exchange.bodies("/orders")
    verify_order(spot)
    verify_order(market)
    verify_order(perp, l2=PERP_L2)
    assert (spot["l2MarketIndex"], perp["l2MarketIndex"]) == (MARKET_INDEX, PERP_MARKET_INDEX)


@pytest.mark.parametrize(
    "change, field, served",
    [({"market_index": 5}, "marketIndex", 5), ({"decimals": (2, 2)}, "quantityDecimals", 2)],
    ids=["index", "units"],
)
def test_a_named_perp_holds_the_first_answer(exchange, trading_client, change, field, served):
    exchange.details["OPRA-PERP"] = market_detail("OPRA-PERP", **change)
    orders = trading_client(markets={"OPRA-PERP": OPRA_PERP}).orders
    with pytest.raises(loaf.MarketChangedError) as info:
        orders.limit_buy("OPRA-PERP", 2, 101.5)
    assert refused(info) == ("OPRA-PERP", field, OPRA_PERP[field], served)
    assert routes(exchange.requests) == ["GET /trade/OPRA-PERP"]


def test_a_ticker_with_no_entry_is_held_to_its_first_read(exchange, trading_client):
    # What a table that does not name the ticker leaves open, as without a table: the first
    # answer is taken as served. From then on it is the market held.
    exchange.details["OPRA"] = opra(decimals=(2, 1))
    orders = trading_client(markets={"OPRA-PERP": OPRA_PERP}).orders
    orders.limit_buy("OPRA", 10, 167.4)
    body = exchange.bodies("/orders")[0]
    verify_order(body, l2={**MARKET_L2, **signed_decimals(1)})
    # Why a table matters: at OPRA's listed 3 quantity decimals, the same signature is the
    # exchange's BUY 0.1 @ 16,740.
    verify_order({**body, "price": body["price"] * 100, "quantity": body["quantity"] / 100})
    exchange.details["OPRA"] = opra()
    with pytest.raises(loaf.MarketChangedError) as info:
        orders.market_buy("OPRA", 10)
    assert refused(info) == ("OPRA", "quantityDecimals", 1, 3)
    assert "this client first read" in info.value.message


def test_held_markets_is_the_table_markets_takes(exchange, trading_client):
    exchange.details["OPRA-PERP"] = market_detail("OPRA-PERP")
    first = trading_client().orders
    assert first.held_markets() == {}
    first.warm("OPRA-PERP")
    first.limit_buy("OPRA", 10, 167.49)
    held = first.held_markets()
    assert held == {"OPRA": OPRA, "OPRA-PERP": OPRA_PERP}
    assert list(held) == ["OPRA", "OPRA-PERP"]  # by ticker
    sent = len(exchange.requests)
    second = trading_client(markets=json.loads(json.dumps(held))).orders  # as kept in a file
    assert second.held_markets() == held  # from the start, before any read
    assert len(exchange.requests) == sent
    held["OPRA"]["marketIndex"] = 1  # a copy: changing it changes nothing held
    assert second.held_markets()["OPRA"] == OPRA
    assert first.held_markets()["OPRA"] == OPRA


def test_a_table_is_read_once(exchange, trading_client):
    # The table is taken at construction: changed afterwards, it changes nothing held.
    table = {"OPRA": dict(OPRA)}
    orders = trading_client(markets=table).orders
    table["OPRA"]["quantityDecimals"] = 1
    table["MUS"] = dict(OPRA)
    assert orders.held_markets() == {"OPRA": OPRA}
    orders.limit_buy("OPRA", 10, 167.49)
    verify_order(exchange.bodies("/orders")[0])


# --------------------------------------------------------------------------- #
# A cancel's scope
# --------------------------------------------------------------------------- #


def test_a_cancels_scope_is_a_named_markets_index_unread(exchange, trading_client):
    orders = trading_client(markets={"OPRA": OPRA}).orders
    exchange.queue("/orders/cancel-all", httpx.Response(200, json=CANCELLED))
    assert orders.cancel_all("OPRA") == CANCELLED
    assert routes(exchange.requests) == ["POST /orders/cancel-all"]
    assert json.loads(exchange.requests[0].content) == {"marketIndex": MARKET_INDEX}


def test_a_cancels_scope_stays_the_market_its_orders_signed(exchange, trading_client):
    # The orders to cancel rest on the market held. A refusal drops the market kept, not the
    # hold: the scope is not read again from an API that now serves another index.
    orders = trading_client().orders
    orders.limit_buy("OPRA", 10, 167.49)
    exchange.queue("/orders", not_found())
    with pytest.raises(loaf.LoafNotFoundError):
        orders.limit_buy("OPRA", 10, 167.49)
    exchange.details["OPRA"] = opra(market_index=2051)
    exchange.queue("/orders/cancel-all", httpx.Response(200, json=CANCELLED))
    sent = len(exchange.requests)
    orders.cancel_all("OPRA")
    assert routes(exchange.requests[sent:]) == ["POST /orders/cancel-all"]
    assert json.loads(exchange.requests[-1].content) == {"marketIndex": MARKET_INDEX}


# --------------------------------------------------------------------------- #
# The table and the error
# --------------------------------------------------------------------------- #


def without(terms: dict, key: str) -> dict:
    return {k: v for k, v in terms.items() if k != key}


@pytest.mark.parametrize(
    "markets, names",
    [
        ([("OPRA", OPRA)], "markets must map each ticker"),
        ("OPRA", "markets must map each ticker"),
        (7, "markets must map each ticker"),
        ({"opra": OPRA}, "markets: 'opra' is not a market's ticker"),
        ({5: OPRA}, "markets: 5 is not a market's ticker"),
        ({"OPRA-perp": OPRA_PERP}, "markets: 'OPRA-perp' is not a market's ticker"),
        ({"": OPRA}, "markets: '' is not a market's ticker"),
        ({"OPRA": None}, "markets['OPRA'] must give"),
        ({"OPRA": [2050, 3, 6, 3]}, "markets['OPRA'] must give"),
        ({"OPRA": without(OPRA, "quoteAssetId")}, "markets['OPRA'] must give"),
        ({"OPRA": without(OPRA, "marketIndex")}, "markets['OPRA'] must give"),
        ({"OPRA": {**OPRA, "priceDecimals": 2}}, "markets['OPRA'] must give"),
        # A misspelt key would hold nothing.
        (
            {"OPRA": {**without(OPRA, "quantityDecimals"), "quantity_decimals": 3}},
            "markets['OPRA'] must give",
        ),
        ({"OPRA": OPRA_PERP}, "markets['OPRA'] must give"),  # a spot market names its assets
        (
            {"OPRA-PERP": {**OPRA_PERP, "baseAssetId": 0, "quoteAssetId": 3}},
            "markets['OPRA-PERP'] must give marketIndex and quantityDecimals (a perp's terms)",
        ),
        ({"OPRA-PERP": without(OPRA_PERP, "quantityDecimals")}, "markets['OPRA-PERP'] must give"),
        *(
            ({"OPRA": {**OPRA, key: value}}, "markets['OPRA'] must give")
            for key in OPRA
            for value in ("3", 3.0, True, None)
        ),
    ],
)
def test_a_table_that_is_none_is_a_config_error(exchange, trading_client, markets, names):
    with pytest.raises(loaf.LoafConfigError) as info:
        trading_client(markets=markets)
    assert str(info.value).startswith(names)
    assert exchange.requests == []


def test_a_spot_tables_refusal_names_its_four_terms():
    with pytest.raises(loaf.LoafConfigError) as info:
        named_markets({"OPRA": {"marketIndex": 2050}})
    assert str(info.value) == (
        "markets['OPRA'] must give marketIndex, quantityDecimals, baseAssetId and quoteAssetId "
        "(a spot market's terms), each a whole number, and nothing else, got "
        "{'marketIndex': 2050}"
    )


@pytest.mark.parametrize(
    "markets",
    [None, {}, {"OPRA": OPRA}, MappingProxyType({"OPRA": MappingProxyType(OPRA)})],
    ids=["none", "empty", "dict", "read-only"],
)
def test_a_table_or_none_is_taken(exchange, trading_client, markets):
    orders = trading_client(markets=markets).orders
    assert orders.held_markets() == ({"OPRA": OPRA} if markets else {})
    orders.limit_buy("OPRA", 10, 167.49)
    verify_order(exchange.bodies("/orders")[0])


def test_the_refusal_says_what_was_held_and_what_to_do(exchange, trading_client):
    first = trading_client().orders
    first.warm("OPRA")
    named = trading_client(markets={"OPRA": OPRA}).orders
    exchange.details["OPRA"] = opra(decimals=(2, 1))
    with pytest.raises(loaf.MarketChangedError) as info:
        first.warm("OPRA")
    assert info.value.message == (
        "GET /trade/OPRA answered another market than the one this client first read for OPRA: "
        "its quantityDecimals is 1, not 3. Nothing was signed. A market's index, quantity "
        "decimals and assets are set at its listing, and an order signs them: if OPRA was listed "
        "again, check the new listing, then start a new client."
    )
    with pytest.raises(loaf.MarketChangedError) as info:
        named.warm("OPRA")
    assert info.value.message == (
        "GET /trade/OPRA answered another market than the one LoafClient(markets=) names for "
        "OPRA: its quantityDecimals is 1, not 3. Nothing was signed. A market's index, quantity "
        "decimals and assets are set at its listing, and an order signs them: if OPRA was listed "
        "again, check the new listing, then pass its terms in markets=."
    )


def test_the_refusal_is_a_server_error_that_survives_pickle(exchange, trading_client):
    orders = trading_client().orders
    orders.warm("OPRA")
    exchange.details["OPRA"] = opra(market_index=2051)
    with pytest.raises(loaf.LoafServerError) as info:  # an answer the SDK cannot use
        orders.warm("OPRA")
    error = info.value
    assert isinstance(error, loaf.MarketChangedError)
    assert "MarketChangedError" in loaf.__all__
    copy = pickle.loads(pickle.dumps(error))
    assert type(copy) is loaf.MarketChangedError
    assert (copy.ticker, copy.field, copy.held, copy.served) == ("OPRA", "marketIndex", 2050, 2051)
    assert (copy.status_code, copy.message, str(copy)) == (200, error.message, str(error))

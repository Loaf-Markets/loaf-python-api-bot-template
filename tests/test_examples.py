"""Offline runs of the order examples (examples/03 and 06) against the in-memory exchange: each
signs the least order its market takes, its minimum order value in the market's lots, on the
first tradeable spot market, for five minutes, and cancels it whatever happens in between."""

from __future__ import annotations

import importlib.util
import sys
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest

import loaf

from conftest import FakeExchange, freeze, market_detail

EXAMPLES = [
    ("03_place_order", "/orders", "/orders/cancel"),  # a limit BUY at 80% of the best bid
    ("06_stop_and_take", "/orders/conditional", "/orders/conditional/cancel"),  # a stop BUY
]
# The read each example makes between its placement and its cancel.
READ = {"03_place_order": "/history/orders/active", "06_stop_and_take": "/portfolio/component"}


def _list(exchange: FakeExchange) -> None:
    """Serve GET /trade once, as the exchange lists ``exchange.details``: each market's row with
    its type."""
    rows = []
    for ticker, detail in exchange.details.items():
        market = detail["market"]
        rows.append({
            "ticker": ticker,
            "type": market["type"],
            "status": market["status"],
            "isHalted": market["isHalted"],
            "tradingStartsAt": market.get("tradingStartsAt"),
            "hasL2Market": market.get("l2") is not None,
            "marketPrice": detail["marketList"][-1]["marketPrice"],
        })
    exchange.queue("/trade", httpx.Response(200, json={"markets": rows}))


def _with_bid(detail: dict, bid: float) -> dict:
    return {**detail, "orderBook": {"bids": [{"price": bid}], "asks": []}}


def _example(example: str, monkeypatch: pytest.MonkeyPatch, trading_client: Any) -> ModuleType:
    monkeypatch.setitem(sys.modules, "dotenv", None)  # never a developer's .env
    path = Path(__file__).parent.parent / "examples" / f"{example}.py"
    spec = importlib.util.spec_from_file_location(f"example_{example}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "LoafClient", trading_client)
    return module


@pytest.mark.parametrize("example, placed, cancelled", EXAMPLES)
@pytest.mark.parametrize("ticker, decimals, bid", [
    ("BTC", (0, 5), 75_000),  # a lot of 0.00001: a whole token would be 6,000 times $10
    ("H100", (4, 1), 3.32),  # a lot of 0.1 on a $0.0001 tick
])
def test_an_example_orders_its_minimum_in_lots(
    example, placed, cancelled, ticker, decimals, bid, exchange, trading_client, monkeypatch,
    l2_clock,
):
    now_ms = freeze(l2_clock)
    detail = market_detail(ticker, decimals=decimals, market_price=bid)
    exchange.details = {ticker: _with_bid(detail, bid)}
    _list(exchange)
    exchange.queue("/history/orders/active", httpx.Response(200, json={"activeOrders": []}))
    exchange.queue(cancelled, httpx.Response(200, json={"success": True}))

    _example(example, monkeypatch, trading_client).main()

    [body] = exchange.bodies(placed)
    lot = Decimal(1).scaleb(-decimals[1])
    quantity, price = Decimal(str(body["quantity"])), Decimal(str(body["price"]))
    assert quantity % lot == 0
    assert quantity * price >= 10 > (quantity - lot) * price  # $10: the market's minOrderValue
    # It rests five minutes at most (the exchange stops filling it 72 hours before its expiry).
    assert body["l2ExpiryMs"] == now_ms + loaf.L2_BUFFER_MS + 300_000
    assert len(exchange.posts(cancelled)) == 1


@pytest.mark.parametrize("example, placed, cancelled", EXAMPLES)
def test_an_example_trades_the_first_spot_market_not_a_perp(
    example, placed, cancelled, exchange, trading_client, monkeypatch
):
    # Perps are listed first (their market indexes are the low ones): a BUY there would open a
    # leveraged long.
    perp = market_detail("OPRA-PERP", market_index=4, market_price=100)
    exchange.details = {
        "OPRA-PERP": _with_bid(perp, 100),
        "OPRA": _with_bid(market_detail("OPRA", market_price=160), 160),
    }
    _list(exchange)
    exchange.queue("/history/orders/active", httpx.Response(200, json={"activeOrders": []}))
    exchange.queue(cancelled, httpx.Response(200, json={"success": True}))

    _example(example, monkeypatch, trading_client).main()

    [body] = exchange.bodies(placed)
    assert body["l2MarketIndex"] == 2050 and "l2Pool" not in body  # OPRA's, a spot order
    assert len(exchange.posts(cancelled)) == 1


@pytest.mark.parametrize("example, placed, cancelled", EXAMPLES)
def test_an_example_with_no_spot_market_places_nothing(
    example, placed, cancelled, exchange, trading_client, monkeypatch
):
    perp = market_detail("OPRA-PERP", market_index=4, market_price=100)
    exchange.details = {"OPRA-PERP": _with_bid(perp, 100)}
    _list(exchange)
    with pytest.raises(SystemExit, match="No spot market is tradeable right now"):
        _example(example, monkeypatch, trading_client).main()
    assert exchange.posts(placed) == []


@pytest.mark.parametrize("example, placed, cancelled", EXAMPLES)
def test_an_example_cancels_its_order_whatever_happens_after_placing_it(
    example, placed, cancelled, exchange, trading_client, monkeypatch, capsys
):
    exchange.details = {"OPRA": _with_bid(market_detail("OPRA", market_price=160), 160)}
    _list(exchange)
    # The read between the placement and the cancel fails, and the cancel is refused: the
    # order filled, or the stop fired, meanwhile (409). Both are reported, never raised.
    exchange.queue(READ[example], httpx.Response(502, json={"error": "Bad Gateway"}))
    exchange.queue(cancelled, httpx.Response(409, json={"error": "It can no longer be cancelled"}))

    _example(example, monkeypatch, trading_client).main()

    assert len(exchange.posts(placed)) == 1 and len(exchange.posts(cancelled)) == 1
    out = capsys.readouterr().out
    assert "Could not read your" in out and "No longer cancellable" in out


@pytest.mark.parametrize("example, placed, cancelled", EXAMPLES)
def test_an_example_interrupted_after_placing_still_cancels(
    example, placed, cancelled, exchange, trading_client, monkeypatch
):
    exchange.details = {"OPRA": _with_bid(market_detail("OPRA", market_price=160), 160)}
    _list(exchange)
    exchange.queue(cancelled, httpx.Response(200, json={"success": True}))
    module = _example(example, monkeypatch, trading_client)

    def interrupted(*args: Any, **kwargs: Any) -> Any:
        raise KeyboardInterrupt  # Ctrl-C during the read

    def client() -> loaf.LoafClient:
        made = trading_client()
        monkeypatch.setattr(made.history, "active_orders", interrupted)
        monkeypatch.setattr(made.portfolio, "component", interrupted)
        return made

    monkeypatch.setattr(module, "LoafClient", client)
    with pytest.raises(KeyboardInterrupt):
        module.main()
    assert len(exchange.posts(placed)) == 1 and len(exchange.posts(cancelled)) == 1


@pytest.mark.parametrize("example, placed, cancelled", EXAMPLES)
@pytest.mark.parametrize("value", [..., None, "", "ten", 0, -10, True])
def test_an_example_without_a_usable_minimum_order_value_places_nothing(
    example, placed, cancelled, value, exchange, trading_client, monkeypatch
):
    # Its order is sized by the market's minOrderValue: with none, it says so and stops,
    # rather than guess one.
    detail = _with_bid(market_detail("OPRA", market_price=160), 160)
    if value is ...:
        del detail["market"]["minOrderValue"]
    else:
        detail["market"]["minOrderValue"] = value
    exchange.details = {"OPRA": detail}
    _list(exchange)
    with pytest.raises(SystemExit) as info:
        _example(example, monkeypatch, trading_client).main()
    assert str(info.value).startswith("OPRA has no usable minOrderValue (")
    assert "places nothing" in str(info.value)
    assert exchange.posts(placed) == []


# What POST /orders/cancel-by-index answers for each outcome, and what the example prints.
SETTLED = [
    ({"outcome": "CANCELLED", "orderId": 41, "status": "CANCELLED"}, "CANCELLED: "),
    ({"outcome": "ENDED", "orderId": 41, "status": "FILLED"}, "ENDED: "),
    ({"outcome": "VOIDED", "orderId": None, "status": None}, "VOIDED: "),
]


@pytest.mark.parametrize("example, placed, cancelled", EXAMPLES)
@pytest.mark.parametrize("answer, said", SETTLED, ids=["cancelled", "ended", "voided"])
def test_an_example_settles_its_order_by_number_when_the_resubmit_is_unsure_too(
    example, placed, cancelled, answer, said, exchange, trading_client, monkeypatch, capsys
):
    exchange.details = {"OPRA": _with_bid(market_detail("OPRA", market_price=160), 160)}
    _list(exchange)
    # Every send of the order, the SDK's own re-sends and the example's resubmit, ends in a
    # 502: whether it landed is never known.
    exchange.queue(placed, *[httpx.Response(502, json={"error": "Bad Gateway"})] * 20)
    exchange.queue("/orders/cancel-by-index", httpx.Response(200, json=answer))

    _example(example, monkeypatch, trading_client).main()  # ends cleanly

    sent = exchange.bodies(placed)
    assert len(sent) > 1 and all(body == sent[0] for body in sent)  # one signed order
    [settle] = exchange.bodies("/orders/cancel-by-index")
    assert settle["l2OrderIndex"] == sent[0]["l2OrderIndex"]
    assert exchange.posts(cancelled) == []
    out = capsys.readouterr().out
    assert said in out
    assert sent[0]["l2SigS"] not in out and sent[0]["l2SigE"] not in out  # never its body


def test_the_stop_example_names_the_armed_stop_a_settle_is_refused_for(
    exchange, trading_client, monkeypatch, capsys
):
    exchange.details = {"OPRA": _with_bid(market_detail("OPRA", market_price=160), 160)}
    _list(exchange)
    exchange.queue("/orders/conditional", *[httpx.Response(502, json={"error": "Bad Gateway"})] * 20)
    # The stop was armed: its row holds the number, so voiding it is refused, naming the row.
    refusal = {"error": "Conditional order 12 holds this order number", "code": "INVALID_REQUEST"}
    exchange.queue("/orders/cancel-by-index", httpx.Response(400, json=refusal))

    _example("06_stop_and_take", monkeypatch, trading_client).main()  # ends cleanly

    out = capsys.readouterr().out
    assert "It was armed: Conditional order 12 holds this order number" in out
    assert "client.orders.cancel_conditional(" in out
    assert exchange.posts("/orders/conditional/cancel") == []

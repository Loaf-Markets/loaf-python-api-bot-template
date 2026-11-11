"""Offline runs of the order examples (examples/03 and 06) against the in-memory exchange: each
signs the least order its market takes, its minimum order value in the market's lots."""

from __future__ import annotations

import importlib.util
import sys
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from conftest import market_detail


@pytest.mark.parametrize("example, placed", [
    ("03_place_order", "/orders"),  # a limit BUY at 80% of the best bid
    ("06_stop_and_take", "/orders/conditional"),  # a stop booking 2% above its trigger
])
@pytest.mark.parametrize("ticker, decimals, bid", [
    ("BTC", (0, 5), 75_000),  # a lot of 0.00001: a whole token would be 6,000 times $10
    ("H100", (4, 1), 3.32),  # a lot of 0.1 on a $0.0001 tick
])
def test_an_example_orders_its_minimum_in_lots(
    example, placed, ticker, decimals, bid, exchange, trading_client, monkeypatch
):
    detail = market_detail(ticker, decimals=decimals, market_price=bid)
    exchange.details = {ticker: {**detail, "orderBook": {"bids": [{"price": bid}], "asks": []}}}
    exchange.queue("/history/orders/active", httpx.Response(200, json={"activeOrders": []}))
    exchange.queue("/orders/cancel", httpx.Response(200, json={"success": True}))
    exchange.queue("/orders/conditional/cancel", httpx.Response(200, json={"success": True}))
    monkeypatch.setitem(sys.modules, "dotenv", None)  # never a developer's .env
    path = Path(__file__).parent.parent / "examples" / f"{example}.py"
    spec = importlib.util.spec_from_file_location(f"example_{example}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "LoafClient", trading_client)

    module.main()

    [body] = exchange.bodies(placed)
    lot = Decimal(1).scaleb(-decimals[1])
    quantity, price = Decimal(str(body["quantity"])), Decimal(str(body["price"]))
    assert quantity % lot == 0
    assert quantity * price >= 10 > (quantity - lot) * price  # $10: the market's minOrderValue

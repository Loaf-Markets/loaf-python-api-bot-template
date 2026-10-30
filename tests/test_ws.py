"""Offline unit tests for the WebSocket client: the reconnect delays it refuses, channel
helpers, frame dispatch, and what a refused auth does to the login token.

Frames are fed straight to the dispatcher; the connection lifecycle against a
real server is covered by test_ws_lifecycle.py.
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from loaf import LoafClient, LoafConfigError, LoafWebSocketClient, from_base_units
from loaf._object import parse

from conftest import TRADING_KEY


def _client() -> LoafWebSocketClient:
    return LoafWebSocketClient(ws_url="ws://test/ws")


def _frame(**fields: Any) -> str:
    return json.dumps({**fields, "timestamp": 0})


@pytest.mark.parametrize(
    "delay", [0, -1.0, float("nan"), float("-inf"), Decimal("1"), "1", None, True]
)
def test_reconnect_delay_must_be_a_positive_number(delay: Any):
    # On the loop thread, 0 or less (and NaN before Python 3.13) would reconnect without a
    # pause, and a str, None or NaN from 3.13 would end the thread.
    message = f"reconnect_delay must be a positive number of seconds, got {delay!r}"
    for make in (
        lambda: LoafWebSocketClient(ws_url="ws://test/ws", reconnect_delay=delay),
        lambda: LoafClient().websocket(reconnect_delay=delay),
    ):
        with pytest.raises(LoafConfigError) as info:
            make()
        assert str(info.value) == message


def test_channel_helpers():
    ws = _client()
    ws.subscribe_orderbook("OPRA")
    ws.subscribe_trades("OPRA")
    ws.subscribe_chart("OPRA")
    ws.subscribe_mark_price("OPRA")
    ws.subscribe_volume("OPRA")
    ws.subscribe_market_status("OPRA")
    ws.subscribe_orderbook("H100-PERP")  # a perp's market ticker
    ws.subscribe_portfolio()
    ws.subscribe_perps_markets()
    assert ws._channels == {
        "orderbook:OPRA", "trades:OPRA", "chart:OPRA", "markprice:OPRA", "volume:OPRA",
        "market:OPRA", "orderbook:H100-PERP", "portfolio", "perps:markets",
    }


def test_a_subscribe_as_the_loop_closes_is_left_to_the_next_connection(
    recwarn: pytest.WarningsRecorder
):
    # The bot's thread can read the live connection just before the run ends and closes its
    # loop: the send is dropped, with no error and no "never awaited" warning, and the channel
    # set still changes, for the next connection to subscribe.
    loop = asyncio.new_event_loop()

    class Connection:
        def send(self, frame: str) -> Any:
            loop.close()  # the run ends between the reads and the send
            return asyncio.sleep(0)

    ws = _client()
    ws.subscribe_orderbook("OPRA")
    ws._loop, ws._ws = loop, Connection()
    ws.subscribe_trades("OPRA")
    ws.unsubscribe("orderbook:OPRA")
    assert ws._channels == {"trades:OPRA"}
    assert [str(w.message) for w in recwarn] == []


def test_market_halt_dispatch():
    ws = _client()
    seen = []
    ws.on_market_halt(seen.append)
    ws._dispatch(_frame(type="market_halt", marketIndex=2050, ticker="OPRA", isHalted=True))
    assert (seen[0].marketIndex, seen[0].ticker, seen[0].isHalted) == (2050, "OPRA", True)


def test_mark_price_frames_match_holdings_by_market_index():
    # Every markprice channel feeds on_mark_price and a frame names its market by index alone, so
    # a frame is matched to the holding with that marketIndex, as the README shows. A holding with
    # None has no spot market: nothing to subscribe, and no frame names it.
    holdings = parse({"holdings": [
        {"ticker": "OPRA", "marketIndex": 2050, "isIpoAllocation": False, "assetId": 6},
        {"ticker": "MUS", "marketIndex": 2049, "isIpoAllocation": False, "assetId": 4},
        {"ticker": "EFL", "marketIndex": None, "isIpoAllocation": True, "quantity": 5},
    ]}).holdings
    by_market = {h.marketIndex: h for h in holdings if h.marketIndex is not None}
    ws = _client()
    marks = {}

    @ws.on_mark_price
    def on_mark(msg):
        held = by_market.get(msg.marketIndex)
        if held is not None:
            marks[held.ticker] = msg.price

    for h in by_market.values():
        ws.subscribe_mark_price(h.ticker)
    assert ws._channels == {"markprice:OPRA", "markprice:MUS"}
    ws._dispatch(_frame(type="mark_price", marketIndex=2049, price=50.5))
    ws._dispatch(_frame(type="mark_price", marketIndex=2050, price=101.25))
    ws._dispatch(_frame(type="mark_price", marketIndex=2051, price=7))  # a market none is held on
    assert marks == {"MUS": 50.5, "OPRA": 101.25}


def test_trade_new_dispatch():
    ws = _client()
    seen = []
    ws.on_trade(seen.append)
    # A fill is final when it matches: its txHash is the trade's content hash from the start.
    tx_hash = "0x" + "3f" * 32
    ws._dispatch(_frame(type="trade_new", trade={
        "tradeId": 1, "orderId": 991, "marketIndex": 2050, "ticker": "OPRA", "txHash": tx_hash,
        "side": "BUY", "role": "TAKER", "quantity": 1, "price": 100, "executedAt": 0,
        "fee": 0.173, "proof": {"status": "PENDING"},
    }))
    trade = seen[0].trade
    assert (trade.tradeId, trade.txHash, trade.fee) == (1, tx_hash, 0.173)
    assert (trade.orderId, trade.role) == (991, "TAKER")  # the user's own order, which aggressed
    assert trade.proof == {"status": "PENDING"}


def test_order_queue_dispatch():
    ws = _client()
    seen = []
    ws.on_order_queue(seen.append)
    entry = {"orderId": 991, "side": "BUY", "price": 159.5, "quantity": 4,
             "quantityAhead": 12.5, "ordersAhead": 3}
    ws._dispatch(_frame(type="order_queue", marketIndex=2050, orders=[entry]))
    ws._dispatch(_frame(type="order_queue", marketIndex=2050, orders=[]))  # none rests there now
    assert [(m.marketIndex, m.orders) for m in seen] == [(2050, [entry]), (2050, [])]
    assert (seen[0].orders[0].quantityAhead, seen[0].orders[0].ordersAhead) == (12.5, 3)


def test_a_liquidation_fill_reaches_on_liquidation_fill_not_on_trade():
    # A liquidation took from the user's resting perps order: a fill of it that is no trade.
    ws = _client()
    fills, trades = [], []
    ws.on_liquidation_fill(fills.append)
    ws.on_trade(trades.append)
    fill = {
        "perpsSeq": 990, "orderId": 77, "marketIndex": 4, "ticker": "MUS-PERP", "side": "BUY",
        "role": "MAKER", "quantity": 1.5, "price": 26.1, "fee": 0, "executedAt": 1_800_000_002,
    }
    ws._dispatch(_frame(type="liquidation_fill", fill=fill, timestamp=0))
    assert trades == []
    (frame,) = fills
    assert frame.fill == fill
    assert (frame.fill.orderId, frame.fill.perpsSeq, frame.fill.role) == (77, 990, "MAKER")


def test_perps_markets_live_reaches_its_handler():
    ws = _client()
    seen = []
    ws.on_perps_markets_live(seen.append)
    live = {"marketIndex": 4, "riskOnly": True, "asOfMs": 1_800_000_001_250, "tradingState": "OPEN"}
    ws._dispatch(_frame(type="perps_markets_live", markets=[live], timestamp=0))
    (frame,) = seen
    assert frame.markets == [live]
    assert frame.markets[0].riskOnly is True


def test_balances_update_keeps_base_unit_strings():
    balances = [
        {"assetId": 3, "symbol": "USDC", "decimals": 6, "isQuote": True,
         "total": "1500000", "hold": "250000", "available": "1250000"},
        # An 18-decimal amount past float precision.
        {"assetId": 6, "symbol": "MUS", "decimals": 18, "isQuote": False,
         "total": "12345678901234567890123", "hold": "0", "available": "12345678901234567890123"},
    ]
    ws = _client()
    seen = []
    ws.on_balances(seen.append)
    ws._dispatch(_frame(type="balances_update", balances=balances))
    assert seen[0].balances == balances
    token = seen[0].balances[1]
    assert from_base_units(token.available, token.decimals) == Decimal("12345.678901234567890123")


def test_on_message_sees_every_frame():
    ws = _client()
    typed, every = [], []
    ws.on_market_halt(typed.append)
    ws.on_message(lambda m: every.append(m.type))
    for message_type in ("connection", "market_halt", "balances_update", "a_future_type"):
        ws._dispatch(_frame(type=message_type))
    assert len(typed) == 1
    assert every == ["connection", "market_halt", "balances_update", "a_future_type"]


def test_the_echo_of_the_ping_marks_the_connection_live():
    ws = _client()
    events = []
    ws.on_connect(lambda _: events.append("on_connect"))
    ws.on_message(lambda m: events.append(m.type))
    ws._dispatch(_frame(type="subscription_confirmed", channels=["orderbook:OPRA"]))
    ws._dispatch(_frame(type="echo", data={"type": "hello"}))
    assert not ws.wait_until_connected(timeout=0)
    ws._dispatch(_frame(type="echo", data={"type": "ping", "timestamp": 0}))
    ws._dispatch(_frame(type="echo", data={"type": "ping", "timestamp": 0}))
    assert ws.wait_until_connected(timeout=0)
    # Once per connection, and the echo itself still reaches the handlers.
    assert events == ["subscription_confirmed", "echo", "on_connect", "echo", "echo"]


def test_a_refused_auth_drops_the_login_token_it_carried():
    # The next auth frame, on this connection or the next, carries a fresh token: a refused one is
    # never sent again. An accepted one is kept, and so is one signed since the refused one.
    ws = LoafClient(TRADING_KEY).websocket()
    tokens = ws._login_tokens
    assert tokens is not None
    ws._auth_token = sent = tokens.token()
    ws._dispatch(_frame(type="auth_result", success=True))
    assert tokens.token() == sent
    ws._dispatch(_frame(type="auth_result", success=False))
    fresh = tokens.token()
    assert fresh != sent
    ws._dispatch(_frame(type="auth_result", success=False))  # sent's again, fresh signed since
    assert tokens.token() == fresh


def test_an_anonymous_client_has_no_login_tokens():
    assert LoafWebSocketClient(ws_url="ws://test/ws")._login_tokens is None
    assert LoafClient().websocket()._login_tokens is None
    assert LoafClient(TRADING_KEY).websocket()._login_tokens is not None


class _Stop(BaseException):
    """Ends a coroutine under test from inside a stand-in."""


def test_a_refresh_that_wakes_early_sends_nothing_until_half_life(
    monkeypatch: pytest.MonkeyPatch, l2_clock: SimpleNamespace
):
    # asyncio's clock is not the wall clock: a sleep can end a little before the token the
    # connection holds is down to half its life. The connection keeps that token, waits out the
    # rest, and only then sends a fresh one.
    now_ms = [1_800_000_000_000]
    l2_clock.time_ns = lambda: now_ms[0] * 1_000_000
    ws = LoafClient(TRADING_KEY).websocket()
    tokens = ws._login_tokens
    assert tokens is not None
    held, renew_in = tokens.token_and_renewal()
    ws._auth_token = held
    waits: list[float] = []
    advances = [299_999, 1]  # ms: the first sleep ends a millisecond early

    async def sleep(seconds: float) -> None:
        waits.append(seconds)
        if not advances:
            raise _Stop
        now_ms[0] += advances.pop(0)

    sent: list[str] = []

    class Connection:
        async def send(self, frame: str) -> None:
            sent.append(json.loads(frame)["token"])

    monkeypatch.setattr("loaf.ws.client.asyncio", SimpleNamespace(sleep=sleep))
    with pytest.raises(_Stop):
        asyncio.run(ws._keep_authenticated(Connection(), tokens, renew_in))
    assert waits == [300.0, 0.001, 300.0]
    assert len(sent) == 1 and sent[0] != held and ws._auth_token == sent[0]

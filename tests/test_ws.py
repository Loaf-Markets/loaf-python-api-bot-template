"""Offline unit tests for the WebSocket client: the API keys and reconnect delays it refuses,
channel helpers and frame dispatch.

Frames are fed straight to the dispatcher; the connection lifecycle against a
real server is covered by test_ws_lifecycle.py.
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from typing import Any

import pytest

from loaf import LoafClient, LoafConfigError, LoafWebSocketClient, from_base_units

from conftest import secret_locals


def _client() -> LoafWebSocketClient:
    return LoafWebSocketClient(ws_url="ws://test/ws")


def _frame(**fields: Any) -> str:
    return json.dumps({**fields, "timestamp": 0})


@pytest.mark.parametrize(
    "api_key, refusal",
    [
        (" 0X" + "AB" * 32 + "\n", "looks like a wallet private key"),
        # As a .env loader may keep it: quoted with a comment, the pasted line's own name, or
        # exported without 0x on a second line after the API key.
        ('"0x' + "ab" * 32 + '" # wallet', "looks like a wallet private key"),
        ("PRIVATE_KEY=0x" + "ab" * 32, "looks like a wallet private key"),
        ("cd" * 32 + "\n" + "ab" * 32, "stray character inside it"),
        ("LOAF_SECRET_KEY=" + "ab" * 40, "looks like your trading key"),
    ],
    ids=["wallet key", "wallet key quoted", "wallet key line", "wallet key bare", "trading key"],
)
def test_a_wallet_or_trading_key_is_never_sent_as_the_api_key(api_key: str, refusal: str):
    # Every auth frame carries the API key: either constructor refuses what LoafClient refuses,
    # before anything is sent, and leaves the key in neither the error nor its frames.
    loaf = LoafClient(api_key="cd" * 32)
    for make in (
        lambda: LoafWebSocketClient(ws_url="ws://test/ws", api_key=api_key),
        lambda: loaf.websocket(api_key=api_key),
    ):
        with pytest.raises(LoafConfigError, match=refusal) as info:
            make()
        assert "ab" * 32 not in str(info.value).lower()
        assert secret_locals(info.value, "ab" * 32) == []


def test_whitespace_around_the_api_key_is_accepted():
    # Sent as given: the server trims the key, as LoafClient strips its own.
    key = " " + "cd" * 32 + "\n"
    assert LoafWebSocketClient(ws_url="ws://test/ws", api_key=key).api_key == key
    assert LoafClient(api_key="cd" * 32).websocket(api_key=key).api_key == key


def test_a_key_passed_as_bytes_is_left_in_no_frame():
    # A str pattern raises on bytes inside the check both clients share: no frame keeps the key.
    key = b"0x" + b"ab" * 32
    for make in (
        lambda: LoafClient(api_key=key),  # type: ignore[arg-type]
        lambda: LoafWebSocketClient(ws_url="ws://test/ws", api_key=key),  # type: ignore[arg-type]
    ):
        with pytest.raises(TypeError) as info:
            make()
        assert secret_locals(info.value, "ab" * 32) == []


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
    ws.subscribe_property_status("OPRA")
    ws.subscribe_leaderboard()
    ws.subscribe_competition()
    ws.subscribe_portfolio()
    ws.subscribe_perps_markets()
    assert ws._channels == {
        "orderbook:OPRA", "trades:OPRA", "chart:OPRA", "markprice:OPRA", "volume:OPRA",
        "property:OPRA", "leaderboard", "competition", "portfolio", "perps:markets",
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


def test_property_halt_dispatch():
    ws = _client()
    seen = []
    ws.on_property_halt(seen.append)
    ws._dispatch(_frame(type="property_halt", propertyId=1, ticker="OPRA", isHalted=True))
    assert seen[0].ticker == "OPRA" and seen[0].isHalted is True


def test_trade_new_dispatch():
    ws = _client()
    seen = []
    ws.on_trade(seen.append)
    # A fill is final when it matches: its txHash is the trade's content hash from the start.
    tx_hash = "0x" + "3f" * 32
    ws._dispatch(_frame(type="trade_new", trade={
        "tradeId": 1, "orderId": 991, "propertyId": 1, "ticker": "OPRA", "txHash": tx_hash,
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
    ws._dispatch(_frame(type="order_queue", propertyId=1, orders=[entry]))
    ws._dispatch(_frame(type="order_queue", propertyId=1, orders=[]))  # none rests there now
    assert [(m.propertyId, m.orders) for m in seen] == [(1, [entry]), (1, [])]
    assert (seen[0].orders[0].quantityAhead, seen[0].orders[0].ordersAhead) == (12.5, 3)


def test_round_update_reaches_on_round_update():
    ws = _client()
    seen = []
    ws.on_round_update(seen.append)
    round_update = {
        "type": "round_update", "roundNumber": 3, "status": "PREPARED",
        "startsAt": 1_800_000_000, "endsAt": None, "arenaTicker": "MUS", "timestamp": 0,
    }
    ws._dispatch(json.dumps(round_update))
    assert seen == [round_update]


def test_a_liquidation_fill_reaches_on_liquidation_fill_not_on_trade():
    # A liquidation took from the user's resting perps order: a fill of it that is no trade.
    ws = _client()
    fills, trades = [], []
    ws.on_liquidation_fill(fills.append)
    ws.on_trade(trades.append)
    fill = {
        "perpsSeq": 990, "orderId": 77, "propertyId": 12, "ticker": "PRP1", "side": "BUY",
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
    ws.on_round_update(typed.append)
    ws.on_message(lambda m: every.append(m.type))
    for message_type in ("connection", "round_update", "balances_update", "a_future_type"):
        ws._dispatch(_frame(type=message_type))
    assert len(typed) == 1
    assert every == ["connection", "round_update", "balances_update", "a_future_type"]


def test_the_echo_of_the_ping_marks_the_connection_live():
    ws = _client()
    events = []
    ws.on_connect(lambda _: events.append("on_connect"))
    ws.on_message(lambda m: events.append(m.type))
    ws._dispatch(_frame(type="subscription_confirmed", channels=["competition"]))
    ws._dispatch(_frame(type="echo", data={"type": "hello"}))
    assert not ws.wait_until_connected(timeout=0)
    ws._dispatch(_frame(type="echo", data={"type": "ping", "timestamp": 0}))
    ws._dispatch(_frame(type="echo", data={"type": "ping", "timestamp": 0}))
    assert ws.wait_until_connected(timeout=0)
    # Once per connection, and the echo itself still reaches the handlers.
    assert events == ["subscription_confirmed", "echo", "on_connect", "echo", "echo"]

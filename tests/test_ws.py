"""Offline unit tests for the WebSocket client: the reconnect delays and stale_after values it
refuses, channel helpers, frame dispatch, the beat (fresh and stale) and its watch, and what a
refused auth does to the login token.

Frames are fed straight to the dispatcher; the connection lifecycle against a
real server is covered by test_ws_lifecycle.py.
"""

from __future__ import annotations

import asyncio
import json
import random
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from loaf import (
    DEFAULT_STALE_AFTER,
    LoafClient,
    LoafConfigError,
    LoafWebSocketClient,
    from_base_units,
)
from loaf._object import parse

from conftest import (
    PERPS_ACCOUNT,
    PERPS_ACCOUNT_READ,
    PERPS_LIVE,
    PERPS_LIVE_READ,
    PERP_L2,
    TRADING_KEY,
    as_decimals,
)

README = (Path(__file__).parent.parent / "README.md").read_text(encoding="utf-8")


def _client() -> LoafWebSocketClient:
    return LoafWebSocketClient(ws_url="wss://test/ws")


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
        lambda: LoafWebSocketClient(ws_url="wss://test/ws", reconnect_delay=delay),
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


def test_mark_price_frames_match_holdings_by_ticker_as_the_readme_shows(capsys):
    # The README's own code: a frame's ticker finds its holding; an IPO allocation subscribes nothing.
    code = README.split("**Valuing holdings live.**")[1].split("```python\n")[1].split("```")[0]
    summary = parse({"holdings": [
        {"ticker": "OPRA", "isIpoAllocation": False, "assetId": 6},
        {"ticker": "MUS", "isIpoAllocation": False, "assetId": 4},
        {"ticker": "EFL", "isIpoAllocation": True, "quantity": 5},
    ]})
    ws = _client()
    exec(code, {"loaf": SimpleNamespace(portfolio=SimpleNamespace(summary=lambda: summary)), "ws": ws})
    assert ws._channels == {"markprice:OPRA", "markprice:MUS"}
    ws._dispatch(_frame(type="mark_price", ticker="MUS", price="50.5"))
    ws._dispatch(_frame(type="mark_price", ticker="OPRA", price="101.25"))
    ws._dispatch(_frame(type="mark_price", ticker="H100", price="7"))  # a market none is held on
    assert capsys.readouterr().out == "MUS marked at 50.5\nOPRA marked at 101.25\n"


def test_trade_new_dispatch():
    ws = _client()
    seen = []
    ws.on_trade(seen.append)
    # A fill is final when it matches: its txHash is the trade's content hash from the start.
    tx_hash = "0x" + "3f" * 32
    ws._dispatch(_frame(type="trade_new", trade={
        "tradeId": 1, "orderId": 991, "marketIndex": 2050, "ticker": "OPRA", "txHash": tx_hash,
        "side": "BUY", "role": "TAKER", "pool": None, "quantity": "1", "price": "100",
        "executedAt": 0, "fee": "0.173", "closedPnl": None, "source": "TRADE", "direction": "BUY",
    }))
    trade = seen[0].trade
    assert (trade.tradeId, trade.txHash, trade.fee) == (1, tx_hash, Decimal("0.173"))
    assert (trade.orderId, trade.role) == (991, "TAKER")  # the user's own order, which aggressed
    assert (trade.source, trade.direction, trade.pool) == ("TRADE", "BUY", None)


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


def test_a_liquidation_row_reaches_on_trade():
    # A liquidation took from the user's resting perps order: a fill, source LIQUIDATION, that is
    # no trade; its tradeId is the liquidation's journal seq.
    ws = _client()
    seen = []
    ws.on_trade(seen.append)
    trade = {
        "tradeId": 990, "orderId": 77, "marketIndex": 4, "ticker": "MUS-PERP", "txHash": None,
        "side": "BUY", "role": "MAKER", "pool": 0, "quantity": "1.5", "price": "26.1", "fee": "0",
        "closedPnl": None, "source": "LIQUIDATION", "direction": "OPEN_LONG",
        "executedAt": 1_800_000_002,
    }
    ws._dispatch(_frame(type="trade_new", trade=trade))
    (frame,) = seen
    assert frame.trade == as_decimals(trade, FILL_DECIMALS) and frame.trade.txHash is None


@pytest.mark.parametrize(
    "register, fields",
    [
        ("on_perps_account", {"type": "perps_account_update", "perps": {"l2AccountIndex": 5, "pools": []}}),
        ("on_holding", {"type": "holding_update", "holding": {"ticker": "MUS", "averageEntryPrice": 12.5}}),
        ("on_transfer", {"type": "transfer_update", "transfer": {"journalSeq": 9}}),
        ("on_fees", {"type": "fees_update", "applicableFees": {
            "spot": {"takerFeeBps": 30, "makerFeeBps": 10}, "perps": {"takerFeeBps": 5, "makerFeeBps": 2}}}),
        ("on_l2_keys", {"type": "l2_keys_update", "apiKeyIndices": [0]}),
        ("on_perps_leverage", {"type": "perps_leverage_update", "ticker": "OPRA-PERP", "leverage": 5}),
        ("on_orderbook", {"type": "orderbook_update", "ticker": "H100-PERP", "bids": [], "asks": []}),
        ("on_candle", {"type": "candle_update", "ticker": "H100-PERP", "candle": {"time": 60, "close": 101.5}}),
        ("on_trades", {"type": "trades_batch", "ticker": "H100-PERP", "trades": [{"tradeId": 7}]}),
        ("on_volume", {"type": "volume_update", "ticker": "H100-PERP", "volume24h": 12.5}),
        ("on_market_halt", {"type": "market_halt", "ticker": "H100-PERP", "isHalted": True}),
    ],
)
def test_each_new_frame_reaches_its_handler(register, fields):
    ws = _client()
    seen = []
    getattr(ws, register)(seen.append)
    ws._dispatch(_frame(**fields))
    assert seen == [{**fields, "timestamp": 0}]


def test_perps_markets_live_reaches_its_handler():
    ws = _client()
    seen = []
    ws.on_perps_markets_live(seen.append)
    ws._dispatch(_frame(type="perps_markets_live", markets=[PERPS_LIVE], timestamp=0))
    (frame,) = seen
    assert frame.markets == [PERPS_LIVE_READ]
    assert frame.markets[0].riskOnly is True


# A fill's served decimals, and a funding payment's: the SDK reads each as a Decimal.
FILL_DECIMALS = ("quantity", "price", "fee", "closedPnl")
FUNDING_DECIMALS = ("base", "payment")
FUNDING = {"journalSeq": 7, "marketIndex": 4, "ticker": "OPRA-PERP", "pool": 0, "base": "2.004",
           "payment": "-0.000012", "rateCbp": -250, "atMs": 1_800_000_000_000}
CLOSE = {"tradeId": 990, "orderId": None, "marketIndex": 4, "ticker": "OPRA-PERP", "txHash": None,
         "side": "SELL", "role": "TAKER", "pool": 0, "quantity": "2.004", "price": "98.7654",
         "fee": "0.098978", "closedPnl": "-5.483937", "source": "LIQUIDATION",
         "direction": "CLOSE_LONG", "executedAt": 1_800_000_002}


@pytest.mark.parametrize(
    "register, fields, read",
    [
        ("on_mark_price", {"type": "mark_price", "ticker": "H100-PERP", "price": "101.2501"},
         {"price": Decimal("101.2501")}),
        ("on_market_terms", {"type": "market_terms_update", "marketIndex": 4, "ticker": "OPRA-PERP",
                             "l2": {**PERP_L2, "minBase": "0.01", "minQuote": "10", "quoteLimit": "281474.976711"}},
         {"l2": {**PERP_L2, "minBase": Decimal("0.01"), "minQuote": Decimal(10),
                 "quoteLimit": Decimal("281474.976711")}}),
        ("on_market_terms", {"type": "market_terms_update", "marketIndex": 4, "l2": {"status": "EXPIRED"}},
         {}),
        ("on_perps_account", {"type": "perps_account_update", "perps": {
            "l2AccountIndex": 5, "pools": PERPS_ACCOUNT["pools"]}, "snapshot": True,
            "accountMode": "UNIFIED", "withdrawable": "4990.000001"},
         {"perps": {"l2AccountIndex": 5, "pools": PERPS_ACCOUNT_READ["pools"]},
          "withdrawable": Decimal("4990.000001")}),
        ("on_perps_account", {"type": "perps_account_update", "perps": {"l2AccountIndex": 5, "pools": []},
                              "accountMode": "MANUAL", "withdrawable": None},
         {}),
        ("on_funding_payment", {"type": "funding_payment", "funding": FUNDING},
         {"funding": as_decimals(FUNDING, FUNDING_DECIMALS)}),
        ("on_trade", {"type": "trade_new", "trade": CLOSE}, {"trade": as_decimals(CLOSE, FILL_DECIMALS)}),
    ],
    ids=["mark", "terms", "terms-expired", "account", "account-unread", "funding", "fill"],
)
def test_a_frames_served_decimals_reach_its_handler_as_decimals(register, fields, read):
    # Each price, size and dollar amount is the Decimal of its served string, exactly; nothing
    # else of the frame changes, and a null (no withdrawable now) stays null.
    ws = _client()
    seen, every = [], []
    getattr(ws, register)(seen.append)
    ws.on_message(every.append)
    ws._dispatch(_frame(**fields))
    assert seen == every == [{**fields, "timestamp": 0, **read}]


@pytest.mark.parametrize(
    "fields",
    [
        {"type": "mark_price", "ticker": "MUS", "price": 50.5},  # a number: never read as dollars
        {"type": "mark_price", "ticker": "MUS", "price": "50.50"},
        {"type": "perps_markets_live", "markets": [PERPS_LIVE, {**PERPS_LIVE, "markPrice": 2_510_000}]},
        {"type": "perps_markets_live", "markets": [{**PERPS_LIVE, "heldPrice": {"price": "2.6e0"}}]},
        {"type": "market_terms_update", "marketIndex": 4, "l2": {**PERP_L2, "minBase": 0}},
        {"type": "perps_account_update", "perps": {"l2AccountIndex": 5, "pools": []},
         "withdrawable": "1.0000001"},
        {"type": "funding_payment", "funding": {**FUNDING, "payment": "-0"}},
        {"type": "trade_new", "trade": {**CLOSE, "closedPnl": "+5"}},
    ],
    ids=["number", "trailing-zero", "old-unit", "exponent", "terms", "places", "negative-zero", "plus"],
)
def test_a_frame_with_a_malformed_served_decimal_is_dropped_and_logged(fields, caplog):
    ws = _client().subscribe_portfolio()
    seen = []
    ws.on_message(seen.append)
    for kind in ("mark_price", "perps_markets_live", "market_terms_update", "perps_account_update",
                 "funding_payment", "trade_new"):
        ws.on(kind, seen.append)
    with caplog.at_level("ERROR", logger="loaf.ws"):
        ws._dispatch(_frame(**fields, engineMs=ENGINE_MS, snapshot=True))
    assert seen == []
    (record,) = caplog.records
    assert record.getMessage().startswith(f"Loaf WebSocket dropped a {fields['type']} frame with a malformed ")
    assert ws.fresh is None and ws.engine_ms is None  # no news: the beat noted nothing


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
    assert LoafWebSocketClient(ws_url="wss://test/ws")._login_tokens is None
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
        asyncio.run(ws._keep_authenticated(Connection(), tokens, renew_in, ws._stop_event))
    assert waits == [300.0, 0.001, 300.0]
    assert len(sent) == 1 and sent[0] != held and ws._auth_token == sent[0]


# ---------------------------------------------------------------------------
# The beat: fresh and stale
# ---------------------------------------------------------------------------

ENGINE_MS = 1_800_000_000_000
# The private frames that relay an engine change and so carry its engineMs.
STAMPED_PRIVATE = [
    "order_status", "order_update", "trade_new", "balances_update", "holding_update",
    "perps_account_update", "funding_payment", "l2_keys_update",
]
_ABSENT = object()


def _stamped(
    engine_ms: Any = ENGINE_MS, *, snapshot: bool | None = None, type: str = "perps_account_update"
) -> str:
    """A stamped private frame as the exchange sends one (a perps account one by default)."""
    fields: dict[str, Any] = {"type": type, "batchSeq": 7}
    if engine_ms is not _ABSENT:
        fields["engineMs"] = engine_ms
    if snapshot is not None:
        fields["snapshot"] = snapshot
    if type == "perps_account_update":
        fields["perps"] = {"l2AccountIndex": 5, "pools": []}
    return _frame(**fields)


def _beat_log(ws: LoafWebSocketClient) -> list[tuple[str, Any]]:
    events: list[tuple[str, Any]] = []
    ws.on_fresh(lambda m: events.append(("fresh", m)))
    ws.on_stale(lambda m: events.append(("stale", m)))
    return events


def test_the_first_snapshot_makes_the_stream_fresh_after_its_own_handlers():
    ws = _client().subscribe_portfolio()
    seen: list[Any] = []
    ws.on_perps_account(lambda m: seen.append(("account", m.engineMs)))
    ws.on_message(lambda m: seen.append(("*", m.type)))
    ws.on_fresh(lambda m: seen.append(("fresh", m)))
    assert ws.fresh is None and ws.engine_ms is None
    # Changes before the first snapshot move the engine clock but arm nothing: a deployment
    # without perps sends no snapshot, and its stream must not go stale for lack of one.
    ws._dispatch(_stamped(ENGINE_MS + 500, type="order_status"))
    ws._dispatch(_stamped(ENGINE_MS + 400, snapshot=False))
    assert ws.fresh is None and ws.engine_ms == ENGINE_MS + 500
    seen.clear()
    # An older stamp than the newest seen: engine_ms keeps the newest.
    ws._dispatch(_stamped(ENGINE_MS + 450, snapshot=True))
    assert ws.fresh is True and ws.engine_ms == ENGINE_MS + 500
    # The frame's own handlers first, so the snapshot is applied before on_fresh.
    assert seen == [
        ("account", ENGINE_MS + 450), ("*", "perps_account_update"),
        ("fresh", {"engineMs": ENGINE_MS + 500}),
    ]
    seen.clear()
    ws._dispatch(_stamped(ENGINE_MS + 600, snapshot=True))  # still fresh: no second turn
    assert seen == [("account", ENGINE_MS + 600), ("*", "perps_account_update")]
    assert ws.engine_ms == ENGINE_MS + 600


@pytest.mark.parametrize(
    "channel, frame",
    [
        ("orderbook:OPRA", _stamped(snapshot=True)),  # portfolio not subscribed
        # A public frame with a stamp: its book's, nothing about your private stream.
        ("portfolio", _frame(
            type="orderbook_update", marketIndex=2050, bids=[], asks=[], engineMs=ENGINE_MS,
            batchSeq=7,
        )),
        ("portfolio", _stamped(True, snapshot=True)),
        ("portfolio", _stamped(1.8e12, snapshot=True)),
        ("portfolio", _stamped(str(ENGINE_MS), snapshot=True)),
        ("portfolio", _stamped(None, snapshot=True)),
        ("portfolio", _stamped(_ABSENT, snapshot=True)),
    ],
)
def test_only_a_stamped_private_frame_on_a_portfolio_stream_is_a_beat(channel: str, frame: str):
    ws = _client().subscribe(channel)
    events = _beat_log(ws)
    ws._dispatch(frame)
    assert ws.fresh is None and ws.engine_ms is None and events == []


@pytest.mark.parametrize("mtype", STAMPED_PRIVATE)
def test_after_a_stale_turn_any_stamped_private_frame_is_news_again(mtype: str):
    # The canonical channel name counts as the "portfolio" alias does.
    ws = _client().subscribe("portfolio:42")
    events = _beat_log(ws)
    ws._dispatch(_stamped(snapshot=True))
    ws._beat_lost("silent", ws._stop_event)  # what the watch calls once stale_after passed
    ws._beat_lost("silent", ws._stop_event)  # once per turn
    assert ws.fresh is False
    # No snapshot needed now: this connection already showed that the exchange beats.
    ws._dispatch(_stamped(ENGINE_MS + 1, type=mtype))
    assert ws.fresh is True and ws.engine_ms == ENGINE_MS + 1
    assert [(kind, dict(m)) for kind, m in events][0::2] == [
        ("fresh", {"engineMs": ENGINE_MS}), ("fresh", {"engineMs": ENGINE_MS + 1}),
    ]
    stale = events[1][1]
    assert events[1][0] == "stale" and len(events) == 3
    assert stale["reason"] == "silent" and stale["engineMs"] == ENGINE_MS
    assert 0 <= stale["silentSeconds"] < 5


def test_a_stale_turn_needs_a_fresh_stream():
    ws = _client().subscribe_portfolio()
    events = _beat_log(ws)
    # Before the first snapshot: nothing was known to be live.
    ws._beat_lost("silent", ws._stop_event)
    assert ws.fresh is None and events == []


def test_unsubscribing_portfolio_turns_the_stream_stale_once():
    ws = _client().subscribe_portfolio().subscribe_orderbook("OPRA")
    events = _beat_log(ws)
    ws._dispatch(_stamped(snapshot=True))
    ws.unsubscribe("orderbook:OPRA")  # portfolio still held
    assert ws.fresh is True and len(events) == 1
    ws.unsubscribe("portfolio")
    ws.unsubscribe("portfolio")
    assert ws.fresh is False and [kind for kind, _ in events] == ["fresh", "stale"]
    stale = events[1][1]
    assert stale["reason"] == "unsubscribed" and stale["engineMs"] == ENGINE_MS
    assert 0 <= stale["silentSeconds"] < 5
    # A frame still in flight is no longer yours to watch.
    ws._dispatch(_stamped(ENGINE_MS + 1, snapshot=True))
    assert ws.fresh is False and ws.engine_ms == ENGINE_MS and len(events) == 2


def test_an_unsubscribe_the_loop_runs_after_a_new_subscribe_turns_nothing_stale():
    # unsubscribe() hands the stale turn to the loop's thread. When another thread subscribed
    # portfolio again before the loop got to it, the stream was never dropped.
    loop = asyncio.new_event_loop()
    try:
        ws = _client().subscribe_portfolio()
        events = _beat_log(ws)
        ws._dispatch(_stamped(snapshot=True))
        ws._loop = loop  # running elsewhere: the turn is queued for it
        ws.unsubscribe("portfolio")
        ws.subscribe_portfolio()
        loop.run_until_complete(asyncio.sleep(0))  # the loop gets to the queued turn
        assert ws.fresh is True and [kind for kind, _ in events] == ["fresh"]
        ws.unsubscribe("portfolio")
        loop.run_until_complete(asyncio.sleep(0))
        assert ws.fresh is False and [kind for kind, _ in events] == ["fresh", "stale"]
    finally:
        loop.close()


@pytest.mark.parametrize(
    "stale_after",
    [0.999, 0, -1.0, float("nan"), float("inf"), float("-inf"), Decimal("3"), "3", None, True,
     False],
)
def test_stale_after_must_be_a_number_of_seconds_from_1(stale_after: Any):
    # Below 1 s the client would call a stream stale between two of the exchange's beats.
    message = (
        "stale_after must be a number of seconds from 1 (the exchange's account beat is about "
        f"one a second), got {stale_after!r}"
    )
    for make in (
        lambda: LoafWebSocketClient(ws_url="wss://test/ws", stale_after=stale_after),
        lambda: LoafClient().websocket(stale_after=stale_after),
    ):
        with pytest.raises(LoafConfigError) as info:
            make()
        assert str(info.value) == message


@pytest.mark.parametrize("stale_after, seconds", [(1, 1.0), (Fraction(3, 2), 1.5), (30, 30.0)])
def test_stale_after_takes_any_real_number_of_seconds_from_1(stale_after: Any, seconds: float):
    for ws in (
        LoafWebSocketClient(ws_url="wss://test/ws", stale_after=stale_after),
        LoafClient().websocket(stale_after=stale_after),
    ):
        assert ws.stale_after == seconds and type(ws.stale_after) is float


def test_stale_after_defaults_to_two_missed_beats():
    assert _client().stale_after == DEFAULT_STALE_AFTER == 3.0


class _Clock:
    """Stands in for ``loaf.ws.client``'s ``time`` and ``asyncio.sleep``: each sleep moves the
    clock on at once, delivering on the way the stamped frames ``beats`` holds by their time,
    and the ``limit``-th sleep ends the watch."""

    def __init__(
        self, ws: LoafWebSocketClient, now: float, beats: dict[float, str] | None = None,
        limit: int = 4,
    ) -> None:
        self.ws, self.now, self.beats, self.limit = ws, now, dict(beats or {}), limit
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        if len(self.sleeps) == self.limit:
            raise _Stop
        end = self.now + seconds
        for at in sorted(self.beats):
            if self.now < at <= end:
                self.now = at
                self.ws._dispatch(self.beats.pop(at))
        self.now = end


def _watch(monkeypatch: pytest.MonkeyPatch, clock: _Clock) -> None:
    monkeypatch.setattr("loaf.ws.client.time", clock)
    monkeypatch.setattr("loaf.ws.client.asyncio", SimpleNamespace(sleep=clock.sleep))
    with pytest.raises(_Stop):
        asyncio.run(clock.ws._watch_beats(clock.ws._stop_event))


def test_the_watch_waits_while_nothing_is_live(monkeypatch: pytest.MonkeyPatch):
    ws = _client().subscribe_portfolio()
    events = _beat_log(ws)
    clock = _Clock(ws, now=100.0)
    _watch(monkeypatch, clock)
    assert clock.sleeps == [3.0] * 4 and events == [] and ws.fresh is None


@pytest.mark.parametrize("stale_after", [3.0, 5.0])
def test_the_watch_turns_stale_exactly_stale_after_the_last_beat(
    monkeypatch: pytest.MonkeyPatch, stale_after: float
):
    ws = LoafWebSocketClient(ws_url="wss://test/ws", stale_after=stale_after).subscribe_portfolio()
    events = _beat_log(ws)
    clock = _Clock(ws, now=100.0)
    monkeypatch.setattr("loaf.ws.client.time", clock)
    ws._dispatch(_stamped(snapshot=True))  # the beat at 100
    clock.now = 101.0
    _watch(monkeypatch, clock)
    # The rest of the wait from the last beat, then stale at exactly stale_after (not one more
    # sleep), then nothing to time.
    assert clock.sleeps == [stale_after - 1.0, stale_after, stale_after, stale_after]
    assert events == [
        ("fresh", {"engineMs": ENGINE_MS}),
        ("stale", {"reason": "silent", "engineMs": ENGINE_MS, "silentSeconds": stale_after}),
    ]
    assert ws.fresh is False


def test_each_beat_restarts_the_wait(monkeypatch: pytest.MonkeyPatch):
    ws = _client().subscribe_portfolio()
    events = _beat_log(ws)
    beats = {
        102.0: _stamped(ENGINE_MS + 2000, snapshot=True),
        104.5: _stamped(ENGINE_MS + 4500, type="order_update"),  # any stamped private frame
    }
    clock = _Clock(ws, now=100.0, beats=beats, limit=5)
    monkeypatch.setattr("loaf.ws.client.time", clock)
    ws._dispatch(_stamped(snapshot=True))
    _watch(monkeypatch, clock)
    assert clock.sleeps == [3.0, 2.0, 2.5, 3.0, 3.0]  # stale at 107.5, 3 s after the beat at 104.5
    assert events == [
        ("fresh", {"engineMs": ENGINE_MS}),
        ("stale", {"reason": "silent", "engineMs": ENGINE_MS + 4500, "silentSeconds": 3.0}),
    ]


def test_a_beat_is_timed_from_when_its_frame_was_taken_off_the_socket(
    monkeypatch: pytest.MonkeyPatch,
):
    # A slow handler must not make the frame it holds look newer than it is.
    ws = LoafWebSocketClient(ws_url="ws://127.0.0.1/ws").subscribe_portfolio()
    events = _beat_log(ws)
    clock = _Clock(ws, now=100.0)
    monkeypatch.setattr("loaf.ws.client.time", clock)

    def slow(_: Any) -> None:
        clock.now += 2.5

    ws.on_perps_account(slow)
    ws._dispatch(_stamped(snapshot=True))
    assert ws._last_beat == 100.0 and clock.now == 102.5
    assert events == [("fresh", {"engineMs": ENGINE_MS})]  # still after the frame's handlers
    clock.now = 103.5  # the beat is 3.5 s old, though its handler returned 1 s ago
    assert ws.fresh is False


def test_fresh_reads_false_once_stale_after_passed_whatever_the_loop_is_doing(
    monkeypatch: pytest.MonkeyPatch,
):
    # The watch that fires on_stale runs on the loop, which a handler can block (or which can be
    # gone): read from any thread, fresh tells the truth by itself.
    ws = LoafWebSocketClient(ws_url="ws://127.0.0.1/ws").subscribe_portfolio()
    events = _beat_log(ws)
    clock = _Clock(ws, now=100.0)
    monkeypatch.setattr("loaf.ws.client.time", clock)
    ws._dispatch(_stamped(snapshot=True))
    clock.now = 103.0
    assert ws.fresh is True  # exactly stale_after: the watch's own turn
    clock.now = 103.001
    assert ws.fresh is False and ws._fresh is True and len(events) == 1  # no on_stale yet
    # The loop runs again, and the next frame reaches it before the watch does: it reports the
    # gap fresh read, then brings on_fresh, so the events turn as the property did.
    ws._dispatch(_stamped(ENGINE_MS + 3001, snapshot=True))
    assert ws.fresh is True and events == [
        ("fresh", {"engineMs": ENGINE_MS}),
        ("stale", {"reason": "silent", "engineMs": ENGINE_MS + 3001, "silentSeconds": 3.001}),
        ("fresh", {"engineMs": ENGINE_MS + 3001}),
    ]


@pytest.mark.parametrize("snapshot", [True, None], ids=["snapshot", "order_update"])
def test_a_handler_that_holds_the_loop_past_stale_after_brings_on_stale_then_on_fresh(
    monkeypatch: pytest.MonkeyPatch, snapshot: bool | None
):
    # One handler call holds the loop 3.6 s, at 1 Hz snapshots and stale_after 3: the watch
    # cannot run meanwhile, and the first stamped frame after it, queued behind the call, is
    # taken off 2.6 s after its read, within bounds. It reports the gap before it counts as a
    # beat: on_stale, then on_fresh after its own handlers.
    ws = LoafWebSocketClient(ws_url="ws://127.0.0.1/ws").subscribe_portfolio()
    events = _beat_log(ws)
    clock = _Clock(ws, now=100.0)
    monkeypatch.setattr("loaf.ws.client.time", clock)
    seen: list[Any] = []

    def hold(m: Any) -> None:
        seen.append((m.type, clock.now, ws.fresh))
        if m.engineMs == ENGINE_MS + 1000:
            clock.now += 3.6

    ws.on("perps_account_update", hold)
    ws.on("order_update", hold)
    ws._dispatch(_stamped(snapshot=True))
    clock.now = 101.0
    ws._dispatch(_stamped(ENGINE_MS + 1000, snapshot=True))  # held until 104.6
    kind = "perps_account_update" if snapshot else "order_update"
    ws._dispatch(_stamped(ENGINE_MS + 2000, snapshot=snapshot, type=kind))
    assert seen[-1] == (kind, 104.6, False)  # fresh read False while the frame was handled
    assert ws.fresh is True and [(k, m.get("reason")) for k, m in events] == [
        ("fresh", None), ("stale", "silent"), ("fresh", None),
    ]
    assert events[1][1]["silentSeconds"] == pytest.approx(3.6)


def test_a_gap_a_frame_reports_is_timed_to_its_arrival(monkeypatch: pytest.MonkeyPatch):
    # The gap ended when the frame was taken off the socket: its own handlers' time is not in it.
    ws = LoafWebSocketClient(ws_url="ws://127.0.0.1/ws").subscribe_portfolio()
    events = _beat_log(ws)
    clock = _Clock(ws, now=100.0)
    monkeypatch.setattr("loaf.ws.client.time", clock)

    def slow(m: Any) -> None:
        if m.engineMs == ENGINE_MS + 4000:
            clock.now += 2.0

    ws.on_perps_account(slow)
    ws._dispatch(_stamped(snapshot=True))
    clock.now = 104.0  # the watch could not run meanwhile
    ws._dispatch(_stamped(ENGINE_MS + 4000, snapshot=True))
    assert clock.now == 106.0 and [kind for kind, _ in events] == ["fresh", "stale", "fresh"]
    assert events[1][1]["reason"] == "silent" and events[1][1]["silentSeconds"] == 4.0


class _Feed:
    """Feeds a client the exchange's account snapshots on a stand-in clock. ``engine`` is the
    engine's clock and ``at`` the moment the client takes the frame off the socket, both in
    seconds; the engine's clock reads ``ENGINE_MS`` at 0."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, stale_after: float = 3.0) -> None:
        self.ws = LoafWebSocketClient(
            ws_url="ws://127.0.0.1/ws", stale_after=stale_after
        ).subscribe_portfolio()
        self.events = _beat_log(self.ws)
        self.clock = _Clock(self.ws, now=0.0)
        monkeypatch.setattr("loaf.ws.client.time", self.clock)

    def snapshot(self, at: float, engine: float) -> None:
        self.clock.now = at
        self.ws._dispatch(_stamped(ENGINE_MS + round(engine * 1000), snapshot=True))

    def turns(self) -> list[Any]:
        return [m["reason"] if kind == "stale" else kind for kind, m in self.events]


def test_snapshots_up_to_a_beat_old_never_fall_behind(monkeypatch: pytest.MonkeyPatch):
    # An hour of 1 Hz snapshots at the tightest stale_after. Each waits for the next beat before
    # it is sent, anywhere from no time to a full second, then 5 to 200 ms in flight: offsets
    # that range over more than a second above the least one, which is never a lag. (A gap over
    # stale_after between two of them is a silent turn; the behind check never trips.)
    feed = _Feed(monkeypatch, stale_after=1.0)
    rng = random.Random(7)
    waits = [0.0, 1.0] + [rng.uniform(0.0, 1.0) for _ in range(3598)]  # the extremes first
    for second, wait in enumerate(waits):
        feed.snapshot(second + wait + rng.uniform(0.005, 0.2), second)
        assert feed.ws._behind_since is None
    assert "behind" not in feed.turns()


def test_a_handler_too_slow_for_the_feed_falls_behind_and_recovers_once_caught_up(
    monkeypatch: pytest.MonkeyPatch,
):
    # Ten snapshots a second and a handler that takes 0.5 s: every frame waits longer than the
    # last. Each frame taken off the socket still looks like a beat, but the engine's clock
    # falls behind this machine's, and once by more than stale_after and a beat the stream is
    # behind.
    feed = _Feed(monkeypatch, stale_after=1.0)
    handling = [0.5]
    lags: list[float] = []

    def handle(m: Any) -> None:
        lags.append(feed.clock.now - (m.engineMs - ENGINE_MS) / 1000)
        feed.clock.now += handling[0]

    feed.ws.on_perps_account(handle)
    sent = [k / 10 for k in range(400)]  # the engine's clock as each was sent, 0 s in flight
    for k, engine in enumerate(sent[:24]):
        feed.snapshot(max(feed.clock.now, engine), engine)
        if k == 5:
            assert lags[-1] == pytest.approx(2.0) and feed.turns() == ["fresh"]
        if k == 6:
            # 2.4 s behind by the 7th frame: no beat, and on_stale fires once.
            assert lags[-1] == pytest.approx(2.4)
            assert feed.turns() == ["fresh", "behind"] and feed.ws.fresh is False
    # The last beat is the 6th frame's, taken off the socket at 2.5 s.
    assert feed.turns() == ["fresh", "behind"] and feed.ws._last_beat == 2.5
    # Another stamped frame, even a fresh one, says nothing about the backlog.
    feed.ws._dispatch(_stamped(ENGINE_MS + 99_000, type="order_update"))
    assert feed.turns() == ["fresh", "behind"] and feed.ws.fresh is False
    # The handler speeds up and works off the backlog within 15 s: fresh at the first snapshot
    # handled within stale_after and a beat of its engine time, on the same connection.
    handling[0] = 0.01
    for engine in sent[24:]:
        feed.snapshot(max(feed.clock.now, engine), engine)
        if feed.turns()[-1] == "fresh":
            break
    assert feed.turns() == ["fresh", "behind", "fresh"] and feed.ws.fresh is True
    assert feed.ws._behind_since is None and feed.clock.now < 3.0 + 15
    # Measured from the baseline (here an offset of ENGINE_MS / 1000 less a frame's lag), which
    # rose by at most 0.1% of the time since: about 0.013 s.
    assert feed.ws._lag_base is not None
    base = feed.ws._lag_base + ENGINE_MS / 1000
    assert 0.01 < base < 0.015
    assert lags[-1] - base <= 2.0 < lags[-2] - base


@pytest.mark.parametrize("ppm", [500, -500])
def test_a_day_of_clock_drift_never_falls_behind(monkeypatch: pytest.MonkeyPatch, ppm: int):
    # This machine's clock and the engine's run 500 ppm apart, as fast as NTP slews one: 43 s a
    # day, which a fixed baseline would take for a lag. Each snapshot up to a beat old.
    feed = _Feed(monkeypatch, stale_after=1.0)
    rng = random.Random(11)
    for second in range(0, 86_400, 2):
        feed.snapshot(second * (1 + ppm / 1e6) + rng.uniform(0.0, 1.0), second)
        assert feed.ws._behind_since is None
    assert "behind" not in feed.turns()


def test_an_unsubscribe_ends_a_behind_stretch_and_the_next_subscription_measures_afresh(
    monkeypatch: pytest.MonkeyPatch,
):
    # Behind, then portfolio unsubscribed, and subscribed again 30 s later while the handlers
    # are still late: the first snapshot back starts a stretch of its own, which gets its own
    # on_stale and its own 15 s before the connection is ended.
    feed = _Feed(monkeypatch)
    feed.snapshot(0.05, 0)
    feed.snapshot(1.05, 1)
    feed.snapshot(7.05, 2)  # 5 s late
    assert feed.turns() == ["fresh", "behind"] and feed.ws._behind_since == 7.05
    feed.ws.unsubscribe("portfolio")
    assert feed.ws._behind_since is None and feed.ws._lag_base is None
    feed.ws.subscribe_portfolio()
    feed.snapshot(42.05, 37)  # still 5 s late, against no baseline yet
    assert feed.turns() == ["fresh", "behind", "fresh"] and feed.ws.fresh is True
    feed.snapshot(48.05, 38)  # 5 s later than the one before
    assert feed.turns() == ["fresh", "behind", "fresh", "behind"]
    feed.snapshot(63.0, 39)  # behind for 14.95 s
    with pytest.raises(ConnectionError, match="the handlers stayed behind the feed"):
        feed.snapshot(63.1, 40)


def test_a_backlog_that_grows_slowly_is_flagged_at_stale_after_and_a_beat(
    monkeypatch: pytest.MonkeyPatch,
):
    # The handlers fall 20 ms further behind every second: flagged once the backlog passes
    # stale_after and a beat (with the 0.1% a second the baseline may rise, 4.2 s here), not
    # minutes later, and the connection is ended once it has stayed behind 15 s.
    feed = _Feed(monkeypatch)  # stale_after 3
    handled: list[int] = []
    flagged: list[int] = []
    with pytest.raises(ConnectionError) as ended:
        for second in range(3600):
            handled.append(second)
            feed.snapshot(second + 0.05 + 0.02 * second, second)
            if not flagged and feed.turns()[-1:] == ["behind"]:
                flagged.append(second)
    assert str(ended.value) == "the handlers stayed behind the feed"
    assert feed.turns() == ["fresh", "behind"]
    (first,) = flagged
    assert 4.0 < 0.02 * first < 4.3
    # Taken off the socket every 1.02 s: the 15th snapshot after the first behind is past 15 s.
    assert handled[-1] == first + 15


def test_an_engine_clock_stepped_back_ends_the_connection_after_15_s(
    monkeypatch: pytest.MonkeyPatch,
):
    # The engine's clock steps back 5 s (an NTP correction, say): on this connection its
    # snapshots look 5 s late for good. Behind at once, and the connection is ended once that
    # has lasted 15 s; the next one measures from a baseline of its own (test_ws_lifecycle).
    feed = _Feed(monkeypatch)
    handled: list[int] = []
    with pytest.raises(ConnectionError) as ended:
        for second in range(3600):
            handled.append(second)
            feed.snapshot(second + 0.1, second if second < 600 else second - 5)
    assert str(ended.value) == "the handlers stayed behind the feed"
    assert feed.turns() == ["fresh", "behind"] and handled[-1] == 616


def test_a_stall_that_drains_at_once_recovers_without_a_reconnect(
    monkeypatch: pytest.MonkeyPatch,
):
    # A handler holds the loop 6 s once, at 1 Hz snapshots: the six read meanwhile are taken off
    # the socket together as it returns, 6 s late down to 1 s late. Behind at the first of them,
    # fresh at the first within bounds, and the connection stays.
    feed = _Feed(monkeypatch)
    for second in range(60):
        late = 10 <= second < 16
        feed.snapshot(16.05 + (second - 10) * 0.001 if late else second + 0.05, second)
    assert feed.turns() == ["fresh", "behind", "fresh"] and feed.ws.fresh is True


def test_a_handler_cancelled_error_is_logged_and_swallowed(caplog: pytest.LogCaptureFixture):
    # A task's cancellation reaches only an await, never a handler's own code: one raised there
    # is the handler's failure, and the other handlers still run.
    ws = _client()
    seen: list[str] = []

    def cancelled(_: Any) -> None:
        raise asyncio.CancelledError

    ws.on_market_halt(cancelled)
    ws.on_market_halt(lambda m: seen.append(m.type))
    ws.on_message(lambda m: seen.append("*"))
    ws._dispatch(_frame(type="market_halt", ticker="OPRA", isHalted=True))
    assert seen == ["market_halt", "*"]
    assert "Loaf WebSocket handler for 'market_halt' raised" in caplog.text
    assert "CancelledError" in caplog.text


def test_an_error_frame_with_no_handler_is_logged(caplog: pytest.LogCaptureFixture):
    ws = _client()
    ws._dispatch(_frame(
        type="error", code="UNKNOWN_CHANNEL", message="Unknown channel orderbook:opra\n" * 500,
    ))
    (record,) = [r for r in caplog.records if r.name == "loaf.ws"]
    assert record.levelname == "WARNING"
    text = record.getMessage()
    assert text.startswith(
        "Loaf WebSocket server error UNKNOWN_CHANNEL: Unknown channel orderbook:opra Unknown"
    )
    assert "\n" not in text and text.endswith("...") and len(text) < 300
    caplog.clear()
    ws.on_error(lambda m: None)  # handled: not logged
    ws._dispatch(_frame(type="error", code="UNKNOWN_CHANNEL", message="Unknown channel"))
    assert not [r for r in caplog.records if r.name == "loaf.ws"]


@pytest.mark.parametrize("mtype", ["__connect__", "__disconnect__", "__stale__", "__fresh__", "*"])
def test_a_wire_type_that_names_an_internal_key_reaches_only_on_message(mtype: str):
    ws = _client().subscribe_portfolio()
    seen: list[Any] = []
    ws.on_connect(lambda m: seen.append("connect"))
    ws.on_disconnect(lambda m: seen.append("disconnect"))
    ws.on_stale(lambda m: seen.append("stale"))
    ws.on_fresh(lambda m: seen.append("fresh"))
    ws.on_message(lambda m: seen.append(("*", m.type)))
    ws._dispatch(_frame(type=mtype, reason="silent", engineMs=ENGINE_MS))
    assert seen == [("*", mtype)]
    assert ws.fresh is None

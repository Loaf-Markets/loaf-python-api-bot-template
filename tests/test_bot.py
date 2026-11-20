"""Offline test of the bot template (bot.py): the trading key it needs, the market it
follows, how it seeds from REST around the live feed, and when its strategy runs."""

from __future__ import annotations

import importlib.util
import signal
import sys
import threading
import time
from decimal import Decimal
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import httpx
import pytest

import loaf
from loaf import LoafObject, LoafServerError

from conftest import L2_PUBLIC_KEY, MARKET_L2, TRADING_KEY

# The fields of GET /trade/{ticker}'s market that let it trade, a halt aside: LIVE, with an L2
# market, no start to wait for.
TRADEABLE: dict[str, Any] = {"status": "LIVE", "l2": MARKET_L2, "tradingStartsAt": None}


def _bot(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    # bot.py loads a .env at import when python-dotenv is installed: never here.
    monkeypatch.setitem(sys.modules, "dotenv", None)
    spec = importlib.util.spec_from_file_location("bot", Path(__file__).parent.parent / "bot.py")
    assert spec is not None and spec.loader is not None
    bot = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bot)
    return bot


def test_the_bot_needs_the_trading_key(monkeypatch: pytest.MonkeyPatch):
    # Every private read and the portfolio channel take its login tokens: no key, no bot.
    bot = _bot(monkeypatch)
    with pytest.raises(SystemExit) as info:
        bot.build_client()
    assert str(info.value).startswith("No LOAF_TRADING_KEY set.")
    assert "Show trading key" in str(info.value)
    monkeypatch.setenv("LOAF_TRADING_KEY", "loaf_tk_4242_5_0x00")  # refused, and never echoed
    with pytest.raises(SystemExit) as info:
        bot.build_client()
    assert "is not a trading key" in str(info.value) and "0x00" not in str(info.value)
    monkeypatch.setenv("LOAF_TRADING_KEY", TRADING_KEY)
    assert bot.build_client().public_key == L2_PUBLIC_KEY


def test_seed_keeps_frames_newer_than_its_read(monkeypatch: pytest.MonkeyPatch):
    # GET /trade/MUS as a stale read can still show it after a reconnect: halted, bid 100.
    stale = LoafObject({
        "market": {**TRADEABLE, "isHalted": True},
        "orderBook": {"bids": [{"price": 100}], "asks": [{"price": 101}]},
    })
    client = SimpleNamespace(market=SimpleNamespace(get=lambda ticker: stale))
    strategy = _bot(monkeypatch).Strategy(client, "MUS")

    def state() -> tuple:
        return strategy.is_halted, strategy.best_bid, strategy.best_ask

    strategy.on_feed_reset(None)  # live: the next tick seeds
    strategy.on_tick()
    assert state() == (True, 100, 101)

    # The feed drops: its book and mark are forgotten, and nothing re-seeds them while it is down.
    strategy.on_mark_price(LoafObject({"price": Decimal("100.5")}))  # as the client reads it
    strategy.on_disconnect(LoafObject({"code": 1006, "reason": ""}))
    strategy.on_tick()
    assert state() == (True, None, None) and strategy.mark_price is None
    assert not strategy.needs_seed

    # A reconnect, then a resume and a book whose bids are gone: newer than the read, so they stay.
    strategy.on_feed_reset(None)
    strategy.on_halt(LoafObject({"ticker": "MUS", "isHalted": False}))
    strategy.on_orderbook(LoafObject({"bids": [], "asks": [{"price": 105}]}))
    strategy.on_tick()
    assert state() == (False, None, 105) and not strategy.needs_seed

    # A reconnect with no frame since: the read seeds both again.
    strategy.on_feed_reset(None)
    strategy.on_tick()
    assert state() == (True, 100, 101)


def test_a_failed_read_is_tried_again_on_the_next_tick(monkeypatch: pytest.MonkeyPatch):
    detail = LoafObject({
        "market": {**TRADEABLE, "isHalted": True},
        "orderBook": {"bids": [{"price": 100}]},
    })
    failures = [LoafServerError("Bad Gateway", status_code=502)]  # reads do not retry a 502

    def read(ticker: str) -> LoafObject:
        if failures:
            raise failures.pop()
        return detail

    client = SimpleNamespace(market=SimpleNamespace(get=read))
    strategy = _bot(monkeypatch).Strategy(client, "MUS")
    strategy.on_feed_reset(None)  # live: the next tick seeds
    strategy.on_tick()  # nothing is seeded, and the next tick asks again
    assert strategy.needs_seed and (strategy.is_halted, strategy.best_bid) == (False, None)
    strategy.on_tick()
    assert not strategy.needs_seed and (strategy.is_halted, strategy.best_bid) == (True, 100)


def test_the_target_is_the_first_market_trading_now(monkeypatch: pytest.MonkeyPatch):
    now = int(time.time())
    # A GET /trade row's fields that let it trade: LIVE, not halted, an L2 market, no start.
    trades = {
        "type": "SPOT", "status": "LIVE", "isHalted": False, "hasL2Market": True,
        "tradingStartsAt": None,
    }
    rows = [
        {**trades, "ticker": "PEND", "status": "PENDING"},
        {**trades, "ticker": "HALT", "isHalted": True},
        {**trades, "ticker": "NOL2", "hasL2Market": False},
        {**trades, "ticker": "SOON", "tradingStartsAt": now + 3600},
        {**trades, "ticker": "OPEN", "tradingStartsAt": now - 60},
        {**trades, "ticker": "MUS"},
    ]
    client = SimpleNamespace(market=SimpleNamespace(list=lambda: LoafObject({"markets": rows})))
    bot = _bot(monkeypatch)
    assert bot.resolve_target(client).ticker == "OPEN"
    del rows[4]  # None: no wait
    assert bot.resolve_target(client).ticker == "MUS"
    del rows[4]  # none trades now: the first LIVE one with an L2 market, its halt waited out
    assert bot.resolve_target(client).ticker == "HALT"
    del rows[1]  # or its start
    assert bot.resolve_target(client).ticker == "SOON"
    del rows[2]  # none LIVE with an L2 market: the first listed, to watch
    assert bot.resolve_target(client).ticker == "PEND"
    # The platform-wide halt is on: every row reads halted (isHalted is the effective halt).
    halted = {**trades, "isHalted": True}
    rows[:] = [{**halted, "ticker": "NEWCO", "status": "PENDING"}, {**halted, "ticker": "MUS"}]
    assert bot.resolve_target(client).ticker == "MUS"


def test_the_loop_waits_for_the_trading_start(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    bot = _bot(monkeypatch)
    clock = [1_800_000_000.0]
    monkeypatch.setattr(bot, "time", SimpleNamespace(time=lambda: clock[0]))
    detail = LoafObject({
        "market": {**TRADEABLE, "isHalted": False, "tradingStartsAt": 1_800_000_060},
    })
    strategy = bot.Strategy(SimpleNamespace(market=SimpleNamespace(get=lambda t: detail)), "MUS")
    strategy.on_tick()  # the read seeds the start
    assert strategy.trading_starts_at == 1_800_000_060
    assert capsys.readouterr().out.endswith(" [NOT STARTED]\n")
    clock[0] = 1_800_000_060.0  # the exchange takes orders from that second on
    strategy.on_tick()
    assert "[NOT STARTED]" not in capsys.readouterr().out


def test_a_market_listed_after_the_seed_is_read_until_it_trades(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    bot = _bot(monkeypatch)
    clock = [1_800_000_000.0]
    monkeypatch.setattr(bot, "time", SimpleNamespace(time=lambda: clock[0]))
    market = {**TRADEABLE, "status": "PENDING", "isHalted": False}
    reads: list[str] = []

    def read(ticker: str) -> LoafObject:
        reads.append(ticker)
        return LoafObject({"market": market, "orderBook": None})

    strategy = bot.Strategy(SimpleNamespace(market=SimpleNamespace(get=read)), "MUS")
    tick = "[MUS] bid=None ask=None spread=None mark=None"
    strategy.on_tick()  # not listed: the strategy waits, and the next tick reads the market again
    assert capsys.readouterr().out == f"{tick} [PENDING]\n" and strategy.needs_seed
    # Now listed, its trading starting in a minute: no frame says so.
    market.update(status="LIVE", tradingStartsAt=1_800_000_060)
    strategy.on_tick()
    assert capsys.readouterr().out == f"{tick} [NOT STARTED]\n" and len(reads) == 2
    clock[0] = 1_800_000_060.0
    strategy.on_tick()  # read at its start: it trades, so the strategy runs and the reads stop
    strategy.on_tick()
    assert capsys.readouterr().out == f"{tick}\n" * 2 and len(reads) == 3


def test_a_read_while_the_feed_is_down_leaves_the_book_alone(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    bot = _bot(monkeypatch)
    clock = [1_800_000_000.0]
    monkeypatch.setattr(bot, "time", SimpleNamespace(time=lambda: clock[0], monotonic=lambda: 0.0))
    market = {**TRADEABLE, "isHalted": False, "tradingStartsAt": 1_800_000_060}
    reads: list[str] = []

    def read(ticker: str) -> LoafObject:
        reads.append(ticker)  # GET /trade/MUS serves a LIVE market's book whatever its start
        return LoafObject({"market": market, "orderBook": {"bids": [{"price": 100}], "asks": []}})

    strategy = bot.Strategy(SimpleNamespace(market=SimpleNamespace(get=read)), "MUS")
    tick = "[MUS] bid={} ask=None spread=None mark=None"
    strategy.on_feed_reset(None)
    strategy.on_tick()  # live: the read seeds the book, and the strategy waits for the start
    assert capsys.readouterr().out == tick.format(100) + " [NOT STARTED]\n"
    strategy.on_disconnect(LoafObject({"code": 1006, "reason": ""}))
    market.update(tradingStartsAt=1_800_000_120)  # moved a minute later: no frame says so
    clock[0] = 1_800_000_060.0
    strategy.on_tick()  # down: the read shows the new start, and leaves the book forgotten
    assert capsys.readouterr().out.endswith(tick.format(None) + " [NOT STARTED]\n")
    clock[0] = 1_800_000_120.0
    strategy.on_tick()  # read at its start: it trades, on no book, and the reads stop
    strategy.on_tick()
    assert capsys.readouterr().out == (tick.format(None) + "\n") * 2 and len(reads) == 3
    strategy.on_feed_reset(None)  # live again: the next read seeds the book
    strategy.on_tick()
    assert capsys.readouterr().out == tick.format(100) + "\n" and len(reads) == 4


@pytest.mark.parametrize(
    ("market", "flags", "read_again"),
    [
        ({"status": "PENDING"}, " [PENDING]", True),
        ({"status": "DELISTED"}, " [DELISTED]", True),
        ({"l2": None}, " [NO L2 MARKET]", True),  # a perp, until perps trade
        ({"tradingStartsAt": 1_800_000_001}, " [NOT STARTED]", True),
        ({"isHalted": True}, " [HALTED]", False),  # a frame ends it
        ({"isHalted": True, "status": "PENDING"}, " [HALTED] [PENDING]", True),
        ({}, "", False),  # it trades: the strategy runs
    ],
)
def test_each_tick_says_why_the_strategy_waits(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    market: dict[str, Any],
    flags: str,
    read_again: bool,
):
    bot = _bot(monkeypatch)
    monkeypatch.setattr(bot, "time", SimpleNamespace(time=lambda: 1_800_000_000.0))
    detail = LoafObject({"market": {**TRADEABLE, "isHalted": False, **market}, "orderBook": None})
    strategy = bot.Strategy(SimpleNamespace(market=SimpleNamespace(get=lambda t: detail)), "MUS")
    strategy.on_tick()
    assert capsys.readouterr().out == f"[MUS] bid=None ask=None spread=None mark=None{flags}\n"
    assert strategy.needs_seed is read_again


@pytest.mark.parametrize(
    ("before", "during", "bid"),
    [
        ("C", "", 100),  # live from the request until the answer is applied
        ("", "", None),  # at startup: the feed has not been live yet
        ("", "C", None),  # at startup, and live before the answer (main ticked before on_connect)
        ("CD", "", None),  # down: no frame would update the book
        ("CD", "C", None),  # down, and live again before the answer
        ("C", "D", None),  # live, and down before the answer
        ("C", "DC", None),  # live, down and live again before the answer
        ("C", "C", None),  # live, and a new connection before the answer (stop() sends no D)
    ],
)
def test_a_read_takes_the_book_only_if_the_feed_was_live_throughout(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    before: str,
    during: str,
    bid: int | None,
):
    # C: the feed goes live (on_connect). D: a connection attempt ends (on_disconnect).
    def feed(events: str) -> None:
        for event in events:
            if event == "C":
                strategy.on_feed_reset(None)
            else:
                strategy.on_disconnect(LoafObject({"code": 1006, "reason": ""}))

    in_flight = [during]  # what happens while the first read is in flight

    def read(ticker: str) -> LoafObject:
        answer = LoafObject({
            "market": {**TRADEABLE, "isHalted": False},
            "orderBook": {"bids": [{"price": 100}], "asks": []},
        })
        if in_flight:
            feed(in_flight.pop())  # after the server answered, before the bot applies it
        return answer

    def tick() -> str:
        strategy.on_tick()
        (line,) = [out for out in capsys.readouterr().out.splitlines() if out.startswith("[MUS]")]
        return line

    strategy = _bot(monkeypatch).Strategy(SimpleNamespace(market=SimpleNamespace(get=read)), "MUS")
    feed(before)
    # It trades, so the strategy runs: on no book unless the feed was live for the whole read.
    assert tick() == f"[MUS] bid={bid} ask=None spread=None mark=None"
    if not (before + during).endswith("C"):
        feed("C")
    # Live with no read in flight: the book is seeded.
    assert tick() == "[MUS] bid=100 ask=None spread=None mark=None"


@pytest.mark.parametrize(
    ("code", "reason", "line"),
    [
        (None, "ConnectError", "  feed: no connection (ConnectError)\n"),  # an attempt failed
        (None, "KeyboardInterrupt", "  feed: no connection (KeyboardInterrupt)\n"),  # run over
        (1006, "", "  feed closed: 1006 ; reconnecting\n"),
    ],
)
def test_an_ended_connection_promises_no_retry_it_cannot_know_of(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], code: Any,
    reason: str, line: str,
):
    # A code of None is also how the feed's run ends on a handler's SystemExit or
    # KeyboardInterrupt, which nothing retries; a retry that works shows as on_connect.
    strategy = _bot(monkeypatch).Strategy(SimpleNamespace(), "MUS")
    strategy.on_disconnect(LoafObject({"code": code, "reason": reason}))
    assert capsys.readouterr().out == line


def test_a_liquidation_row_is_reported_as_a_fill(monkeypatch: pytest.MonkeyPatch, capsys):
    strategy = _bot(monkeypatch).Strategy(SimpleNamespace(), "MUS-PERP")
    trade = {"tradeId": 990, "orderId": 77, "source": "LIQUIDATION", "side": "BUY",
             "quantity": 1.5, "ticker": "MUS-PERP", "price": 26.1, "fee": 0}
    strategy.on_my_fill(LoafObject({"type": "trade_new", "trade": trade}))
    printed = capsys.readouterr().out
    assert "FILLED (LIQUIDATION): BUY 1.5 MUS-PERP @ 26.1 (fee 0, order #77) #990" in printed


def test_the_tick_shows_a_stale_private_stream_until_it_is_live_again(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    detail = LoafObject({"market": {**TRADEABLE, "isHalted": False}, "orderBook": None})
    strategy = _bot(monkeypatch).Strategy(
        SimpleNamespace(market=SimpleNamespace(get=lambda t: detail)), "MUS-PERP"
    )

    def tick() -> str:
        strategy.on_tick()
        (line,) = [out for out in capsys.readouterr().out.splitlines() if out.startswith("[MUS")]
        return line

    quiet = "[MUS-PERP] bid=None ask=None spread=None mark=None"
    assert strategy.private_fresh is None and tick() == quiet  # no beat yet: nothing claimed
    strategy.on_fresh(LoafObject({"engineMs": 1_800_000_000_000}))
    assert strategy.private_fresh is True and capsys.readouterr().out == ""  # the first: quiet
    strategy.on_stale(LoafObject({"reason": "silent", "engineMs": 1_800_000_000_000,
                                  "silentSeconds": 3.0}))
    assert capsys.readouterr().out == "  *** private stream STALE (silent; last news 3.0 s ago)\n"
    assert strategy.private_fresh is False and tick() == quiet + " [STALE]"
    strategy.on_fresh(LoafObject({"engineMs": 1_800_000_004_000}))
    assert capsys.readouterr().out == "  *** private stream live again\n"
    assert strategy.private_fresh is True and tick() == quiet


# A GET /trade row's fields that let it trade, its type aside.
TRADES: dict[str, Any] = {
    "status": "LIVE", "isHalted": False, "hasL2Market": True, "tradingStartsAt": None,
}
PERP_ROW = {**TRADES, "ticker": "OPRA-PERP", "type": "PERP", "name": "Opera", "marketIndex": 4}
SPOT_ROW = {**TRADES, "ticker": "OPRA", "type": "SPOT", "name": "Opera", "marketIndex": 2050}


def _listed(*rows: dict[str, Any]) -> SimpleNamespace:
    listing = LoafObject({"markets": list(rows)})
    return SimpleNamespace(market=SimpleNamespace(list=lambda: listing))


def test_the_target_is_a_spot_market_before_a_perp(monkeypatch: pytest.MonkeyPatch):
    bot = _bot(monkeypatch)
    # Perps are listed first: their market indexes are the low ones.
    assert bot.resolve_target(_listed(PERP_ROW, SPOT_ROW)).ticker == "OPRA"
    # No spot market trades now: the first tradeable market of any type.
    halted = {**SPOT_ROW, "isHalted": True}
    assert bot.resolve_target(_listed(PERP_ROW, halted)).ticker == "OPRA-PERP"


@pytest.mark.parametrize("ticker", ["opra", "MUS"])  # a typo (the case); a market not listed
def test_a_target_ticker_that_is_not_listed_stops_the_bot(
    monkeypatch: pytest.MonkeyPatch, ticker: str
):
    bot = _bot(monkeypatch)
    client = _listed(PERP_ROW, SPOT_ROW)
    monkeypatch.setattr(bot, "TARGET_TICKER", ticker)
    with pytest.raises(SystemExit) as info:  # never another market in its place
        bot.resolve_target(client)
    assert str(info.value).startswith(f"TARGET_TICKER {ticker!r} is not a listed market.\n")
    assert "  Listed: OPRA, OPRA-PERP\n" in str(info.value)
    monkeypatch.setattr(bot, "TARGET_TICKER", "OPRA-PERP")  # a listed one is followed, perp or not
    assert bot.resolve_target(client).ticker == "OPRA-PERP"


@pytest.mark.parametrize(
    ("quantity_decimals", "dollars", "price", "quantity"),
    [
        (5, 25, 75_000, Decimal("0.00033")),  # a lot of 0.00001: $24.75, rounded down
        (1, 25, 3.32, Decimal("7.5")),  # a lot of 0.1: $24.90
        (3, 25, Decimal("160.01"), Decimal("0.156")),
        (2, 400, 40_000, Decimal("0.01")),  # exactly one lot
        (1, 1, 75_000, None),  # one lot of 0.1 is $7,500: none, never more than asked for
        (2, 25, 40_000, None),  # one lot of 0.01 is $400
        (0, Decimal("2.99"), 3, None),
    ],
)
def test_an_amount_in_dollars_is_a_quantity_on_the_markets_lot(
    monkeypatch: pytest.MonkeyPatch, quantity_decimals: int, dollars: Any, price: Any,
    quantity: Decimal | None,
):
    strategy = _bot(monkeypatch).Strategy(SimpleNamespace(), "MUS")
    with pytest.raises(RuntimeError, match="its lot is not known"):  # before the market is read
        strategy.quantity_worth(dollars, price)
    strategy.quantity_decimals = quantity_decimals
    worth = strategy.quantity_worth(dollars, price)
    assert worth == quantity
    if worth is not None:
        assert worth.as_tuple().exponent == -quantity_decimals
        assert worth * Decimal(str(price)) <= Decimal(str(dollars))
    for bad in ((0, price), (dollars, 0), (dollars, -1), (dollars, float("nan"))):
        with pytest.raises(ValueError, match="must be positive"):
            strategy.quantity_worth(*bad)


class _Orders:
    """The order methods the strategy's examples call: each call recorded, nothing sent."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def __getattr__(self, name: str) -> Any:
        def record(*args: Any, **kwargs: Any) -> Any:
            self.calls.append((name, kwargs))
            return SimpleNamespace(orderId=len(self.calls))

        return record


def _examples_of_trade(bot: ModuleType) -> Any:
    """The examples commented in Strategy.trade, uncommented, as a function of the strategy
    and the tick's bid, ask and mark: what a reader who pastes them runs."""
    lines = (Path(__file__).parent.parent / "bot.py").read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if "def trade(self" in line)
    end = next(i for i, line in enumerate(lines) if "def on_shutdown(self)" in line)
    prefix = "        #   "
    body = [line[len(prefix):] for line in lines[start:end] if line.startswith(prefix)]
    source = "def examples(self, bid, ask, mark):\n" + "\n".join("    " + b for b in body)
    scope: dict[str, Any] = {}
    exec(compile(source, "trade examples", "exec"), vars(bot), scope)
    return scope["examples"]


@pytest.mark.parametrize(
    ("decimals", "price", "placed"),
    [
        ((2, 3), 160, True),  # a lot of 0.001 is $0.16: about $25 each
        ((0, 2), 40_000, False),  # a lot of 0.01 is $400: no order rather than $400 for $25
        ((2, 1), 3_000, False),  # a lot of 0.1 is $300
    ],
)
def test_the_examples_of_trade_never_order_more_than_25_dollars(
    monkeypatch: pytest.MonkeyPatch, decimals: tuple, price: int, placed: bool
):
    bot = _bot(monkeypatch)
    orders = _Orders()
    strategy = bot.Strategy(SimpleNamespace(orders=orders), "OPRA")
    strategy.price_decimals, strategy.quantity_decimals = decimals
    strategy.min_order_value, strategy.market_type = 10, "SPOT"
    strategy.quote_id = None
    _examples_of_trade(bot)(strategy, float(price), float(price) * 1.01, Decimal(price))
    names = [name for name, _ in orders.calls]
    sized = [kwargs for name, kwargs in orders.calls if "quantity" in kwargs]
    if placed:
        assert names == ["limit_buy", "market_sell", "stop_loss", "cancel_all", "cancel_after",
                         "limit_buy"]
    else:  # the cancels only
        assert names == ["cancel_all", "cancel_after"] and sized == []
    for kwargs in sized:
        at = kwargs.get("price") or kwargs.get("trigger_price") or price
        assert 0 < Decimal(str(kwargs["quantity"])) * Decimal(str(at)) <= 25


@pytest.mark.parametrize(
    "answer",
    [
        "<html>Bad Gateway</html>",  # a body that is not JSON
        [],
        LoafObject({"error": "busy"}),
        LoafObject({"market": "MUS"}),
        LoafObject({"market": {**TRADEABLE}, "orderBook": {"bids": [{}]}}),  # a level, no price
    ],
    ids=["text", "list", "no-market", "market-not-an-object", "level-without-a-price"],
)
def test_an_answer_that_is_not_a_market_is_read_again(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], answer: Any
):
    market = {**TRADEABLE, "type": "PERP", "isHalted": False, "priceDecimals": 2,
              "quantityDecimals": 3, "minOrderValue": 10}
    answers = [answer, LoafObject({"market": market, "orderBook": None})]
    client = SimpleNamespace(market=SimpleNamespace(get=lambda ticker: answers.pop(0)))
    strategy = _bot(monkeypatch).Strategy(client, "OPRA-PERP")
    strategy.on_tick()
    out = capsys.readouterr().out
    assert out.startswith("  could not read OPRA-PERP (")
    assert out.endswith("; trying again next tick\n") and strategy.needs_seed
    strategy.on_tick()  # the next tick reads it again, and keeps what never changes
    assert not strategy.needs_seed
    assert (strategy.market_type, strategy.price_decimals) == ("PERP", 2)
    assert (strategy.quantity_decimals, strategy.min_order_value) == (3, 10)


def _trades_on(strategy: Any, monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    """Each (bid, ask, mark) the strategy's trade is called with."""
    calls: list[tuple] = []
    monkeypatch.setattr(strategy, "trade", lambda bid, ask, mark: calls.append((bid, ask, mark)))
    return calls


def test_the_strategy_waits_while_the_private_stream_is_stale(monkeypatch: pytest.MonkeyPatch):
    detail = LoafObject({"market": {**TRADEABLE, "isHalted": False}, "orderBook": None})
    strategy = _bot(monkeypatch).Strategy(
        SimpleNamespace(market=SimpleNamespace(get=lambda t: detail)), "MUS"
    )
    calls = _trades_on(strategy, monkeypatch)
    strategy.on_tick()  # no beat yet: nothing claimed, so it trades
    strategy.on_stale(LoafObject({"reason": "silent", "engineMs": None, "silentSeconds": 3.0}))
    strategy.on_tick()  # your orders and fills may be out of date
    assert len(calls) == 1
    strategy.on_fresh(LoafObject({"engineMs": 1_800_000_000_000}))
    strategy.on_tick()
    assert len(calls) == 2
    strategy.on_halt(LoafObject({"ticker": "MUS", "isHalted": True}))
    strategy.on_tick()
    assert len(calls) == 2


def test_a_book_the_feed_stopped_sending_is_not_known(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    bot = _bot(monkeypatch)
    # The book's age is on the monotonic clock: the computer's clock (time.time) is set back
    # an hour, then forward a day, and changes nothing.
    clock, wall = [5_000.0], [1_800_000_000.0]
    clocks = SimpleNamespace(time=lambda: wall[0], monotonic=lambda: clock[0])
    monkeypatch.setattr(bot, "time", clocks)
    detail = LoafObject({
        "market": {**TRADEABLE, "isHalted": False},
        "orderBook": {"bids": [{"price": 100}], "asks": [{"price": 101}]},
    })
    strategy = bot.Strategy(SimpleNamespace(market=SimpleNamespace(get=lambda t: detail)), "MUS")
    calls = _trades_on(strategy, monkeypatch)

    def tick() -> str:
        strategy.on_tick()
        return capsys.readouterr().out

    strategy.on_feed_reset(None)
    assert tick() == "[MUS] bid=100 ask=101 spread=1 mark=None\n"  # the read seeds the book
    clock[0] += 15  # no frame since (a refused or stalled subscription; one comes every 5 s)
    wall[0] -= 3600
    assert tick() == "[MUS] bid=100 ask=101 spread=1 mark=None\n"
    clock[0] += 0.5
    assert tick() == "[MUS] bid=None ask=None spread=None mark=None [OLD BOOK]\n"
    strategy.on_orderbook(LoafObject({"bids": [{"price": 102}], "asks": []}))
    wall[0] += 86_400
    assert tick() == "[MUS] bid=102 ask=None spread=None mark=None\n"
    clock[0] += 16
    assert tick() == "[MUS] bid=None ask=None spread=None mark=None [OLD BOOK]\n"
    assert [call[:2] for call in calls] == [
        (100, 101), (100, 101), (None, None), (102, None), (None, None),
    ]


def _unknown(index: int) -> loaf.OrderOutcomeUnknownError:
    signed = {"l2OrderIndex": index, "l2AccountIndex": 5, "l2ExpiryMs": 1_800_259_200_000,
              "l2SigS": "ab" * 40, "l2SigE": "cd" * 40}
    return loaf.OrderOutcomeUnknownError(
        signed_body=signed, attempts=3, last_error=LoafServerError("Bad Gateway", status_code=502)
    )


def test_a_loaf_error_from_a_tick_is_printed_and_the_next_tick_runs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    bot = _bot(monkeypatch)
    monkeypatch.setattr(bot, "TICK_SECONDS", 0)
    stop = threading.Event()
    errors: list[Exception] = [LoafServerError("Bad Gateway", status_code=502)]
    ticks: list[int] = []

    def on_tick() -> None:
        ticks.append(1)
        if errors:
            raise errors.pop()
        stop.set()

    bot.run(SimpleNamespace(on_tick=on_tick), stop)
    assert len(ticks) == 2 and capsys.readouterr().out.startswith("  tick failed: ")


def test_an_order_of_unknown_outcome_stops_the_loop(monkeypatch: pytest.MonkeyPatch):
    bot = _bot(monkeypatch)
    monkeypatch.setattr(bot, "TICK_SECONDS", 0)
    ticks: list[int] = []

    def on_tick() -> None:
        ticks.append(1)
        raise _unknown(4097)

    with pytest.raises(SystemExit) as info:
        bot.run(SimpleNamespace(on_tick=on_tick), threading.Event())
    text = str(info.value)
    assert ticks == [1] and "the outcome of order number 4097 is unknown" in text
    assert "client.orders.cancel_by_index(4097)" in text and "Never place it afresh" in text
    assert "ab" * 40 not in text and "cd" * 40 not in text  # never its signed body

    def broken() -> None:
        raise RuntimeError("a bug in the strategy")

    with pytest.raises(RuntimeError):  # anything else ends the loop as it is
        bot.run(SimpleNamespace(on_tick=broken), threading.Event())


def _op_body(**fields: Any) -> dict:
    return {"l2AccountIndex": 5, "l2Nonce": 31, "l2ExpiryMs": 1_800_259_200_000,
            "l2SigS": "ab" * 40, "l2SigE": "cd" * 40, **fields}


@pytest.mark.parametrize(
    ("error", "starts", "settles"),
    [
        (loaf.PerpsOutcomeUnknownError(
            signed_body=_op_body(fromSide=0, amount="1000000"), attempts=3,
            last_error=LoafServerError("Bad Gateway", status_code=502)),
         "Stopping: the margin move signed at your account's nonce 31 may or may not",
         ("client.perps.resubmit(error.signed_body)", "catch\n  loaf.PerpsOutcomeUnknownError")),
        (loaf.PerpsOutcomeUnknownError(
            signed_body=_op_body(l2MarketIndex=4), attempts=3,
            last_error=LoafServerError("Bad Gateway", status_code=502)),
         "Stopping: the exit signed at your account's nonce 31 may or may not",
         ("client.perps.resubmit(error.signed_body)", "Never sign it afresh")),
        (loaf.WithdrawalOutcomeUnknownError(
            signed_body=_op_body(l2AssetId=1, amount="1000000"), attempts=3,
            last_error=LoafServerError("Bad Gateway", status_code=502)),
         "Stopping: the withdrawal signed at your account's nonce 31 may or may not",
         ("client.funds.resubmit(error.signed_body)", "ORDER_INDEX_USED", "WITHDRAW_FEE_ABOVE_MAX",
          "catch\n  loaf.WithdrawalOutcomeUnknownError")),
    ],
    ids=["margin-move", "exit", "withdrawal"],
)
def test_a_perps_op_or_withdrawal_of_unknown_outcome_stops_the_loop(
    monkeypatch: pytest.MonkeyPatch, error: loaf.LoafError, starts: str, settles: tuple
):
    # A next tick could sign the same intent afresh, at the next nonce: both could apply.
    bot = _bot(monkeypatch)
    monkeypatch.setattr(bot, "TICK_SECONDS", 0)
    stop = threading.Event()
    ticks: list[int] = []

    def on_tick() -> None:
        ticks.append(1)
        if len(ticks) > 1:
            stop.set()
            return
        raise error

    with pytest.raises(SystemExit) as info:
        bot.run(SimpleNamespace(on_tick=on_tick), stop)
    text = str(info.value)
    assert ticks == [1] and text.startswith(starts)
    for words in settles:
        assert words in text
    assert "ab" * 40 not in text and "cd" * 40 not in text  # never its signed body


class _Feed:
    """The real-time client main wires up: each call recorded, nothing sent."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __getattr__(self, name: str) -> Any:
        def record(*args: Any, **kwargs: Any) -> bool:
            self.calls.append(name)
            return True

        return record


def _main(monkeypatch: pytest.MonkeyPatch, exchange: Any, trading_client: Any,
          row: dict[str, Any], tick: Any) -> SimpleNamespace:
    """bot.py with main's client talking to ``exchange``, which lists ``row``, the feed and
    the signals faked, and ``tick(strategy, handlers)`` run as each tick."""
    bot = _bot(monkeypatch)
    exchange.queue("/trade", httpx.Response(200, json={"markets": [row]}))
    client = trading_client()
    seen = SimpleNamespace(bot=bot, feed=_Feed(), closed=[], handlers={}, shutdowns=[])
    monkeypatch.setattr(client, "websocket", lambda **kwargs: seen.feed)
    monkeypatch.setattr(client, "close", lambda: seen.closed.append(True))
    monkeypatch.setattr(bot, "build_client", lambda: client)
    monkeypatch.setattr(bot, "signal", SimpleNamespace(
        SIGINT=signal.SIGINT, SIGTERM=signal.SIGTERM, SIG_DFL=signal.SIG_DFL,
        default_int_handler=signal.default_int_handler,
        signal=lambda signum, handler: seen.handlers.__setitem__(signum, handler),
    ))
    monkeypatch.setattr(bot, "TICK_SECONDS", 0)
    monkeypatch.setattr(bot.Strategy, "on_tick", lambda self: tick(self, seen.handlers))
    return seen


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_a_signal_stops_the_bot_through_its_shutdown(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], exchange: Any,
    trading_client: Any, signum: int,
):
    ticks: list[str] = []

    def tick(strategy: Any, handlers: dict) -> None:
        ticks.append(strategy.ticker)
        handlers[signum](signum, None)  # docker stop, systemd or kill; or Ctrl-C

    seen = _main(monkeypatch, exchange, trading_client, PERP_ROW, tick)
    monkeypatch.setattr(seen.bot.Strategy, "on_shutdown", lambda self: seen.shutdowns.append(1))
    seen.bot.main()
    assert ticks == ["OPRA-PERP"]
    assert seen.shutdowns == [1] and seen.feed.calls[-1] == "stop" and seen.closed == [True]
    # A second signal acts as in any Python program: Ctrl-C raises KeyboardInterrupt, and
    # SIGTERM ends the process.
    assert seen.handlers == {
        signal.SIGINT: signal.default_int_handler, signal.SIGTERM: signal.SIG_DFL,
    }
    out = capsys.readouterr().out
    assert "Following market OPRA-PERP (Opera, PERP, index 4).\n" in out
    assert "  A perpetual future: its orders open and close leveraged positions" in out
    assert out.count("Stopping after the current tick (press Ctrl-C again to stop now)") == 1


def test_a_second_ctrl_c_during_a_slow_tick_stops_the_bot_at_once_through_its_shutdown(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], exchange: Any,
    trading_client: Any,
):
    ticks: list[int] = []

    def tick(strategy: Any, handlers: dict) -> None:  # a placement re-sending for minutes
        ticks.append(1)
        handlers[signal.SIGINT](signal.SIGINT, None)  # asks for a stop after this tick
        handlers[signal.SIGINT](signal.SIGINT, None)  # and now: the tick ends here
        raise AssertionError("the second Ctrl-C did not interrupt the tick")

    seen = _main(monkeypatch, exchange, trading_client, SPOT_ROW, tick)
    monkeypatch.setattr(seen.bot.Strategy, "on_shutdown", lambda self: seen.shutdowns.append(1))
    with pytest.raises(KeyboardInterrupt):
        seen.bot.main()
    assert ticks == [1] and seen.shutdowns == [1]
    assert seen.feed.calls[-1] == "stop" and seen.closed == [True]
    assert "Shutting down ..." in capsys.readouterr().out


def test_a_failed_shutdown_still_stops_the_feed_and_closes_the_client(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], exchange: Any,
    trading_client: Any,
):
    def tick(strategy: Any, handlers: dict) -> None:
        handlers[signal.SIGTERM](signal.SIGTERM, None)

    def shutdown(self: Any) -> None:
        raise LoafServerError("Bad Gateway", status_code=502)

    seen = _main(monkeypatch, exchange, trading_client, SPOT_ROW, tick)
    monkeypatch.setattr(seen.bot.Strategy, "on_shutdown", shutdown)
    seen.bot.main()
    assert seen.feed.calls[-1] == "stop" and seen.closed == [True]
    out = capsys.readouterr().out
    assert "Following market OPRA (Opera, SPOT, index 2050).\n\n" in out
    assert "perpetual" not in out and "  on_shutdown failed: " in out


def test_an_order_of_unknown_outcome_stops_the_bot_through_its_shutdown(
    monkeypatch: pytest.MonkeyPatch, exchange: Any, trading_client: Any
):
    def tick(strategy: Any, handlers: dict) -> None:
        raise _unknown(4100)

    seen = _main(monkeypatch, exchange, trading_client, SPOT_ROW, tick)
    monkeypatch.setattr(seen.bot.Strategy, "on_shutdown", lambda self: seen.shutdowns.append(1))
    with pytest.raises(SystemExit, match="order number 4100"):
        seen.bot.main()
    assert seen.shutdowns == [1] and seen.feed.calls[-1] == "stop" and seen.closed == [True]


def test_the_shutdown_as_shipped_sends_nothing(monkeypatch: pytest.MonkeyPatch):
    # The template places no order, so it cancels none: its client has nothing to send with.
    _bot(monkeypatch).Strategy(SimpleNamespace(), "MUS").on_shutdown()

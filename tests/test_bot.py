"""Offline test of the bot template (bot.py): the market it follows, how it seeds from
REST around the live feed, and when its strategy runs."""

from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from loaf import LoafObject, LoafServerError

from conftest import MARKET_L2

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
    strategy.on_mark_price(LoafObject({"price": 100.5}))
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
    trades = {"status": "LIVE", "isHalted": False, "hasL2Market": True, "tradingStartsAt": None}
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
    # Listed (POST /admin/markets/status), its trading a minute out: no frame says so.
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
    monkeypatch.setattr(bot, "time", SimpleNamespace(time=lambda: clock[0]))
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


def test_a_liquidation_fill_is_reported_as_a_fill(monkeypatch: pytest.MonkeyPatch, capsys):
    strategy = _bot(monkeypatch).Strategy(SimpleNamespace(), "MUS-PERP")
    fill = {"perpsSeq": 990, "orderId": 77, "marketIndex": 4, "ticker": "MUS-PERP",
            "side": "BUY", "quantity": 1.5, "price": 26.1}
    strategy.on_my_liquidation_fill(LoafObject({"type": "liquidation_fill", "fill": fill}))
    printed = capsys.readouterr().out
    assert "FILLED by a liquidation: BUY 1.5 MUS-PERP @ 26.1 (order #77, seq 990)" in printed

"""Offline test of the bot template (bot.py): how it seeds from REST around the live feed."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from loaf import LoafObject, LoafServerError


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
        "property": {"isHalted": True},
        "orderBook": {"bids": [{"price": 100}], "asks": [{"price": 101}]},
    })
    client = SimpleNamespace(market=SimpleNamespace(property=lambda ticker: stale))
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
    detail = LoafObject({"property": {"isHalted": True}, "orderBook": {"bids": [{"price": 100}]}})
    failures = [LoafServerError("Bad Gateway", status_code=502)]  # reads do not retry a 502

    def read(ticker: str) -> LoafObject:
        if failures:
            raise failures.pop()
        return detail

    client = SimpleNamespace(market=SimpleNamespace(property=read))
    strategy = _bot(monkeypatch).Strategy(client, "MUS")
    strategy.on_tick()  # nothing is seeded, and the next tick asks again
    assert strategy.needs_seed and (strategy.is_halted, strategy.best_bid) == (False, None)
    strategy.on_tick()
    assert not strategy.needs_seed and (strategy.is_halted, strategy.best_bid) == (True, 100)


def test_a_reconnect_during_the_read_asks_again(monkeypatch: pytest.MonkeyPatch):
    def read(ticker: str) -> LoafObject:
        strategy.on_feed_reset(None)  # the feed goes live again while the read is in flight
        return LoafObject({"property": {"isHalted": True}, "orderBook": None})

    client = SimpleNamespace(market=SimpleNamespace(property=read))
    strategy = _bot(monkeypatch).Strategy(client, "MUS")
    strategy.on_tick()
    assert strategy.needs_seed  # that read began before the feed went live: read again


def test_a_liquidation_fill_is_reported_as_a_fill(monkeypatch: pytest.MonkeyPatch, capsys):
    strategy = _bot(monkeypatch).Strategy(SimpleNamespace(), "PRP1")
    fill = {"perpsSeq": 990, "orderId": 77, "ticker": "PRP1", "side": "BUY", "quantity": 1.5,
            "price": 26.1}
    strategy.on_my_liquidation_fill(LoafObject({"type": "liquidation_fill", "fill": fill}))
    printed = capsys.readouterr().out
    assert "FILLED by a liquidation: BUY 1.5 PRP1 @ 26.1 (order #77, seq 990)" in printed

#!/usr/bin/env python3
"""Loaf trading bot — template.

A runnable starting point that:
  1. loads your credentials from the environment (or a .env file),
  2. verifies the connection and trading prerequisites,
  3. prints your balances and positions,
  4. opens the real-time feed (order book, halt status, competition round
     notices, and your private portfolio stream),
  5. runs a simple strategy loop you can replace with your own logic.

Run it:
    cp .env.example .env
    chmod 600 .env                # only you can read it; then set LOAF_API_KEY, and
                                  # LOAF_SECRET_KEY to place orders (README §2)
    pip install -e ".[dotenv]"    # the dotenv extra loads .env; or export the variables yourself
    python bot.py

Stop with Ctrl-C.
"""

from __future__ import annotations

import os
import signal
import sys
import threading
from decimal import Decimal
from typing import Any

import loaf
from loaf import LoafClient

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

# Optionally load a .env file (pip install python-dotenv). Harmless if absent.
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

# The property this template watches/trades: a ticker from
# `loaf.market.properties()`, exactly as listed (uppercase), e.g. "OPRA". Leave it
# empty to follow the first tradeable listed property.
TARGET_TICKER = ""


def build_client() -> LoafClient:
    """Create a client from the environment, with friendly errors."""
    api_key = os.environ.get("LOAF_API_KEY")
    if not api_key:
        sys.exit(
            "No LOAF_API_KEY set.\n"
            "  1. Create an API key in the Loaf web app (API -> Generate API Token).\n"
            "  2. cp .env.example .env && chmod 600 .env  and put it in LOAF_API_KEY.\n"
            "  3. To place orders, also put your trading key in LOAF_SECRET_KEY: the\n"
            "     API page's Show trading key gives it (README §2).\n"
            "  .env is read only when python-dotenv is installed\n"
            "  (pip install -e \".[dotenv]\"); otherwise export the variables.\n"
        )
    # The trading key is read from $LOAF_SECRET_KEY, and base_url / ws_url
    # from $LOAF_API_BASE_URL / $LOAF_WS_URL, if set.
    try:
        return LoafClient(api_key=api_key)
    except loaf.LoafConfigError as exc:
        # A malformed key or LOAF_MAX_SLIPPAGE_BPS, or LOAF_WALLET_PRIVATE_KEY left in the
        # environment.
        sys.exit(str(exc))


def cash_of(balances: Any) -> Decimal:
    """Your cash: the ``available`` amount of the quote-asset (``isQuote``) balances rows."""
    return sum(
        (loaf.from_base_units(b.available, b.decimals) for b in balances if b.isQuote),
        Decimal(0),
    )


# --------------------------------------------------------------------------- #
# Preflight checks
# --------------------------------------------------------------------------- #


def preflight(client: LoafClient) -> None:
    """Confirm credentials work and print balances/positions."""
    try:
        comp = client.portfolio.component()
    except loaf.LoafAuthError as exc:
        sys.exit(f"Authentication failed — check your API key. ({exc.message})")

    balances = comp.get("balances") or []
    print(f"  Cash: {cash_of(balances):,.2f} available")
    positions = comp.get("positions") or []
    if positions:
        rows = {b.assetId: b for b in balances}
        print("  Positions:")
        for p in positions:
            if p.get("isIpoAllocation"):
                held = f"{p.quantity:,.1f} (IPO allocation)"
            elif p.get("assetId") in rows:  # an L2 holding: its amounts are its balances row
                row = rows[p.assetId]
                total = loaf.from_base_units(row.total, row.decimals)
                available = loaf.from_base_units(row.available, row.decimals)
                held = f"{total:,.1f} ({available:,.1f} available)"
            else:  # no balances row: nothing held
                held = "0.0"
            avg = p.get("averageEntryPrice")
            print(f"    {p.ticker}: {held} @ avg {'n/a' if avg is None else avg} "
                  f"(mkt {p.marketPrice})")
    if client.public_key:
        bps = client.max_slippage_bps  # $LOAF_MAX_SLIPPAGE_BPS, default 200
        print(f"  Orders are signed by trading key {client.public_key}")
        print(f"  Max slippage: {bps} bps ({bps / 100:g}%) for MARKET orders, stops and TP/SL legs")
    else:
        print("  No LOAF_SECRET_KEY: watching only (placing orders needs it).")
    print(
        "  Note: during a competition round only the round's market trades, with a\n"
        "        trading key of its own (README: Competition rounds)."
    )


def resolve_target(client: LoafClient) -> Any:
    """Pick the property to follow: the configured one, else the first tradeable
    one, else the first listed (or None)."""
    listing = client.market.properties().get("properties") or []
    if not listing:
        print("No properties available on this environment.")
        return None
    if TARGET_TICKER:
        for prop in listing:
            if prop.ticker == TARGET_TICKER:
                return prop
        print(f"Ticker {TARGET_TICKER!r} not found; falling back to the first tradeable one.")
    # Tradeable: LIVE with an L2 market (halts, trading hours and round seats still apply).
    return next(
        (p for p in listing if p.get("status") == "LIVE" and p.get("hasL2Market")), listing[0]
    )


# --------------------------------------------------------------------------- #
# Strategy
# --------------------------------------------------------------------------- #


class Strategy:
    """Holds live state fed by the WebSocket, and decides what to do.

    This template only OBSERVES the market (it prints a snapshot each tick and
    never sends an order). Replace `on_tick` with your own logic — the order
    helpers you need are commented inline.
    """

    def __init__(self, client: LoafClient, ticker: str) -> None:
        self.client = client
        self.ticker = ticker
        self._lock = threading.Lock()
        self.best_bid: float | None = None
        self.best_ask: float | None = None
        self.mark_price: float | None = None
        self.last_trade: dict | None = None
        self.is_halted = False
        # The book and the halt flag are read from REST on the next tick: at startup,
        # and each time the feed goes live again (nothing is replayed).
        self.needs_seed = True
        # Which of them a frame has set since the feed went live: that read leaves those
        # alone, since a shared cache can serve it older than the frame.
        self.from_feed: set[str] = set()

    # -- WebSocket handlers (called on the WS thread) --------------------- #

    def on_orderbook(self, msg) -> None:
        with self._lock:
            self._set_book(msg)
            self.from_feed.add("book")

    def _set_book(self, book) -> None:  # under the lock
        self.best_bid = book.bids[0].price if book.get("bids") else None
        self.best_ask = book.asks[0].price if book.get("asks") else None

    def on_mark_price(self, msg) -> None:
        with self._lock:
            self.mark_price = msg.price

    def on_feed_reset(self, _msg) -> None:
        # The feed is live: the server has answered the subscriptions, and no frame
        # from here on is missed. Drop what may be stale and re-seed on the next tick,
        # from the main thread (a slow REST read in a handler stalls the socket).
        with self._lock:
            self.best_bid = self.best_ask = None
            self.mark_price = None
            self.from_feed.clear()
            self.needs_seed = True

    def on_disconnect(self, msg) -> None:
        # Every ended connection attempt; the client reconnects with backoff. Forget
        # the book and the mark so no order is priced off a dead feed: the book is
        # re-seeded once the feed is live again, and with no mark the SDK reads the
        # reference itself.
        with self._lock:
            self.best_bid = self.best_ask = None
            self.mark_price = None
        if msg.code is None:
            print(f"  feed: no connection ({msg.reason}); retrying")
        else:
            print(f"  feed closed: {msg.code} {msg.reason}; reconnecting")

    def on_round(self, msg) -> None:
        # Only report it: after a frame that switches the ledger (PREPARED, ENDING,
        # or DRAFT after PREPARED) the server closes the portfolio socket with 1012
        # "venue" about 20 s later, and on_connect then re-seeds; a REST read on the
        # frame itself can still see the previous ledger.
        print(f"  *** competition round {msg.roundNumber} {msg.status} "
              f"(its market: {msg.arenaTicker})")

    def on_halt(self, msg) -> None:
        # `isHalted` is the effective state (this property's own flag OR a
        # platform-wide halt), so it replaces the value seeded from REST.
        with self._lock:
            self.is_halted = msg.isHalted
            self.from_feed.add("halt")
        print(f"  *** {msg.ticker} {'HALTED' if msg.isHalted else 'RESUMED'}")

    def on_trade_tick(self, msg) -> None:
        trades = msg.get("trades") or []
        if trades:
            with self._lock:
                self.last_trade = trades[0]

    def on_my_fill(self, msg) -> None:
        # A fill on YOUR orders.
        t = msg.trade
        print(f"  *** FILLED: {t.side} {t.quantity} {t.ticker} @ {t.price} "
              f"(fee {t.fee}) #{t.tradeId}")

    def on_my_order(self, msg) -> None:
        # `order_status` is the transition only (FILLED / CANCELLED / ...).
        print(f"  order #{msg.orderId} -> {msg.status} (left {msg.get('quantityLeft')})")

    def on_my_order_accepted(self, msg) -> None:
        # `order_update` carries the whole committed order. It arrives when the
        # exchange accepts a new order, and for one that trades immediately it
        # can come AFTER its fill frames, already FILLED.
        o = msg.order
        print(f"  order #{o.id} {o.side} {o.quantity} {o.ticker} @ {o.price} -> {o.status}")

    def on_balances(self, msg) -> None:
        # Your whole balance book after every change.
        print(f"  balance update: cash {cash_of(msg.balances):,.2f} available")

    # -- Decision loop (called on the main thread every few seconds) ------ #

    def seed(self) -> bool:
        """Read the book and the halt flag, which the feed does not send on
        subscribe, keeping what a frame has set since the feed went live. False
        if the read failed: the next tick tries again."""
        with self._lock:
            self.needs_seed = False  # first: a reconnect during the read asks again
        try:
            detail = self.client.market.property(self.ticker)
        except loaf.LoafError as exc:
            with self._lock:
                self.needs_seed = True
            print(f"  could not read {self.ticker} ({exc}); trying again next tick")
            return False
        with self._lock:
            if "halt" not in self.from_feed:
                self.is_halted = bool(detail.property.isHalted)
            if "book" not in self.from_feed and detail.get("orderBook"):  # None: not known now
                self._set_book(detail.orderBook)
        return True

    def on_tick(self) -> None:
        if self.needs_seed and not self.seed():
            return
        with self._lock:
            bid, ask, mark = self.best_bid, self.best_ask, self.mark_price
            halted = self.is_halted
        spread = (ask - bid) if (bid is not None and ask is not None) else None
        print(f"[{self.ticker}] bid={bid} ask={ask} spread={spread} mark={mark}"
              f"{' [HALTED]' if halted else ''}")

        if halted:
            return  # orders would be rejected with loaf.TradingHaltedError

        # ----------------------------------------------------------------- #
        # YOUR STRATEGY GOES HERE. Examples (uncomment & adapt). on_tick runs
        # every 5 s, and each example as written acts on every tick (a new
        # order each time): gate it, e.g. keep the orderId a placement returns
        # and place again only once that order is gone.
        #
        #   # Quote a limit buy 1% below the best bid, resting for an hour at
        #   # most (ttl_ms; the default is 7 days): a bot's quotes should die
        #   # with it.
        #   if bid:
        #       price = round(bid * 0.99, 2)
        #       self.client.orders.limit_buy(self.ticker, quantity=1, price=price,
        #                                    ttl_ms=3_600_000)
        #
        #   # Market sell 0.5 tokens at no worse than mark x (1 - max slippage);
        #   # with no mark yet (None) the SDK reads the reference itself. The
        #   # unfilled rest RESTS at that price — cancel it if you don't want it
        #   # on the book:
        #   res = self.client.orders.market_sell(self.ticker, quantity=0.5,
        #                                        reference_price=mark)
        #   if res.status in ("OPEN", "PARTIALLY_FILLED"):
        #       self.client.orders.cancel(res.orderId)
        #
        #   # Arm a stop-loss 5% below the mark (STOP_MARKET SELL; rests
        #   # server-side, and books at up to the max slippage below the trigger).
        #   # It FAILS if it fires after its life (ttl_ms, default 7 days), yet
        #   # keeps its open-order slot until it fires or you cancel it, so
        #   # cancel and re-arm it before then. Move a trailing stop on meaningful
        #   # moves, never on every tick: each stop holds one of your account's
        #   # order numbers until it fires.
        #   if mark:
        #       trigger = round(mark * 0.95, 2)
        #       self.client.orders.stop_loss(self.ticker, quantity=1, trigger_price=trigger)
        #
        #   # Cancel every open order, stop and take (it sells nothing):
        #   self.client.orders.cancel_all()
        #
        # Wrap calls in try/except. Every loaf.LoafAPIError from a placement
        # means it was not placed: loaf.VenueError (the current market mode
        # does not take it), loaf.VenueSwitchingError (the mode is switching:
        # try again next tick), loaf.TradingHaltedError (halted), and the rest.
        # On loaf.LoafConfigError from a placement, stop and alert: e.g. your
        # trading key is no longer in force (set the new one, README §2,
        # restart, and re-arm your stops).
        # On loaf.OrderOutcomeUnknownError the order may be live or already
        # filled. If you still want it, resubmit(e.signed_body) (the same order
        # if it landed, placed now if not); otherwise look for it in
        # history.orders() by its expiryMs, where a miss proves nothing until
        # its ttl_ms has passed: never place it afresh before then.
        # ----------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #


def main() -> None:
    client = build_client()
    print(f"Connecting to {client.base_url} ...")
    preflight(client)

    target = resolve_target(client)
    if target is None:
        client.close()
        return
    print(f"\nFollowing property {target.ticker} ({target.tokenName}, id {target.propertyId}).\n")
    strategy = Strategy(client, target.ticker)

    # Wire up the real-time feed.
    ws = client.websocket()
    ws.on_orderbook(strategy.on_orderbook)
    ws.on_mark_price(strategy.on_mark_price)
    ws.on_connect(strategy.on_feed_reset)     # live (again): re-seed on the next tick
    ws.on_disconnect(strategy.on_disconnect)  # every ended attempt: drop book and mark
    ws.on_property_halt(strategy.on_halt)     # public: halt/resume for this property
    ws.on_round_update(strategy.on_round)     # public: competition round transitions
    ws.on_trades(strategy.on_trade_tick)
    ws.on_trade(strategy.on_my_fill)          # private: your fills
    ws.on_order_update(strategy.on_my_order_accepted)  # private: your new orders
    ws.on_order_status(strategy.on_my_order)  # private: your order transitions
    ws.on_balances(strategy.on_balances)      # private: your balance changes
    ws.on_error(lambda m: print(f"  WS error: {m.get('message')}"))

    ws.subscribe_orderbook(target.ticker)
    ws.subscribe_mark_price(target.ticker)
    ws.subscribe_trades(target.ticker)
    ws.subscribe_property_status(target.ticker)
    ws.subscribe_competition()
    ws.subscribe_portfolio()  # your private fills/balances stream (keyed by your API key)

    ws.start()  # background thread
    if ws.wait_until_connected(timeout=10):
        print("Live feed connected. Press Ctrl-C to stop.\n")
    else:
        print("Live feed not connected yet; still trying. Press Ctrl-C to stop.\n")

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    try:
        while not stop.is_set():
            strategy.on_tick()
            stop.wait(5.0)  # tick every 5 seconds
    finally:
        print("\nShutting down ...")
        ws.stop()
        client.close()


if __name__ == "__main__":
    main()

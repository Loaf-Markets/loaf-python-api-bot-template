#!/usr/bin/env python3
"""Loaf trading bot — template.

A runnable starting point that:
  1. loads your trading key from the environment (or a .env file),
  2. verifies the connection and trading prerequisites,
  3. prints your balances and holdings,
  4. opens the real-time feed (order book, halt status, and your private
     portfolio stream),
  5. runs a simple strategy loop you can replace with your own logic.

Run it:
    cp .env.example .env
    chmod 600 .env                # only you can read it; then set LOAF_TRADING_KEY (README §2)
    pip install -e ".[dotenv]"    # the dotenv extra loads .env; or export the variables yourself
    python bot.py

Stop with Ctrl-C (or SIGTERM: docker stop, systemd, kill): the bot stops after the
current tick. A second Ctrl-C stops it at once.
"""

from __future__ import annotations

import os
import signal
import sys
import threading
import time
from decimal import ROUND_FLOOR, Decimal
from typing import Any, Mapping

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

# The market this template watches/trades: a market's ticker from
# `loaf.market.list()`, exactly as listed (uppercase), e.g. "OPRA" or "OPRA-PERP".
# One that is not listed stops the bot. Leave it empty to follow the first
# tradeable spot market (resolve_target says which while none trades).
TARGET_TICKER = ""

# The tag this bot's orders carry once your strategy places some (order_tag=, a whole number
# from 1 to 4,294,967,295): its cancels and its dead man's switch then touch its own orders
# only, never the ones you place by hand in the web app.
BOT_ORDER_TAG = 7

# How often the strategy runs, in seconds.
TICK_SECONDS = 5.0

# The feed sends each subscribed order book at least every 5 s, changed or not. A book
# older than this many seconds (the subscription was refused, or the feed stalled) is
# treated as unknown: the tick reads its bid and ask as None.
BOOK_MAX_AGE = 15.0


def build_client() -> LoafClient:
    """Create a client from the environment, with friendly errors."""
    if not os.environ.get("LOAF_TRADING_KEY", "").strip():
        sys.exit(
            "No LOAF_TRADING_KEY set.\n"
            "  1. In the Loaf web app, open the API page and select Show trading key\n"
            "     (deposit and enable trading on a market's Trade page first).\n"
            "  2. cp .env.example .env && chmod 600 .env  and put it in LOAF_TRADING_KEY:\n"
            "     the one loaf_tk_... string, whole (README §2).\n"
            "  .env is read only when python-dotenv is installed\n"
            "  (pip install -e \".[dotenv]\"); otherwise export the variable.\n"
        )
    # The trading key is read from $LOAF_TRADING_KEY, and base_url / ws_url
    # from $LOAF_API_BASE_URL / $LOAF_WS_URL, if set.
    try:
        return LoafClient()
    except loaf.LoafConfigError as exc:
        sys.exit(str(exc))  # a malformed trading key or LOAF_MAX_SLIPPAGE_BPS


def cash_of(balances: Any) -> Decimal:
    """Your cash: the ``available`` amount of the quote-asset (``isQuote``) balances rows."""
    return sum(
        (loaf.from_base_units(b.available, b.decimals) for b in balances if b.isQuote),
        Decimal(0),
    )


def trading_started(starts_at: int | None) -> bool:
    """Whether a market's trading has started, from its ``tradingStartsAt`` (unix seconds;
    ``None``: no wait). The exchange refuses its orders before then."""
    return starts_at is None or starts_at <= time.time()


def best_prices(book: Any) -> tuple[Any, Any]:
    """A book's best bid and best ask prices, from its ``bids`` and ``asks`` levels (best
    first): None for an empty side."""
    bids, asks = book.get("bids"), book.get("asks")
    return (bids[0]["price"] if bids else None), (asks[0]["price"] if asks else None)


# --------------------------------------------------------------------------- #
# Preflight checks
# --------------------------------------------------------------------------- #


def preflight(client: LoafClient) -> None:
    """Confirm credentials work and print balances/holdings."""
    try:
        comp = client.portfolio.summary()
    except loaf.LoafAuthError as exc:
        sys.exit(f"Your trading key was refused: {exc.message}")

    balances = comp.get("balances") or []
    print(f"  Cash: {cash_of(balances):,.2f} available")
    holdings = comp.get("holdings") or []
    if holdings:
        rows = {b.assetId: b for b in balances}
        print("  Holdings:")
        for h in holdings:
            if h.get("isIpoAllocation"):
                held = f"{h.quantity:,.1f} (IPO allocation)"
            elif h.get("assetId") in rows:  # an L2 holding: its amounts are its balances row
                row = rows[h.assetId]
                total = loaf.from_base_units(row.total, row.decimals)
                available = loaf.from_base_units(row.available, row.decimals)
                held = f"{total:,.1f} ({available:,.1f} available)"
            else:  # no balances row: nothing held
                held = "0.0"
            avg = h.get("averageEntryPrice")
            print(f"    {h.ticker}: {held} @ avg {'n/a' if avg is None else avg} "
                  f"(mkt {h.marketPrice})")
    bps = client.max_slippage_bps  # $LOAF_MAX_SLIPPAGE_BPS, default 200
    print(f"  Orders are signed by trading key {client.public_key}")
    print(f"  Max slippage: {bps} bps ({bps / 100:g}%) for MARKET orders, stops and TP/SL legs")


def resolve_target(client: LoafClient) -> Any:
    """Pick the market to follow.

    With TARGET_TICKER set: that market, or stop, listing the tickers there are,
    when it is not listed (a typo, or a delisted market): never another market in
    its place. With it empty: the first tradeable spot market, else the first
    tradeable market of any type, else the first LIVE one with an L2 market, whose
    halt or start the loop waits out (while the platform-wide halt is on, every row
    reads halted), else the first listed (or None when none is)."""
    listing = client.market.list().get("markets") or []
    if not listing:
        print("No markets available on this environment.")
        return None
    if TARGET_TICKER:
        for market in listing:
            if market.get("ticker") == TARGET_TICKER:
                return market
        listed = ", ".join(sorted(str(m.get("ticker")) for m in listing))
        sys.exit(
            f"TARGET_TICKER {TARGET_TICKER!r} is not a listed market.\n"
            f"  Listed: {listed}\n"
            "  Set TARGET_TICKER in bot.py to one of them, exactly as listed, or to \"\" to\n"
            "  follow the first tradeable spot market."
        )
    # Tradeable: LIVE, not halted, with an L2 market, its trading started.
    live = [m for m in listing if m.get("status") == "LIVE" and m.get("hasL2Market")]
    tradeable = [
        m for m in live if not m.get("isHalted") and trading_started(m.get("tradingStartsAt"))
    ]
    spot = [m for m in tradeable if m.get("type") == "SPOT"]
    return (spot or tradeable or live or listing)[0]


# --------------------------------------------------------------------------- #
# Strategy
# --------------------------------------------------------------------------- #


class Strategy:
    """Holds live state fed by the WebSocket, and decides what to do.

    This template only OBSERVES the market (it prints a snapshot each tick and
    never sends an order). Put your own logic in `trade`, which `on_tick` runs
    only while the market trades and your private stream is not stale — the
    order helpers you need are commented inline there.
    """

    def __init__(self, client: LoafClient, ticker: str) -> None:
        self.client = client
        self.ticker = ticker
        self._lock = threading.Lock()
        self.best_bid: float | None = None
        self.best_ask: float | None = None
        self.mark_price: Decimal | None = None  # a mark_price frame's: exact dollars
        self.last_trade: dict | None = None
        self.is_halted = False
        # When the book above was last set (a frame, or the read below), by time.monotonic(),
        # which a change to the computer's clock leaves alone: None while no book is held.
        # One older than BOOK_MAX_AGE is not used.
        self.book_at: float | None = None
        # What no frame sends: the market's status, whether it has an L2 market (its
        # `l2`), and its tradingStartsAt; and what never changes: its type ("SPOT", or
        # "PERP": a perpetual future, traded from a margin pool), its priceDecimals and
        # quantityDecimals (its prices have at most that many decimal places, its tick,
        # and its quantities at most that many, its lot), and its minOrderValue, the
        # least price x quantity, in dollars, of an order on it.
        self.status: str | None = None
        self.has_l2_market = False
        self.trading_starts_at: int | None = None
        self.market_type: str | None = None
        self.price_decimals: int | None = None
        self.quantity_decimals: int | None = None
        self.min_order_value: Any = None
        # The book, the halt flag and all of those are read from REST on the next tick: at
        # startup and each time the feed goes live again (nothing is replayed). Until the
        # market trades, a halt aside (a frame ends it), every tick reads them again.
        self.needs_seed = True
        # Which of them a frame has set since the feed went live: that read leaves those
        # alone, since the read can be older than the frame.
        self.from_feed: set[str] = set()
        # The feed is live from on_connect until that connection ends (not yet at startup),
        # and feed_changes counts each on_connect and ended attempt. A read takes the book
        # only if the feed was live from its request until its answer is applied: one made
        # otherwise can miss a change to the book that no frame brings.
        self.feed_live = False
        self.feed_changes = 0
        # Whether your private stream is live, by the exchange's account beat: None until
        # the first snapshot (always, on a deployment without perps), then on_stale and
        # on_fresh turn it. A strategy that quotes pulls its quotes while it is False.
        self.private_fresh: bool | None = None

    # -- WebSocket handlers (called on the WS thread) --------------------- #

    def on_orderbook(self, msg) -> None:
        with self._lock:
            self._set_book(msg)
            self.from_feed.add("book")

    def _set_book(self, book) -> None:  # under the lock
        self.best_bid, self.best_ask = best_prices(book)
        self.book_at = time.monotonic()

    def on_mark_price(self, msg) -> None:
        with self._lock:
            self.mark_price = msg.price

    def on_feed_reset(self, _msg) -> None:
        # The feed is live: the server has answered the subscriptions, and no frame
        # from here on is missed. Drop what may be stale and re-seed on the next tick,
        # from the main thread (a slow REST read in a handler stalls the socket).
        with self._lock:
            self.best_bid = self.best_ask = self.book_at = None
            self.mark_price = None
            self.from_feed.clear()
            self.feed_live = True
            self.feed_changes += 1
            self.needs_seed = True

    def on_disconnect(self, msg) -> None:
        # Every ended connection attempt; the client reconnects with backoff (its
        # on_connect, on_feed_reset here, says when it is live again). Forget
        # the book and the mark so no order is priced off a dead feed: no read made
        # before the feed is live again seeds the book, and with no mark the SDK reads
        # the reference itself.
        with self._lock:
            self.best_bid = self.best_ask = self.book_at = None
            self.mark_price = None
            self.feed_live = False
            self.feed_changes += 1
        if msg.code is None:
            # No close from the server: an attempt that failed, or the feed's own run ending
            # (a handler's SystemExit or KeyboardInterrupt, named here), which no retry follows.
            print(f"  feed: no connection ({msg.reason})")
        else:
            print(f"  feed closed: {msg.code} {msg.reason}; reconnecting")

    def on_stale(self, msg) -> None:
        # No news from the exchange's engine for stale_after seconds, the connection
        # dropped (before on_disconnect), or portfolio was unsubscribed: your fills and
        # orders may be out of date. The next tick acts on it, from the main thread.
        with self._lock:
            self.private_fresh = False
        print(f"  *** private stream STALE ({msg.reason}; last news {msg.silentSeconds} s ago)")

    def on_fresh(self, _msg) -> None:
        # A connection's first account snapshot, or news again after on_stale.
        with self._lock:
            was, self.private_fresh = self.private_fresh, True
        if was is False:
            print("  *** private stream live again")

    def on_halt(self, msg) -> None:
        # `isHalted` is the effective state (this market's own flag OR a
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
        # A fill of YOURS: a trade, or a perp's liquidation, deleverage, exit, ... (source).
        t = msg.trade
        print(f"  *** FILLED ({t.source}): {t.side} {t.quantity} {t.ticker} @ {t.price} "
              f"(fee {t.fee}, order #{t.orderId}) #{t.tradeId}")

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
        """Read the market's status, L2 market and trading start, which no frame
        sends, and its halt flag and book, which the feed does not send on
        subscribe. The read leaves alone what a frame has set since the feed went
        live, and the book unless the feed was live from the request until the
        answer is applied. False if the read failed, or its answer is not a market's:
        the next tick tries again."""
        with self._lock:
            self.needs_seed = False  # first: a reconnect during the read asks again
            live_at_request, changes = self.feed_live, self.feed_changes
        try:
            detail = self.client.market.get(self.ticker)
            market = detail.get("market") if isinstance(detail, Mapping) else None
            if not isinstance(market, Mapping):
                raise ValueError(f"no market in the answer ({type(detail).__name__})")
            book = detail.get("orderBook")  # None: not known now
            top = best_prices(book) if book else None
        except (loaf.LoafError, ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
            with self._lock:
                self.needs_seed = True
            print(f"  could not read {self.ticker} ({exc}); trying again next tick")
            return False
        with self._lock:
            self.status = market.get("status")
            self.has_l2_market = market.get("l2") is not None
            self.trading_starts_at = market.get("tradingStartsAt")
            self.market_type = market.get("type")
            self.price_decimals = market.get("priceDecimals")
            self.quantity_decimals = market.get("quantityDecimals")
            self.min_order_value = market.get("minOrderValue")
            if "halt" not in self.from_feed:
                self.is_halted = bool(market.get("isHalted"))
            live_throughout = live_at_request and changes == self.feed_changes
            if live_throughout and "book" not in self.from_feed and top is not None:
                self.best_bid, self.best_ask = top
                self.book_at = time.monotonic()
        return True

    def quantity_worth(
        self, dollars: float | Decimal, price: float | Decimal
    ) -> Decimal | None:
        """At most ``dollars`` worth of this market at ``price``: that over the price, rounded
        down to the market's lot (10^-quantityDecimals tokens), in Decimal, so the rounding
        is exact. None when even one lot is worth more than ``dollars`` (a coarse lot at a
        high price): place no order then, rather than one worth more than you asked for.
        Rounded down, it can be worth up to one lot less than ``dollars``: the exchange
        refuses an order worth less than the market's ``min_order_value`` at the price it
        signs, so ask for more than that."""
        if self.quantity_decimals is None:
            raise RuntimeError(f"{self.ticker} has not been read yet, so its lot is not known")
        value, at = Decimal(str(dollars)), Decimal(str(price))
        if not (value.is_finite() and at.is_finite() and value > 0 and at > 0):
            raise ValueError(f"dollars and price must be positive (got {dollars!r}, {price!r})")
        lot = Decimal(1).scaleb(-self.quantity_decimals)
        quantity = (value / at).quantize(lot, rounding=ROUND_FLOOR)
        return quantity if quantity >= lot else None

    def _waiting(self) -> list[str]:  # under the lock
        """Why the market does not trade, a halt aside: no frame announces a
        listing, an L2 market or a trading start, so only a read shows these end."""
        reasons: list[str] = []
        if self.status != "LIVE":
            reasons.append(str(self.status))  # PENDING: not listed yet; or DELISTED
        if not self.has_l2_market:
            reasons.append("NO L2 MARKET")  # market.l2 is None: the SDK refuses its orders
        if not trading_started(self.trading_starts_at):
            reasons.append("NOT STARTED")  # orders are refused (403) until its start
        return reasons

    def on_tick(self) -> None:
        if self.needs_seed and not self.seed():
            return
        with self._lock:
            bid, ask, mark = self.best_bid, self.best_ask, self.mark_price
            # A book the feed has not sent again for BOOK_MAX_AGE is not known now.
            age = None if self.book_at is None else time.monotonic() - self.book_at
            old_book = age is not None and age > BOOK_MAX_AGE
            if old_book:
                bid = ask = None
            halted, waiting = self.is_halted, self._waiting()
            stale = self.private_fresh is False
            if waiting:
                self.needs_seed = True  # the next tick reads the market again
        spread = (ask - bid) if (bid is not None and ask is not None) else None
        flags = "".join(
            f" [{reason}]"
            for reason in (["HALTED"] if halted else [])
            + waiting
            + (["OLD BOOK"] if old_book else [])
            + (["STALE"] if stale else [])
        )
        print(f"[{self.ticker}] bid={bid} ask={ask} spread={spread} mark={mark}{flags}")

        if stale:
            # Your orders and fills may be out of date: act on nothing until the
            # private stream is live again (on_fresh). A strategy that quotes pulls
            # its quotes here, scoped to its tag:
            #   self.client.orders.cancel_all(self.ticker, order_tag=BOT_ORDER_TAG)
            return
        if halted:
            return  # orders would be rejected with loaf.TradingHaltedError
        if waiting:
            return  # orders would be refused until the market trades
        self.trade(bid, ask, mark)

    def trade(self, bid: Any, ask: Any, mark: Decimal | None) -> None:
        """Your strategy: run each tick while the market trades and your private
        stream is not stale, with this tick's best bid and ask (None while not known)
        and mark price (None until the feed sends one)."""
        # ----------------------------------------------------------------- #
        # YOUR STRATEGY GOES HERE. Examples (uncomment & adapt). This runs
        # every tick (5 s), and each example as written acts on every one (a new
        # order each time): gate it, e.g. keep the orderId a placement returns
        # and place again only once that order is gone. bid and ask are None
        # while the book is not known (none yet, the feed down, or OLD BOOK).
        #
        # Size orders by value, never by a fixed number of tokens: one market's
        # token can cost $3 and another's $75,000. self.quantity_worth(25, price)
        # is at most $25 worth at that price on the market's lot, rounded down,
        # or None when even one lot is worth more than $25 (each example below
        # then places nothing); the exchange refuses an order worth less than
        # self.min_order_value ($10) at the price it signs. On a perp
        # (self.market_type == "PERP") a BUY opens or grows a long and a SELL a
        # short, with leverage, from margin pool 0 unless pool= names another:
        # pass reduce_only=True to an order that must only close.
        #
        #   # Quote a limit buy of $25, 1% below the best bid, on the market's
        #   # tick, resting for an hour at most (ttl_ms; the default is 7 days):
        #   # a bot's quotes should die with it.
        #   if bid:
        #       price = round(bid * 0.99, self.price_decimals)
        #       quantity = self.quantity_worth(25, price)
        #       if quantity is not None:  # None: one lot is worth more than $25
        #           self.client.orders.limit_buy(self.ticker, quantity=float(quantity),
        #                                        price=price, ttl_ms=3_600_000)
        #
        #   # Market sell $25 worth (on a spot market, of tokens you hold) at no
        #   # worse than mark x (1 - max slippage); with no mark yet (None) the
        #   # SDK reads the reference itself. A MARKET order never rests: what
        #   # cannot fill at once is cancelled, and one that fills nothing is
        #   # refused (loaf.LoafValidationError).
        #   if bid:
        #       quantity = self.quantity_worth(25, bid)
        #       if quantity is not None:  # None: one lot is worth more than $25
        #           self.client.orders.market_sell(self.ticker, quantity=float(quantity),
        #                                          reference_price=mark)
        #
        #   # Arm a stop-loss 5% below the mark (STOP_MARKET SELL; the exchange
        #   # holds it, and it books at up to the max slippage below the trigger).
        #   # Its quantity is the holding (spot) or the long (perp) it protects,
        #   # here $25 worth. On a perp it must be reduce-only: a stop that fires
        #   # after the long is gone would open a short (the SDK refuses
        #   # reduce_only on a spot market).
        #   # The exchange FAILS it if it is still ARMED once its life (ttl_ms,
        #   # default 7 days) ends, fired or not (a fired one it has not booked
        #   # yet stays ARMED); once it has booked its order (PLACED), that order
        #   # is cancelled at the same life's end instead. So cancel and re-arm it
        #   # before then if you want it kept. Move a trailing stop on meaningful
        #   # moves, never on every tick: each stop holds one of your account's
        #   # order numbers until it fires.
        #   if mark:
        #       trigger = round(mark * Decimal("0.95"), self.price_decimals)
        #       quantity = self.quantity_worth(25, trigger)
        #       if quantity is not None:  # None: one lot is worth more than $25
        #           self.client.orders.stop_loss(
        #               self.ticker, quantity=float(quantity),
        #               trigger_price=trigger, reduce_only=self.market_type == "PERP")
        #
        #   # Cancel every open order, stop and take, the ones you placed by
        #   # hand included (it sells nothing):
        #   self.client.orders.cancel_all()
        #
        #   # Quoting: tag the bot's quotes, replace in one step, never let a
        #   # quote land late, and keep the exchange's dead man's switch armed
        #   # from this loop, scoped to the tag: if this loop stops, the
        #   # exchange cancels the bot's quotes within 60 s. (While the private
        #   # stream is stale on_tick does not call this, and pulls them instead.)
        #   self.client.orders.cancel_after(60_000, order_tag=BOT_ORDER_TAG)
        #   if bid:
        #       price = round(bid * 0.99, self.price_decimals)
        #       quantity = self.quantity_worth(25, price)
        #       if quantity is not None:  # None: one lot is worth more than $25
        #           res = self.client.orders.limit_buy(
        #               self.ticker, quantity=float(quantity), price=price,
        #               ttl_ms=3_600_000,
        #               order_tag=BOT_ORDER_TAG,
        #               send_by_ms=time.time_ns() // 1_000_000 + 2_000,
        #               replaces_order_id=self.quote_id)  # set self.quote_id = None in __init__
        #           self.quote_id = res.orderId
        #   # On loaf.ReplaceTargetGoneError the old quote no longer rests (it
        #   # filled or was cancelled): set self.quote_id = None and quote again.
        #
        # Wrap calls in try/except. Every loaf.LoafAPIError from a placement
        # means it was not placed: loaf.TradingHaltedError (halted), and the rest.
        # A loaf.LoafForbiddenError about the market itself (delisted, not yet
        # available, not started) means it stopped trading, which no frame
        # announces: set self.needs_seed = True, and the next tick reads it again.
        # On loaf.LoafConfigError or loaf.LoafAuthError from a placement, stop and
        # alert: e.g. your trading key is no longer in force (the message says;
        # set the new one, README §2, restart, and re-arm your stops).
        # On loaf.OrderOutcomeUnknownError the order may be live or already
        # filled. If you still want it, resubmit(e.signed_body) (the same order
        # if it landed, placed now if not); otherwise settle it with
        # cancel_by_index(e.signed_body["l2OrderIndex"]): CANCELLED, ENDED or
        # VOIDED (never placed, and never will be). Never place it afresh.
        # On loaf.PerpsOutcomeUnknownError (a margin move or an exit) or
        # loaf.WithdrawalOutcomeUnknownError it may have been applied: never
        # sign it afresh (a fresh one takes the next nonce, so both could
        # apply), but re-send the same body with
        # self.client.perps.resubmit(e.signed_body) or
        # self.client.funds.resubmit(e.signed_body), whose answer settles it
        # (each error's help() says how).
        # A loaf.LoafError that this lets out is printed and the next tick
        # runs, but any of those three that it lets out stops the bot (run,
        # below): only your strategy holds the signed body to re-send.
        # ----------------------------------------------------------------- #

    def on_shutdown(self) -> None:
        """Called once as the bot stops (Ctrl-C, SIGTERM, or an error), before the
        feed stops and the client closes: settle what the strategy leaves on the book.

        As shipped the template places no order, so this sends nothing. Once your
        strategy places orders tagged BOT_ORDER_TAG, the usual choice is to cancel
        them here, leaving the ones you placed by hand alone (a tagged cancel leaves
        stops and takes, which carry no tag: cancel_conditional them by id). The dead
        man's switch, cancel_after (in the quoting example above), is the exchange's
        side of the same guard: it cancels them even when this never runs (the
        process killed, the machine or its network down).
        """
        # self.client.orders.cancel_all(order_tag=BOT_ORDER_TAG)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #


def run(strategy: Strategy, stop: threading.Event) -> None:
    """Run the strategy every TICK_SECONDS until ``stop`` is set.

    A loaf.LoafError that on_tick lets out is printed, and the next tick runs. A
    loaf.OrderOutcomeUnknownError, loaf.PerpsOutcomeUnknownError or
    loaf.WithdrawalOutcomeUnknownError it lets out stops the bot: that order, margin
    move, exit or withdrawal may have gone through, so nothing more is traded until
    you have settled it. Any other exception ends the loop as it is. Either way main's
    shutdown runs. A signed body is never printed: anyone who has it can send it.
    """
    while not stop.is_set():
        try:
            strategy.on_tick()
        except loaf.PerpsOutcomeUnknownError as exc:
            kind = "margin move" if "fromSide" in exc.signed_body else "exit"
            sys.exit(
                f"Stopping: the {kind} signed at your account's nonce "
                f"{exc.signed_body.get('l2Nonce')} may or may not\n"
                "  have been applied. Never sign it afresh: a fresh one takes the next nonce,\n"
                "  so both could apply. A re-send of the same signed body settles it:\n"
                "  client.perps.resubmit(error.signed_body) answers 202 (applied, before or\n"
                "  now), and once its expiry has passed a refusal (422) means it was never\n"
                "  applied. The bot cannot re-send it once stopped: catch\n"
                "  loaf.PerpsOutcomeUnknownError in your strategy and resubmit\n"
                "  error.signed_body there (help(loaf.PerpsOutcomeUnknownError) says more)."
            )
        except loaf.WithdrawalOutcomeUnknownError as exc:
            sys.exit(
                "Stopping: the withdrawal signed at your account's nonce "
                f"{exc.signed_body.get('l2Nonce')} may or may not\n"
                "  have been journaled (to be paid out). Never withdraw it afresh: a fresh one\n"
                "  takes the next nonce, so both could be paid. A re-send of the same signed\n"
                "  body settles it: client.funds.resubmit(error.signed_body) answers 202\n"
                "  (journaled, before or now), or is refused ORDER_INDEX_USED or\n"
                "  WITHDRAW_FEE_ABOVE_MAX (not journaled: withdraw again). The bot cannot\n"
                "  re-send it once stopped: catch\n"
                "  loaf.WithdrawalOutcomeUnknownError in your strategy and resubmit\n"
                "  error.signed_body there (help(loaf.WithdrawalOutcomeUnknownError) also says\n"
                "  what settles it once the exchange no longer takes the body)."
            )
        except loaf.OrderOutcomeUnknownError as exc:
            index = exc.signed_body.get("l2OrderIndex")
            sys.exit(
                f"Stopping: the outcome of order number {index} is unknown. It may be live,\n"
                "  or already filled. Settle it before you start the bot again:\n"
                f"  client.orders.cancel_by_index({index}) cancels it if it rests, and voids it\n"
                "  if it never arrived (a stop or take that was placed is refused there, the\n"
                "  message naming its id: cancel_conditional that). Never place it afresh.\n"
                "  To keep the bot running instead, catch the error in on_tick, and resubmit\n"
                "  its signed body while you still want the order."
            )
        except loaf.LoafError as exc:
            print(f"  tick failed: {exc}; trying again next tick")
        stop.wait(TICK_SECONDS)


def ask_to_stop(stop: threading.Event) -> None:
    """The first Ctrl-C or SIGTERM: stop after the current tick, and let a second one act as
    it does in any Python program."""
    stop.set()
    signal.signal(signal.SIGINT, signal.default_int_handler)  # raises KeyboardInterrupt
    signal.signal(signal.SIGTERM, signal.SIG_DFL)  # ends the process
    print("\nStopping after the current tick (press Ctrl-C again to stop now) ...")


def main() -> None:
    client = build_client()
    print(f"Connecting to {client.base_url} ...")
    preflight(client)

    target = resolve_target(client)
    if target is None:
        client.close()
        return
    kind = target.get("type") or "unknown"
    print(f"\nFollowing market {target.ticker} ({target.get('name')}, {kind}, "
          f"index {target.get('marketIndex')}).")
    if kind == "PERP":
        print("  A perpetual future: its orders open and close leveraged positions, backed by")
        print("  a margin pool (pool 0, the cross pool, unless an order names another).")
    print()
    strategy = Strategy(client, target.ticker)

    # Wire up the real-time feed.
    ws = client.websocket()
    ws.on_orderbook(strategy.on_orderbook)
    ws.on_mark_price(strategy.on_mark_price)
    ws.on_connect(strategy.on_feed_reset)     # live (again): re-seed on the next tick
    ws.on_disconnect(strategy.on_disconnect)  # every ended attempt: drop book and mark
    ws.on_market_halt(strategy.on_halt)       # public: halt/resume for this market
    ws.on_trades(strategy.on_trade_tick)
    ws.on_trade(strategy.on_my_fill)          # private: your fills
    ws.on_order_update(strategy.on_my_order_accepted)  # private: your new orders
    ws.on_order_status(strategy.on_my_order)  # private: your order transitions
    ws.on_balances(strategy.on_balances)      # private: your balance changes
    ws.on_stale(strategy.on_stale)            # private stream: no news for stale_after s
    ws.on_fresh(strategy.on_fresh)            # private stream: live (again)
    ws.on_error(lambda m: print(f"  WS error: {m.get('message')}"))

    ws.subscribe_orderbook(target.ticker)
    ws.subscribe_mark_price(target.ticker)
    ws.subscribe_trades(target.ticker)
    ws.subscribe_market_status(target.ticker)
    ws.subscribe_portfolio()  # your private fills/balances stream (your trading key's account)

    # Ctrl-C (SIGINT) and SIGTERM (docker stop, systemd, kill) both end the loop after the
    # current tick, so the shutdown below runs. A tick can take minutes (a placement whose
    # answer is lost is re-sent), so a second one stops the bot at once: Ctrl-C raises
    # KeyboardInterrupt, whose shutdown below still runs, and SIGTERM ends the process.
    stop = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: ask_to_stop(stop))

    try:
        ws.start()  # background thread
        if ws.wait_until_connected(timeout=10):
            print("Live feed connected. Press Ctrl-C to stop.\n")
        else:
            print("Live feed not connected yet; still trying. Press Ctrl-C to stop.\n")
        run(strategy, stop)
    finally:
        print("\nShutting down ...")
        try:
            strategy.on_shutdown()
        except loaf.LoafError as exc:
            print(f"  on_shutdown failed: {exc}")
        finally:
            ws.stop()
            client.close()


if __name__ == "__main__":
    main()

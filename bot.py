#!/usr/bin/env python3
"""Loaf trading bot — template.

A runnable starting point that:
  1. loads your credentials from the environment (or a .env file),
  2. verifies the connection and trading prerequisites,
  3. prints your balances and positions,
  4. opens the real-time feed (order book, halt status, and your private
     portfolio stream),
  5. runs a simple strategy loop you can replace with your own logic.

Run it:
    cp .env.example .env          # then set LOAF_API_KEY, and LOAF_AGENT_PRIVATE_KEY
                                  # to place orders
    pip install -e ".[dotenv]"    # the dotenv extra loads .env; or export the variables yourself
    python bot.py

Stop with Ctrl-C.
"""

from __future__ import annotations

import os
import signal
import sys
import threading
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

# The property this template watches/trades: a tokenName from
# `loaf.market.properties()` (lowercase letters), e.g. "opera". Leave it empty to
# follow the first listed property.
TARGET_TOKEN_NAME = ""


def build_client() -> LoafClient:
    """Create a client from the environment, with friendly errors."""
    api_key = os.environ.get("LOAF_API_KEY")
    if not api_key:
        sys.exit(
            "No LOAF_API_KEY set.\n"
            "  1. Create an API key in the Loaf web app (Settings -> API keys).\n"
            "  2. cp .env.example .env  and put its two values in LOAF_API_KEY\n"
            "     and LOAF_AGENT_PRIVATE_KEY.\n"
            "  .env is read only when python-dotenv is installed\n"
            "  (pip install -e \".[dotenv]\"); otherwise export both variables.\n"
        )
    # The agent key is read from $LOAF_AGENT_PRIVATE_KEY, and base_url / ws_url
    # from $LOAF_API_BASE_URL / $LOAF_WS_URL, if set.
    try:
        return LoafClient(api_key=api_key)
    except loaf.LoafConfigError as exc:  # a bad key or LOAF_MAX_SLIPPAGE_BPS, or no eth-account
        sys.exit(str(exc))


# --------------------------------------------------------------------------- #
# Preflight checks
# --------------------------------------------------------------------------- #


def preflight(client: LoafClient) -> None:
    """Confirm credentials work and print balances/positions."""
    try:
        comp = client.portfolio.component()
    except loaf.LoafAuthError as exc:
        sys.exit(f"Authentication failed — check your API key. ({exc.message})")

    print(f"  Cash: {comp.cash:,.2f} USDC  (frozen {comp.frozen:,.2f})  "
          f"Portfolio value: {comp.portfolioValue:,.2f}  PnL: {comp.portfolioPnl:,.2f}")
    positions = comp.get("positions") or []
    if positions:
        print("  Positions:")
        for p in positions:
            print(f"    {p.tokenName}: {p.quantity} @ avg {p.averageEntryPrice} "
                  f"(mkt {p.marketPrice}, PnL {p.propertyPnl})")
    if client.agent_address:
        bps = client.max_slippage_bps  # $LOAF_MAX_SLIPPAGE_BPS, default 200
        print(f"  Orders are signed by agent {client.agent_address}")
        print(f"  Max slippage: {bps} bps ({bps / 100:g}%) for MARKET orders, stops and TP/SL legs")
    else:
        print("  No LOAF_AGENT_PRIVATE_KEY: watching only (placing orders needs it).")
    print(
        "  Note: while a competition round is ACTIVE, placing orders requires\n"
        "        admission to the round (loaf.competition.queue_position() shows\n"
        "        your standing). You'll get a clear error if not eligible."
    )


def resolve_target(client: LoafClient) -> Any:
    """Pick the property to follow: the configured one, or the first listed (or None)."""
    listing = client.market.properties().get("properties") or []
    if not listing:
        print("No properties available on this environment.")
        return None
    if TARGET_TOKEN_NAME:
        for prop in listing:
            if prop.tokenName == TARGET_TOKEN_NAME:
                return prop
        print(f"Token {TARGET_TOKEN_NAME!r} not found; falling back to first listed.")
    return listing[0]


# --------------------------------------------------------------------------- #
# Strategy
# --------------------------------------------------------------------------- #


class Strategy:
    """Holds live state fed by the WebSocket, and decides what to do.

    This template only OBSERVES the market (it prints a snapshot each tick and
    never sends an order). Replace `on_tick` with your own logic — the order
    helpers you need are commented inline.
    """

    def __init__(self, client: LoafClient, token_name: str, is_halted: bool = False) -> None:
        self.client = client
        self.token_name = token_name
        self._lock = threading.Lock()
        self.best_bid: float | None = None
        self.best_ask: float | None = None
        self.mark_price: float | None = None
        self.last_trade: dict | None = None
        self.is_halted = is_halted

    # -- WebSocket handlers (called on the WS thread) --------------------- #

    def on_orderbook(self, msg) -> None:
        with self._lock:
            self.best_bid = msg.bids[0].price if msg.get("bids") else None
            self.best_ask = msg.asks[0].price if msg.get("asks") else None

    def on_mark_price(self, msg) -> None:
        with self._lock:
            self.mark_price = msg.price

    def on_feed_reset(self, _msg) -> None:
        # The socket dropped or reconnected: forget the mark until a fresh frame
        # arrives (the server sends one on every (re)subscribe), so no order is
        # priced off a dead feed. Meanwhile the SDK fetches the reference itself.
        with self._lock:
            self.mark_price = None

    def on_halt(self, msg) -> None:
        # `isHalted` is the effective state (this property's own flag OR a
        # platform-wide halt), so it replaces the value seeded from REST.
        with self._lock:
            self.is_halted = msg.isHalted
        print(f"  *** {msg.tokenName} {'HALTED' if msg.isHalted else 'RESUMED'}")

    def on_trade_tick(self, msg) -> None:
        trades = msg.get("trades") or []
        if trades:
            with self._lock:
                self.last_trade = trades[0]

    def on_my_fill(self, msg) -> None:
        # A fill on YOUR orders.
        t = msg.trade
        print(f"  *** FILLED: {t.side} {t.quantity} {t.tokenName} @ {t.price} "
              f"(fee {t.fee}) #{t.tradeId}")

    def on_my_order(self, msg) -> None:
        # `order_status` is the transition only (FILLED / CANCELLED / ...).
        print(f"  order #{msg.orderId} -> {msg.status} (left {msg.get('quantityLeft')})")

    def on_my_order_accepted(self, msg) -> None:
        # `order_update` carries the whole committed order. It arrives when the
        # exchange accepts a new order, and for one that trades immediately it
        # can come AFTER its fill frames, already FILLED.
        o = msg.order
        print(f"  order #{o.id} {o.side} {o.quantity} {o.tokenName} @ {o.price} -> {o.status}")

    def on_balances(self, msg) -> None:
        print(f"  balance update: cash {msg.cash:,.2f}  frozen {msg.frozen:,.2f}")

    # -- Decision loop (called on the main thread every few seconds) ------ #

    def on_tick(self) -> None:
        with self._lock:
            bid, ask, mark = self.best_bid, self.best_ask, self.mark_price
            halted = self.is_halted
        spread = (ask - bid) if (bid is not None and ask is not None) else None
        print(f"[{self.token_name}] bid={bid} ask={ask} spread={spread} mark={mark}"
              f"{' [HALTED]' if halted else ''}")

        if halted:
            return  # orders would be rejected with loaf.TradingHaltedError

        # ----------------------------------------------------------------- #
        # YOUR STRATEGY GOES HERE. Examples (uncomment & adapt):
        #
        #   # Place a limit buy 1% below the best bid:
        #   if bid:
        #       price = round(bid * 0.99, 2)
        #       self.client.orders.limit_buy(self.token_name, quantity=1, price=price)
        #
        #   # Market sell 0.5 tokens at no worse than mark x (1 - max slippage);
        #   # with no mark yet (None) the SDK fetches the reference itself. The
        #   # unfilled rest RESTS at that price — cancel it if you don't want it
        #   # on the book:
        #   res = self.client.orders.market_sell(self.token_name, quantity=0.5,
        #                                        reference_price=mark)
        #   if res.status in ("OPEN", "PARTIALLY_FILLED"):
        #       self.client.orders.cancel(res.orderId)
        #
        #   # Arm a stop-loss 5% below the mark (STOP_MARKET SELL; rests
        #   # server-side, and books at up to the max slippage below the trigger):
        #   if mark:
        #       trigger = round(mark * 0.95, 2)
        #       self.client.orders.stop_loss(self.token_name, quantity=1, trigger_price=trigger)
        #
        #   # Flatten everything:
        #   self.client.orders.cancel_all()
        #
        # During an ACTIVE competition round, order placement requires admission
        # (loaf.CompetitionEligibilityError otherwise; loaf.TradingHaltedError
        # while this property or the whole platform is halted).
        # Wrap calls in try/except loaf.LoafAPIError to handle rejections.
        # On loaf.LoafAuthError from a placement, stop trading and alert: your
        # agent key was refused (expired or deleted API key).
        # On loaf.OrderOutcomeUnknownError the order may be live or already
        # filled. If you still want it, resubmit(e.signed_body) (the same order
        # if it landed, placed now if not); otherwise check history.orders()
        # (not openOrders) before placing again.
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
    print(f"\nFollowing property {target.tokenName} (id {target.propertyId}).\n")

    # `isHalted` is only on the property DETAIL response, not the list above. Its
    # book seeds bid/ask too: the order-book channel only pushes on change.
    detail = client.market.property(target.tokenName)
    strategy = Strategy(client, target.tokenName, is_halted=bool(detail.property.isHalted))
    if detail.get("orderBook"):
        strategy.on_orderbook(detail.orderBook)
    if strategy.is_halted:
        print("  Trading is currently HALTED for this property.")

    # Wire up the real-time feed.
    ws = client.websocket()
    ws.on_orderbook(strategy.on_orderbook)
    ws.on_mark_price(strategy.on_mark_price)
    ws.on_connect(strategy.on_feed_reset)            # drop the mark until a fresh one arrives
    ws.on_transport_error(strategy.on_feed_reset)
    ws.on_property_halt(strategy.on_halt)     # public: halt/resume for this property
    ws.on_trades(strategy.on_trade_tick)
    ws.on_trade(strategy.on_my_fill)          # private: your fills
    ws.on_order_update(strategy.on_my_order_accepted)  # private: your new orders
    ws.on_order_status(strategy.on_my_order)  # private: your order transitions
    ws.on_balances(strategy.on_balances)      # private: your balance changes
    ws.on_error(lambda m: print(f"  WS error: {m.get('message')}"))

    ws.subscribe_orderbook(target.tokenName)
    ws.subscribe_mark_price(target.tokenName)
    ws.subscribe_trades(target.tokenName)
    ws.subscribe_property_status(target.tokenName)
    ws.subscribe_portfolio()  # your private fills/balances stream (keyed by your API key)

    ws.start()  # background thread
    ws.wait_until_connected(timeout=10)
    print("Live feed connected. Press Ctrl-C to stop.\n")

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

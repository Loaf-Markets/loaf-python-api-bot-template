"""03 — Place, inspect, and cancel an order.

This DOES place a real (limit) order on a spot market, priced below the best bid
and post-only so it rests on the book rather than filling, and cancels it again.
Review it before running against a live account.

It needs your trading key (LOAF_TRADING_KEY): it signs the order, and the
login tokens that read your trading account and cancel the order again.

    python examples/03_place_order.py
"""

from __future__ import annotations

import time
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from typing import Any

import loaf
from loaf import LoafClient

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


def main() -> None:
    with LoafClient() as client:
        if client.public_key is None:
            raise SystemExit("Set LOAF_TRADING_KEY to place orders (see README §2).")
        place_and_cancel(client)


def place_and_cancel(client: LoafClient) -> None:
    # Pick a spot market you can trade now: LIVE, not halted, with an L2 market, its trading
    # started (tradingStartsAt None or passed). A halt since this read still refuses the
    # order (caught below). A perp ("PERP") would take the BUY as a leveraged long.
    now = time.time()
    tradeable = [
        m
        for m in client.market.list().get("markets") or []
        if m.get("type") == "SPOT"
        and m.get("status") == "LIVE"
        and not m.get("isHalted")
        and m.get("hasL2Market")
        and (m.get("tradingStartsAt") is None or m.tradingStartsAt <= now)
    ]
    if not tradeable:
        raise SystemExit("No spot market is tradeable right now.")
    market = tradeable[0]
    detail = client.market.get(market.ticker)
    book = detail.get("orderBook")
    best_bid = book.bids[0].price if book and book.get("bids") else (market.marketPrice or 1.0)

    # A passive limit BUY 2% below the best bid, behind the bids above it, on the market's
    # tick (its priceDecimals). Not much further: where the exchange bands its markets, a
    # price too far from the market's reference is refused (PRICE_BAND). It is worth its
    # minimum order value (minOrderValue dollars): that over the price, rounded up to the
    # market's lot (10^-quantityDecimals tokens), in Decimal so the rounding is exact.
    places = detail.market.priceDecimals
    price = max(round(best_bid * 0.98, places), 10**-places)  # at least one tick
    lot = Decimal(1).scaleb(-detail.market.quantityDecimals)
    tokens = min_order_value(detail.market) / Decimal(str(price))
    quantity = tokens.quantize(lot, rounding=ROUND_CEILING)  # at least one lot
    print(f"Placing LIMIT BUY {quantity} {market.ticker} @ {price} (best bid {best_bid})")

    try:
        # The order methods take floats; one on the lot is sent as exactly its decimal. It
        # rests 5 minutes at most (ttl_ms; the default is 7 days), so an interrupted run
        # leaves nothing on the book for long. Post-only: if it would trade at once (an
        # ask at or below its price), it is refused (PostOnlyWouldTakeError), not filled.
        result = client.orders.limit_buy(
            market.ticker, quantity=float(quantity), price=price, ttl_ms=300_000,
            time_in_force="POST_ONLY",
        )
    except loaf.LoafConfigError as exc:  # your trading key or account: the message says what to do
        raise SystemExit(str(exc))
    except loaf.LoafAPIError as exc:  # halted, order refused, rate limited
        raise SystemExit(f"Refused: {exc}")
    except loaf.OrderOutcomeUnknownError as exc:
        # Unsure whether it landed: re-send the same signed order to find out (the
        # same order if it landed, placed now if not), then cancel it below. If that
        # is unsure too, settle it by its order number instead, and stop. (Never print
        # exc.signed_body: anyone who has it can place the order.)
        try:
            result = client.orders.resubmit(exc.signed_body)
        except loaf.OrderOutcomeUnknownError:
            settle(client, exc.signed_body["l2OrderIndex"])
            return

    order_id = result.orderId
    print(f"Accepted. orderId={order_id} status={result.status} left={result.quantityLeft}")

    # From here on the order is cancelled whatever happens in between (a failed read,
    # Ctrl-C).
    try:
        # See it among active orders.
        try:
            active = client.history.active_orders().get("activeOrders") or []
            print(f"Active orders: {[o.orderId for o in active]}")
        except loaf.LoafError as exc:
            print(f"Could not read your active orders ({exc}).")
    finally:
        cancel(client, order_id)


def settle(client: LoafClient, order_index: int) -> None:
    """Settle an order whose outcome is unknown by its order number, and report how."""
    print(f"Still unsure whether it landed: settling order number {order_index} ...")
    try:
        answer = client.orders.cancel_by_index(order_index)
    except loaf.LoafError as exc:
        print(f"Could not settle it ({exc}). It may rest until it expires, five minutes after "
              f"it was placed: call client.orders.cancel_by_index({order_index}) again, or "
              "cancel it in the web app.")
        return
    outcome = answer.get("outcome")
    if outcome == loaf.CancelByIndexOutcome.CANCELLED:
        print(f"CANCELLED: it was resting (orderId={answer.get('orderId')}), and is cancelled now.")
    elif outcome == loaf.CancelByIndexOutcome.ENDED:
        print(f"ENDED: it was placed (orderId={answer.get('orderId')}) and no longer rests: "
              f"{answer.get('status')}.")
    elif outcome == loaf.CancelByIndexOutcome.VOIDED:
        print("VOIDED: it was never placed, and never will be.")
    else:
        print(f"Settled: {outcome}.")


def min_order_value(market: Any) -> Decimal:
    """The market's minOrderValue (dollars), or stop when its answer has no usable one: this
    example sizes its order by it, and does not guess."""
    raw = market.get("minOrderValue")
    try:
        value = Decimal(str(raw))
    except InvalidOperation:
        value = Decimal("NaN")
    if isinstance(raw, bool) or not value.is_finite() or value <= 0:
        raise SystemExit(f"{market.get('ticker')} has no usable minOrderValue ({raw!r}), so this "
                         "example cannot size its order: it places nothing.")
    return value


def cancel(client: LoafClient, order_id: int) -> None:
    """Cancel the order, reporting a refusal instead of raising it."""
    print(f"Cancelling {order_id} ...")
    try:
        client.orders.cancel(order_id)
        print("Cancelled.")
    except (loaf.LoafConflictError, loaf.LoafValidationError) as exc:  # 409 / 400
        print(f"No longer cancellable (it filled or ended): {exc.message}")
    except loaf.LoafError as exc:
        print(f"Cancel failed: {exc}. Order {order_id} may still rest until it expires, five "
              "minutes after it was placed: cancel it in the web app, or with "
              f"client.orders.cancel({order_id}).")


if __name__ == "__main__":
    main()

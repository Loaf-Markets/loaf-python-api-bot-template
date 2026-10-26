"""03 — Place, inspect, and cancel an order.

This DOES place a real (limit) order, priced far from the market so it rests on
the book rather than filling. Review it before running against a live account.

It needs both keys: LOAF_SECRET_KEY signs the order, and LOAF_API_KEY reads
your trading account and cancels the order again.

    python examples/03_place_order.py
"""

from __future__ import annotations

import time

import loaf
from loaf import LoafClient

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


def main() -> None:
    client = LoafClient()
    if client.public_key is None:
        raise SystemExit("Set LOAF_SECRET_KEY to place orders (see README §2).")
    if not client.api_key:
        raise SystemExit(
            "Set LOAF_API_KEY too, to read your trading account and cancel the order "
            "(see README §2)."
        )

    # Pick a market you can trade now: LIVE, not halted, with an L2 market, its trading
    # started (tradingStartsAt None or passed). A halt since this read still refuses the
    # order (caught below).
    now = time.time()
    tradeable = [
        m
        for m in client.market.list().get("markets") or []
        if m.get("status") == "LIVE"
        and not m.get("isHalted")
        and m.get("hasL2Market")
        and (m.get("tradingStartsAt") is None or m.tradingStartsAt <= now)
    ]
    if not tradeable:
        raise SystemExit("No tradeable markets right now.")
    market = tradeable[0]
    detail = client.market.get(market.ticker)
    book = detail.get("orderBook")
    best_bid = book.bids[0].price if book and book.get("bids") else (market.marketPrice or 1.0)

    # A passive limit BUY well below the best bid (unlikely to fill immediately).
    price = round(best_bid * 0.80, 2)
    print(f"Placing LIMIT BUY 1 {market.ticker} @ {price} (best bid {best_bid})")

    try:
        result = client.orders.limit_buy(market.ticker, quantity=1, price=price)
    except loaf.LoafConfigError as exc:  # your trading key or account: the message says what to do
        raise SystemExit(str(exc))
    except loaf.LoafAPIError as exc:  # halted, order refused, rate limited
        raise SystemExit(f"Refused: {exc}")
    except loaf.OrderOutcomeUnknownError as exc:
        # Unsure whether it landed: re-send the same signed order to find out (the
        # same order if it landed, placed now if not), then cancel it below. If this
        # raises again, look for the order in client.history.orders().
        result = client.orders.resubmit(exc.signed_body)

    order_id = result.orderId
    print(f"Accepted. orderId={order_id} status={result.status} left={result.quantityLeft}")

    # See it among active orders.
    active = client.history.active_orders().get("activeOrders") or []
    print(f"Active orders: {[o.orderId for o in active]}")

    # Cancel it.
    print(f"Cancelling {order_id} ...")
    try:
        client.orders.cancel(order_id)
        print("Cancelled.")
    except (loaf.LoafConflictError, loaf.LoafValidationError) as exc:
        print(f"No longer cancellable: {exc.message}")

    client.close()


if __name__ == "__main__":
    main()

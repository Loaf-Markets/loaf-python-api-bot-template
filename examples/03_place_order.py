"""03 — Place, inspect, and cancel an order.

This DOES place a real (limit) order, priced far from the market so it rests on
the book rather than filling. Review it before running against a live account.

    python examples/03_place_order.py
"""

from __future__ import annotations

import loaf
from loaf import LoafClient

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


def main() -> None:
    client = LoafClient()

    # Pick a property to trade.
    properties = client.market.properties().get("properties") or []
    if not properties:
        raise SystemExit("No properties available.")
    prop = properties[0]
    detail = client.market.property(prop.tokenName)
    book = detail.get("orderBook")
    best_bid = book.bids[0].price if book and book.get("bids") else (prop.marketPrice or 1.0)

    # A passive limit BUY well below the best bid (unlikely to fill immediately).
    price = round(best_bid * 0.80, 2)
    print(f"Placing LIMIT BUY 1 {prop.tokenName} @ {price} (best bid {best_bid})")

    try:
        result = client.orders.limit_buy(prop.tokenName, quantity=1, price=price)
    except loaf.CompetitionEligibilityError:
        raise SystemExit(
            "Not admitted to the active competition round — check "
            "client.competition.queue_position() for your place in the queue."
        )
    except loaf.TradingHaltedError:
        raise SystemExit("Trading is currently halted platform-wide. Try again later.")
    except loaf.LoafValidationError as exc:
        raise SystemExit(f"Rejected: {exc.message} {exc.details}")

    order_id = result.orderId
    print(f"Accepted. orderId={order_id}")

    # See it among active orders.
    active = client.history.active_orders().get("activeOrders") or []
    print(f"Active orders: {[o.orderId for o in active]}")

    # Cancel it.
    print(f"Cancelling {order_id} ...")
    try:
        client.orders.cancel(order_id)
        print("Cancelled.")
    except loaf.LoafConflictError:
        print("Order was no longer cancellable (likely already filled/cancelled).")

    client.close()


if __name__ == "__main__":
    main()

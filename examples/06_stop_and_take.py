"""06 — Arm a stop, read it back, cancel it.

This DOES place a real conditional order, triggered far from the market so it
never fires. Review it before running against a live account.

    python examples/06_stop_and_take.py
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
    mark = book.bids[0].price if book and book.get("bids") else (prop.marketPrice or 1.0)

    # A STOP BUY fires when the mark RISES to the trigger, so a trigger 50%
    # above the market just rests. (A stop SELL would need a holding to sell.)
    trigger = round(mark * 1.50, 2)
    print(f"Arming STOP_MARKET BUY 1 {prop.tokenName} @ trigger {trigger} (mark {mark})")

    try:
        result = client.orders.create_conditional(
            prop.tokenName,
            "BUY",
            quantity=1,
            type="STOP_MARKET",
            trigger_price=trigger,
        )
    except loaf.CompetitionEligibilityError:
        raise SystemExit(
            "Not admitted to the active competition round — check "
            "client.competition.queue_position() for your place in the queue."
        )
    except loaf.TradingHaltedError:
        raise SystemExit("Trading is currently halted platform-wide. Try again later.")
    except loaf.LoafValidationError as exc:
        raise SystemExit(
            f"Refused: {exc}. If this says 'would trigger immediately', the trigger "
            "was too close to the mark."
        )

    order_id = result.orderId
    print(f"Armed. orderId={order_id} — ARMED only means it rests, not that it will book.")

    # Conditionals show up in portfolio openOrders, NOT in history.active_orders(),
    # and share that list with booked orders — narrow on the type before reading them.
    open_orders = client.portfolio.component().get("openOrders") or []
    row = next(
        (o for o in open_orders if loaf.is_conditional_order(o) and o.get("id") == order_id),
        None,
    )
    if row is not None:
        print(f"  type={row.type:<12} status={row.status:<10} trigger={row.triggerPrice}")
    else:
        print("  Row not in the openOrders snapshot yet — it can lag placement slightly.")

    # Cancel it. A PENDING/ARMED conditional goes to its own cancel route; once it
    # is PLACED you cancel the booked order it created (orders.cancel_row picks).
    print(f"Cancelling conditional {order_id} ...")
    try:
        client.orders.cancel_conditional(order_id)
        print("Cancelled.")
    except loaf.LoafNotFoundError:
        print("No such conditional order (already cancelled, or a booked-order id).")
    except loaf.LoafValidationError as exc:
        print(f"No longer cancellable: {exc.message}")

    client.close()


if __name__ == "__main__":
    main()

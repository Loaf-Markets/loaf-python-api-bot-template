"""03 — Place, inspect, and cancel an order.

This DOES place a real (limit) order, priced far from the market so it rests on
the book rather than filling. Review it before running against a live account.

It needs both keys: LOAF_AGENT_PRIVATE_KEY signs the order, and LOAF_API_KEY
cancels it again.

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
    if client.agent_address is None:
        raise SystemExit("Set LOAF_AGENT_PRIVATE_KEY to place orders (see README §2).")
    if not client.api_key:
        raise SystemExit("Set LOAF_API_KEY too, to cancel the order again (see README §2).")

    # Pick a property you can trade now: LIVE, with a deployed token, and in the
    # market that is open (competition properties trade only during a round).
    listing = client.market.properties()
    competition = bool(listing.get("competitionModeActive"))
    tradeable = [
        p
        for p in listing.get("properties") or []
        if p.get("status") == "LIVE"
        and bool(p.get("isCompetition")) == competition
        and p.get("contractAddress")
    ]
    if not tradeable:
        raise SystemExit("No tradeable properties right now.")
    prop = tradeable[0]
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
    except loaf.LoafAuthError as exc:
        raise SystemExit(
            f"Order signature refused: {exc.message}. Is LOAF_AGENT_PRIVATE_KEY "
            "the agent key of a current API key?"
        )
    except loaf.LoafAPIError as exc:  # halted, market closed, order refused, rate limited
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
    except loaf.LoafConflictError:
        print("Order was no longer cancellable (likely already filled/cancelled).")

    client.close()


if __name__ == "__main__":
    main()

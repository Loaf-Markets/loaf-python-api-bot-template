"""06 — Arm a stop, read it back, cancel it.

This DOES place a real conditional order, triggered far from the market so it
never fires. Review it before running against a live account.

It needs both keys: LOAF_AGENT_PRIVATE_KEY signs the order, and LOAF_API_KEY
reads it back and cancels it.

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
    mark = book.bids[0].price if book and book.get("bids") else (prop.marketPrice or 1.0)

    # A STOP BUY fires when the mark RISES to the trigger, so a trigger 50%
    # above the market just rests. (A stop SELL would need a holding to sell.)
    trigger = round(mark * 1.50, 2)
    # When it fires it books a LIMIT BUY at the trigger plus the client's max
    # slippage ($LOAF_MAX_SLIPPAGE_BPS, default 2%). Placing it needs total USDC
    # of at least that price x quantity plus the taker fee, and that value must
    # be at least 10 USDC.
    books_at = loaf.worst_price(trigger, "BUY", client.max_slippage_bps)
    print(f"Arming STOP_MARKET BUY 1 {prop.tokenName} @ trigger {trigger} "
          f"(books at up to {books_at}; mark {mark})")

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
    except loaf.LoafAuthError as exc:
        raise SystemExit(
            f"Order signature refused: {exc.message}. Is LOAF_AGENT_PRIVATE_KEY "
            "the agent key of a current API key?"
        )
    except loaf.LoafValidationError as exc:
        raise SystemExit(
            f"Refused: {exc}. If this says 'would trigger immediately', the trigger "
            "was too close to the mark."
        )
    except loaf.LoafAPIError as exc:  # halted, market closed, rate limited
        raise SystemExit(f"Refused: {exc}")
    except loaf.OrderOutcomeUnknownError as exc:
        # Unsure whether it was armed: re-send the same signed order to find out (the
        # same row if it landed, armed now if not), then cancel it below. If this raises
        # again, look for the row in client.history.orders() and cancel_conditional it.
        result = client.orders.resubmit(exc.signed_body)

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

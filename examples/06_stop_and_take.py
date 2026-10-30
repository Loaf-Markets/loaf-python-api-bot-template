"""06 — Arm a stop, read it back, cancel it.

This DOES place a real conditional order, triggered far from the market so it
never fires. Review it before running against a live account.

It needs your trading key (LOAF_TRADING_KEY): it signs the order, and the
login tokens that read your trading account, read the row back and cancel it.

    python examples/06_stop_and_take.py
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
        raise SystemExit("Set LOAF_TRADING_KEY to place orders (see README §2).")

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
    mark = book.bids[0].price if book and book.get("bids") else (market.marketPrice or 1.0)

    # A STOP BUY fires when the mark RISES to the trigger, so a trigger 50%
    # above the market just rests. (A stop SELL would need tokens to sell when it
    # fires.)
    trigger = round(mark * 1.50, 2)
    # When it fires it books a LIMIT BUY at the trigger plus the client's max
    # slippage ($LOAF_MAX_SLIPPAGE_BPS, default 2%). That price x quantity must be
    # at least $10; your cash is checked only when it fires.
    books_at = loaf.worst_price(trigger, "BUY", client.max_slippage_bps)
    print(f"Arming STOP_MARKET BUY 1 {market.ticker} @ trigger {trigger} "
          f"(books at up to {books_at}; rounded toward the trigger; mark {mark})")

    try:
        result = client.orders.create_conditional(
            market.ticker,
            "BUY",
            quantity=1,
            type="STOP_MARKET",
            trigger_price=trigger,
        )
    except loaf.LoafConfigError as exc:  # your trading key or account: the message says what to do
        raise SystemExit(str(exc))
    except loaf.LoafValidationError as exc:
        raise SystemExit(
            f"Refused: {exc}. If this says 'would trigger immediately', the trigger "
            "was too close to the mark."
        )
    except loaf.LoafAPIError as exc:  # halted, rate limited
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
        print("No such conditional order (an unknown id, or a booked-order id).")
    except loaf.LoafValidationError as exc:
        print(f"No longer cancellable: {exc.message}")

    client.close()


if __name__ == "__main__":
    main()

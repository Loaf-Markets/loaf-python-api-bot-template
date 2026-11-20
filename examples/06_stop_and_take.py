"""06 — Arm a stop, read it back, cancel it.

This DOES place a real conditional order on a spot market, triggered far from
the market so it never fires, and cancels it again. Review it before running
against a live account.

It needs your trading key (LOAF_TRADING_KEY): it signs the order, and the
login tokens that read your trading account, read the row back and cancel it.

    python examples/06_stop_and_take.py
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
        arm_and_cancel(client)


def arm_and_cancel(client: LoafClient) -> None:
    # Pick a spot market you can trade now: LIVE, not halted, with an L2 market, its trading
    # started (tradingStartsAt None or passed). A halt since this read still refuses the
    # order (caught below). On a perp ("PERP") a stop BUY that fires opens a leveraged long.
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
    mark = book.bids[0].price if book and book.get("bids") else (market.marketPrice or 1.0)

    # A STOP BUY fires when the mark RISES to the trigger, so a trigger 50%
    # above the market just rests. (A stop SELL would need tokens to sell when it
    # fires.) The trigger is on the market's tick: at most priceDecimals decimals.
    decimals = detail.market.priceDecimals, detail.market.quantityDecimals
    trigger = round(mark * 1.50, decimals[0])
    # When it fires it books a LIMIT BUY at the trigger plus the client's max
    # slippage ($LOAF_MAX_SLIPPAGE_BPS, default 2%), on the tick. That price x
    # quantity must be at least the market's minOrderValue ($10), so the quantity
    # is that over the price, rounded up to the market's lot (10^-quantityDecimals
    # tokens) in Decimal; your cash is checked only when it fires.
    books_at = loaf.worst_price(trigger, "BUY", client.max_slippage_bps,
                                price_decimals=decimals[0], quantity_decimals=decimals[1])
    lot = Decimal(1).scaleb(-decimals[1])
    tokens = min_order_value(detail.market) / Decimal(str(books_at))
    quantity = tokens.quantize(lot, rounding=ROUND_CEILING)  # at least one lot
    print(f"Arming STOP_MARKET BUY {quantity} {market.ticker} @ trigger {trigger} "
          f"(books at up to {books_at}; rounded toward the trigger; mark {mark})")

    try:
        result = client.orders.create_conditional(
            market.ticker,
            "BUY",
            quantity=float(quantity),  # a float on the lot is sent as exactly its decimal
            type="STOP_MARKET",
            trigger_price=trigger,
            # 5 minutes (the default is 7 days): an interrupted run leaves it armed for
            # that long at most, then the exchange FAILS it.
            ttl_ms=300_000,
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
        # same row if it landed, armed now if not), then cancel it below. If that is
        # unsure too, settle it by its order number instead, and stop. (Never print
        # exc.signed_body: anyone who has it can arm the stop.)
        try:
            result = client.orders.resubmit(exc.signed_body)
        except loaf.OrderOutcomeUnknownError:
            settle(client, exc.signed_body["l2OrderIndex"])
            return

    order_id = result.orderId
    print(f"Armed. orderId={order_id} — ARMED only means it rests, not that it will book.")

    # From here on the row is cancelled whatever happens in between (a failed read,
    # Ctrl-C).
    try:
        try:
            show(client, order_id)
        except loaf.LoafError as exc:
            print(f"Could not read your open orders ({exc}).")
    finally:
        cancel(client, order_id)


def show(client: LoafClient, order_id: int) -> None:
    # Conditionals show up in portfolio openOrders, NOT in history.active_orders(),
    # and share that list with booked orders — narrow on the type before reading them.
    # The list is None when it went unread just now, which never means empty.
    open_orders = client.portfolio.component().get("openOrders") or []
    row = next(
        (o for o in open_orders if loaf.is_conditional_order(o) and o.get("id") == order_id),
        None,
    )
    if row is not None:
        print(f"  type={row.type:<12} status={row.status:<10} trigger={row.triggerPrice}")
    else:
        print("  Row not in the openOrders snapshot — it can lag placement, "
              "or the list went unread.")


def settle(client: LoafClient, order_index: int) -> None:
    """Settle a stop whose outcome is unknown by its order number, and report how."""
    print(f"Still unsure whether it was armed: settling order number {order_index} ...")
    try:
        answer = client.orders.cancel_by_index(order_index)
    except loaf.LoafValidationError as exc:
        if exc.code != "INVALID_REQUEST":
            print(f"Could not settle it: {exc.message}")
            return
        # It was armed: a placed stop holds the number, and the refusal names its id. Voiding
        # the number would leave the stop unable to fire, so it is cancelled by that id.
        print(f"It was armed: {exc.message}\n"
              "Cancel that stop with client.orders.cancel_conditional(<the id named there>), "
              "or in the web app; else it stays armed until its five minutes end.")
        return
    except loaf.LoafError as exc:
        print(f"Could not settle it ({exc}). It may stay armed until its five minutes end: call "
              f"client.orders.cancel_by_index({order_index}) again, or cancel it in the web app.")
        return
    outcome = answer.get("outcome")
    if outcome == loaf.CancelByIndexOutcome.CANCELLED:
        print(f"CANCELLED: the order on that number (orderId={answer.get('orderId')}) was "
              "resting, and is cancelled now.")
    elif outcome == loaf.CancelByIndexOutcome.ENDED:
        print(f"ENDED: the order on that number (orderId={answer.get('orderId')}) no longer "
              f"rests: {answer.get('status')}.")
    elif outcome == loaf.CancelByIndexOutcome.VOIDED:
        print("VOIDED: it was never armed, and never will be.")
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
    """Cancel the row, reporting a refusal instead of raising it. A PENDING/ARMED
    conditional goes to its own cancel route; once it is PLACED you cancel the booked
    order it created (orders.cancel_row picks)."""
    print(f"Cancelling conditional {order_id} ...")
    try:
        client.orders.cancel_conditional(order_id)
        print("Cancelled.")
    except loaf.LoafNotFoundError:
        print("No such conditional order (an unknown id, or a booked-order id).")
    except (loaf.LoafConflictError, loaf.LoafValidationError) as exc:  # 409 / 400
        print(f"No longer cancellable (it fired or ended): {exc.message}")
    except loaf.LoafError as exc:
        print(f"Cancel failed: {exc}. Row {order_id} may still be armed until its five "
              "minutes end: cancel it in the web app, or with "
              f"client.orders.cancel_conditional({order_id}).")


if __name__ == "__main__":
    main()

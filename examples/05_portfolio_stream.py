"""05 — Stream your PRIVATE portfolio events (requires an API key).

Receives balance changes, position updates, order transitions, your fills,
transfers, and IPO order updates in real time.

    python examples/05_portfolio_stream.py

The channel carries no id — your API key tells the server whose events to send.
"""

from __future__ import annotations

from loaf import LoafClient

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


def main() -> None:
    client = LoafClient()  # needs LOAF_API_KEY for the private channel

    ws = client.websocket()

    def units(row, key):
        return int(row[key]) / 10 ** row.decimals  # base units as a decimal string

    # The whole L2 book as the engine committed it: replace what you hold.
    ws.on_balances(lambda m: print("BALANCES " + "  ".join(
        f"{b.symbol}={units(b, 'available'):,.6f}" for b in m.balances)))
    # An IPO allocation (an L2 holding's position is read from portfolio.component()).
    ws.on_position(lambda m: print(f"POSITION {m.position.ticker} qty={m.position.get('quantity')}"))
    # Two order frames: `order_update` carries the full committed order (sent
    # when it is accepted and on every conditional transition; for an order that
    # trades immediately it can follow the fill frames), `order_status` just the
    # transition (FILLED / CANCELLED / ...).
    ws.on_order_update(lambda m: print(f"ORDER #{m.order.id} {m.order.side} {m.order.quantity} "
                                       f"{m.order.ticker} @ {m.order.price} -> {m.order.status}"))
    ws.on_order_status(lambda m: print(f"ORDER #{m.orderId} -> {m.status} "
                                       f"(left {m.get('quantityLeft')})"))
    ws.on_trade(lambda m: print(f"FILL {m.trade.side} {m.trade.quantity} "
                                f"{m.trade.ticker} @ {m.trade.price} fee={m.trade.fee} "
                                f"#{m.trade.tradeId} proof={(m.trade.get('proof') or {}).get('status')}"))
    ws.on_transfer(lambda m: print(f"TRANSFER {m.transfer.type} {units(m.transfer, 'amount'):,.6f} "
                                   f"{m.transfer.symbol} -> {m.transfer.status}"))
    ws.on_offering_order(lambda m: print(f"IPO ORDER #{m.order.ipoOrderId} -> {m.order.status}"))
    ws.on_error(lambda m: print(f"ERROR {m.get('message')}"))

    # Subscribe to your own private channel.
    ws.subscribe_portfolio()
    print("Listening for portfolio events. Trade in another window to see updates. "
          "Ctrl-C to stop.\n")

    ws.run_forever()


if __name__ == "__main__":
    main()

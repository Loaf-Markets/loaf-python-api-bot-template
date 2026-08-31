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

    ws.on_balances(lambda m: print(f"BALANCE cash={m.cash:,.2f} frozen={m.frozen:,.2f}"))
    ws.on_position(lambda m: print(f"POSITION {m.tokenName} qty={m.quantity} "
                                   f"total={m.totalQuantity} avg={m.averageEntryPrice}"))
    # Two order frames: `order_update` carries the full order (sent when it is
    # accepted), `order_status` just the transition (FILLED / CANCELLED / ...).
    ws.on_order_update(lambda m: print(f"ORDER #{m.order.id} {m.order.side} {m.order.quantity} "
                                       f"{m.order.tokenName} @ {m.order.price} -> {m.order.status}"))
    ws.on_order_status(lambda m: print(f"ORDER #{m.orderId} -> {m.status} "
                                       f"(left {m.get('quantityLeft')})"))
    ws.on_trade(lambda m: print(f"FILL {m.trade.side} {m.trade.quantity} "
                                f"{m.trade.tokenName} @ {m.trade.price} "
                                f"[{m.trade.status}] #{m.trade.tradeId}"))
    ws.on_transfer(lambda m: print(f"TRANSFER {m.transfer.type} {m.transfer.amount} "
                                   f"-> {m.transfer.status}"))
    ws.on_offering_order(lambda m: print(f"IPO ORDER #{m.order.ipoOrderId} -> {m.order.status}"))
    ws.on_error(lambda m: print(f"ERROR {m.get('message')}"))

    # Subscribe to your own private channel.
    ws.subscribe_portfolio()
    print("Listening for portfolio events. Trade in another window to see updates. "
          "Ctrl-C to stop.\n")

    ws.run_forever()


if __name__ == "__main__":
    main()

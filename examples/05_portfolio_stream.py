"""05 — Stream your PRIVATE portfolio events (requires your trading key).

Receives your balance book, order transitions, your fills and your perps account
in real time.

    python examples/05_portfolio_stream.py

The channel carries no id — the login token your trading key signs tells the
server whose events to send.
"""

from __future__ import annotations

from loaf import LoafClient, from_base_units

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


def main() -> None:
    client = LoafClient()
    if client.public_key is None:
        raise SystemExit("Set LOAF_TRADING_KEY for your private channel (see README §2).")

    ws = client.websocket()

    # Your whole balance book after every change: replace yours with it (a missing
    # row means 0). Amounts are base-unit strings; the isQuote rows are your cash.
    @ws.on_balances
    def on_balances(m):
        for b in m.balances:
            print(f"BALANCE {b.symbol} {from_base_units(b.available, b.decimals)} available")

    # Two order frames: `order_update` carries the full committed order (sent
    # when it is accepted and on every conditional transition; for an order that
    # trades immediately it can follow the fill frames), `order_status` just the
    # transition (FILLED / CANCELLED / ...).
    ws.on_order_update(lambda m: print(f"ORDER #{m.order.id} {m.order.side} {m.order.quantity} "
                                       f"{m.order.ticker} @ {m.order.price} -> {m.order.status}"))
    ws.on_order_status(lambda m: print(f"ORDER #{m.orderId} -> {m.status} "
                                       f"(left {m.get('quantityLeft')})"))
    # Every fill of yours; source says what made it. txHash is a trade's content hash, not an
    # on-chain transaction, and None on a fill that is no trade (a liquidation's, say).
    ws.on_trade(lambda m: print(f"FILL {m.trade.source} {m.trade.side} {m.trade.quantity} "
                                f"{m.trade.ticker} @ {m.trade.price} fee={m.trade.fee} "
                                f"#{m.trade.tradeId}"))
    # Your perps pools, whole, after every commit that touches them.
    ws.on_perps_account(lambda m: print(f"PERPS {len(m.perps.pools)} pools"))
    # Every ended connection attempt (code None: no connection was made); it reconnects.
    ws.on_disconnect(lambda m: print(f"DISCONNECTED code={m.code} reason={m.reason!r}"))
    ws.on_error(lambda m: print(f"ERROR {m.get('message')}"))

    # Subscribe to your own private channel.
    ws.subscribe_portfolio()
    print("Listening for portfolio events. Trade in another window to see updates. "
          "Ctrl-C to stop.\n")

    ws.run_forever()


if __name__ == "__main__":
    main()

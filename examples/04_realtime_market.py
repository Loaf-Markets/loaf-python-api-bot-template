"""04 — Stream live market data (public order book + trades + mark price + halts).

    python examples/04_realtime_market.py

No trading key required for public market channels.
"""

from __future__ import annotations

from loaf import LoafClient

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


def main() -> None:
    client = LoafClient()

    markets = client.market.list().get("markets") or []
    if not markets:
        raise SystemExit("No markets available.")
    market = markets[0]
    print(f"Streaming {market.ticker} (index {market.marketIndex}). Ctrl-C to stop.\n")

    ws = client.websocket()

    @ws.on_orderbook
    def on_book(msg):
        bid = msg.bids[0] if msg.get("bids") else None
        ask = msg.asks[0] if msg.get("asks") else None
        print(f"BOOK  bid={bid.price if bid else '-':<10} ask={ask.price if ask else '-'}")

    last_trade_id = 0

    @ws.on_trades
    def on_trades(msg):
        nonlocal last_trade_id
        # Newest first; each (re)subscription brings the recent trades again: skip those printed.
        for t in reversed(msg.get("trades") or []):
            if t.tradeId > last_trade_id:
                last_trade_id = t.tradeId
                print(f"TRADE {t.aggressorSide} {t.quantity} @ {t.price}")

    @ws.on_mark_price
    def on_mark(msg):
        print(f"MARK  {msg.price}")

    @ws.on_market_halt
    def on_halt(msg):
        print(f"HALT  {msg.ticker} -> {'HALTED' if msg.isHalted else 'RESUMED'}")

    ws.subscribe_orderbook(market.ticker)
    ws.subscribe_trades(market.ticker)
    ws.subscribe_mark_price(market.ticker)
    ws.subscribe_market_status(market.ticker)

    ws.run_forever()  # blocking until Ctrl-C


if __name__ == "__main__":
    main()

"""04 — Stream live market data (public order book + trades + mark price + halts).

    python examples/04_realtime_market.py

No API key required for public market channels.
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

    properties = client.market.properties().get("properties") or []
    if not properties:
        raise SystemExit("No properties available.")
    prop = properties[0]
    print(f"Streaming {prop.tokenName} (id {prop.propertyId}). Ctrl-C to stop.\n")

    ws = client.websocket()

    @ws.on_orderbook
    def on_book(msg):
        bid = msg.bids[0] if msg.get("bids") else None
        ask = msg.asks[0] if msg.get("asks") else None
        print(f"BOOK  bid={bid.price if bid else '-':<10} ask={ask.price if ask else '-'}")

    @ws.on_trades
    def on_trades(msg):
        for t in (msg.get("trades") or [])[:1]:  # newest
            print(f"TRADE {t.aggressorSide} {t.quantity} @ {t.price}")

    @ws.on_mark_price
    def on_mark(msg):
        print(f"MARK  {msg.price}")

    @ws.on_property_halt
    def on_halt(msg):
        print(f"HALT  {msg.tokenName} -> {'HALTED' if msg.isHalted else 'RESUMED'}")

    ws.subscribe_orderbook(prop.tokenName)
    ws.subscribe_trades(prop.tokenName)
    ws.subscribe_mark_price(prop.tokenName)
    ws.subscribe_property_status(prop.tokenName)

    ws.run_forever()  # blocking until Ctrl-C


if __name__ == "__main__":
    main()

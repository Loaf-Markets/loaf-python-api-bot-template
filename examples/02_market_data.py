"""02 — Public market data (no API key needed).

    python examples/02_market_data.py
"""

from __future__ import annotations

from loaf import LoafClient

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


def main() -> None:
    # Market data is public — anonymous client is fine.
    client = LoafClient()

    properties = client.market.properties().get("properties") or []
    print(f"{len(properties)} properties listed:\n")
    for prop in properties[:10]:
        print(f"  {prop.ticker:<6} {prop.tokenName:<20} "
              f"price={prop.marketPrice}  24h vol={prop.volume24h}  status={prop.status}")

    if not properties:
        return

    ticker = properties[0].ticker
    print(f"\n--- detail for {ticker} ---")
    detail = client.market.property(ticker)
    book = detail.get("orderBook")
    if book:
        best_bid = book.bids[0] if book.get("bids") else None
        best_ask = book.asks[0] if book.get("asks") else None
        print(f"  best bid: {best_bid}")
        print(f"  best ask: {best_ask}")

    # Chart history lives on its own endpoint (the detail response has none).
    print(f"\n--- last 24 hourly candles for {ticker} ---")
    history = client.market.candles(ticker, "1h", count_back=24)
    candles = history.get("candles") or []
    print(f"  {len(candles)} candles (hasMore={history.hasMore}); "
          f"latest: {candles[-1] if candles else 'n/a'}")

    client.close()


if __name__ == "__main__":
    main()

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

    markets = client.market.list().get("markets") or []
    print(f"{len(markets)} markets listed:\n")
    for market in markets[:10]:
        print(f"  {market.ticker:<10} {market.name:<20} "
              f"price={market.marketPrice}  24h vol={market.volume24h}  status={market.status}")

    if not markets:
        return

    ticker = markets[0].ticker
    print(f"\n--- detail for {ticker} ---")
    detail = client.market.get(ticker)
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

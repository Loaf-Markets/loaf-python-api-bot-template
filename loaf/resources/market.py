"""Public market data: markets, candles, trades, asset info pages.

None of these require authentication — they work with an anonymous client.
All currency values are plain dollars and quantities plain tokens (see
:mod:`loaf.money`).

A market is one order book, spot or perp, named by its ticker: its asset's
ticker, ``-PERP`` appended for a perp (``"MUS"``, ``"H100-PERP"``). The info
pages describe an asset, named by the asset's ticker. Every call that takes a
ticker refuses a malformed one (``"mus"``, say) before sending.
"""

from __future__ import annotations

from typing import Any, Iterator

from ..exceptions import LoafError
from .base import Resource, _check_asset_ticker, _check_market_ticker


class MarketResource(Resource):
    # -- Markets ----------------------------------------------------------- #

    def list(self) -> Any:
        """``GET /trade`` — every market that is not delisted (``PENDING`` and
        ``LIVE``), each with its market data and a 24h sparkline.

        A row is tradeable when ``status == "LIVE"``, it is not ``isHalted``,
        ``hasL2Market`` and its ``tradingStartsAt`` has passed: :meth:`get`'s
        rule, read off the row.

        Returns ``{"markets": [...]}``. Each item has ``marketIndex``,
        ``ticker`` (the market's, the identifier every market call takes),
        ``type`` (``SPOT`` / ``PERP``), ``status``, ``isHalted``,
        ``tradingStartsAt``, ``name`` (the asset's display name), ``kind``
        (the asset's, :class:`~loaf.enums.AssetKind`), ``tokenAddress``,
        ``marketPrice``, ``dailyReferencePrice``, ``volume24h``,
        ``hasL2Market``, ``candlesticks`` (trailing-24h hourly OHLCV sparkline
        only — use :meth:`candles` for real chart history), ``property`` (a
        property's address, type, supply and yield; ``None`` for any other
        kind), etc.

        ``isHalted`` is the effective trading-halt flag when read, as
        :meth:`get`'s ``market.isHalted``; ``market_halt`` frames carry each
        change on the ``market:{ticker}`` channel
        (:meth:`loaf.ws.client.LoafWebSocketClient.subscribe_market_status`).
        ``tradingStartsAt`` is when the market's trading starts (unix seconds):
        orders on it are refused before then (403 ``Trading on this market has
        not started yet``); ``None``: no wait. ``tokenAddress`` is the asset's
        token contract on the custody chain (``None`` without a token).
        ``marketPrice`` is ``0`` when the exchange has no reference yet.
        """
        return self._client.get("/trade", auth=False)

    def get(self, ticker: str) -> Any:
        """``GET /trade/{ticker}`` — full detail for one market.

        Returns the ``market``, its ``asset``, a ``marketList`` selector, the
        current ``orderBook`` snapshot (``bids``/``asks`` price levels),
        ``recentTrades``, ``volume24h``, ``dailyReferencePrice`` and
        ``liquidity``. Candle history is NOT included — fetch it from the
        dedicated :meth:`candles` endpoint.

        ``market`` is ``{marketIndex, ticker, type, status, isHalted,
        tradingStartsAt, l2}``.
        ``market.l2`` is, by ``type``, a spot market's ``{chainId,
        proxyAddress, l2ChainId, baseAssetId, quoteAssetId}``
        (:class:`~loaf.models.MarketL2`), or a perp's, with its ``status`` and
        terms and no base asset (:class:`~loaf.models.PerpMarketL2`, as
        ``perps.markets()`` lists it): with ``marketIndex``, what every order
        on the market signs (the SDK reads both for you), or ``None``: the
        market cannot be traded now. ``asset`` is ``{ticker, name, kind,
        assetId, tokenAddress, videoUrl, stakeholders, property}``:
        ``assetId`` is its token's balances row (``None`` without one), and
        ``property`` a property's own facts (``None`` for any other kind). A
        spot market and the perp of one asset carry the same ``asset``. The
        detail has no ``marketPrice``: the reference MARKET orders are priced
        from is the ``marketList`` entry with the same ``ticker``.

        The market trades while its ``status`` is ``LIVE``, it is not
        ``isHalted``, its ``l2`` is not ``None`` and its ``tradingStartsAt``
        (unix seconds) has passed (``None``: no wait): orders before then are
        refused (403 ``Trading on this market has not started yet``). A perp
        whose ``l2.status`` is ``EXPIRED`` takes no more orders.

        ``recentTrades`` holds the newest trades first (at most 20), each with a
        ``txHash``: the trade's content hash (``0x`` + 64 hex), not an on-chain
        transaction (a perp's liquidation take has ``isLiquidation: True`` and
        no ``txHash``: :class:`~loaf.models.TradeTick`). ``orderBook`` is
        ``None`` when the market is not LIVE or no fresh book is known: treat
        ``None`` as unknown, not empty.

        ``market.isHalted`` is the effective trading-halt flag (this market's
        own state OR'd with the platform-wide kill switch), as a :meth:`list`
        row's ``isHalted``. Seed from it, then keep it live with the
        ``market:{ticker}`` WebSocket channel
        (:meth:`loaf.ws.client.LoafWebSocketClient.subscribe_market_status`).
        """
        _check_market_ticker(ticker)
        return self._client.get(f"/trade/{ticker}", auth=False)

    # -- Candles (chart history) -------------------------------------------- #

    def candles(
        self,
        ticker: str,
        resolution: str,
        *,
        to: int | None = None,
        count_back: int | None = None,
    ) -> Any:
        """``GET /trade/{ticker}/candles`` — paginated OHLCV candle history.

        Candles are aggregated server-side to ``resolution``, so the payload
        stays small regardless of range.

        Args:
            resolution: bucket size — one of ``1m``, ``5m``, ``15m``, ``1h``,
                ``4h``, ``1d``, ``1w`` (:class:`~loaf.enums.CandleResolution`).
            to: only return candles strictly OLDER than this unix-seconds
                timestamp. Omit for the most recent candles.
            count_back: how many candles to return (server default 1000, at
                most 1500).

        Returns ``{resolution, candles, oldestTs, hasMore}`` where ``candles``
        is oldest -> newest, each ``{time, open, high, low, close, volume}``.
        To page back, pass the previous response's ``oldestTs`` as ``to`` while
        ``hasMore`` is true (or use :meth:`iter_candles`).

        Resolutions coarser than ``1m`` may answer 503 ``Candle history is busy.
        Please try again shortly.`` under load. The client retries it (honouring
        ``Retry-After``) up to ``max_retries``, then raises
        :class:`~loaf.exceptions.LoafServiceUnavailableError`.
        """
        _check_market_ticker(ticker)
        return self._client.get(
            f"/trade/{ticker}/candles",
            params={"resolution": str(resolution), "to": to, "countBack": count_back},
            auth=False,
        )

    def iter_candles(
        self, ticker: str, resolution: str, *, page_size: int = 1000
    ) -> Iterator[Any]:
        """Yield every candle for ``ticker``, newest first, paging via ``to``.

        Note the ordering: pages are walked backwards in time and each page is
        yielded newest -> oldest, so the stream is strictly reverse-chronological.
        A page that stays busy (503, see :meth:`candles`) raises
        :class:`~loaf.exceptions.LoafServiceUnavailableError` mid-iteration,
        and a page whose ``oldestTs`` is not before the ``to`` it was asked for
        raises :class:`~loaf.exceptions.LoafError` instead of paging forever.
        """
        to: int | None = None
        while True:
            page = self.candles(ticker, resolution, to=to, count_back=page_size)
            for candle in reversed(page.get("candles") or []):
                yield candle
            if not page.get("hasMore") or page.get("oldestTs") is None:
                return
            if to is not None and not page["oldestTs"] < to:
                raise LoafError(
                    f"GET /trade/{ticker}/candles answered oldestTs {page['oldestTs']} for to={to}; "
                    "stopped paging"
                )
            to = page["oldestTs"]

    # -- Trades (the tape's backfill) -------------------------------------- #

    def trades(self, ticker: str, *, after_trade_id: int = 0, limit: int = 100) -> Any:
        """``GET /trade/{ticker}/trades`` — the market's trades after
        ``after_trade_id``, oldest first: what the ``trades:{ticker}`` channel
        pushed, to fill a gap in it.

        Returns ``{"trades": [...]}``: at most ``limit`` (1-1000) trades whose
        ``tradeId`` is above ``after_trade_id`` (0: from the first), each
        ``{tradeId, marketIndex, txHash, aggressorSide, price, quantity,
        timestamp}`` as the channel pushes it (a perp's liquidation take has
        ``isLiquidation: True`` and no ``txHash``:
        :class:`~loaf.models.TradeTick`). While a page comes back full,
        pass its last ``tradeId`` as ``after_trade_id`` for the next; stop at
        an empty or short page, or one whose last ``tradeId`` is not above the
        ``after_trade_id`` you sent.

        Rows trail the live push by up to 2 s, so after a reconnect read from
        the newest ``tradeId`` you hold 2 s or more after
        :meth:`~loaf.ws.client.LoafWebSocketClient.on_connect` fires, and merge
        them with the frames by ``tradeId``.
        """
        _check_market_ticker(ticker)
        return self._client.get(
            f"/trade/{ticker}/trades",
            params={"afterTradeId": after_trade_id, "limit": limit},
            auth=False,
        )

    # -- Info pages (by the asset's ticker) -------------------------------- #

    def info_header(self, ticker: str) -> Any:
        """``GET /info/{ticker}/header`` — address, hero image, bed/bath/car, offering valuation."""
        _check_asset_ticker(ticker)
        return self._client.get(f"/info/{ticker}/header", auth=False)

    def info_overview(self, ticker: str) -> Any:
        """``GET /info/{ticker}/overview`` — description, media, metrics, amenities."""
        _check_asset_ticker(ticker)
        return self._client.get(f"/info/{ticker}/overview", auth=False)

    def info_documents(self, ticker: str) -> Any:
        """``GET /info/{ticker}/documents`` — public document list (title + URL)."""
        _check_asset_ticker(ticker)
        return self._client.get(f"/info/{ticker}/documents", auth=False)

"""Public market data: properties, candles, trades, info pages.

None of these require authentication — they work with an anonymous client.
All currency values are plain dollars and quantities plain tokens (see
:mod:`loaf.money`).
"""

from __future__ import annotations

from typing import Any, Iterator

from .base import Resource, _check_ticker


class MarketResource(Resource):
    # -- Properties / trade ------------------------------------------------ #

    def properties(self) -> Any:
        """``GET /trade`` — every non-delisted property (PENDING and LIVE) with
        market data + a 24h sparkline.

        While a trading-competition round is PREPARED or ACTIVE
        (``competitionModeActive``), only competition properties are listed, and
        only the round's market has ``hasL2Market``. Outside a round they are
        listed too (``isCompetition`` true) but cannot be traded. A row is
        tradeable when ``status == "LIVE"`` and ``hasL2Market`` (halts, a real
        market's trading hours and round eligibility still apply).

        Returns ``{"properties": [...], "competitionModeActive": bool}``. Each
        item has ``propertyId``, ``ticker`` (the identifier every other call
        takes), ``tokenName`` (the display name), ``contractAddress``,
        ``propertyType``, ``marketPrice``, ``dailyReferencePrice``,
        ``volume24h``, ``status``, ``isCompetition``, ``hasL2Market``,
        ``candlesticks`` (trailing-24h hourly OHLCV sparkline only — use
        :meth:`candles` for real chart history), etc.

        ``contractAddress`` is the property token's address. Orders do not sign
        it, but a real market refuses them while it is ``None`` (the token is
        not deployed yet). ``marketPrice`` is ``0`` when the exchange has no
        reference yet.
        """
        return self._client.get("/trade", auth=False)

    def property(self, ticker: str) -> Any:
        """``GET /trade/{ticker}`` — full detail for one property.

        Returns the ``property``, a ``propertyList`` selector, current
        ``orderBook`` snapshot (``bids``/``asks`` price levels), ``recentTrades``,
        ``volume24h``, ``dailyReferencePrice``, ``liquidity``,
        ``competitionModeActive`` and market-hours metadata (real markets only:
        a competition round keeps no trading hours). ``ticker`` is 1-4 uppercase
        letters or digits (``"OPRA"``, not ``"opra"``); every REST call that
        takes one refuses anything else before sending. Candle history is NOT
        included — fetch it from the dedicated :meth:`candles` endpoint.

        ``property.l2Market`` is ``{chainId, proxyAddress, l2ChainId,
        marketIndex, baseAssetId, quoteAssetId}``, the market every order on the
        property signs (the SDK reads it for you), or ``None``: the property
        cannot be traded now. A real property keeps its own during a
        competition round although only the round's market trades then (orders
        are taken only where ``property.isCompetition ==
        competitionModeActive``). ``property`` has no ``marketPrice``: the
        reference MARKET orders are priced from is the ``propertyList`` entry
        with the same ``ticker``.

        ``recentTrades`` holds the newest trades first (at most 20), each with a
        ``txHash``: the trade's content hash (``0x`` + 64 hex), not an on-chain
        transaction. ``orderBook`` is ``None`` when the property is not LIVE or
        no fresh book is known: treat ``None`` as unknown, not empty.

        ``property.isHalted`` is the effective trading-halt flag (this
        property's own state OR'd with the platform-wide kill switch); it is
        served here but NOT on the :meth:`properties` list. Seed from it, then
        keep it live with the ``property:{ticker}`` WebSocket channel
        (:meth:`loaf.ws.client.LoafWebSocketClient.subscribe_property_status`).
        """
        _check_ticker(ticker)
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
        _check_ticker(ticker)
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
        :class:`~loaf.exceptions.LoafServiceUnavailableError` mid-iteration.
        """
        to: int | None = None
        while True:
            page = self.candles(ticker, resolution, to=to, count_back=page_size)
            for candle in reversed(page.get("candles") or []):
                yield candle
            if not page.get("hasMore") or page.get("oldestTs") is None:
                return
            to = page["oldestTs"]

    # -- Trades (the tape's backfill) -------------------------------------- #

    def trades(self, ticker: str, *, after_trade_id: int = 0, limit: int = 100) -> Any:
        """``GET /trade/{ticker}/trades`` — the property's trades after
        ``after_trade_id``, oldest first: what the ``trades:{ticker}`` channel
        pushed, to fill a gap in it.

        Returns ``{"trades": [...]}``: at most ``limit`` (1-1000) trades whose
        ``tradeId`` is above ``after_trade_id`` (0: from the first), each
        ``{tradeId, propertyId, txHash, aggressorSide, price, quantity,
        timestamp}`` as the channel pushes it. While a page comes back full,
        pass its last ``tradeId`` as ``after_trade_id`` for the next; stop at
        an empty or short page, or one whose last ``tradeId`` is not above the
        ``after_trade_id`` you sent.

        Rows trail the live push by up to 2 s, so after a reconnect read from
        the newest ``tradeId`` you hold 2 s or more after
        :meth:`~loaf.ws.client.LoafWebSocketClient.on_connect` fires, and merge
        them with the frames by ``tradeId``.
        """
        _check_ticker(ticker)
        return self._client.get(
            f"/trade/{ticker}/trades",
            params={"afterTradeId": after_trade_id, "limit": limit},
            auth=False,
        )

    # -- Info pages -------------------------------------------------------- #

    def info_header(self, ticker: str) -> Any:
        """``GET /info/{ticker}/header`` — address, hero image, bed/bath/car, offering valuation."""
        _check_ticker(ticker)
        return self._client.get(f"/info/{ticker}/header", auth=False)

    def info_overview(self, ticker: str) -> Any:
        """``GET /info/{ticker}/overview`` — description, media, metrics, amenities."""
        _check_ticker(ticker)
        return self._client.get(f"/info/{ticker}/overview", auth=False)

    def info_documents(self, ticker: str) -> Any:
        """``GET /info/{ticker}/documents`` — public document list (title + URL)."""
        _check_ticker(ticker)
        return self._client.get(f"/info/{ticker}/documents", auth=False)

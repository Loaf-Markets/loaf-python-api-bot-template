"""Public market data: properties, candles, info pages.

None of these require authentication — they work with an anonymous client.
All currency values are plain dollars and quantities plain tokens (see
:mod:`loaf.money`).
"""

from __future__ import annotations

from typing import Any, Iterator

from .base import Resource


class MarketResource(Resource):
    # -- Properties / trade ------------------------------------------------ #

    def properties(self) -> Any:
        """``GET /trade`` — every non-delisted property (PENDING and LIVE) with
        market data + a 24h sparkline.

        While a trading-competition round is being prepared or is active, only
        competition properties are listed. Outside a round they are listed too
        (``isCompetition`` true) but cannot be traded. A row is tradeable only
        when ``status == "LIVE"``, ``isCompetition == competitionModeActive``
        and ``contractAddress`` is set (halts and market hours still apply).

        Returns ``{"properties": [...], "paymentTokenAddress": "0x...",
        "competitionModeActive": bool}``. Each item has ``propertyId``,
        ``tokenName``, ``assetName``, ``ticker``, ``contractAddress``,
        ``propertyType``, ``marketPrice``, ``dailyReferencePrice``,
        ``volume24h``, ``status``, ``isCompetition``, ``candlesticks``
        (trailing-24h hourly OHLCV sparkline only — use :meth:`candles` for
        real chart history), etc.

        ``contractAddress`` is ``None`` until a property's token is deployed,
        and a competition property gets a new one every round. ``marketPrice``
        is ``0`` when the exchange has no reference yet. ``paymentTokenAddress``
        is informational only.
        """
        return self._client.get("/trade", auth=False)

    def property(self, token_name: str) -> Any:
        """``GET /trade/{token_name}`` — full detail for one property.

        Returns the ``property``, a ``propertyList`` selector, current
        ``orderBook`` snapshot (``bids``/``asks`` price levels), ``recentTrades``,
        ``volume24h``, ``dailyReferencePrice``, ``paymentTokenAddress``,
        ``liquidity``, ``competitionModeActive`` and market-hours metadata.
        ``token_name`` is lowercase letters only. Candle history is NOT included
        — fetch it from the dedicated :meth:`candles` endpoint.

        ``property.contractAddress`` is the token contract orders are signed
        over; the SDK reads it for you. ``property`` has no ``marketPrice``: the
        reference MARKET orders are priced from is the ``propertyList`` entry
        with the same ``tokenName``. ``orderBook`` is ``None`` when the property
        is not LIVE, and can be ``None`` briefly after an exchange restart until
        the book next changes: treat ``None`` as unknown, not empty.

        ``property.isHalted`` is the effective trading-halt flag (this
        property's own state OR'd with the platform-wide kill switch); it is
        served here but NOT on the :meth:`properties` list. Seed from it, then
        keep it live with the ``property:{tokenName}`` WebSocket channel
        (:meth:`loaf.ws.client.LoafWebSocketClient.subscribe_property_status`).
        """
        return self._client.get(f"/trade/{token_name}", auth=False)

    # -- Candles (chart history) -------------------------------------------- #

    def candles(
        self,
        token_name: str,
        resolution: str,
        *,
        to: int | None = None,
        count_back: int | None = None,
    ) -> Any:
        """``GET /trade/{token_name}/candles`` — paginated OHLCV candle history.

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
        return self._client.get(
            f"/trade/{token_name}/candles",
            params={"resolution": str(resolution), "to": to, "countBack": count_back},
            auth=False,
        )

    def iter_candles(
        self, token_name: str, resolution: str, *, page_size: int = 1000
    ) -> Iterator[Any]:
        """Yield every candle for ``token_name``, newest first, paging via ``to``.

        Note the ordering: pages are walked backwards in time and each page is
        yielded newest -> oldest, so the stream is strictly reverse-chronological.
        A page that stays busy (503, see :meth:`candles`) raises
        :class:`~loaf.exceptions.LoafServiceUnavailableError` mid-iteration.
        """
        to: int | None = None
        while True:
            page = self.candles(token_name, resolution, to=to, count_back=page_size)
            for candle in reversed(page.get("candles") or []):
                yield candle
            if not page.get("hasMore") or page.get("oldestTs") is None:
                return
            to = page["oldestTs"]

    # -- Info pages -------------------------------------------------------- #

    def info_header(self, token_name: str) -> Any:
        """``GET /info/{token_name}/header`` — address, hero image, bed/bath/car, offering valuation."""
        return self._client.get(f"/info/{token_name}/header", auth=False)

    def info_overview(self, token_name: str) -> Any:
        """``GET /info/{token_name}/overview`` — description, media, metrics, amenities."""
        return self._client.get(f"/info/{token_name}/overview", auth=False)

    def info_documents(self, token_name: str) -> Any:
        """``GET /info/{token_name}/documents`` — public document list (title + URL)."""
        return self._client.get(f"/info/{token_name}/documents", auth=False)

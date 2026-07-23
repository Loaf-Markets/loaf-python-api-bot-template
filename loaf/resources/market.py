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
        """``GET /trade`` — every LIVE property with market data + a 24h sparkline.

        Returns ``{"properties": [...], "paymentTokenAddress": "0x..."}``. Each
        item has ``propertyId``, ``tokenName``, ``assetName``, ``ticker``,
        ``contractAddress``, ``propertyType``, ``marketPrice``,
        ``dailyReferencePrice``, ``volume24h``, ``status``, ``candlesticks``
        (trailing-24h hourly OHLCV sparkline only — use :meth:`candles` for
        real chart history), etc. Delisted properties are excluded.
        """
        return self._client.get("/trade", auth=False)

    def property(self, token_name: str) -> Any:
        """``GET /trade/{token_name}`` — full detail for one property.

        Returns the ``property``, a ``propertyList`` selector, current
        ``orderBook`` snapshot (``bids``/``asks`` price levels, may be ``null``),
        ``recentTrades``, ``volume24h``, ``dailyReferencePrice``,
        ``paymentTokenAddress``, ``maxSlippageBps`` and market-hours metadata.
        ``token_name`` is lowercase letters only. Candle history is NOT included
        — fetch it from the dedicated :meth:`candles` endpoint.
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
            count_back: how many candles to return (server default 1000).

        Returns ``{resolution, candles, oldestTs, hasMore}`` where ``candles``
        is oldest -> newest, each ``{time, open, high, low, close, volume}``.
        To page back, pass the previous response's ``oldestTs`` as ``to`` while
        ``hasMore`` is true (or use :meth:`iter_candles`).
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

    def info_documents_private(
        self, token_name: str, password: str, *, lang: str | None = None
    ) -> Any:
        """``GET /info/{token_name}/documents/private/{password}`` — password-gated documents.

        Args:
            token_name: lowercase property token symbol.
            password: the document access password (sent as a path segment,
                not as an API key — this is unrelated to ``LOAF_API_KEY``).
            lang: optional document language, ``"en"`` (default) or ``"cn"``.

        Returns the same shape as :meth:`info_documents` for the private
        document set. Raises :class:`~loaf.exceptions.LoafNotFoundError` (404)
        or :class:`~loaf.exceptions.LoafAuthError`-style rejection if the
        password is wrong, depending on how the backend reports it.
        """
        return self._client.get(
            f"/info/{token_name}/documents/private/{password}",
            params={"lang": lang},
            auth=False,
        )

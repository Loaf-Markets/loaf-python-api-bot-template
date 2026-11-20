"""Public market data: markets, candles, trades.

None of these require authentication — they work with an anonymous client.
All currency values are plain dollars and quantities plain tokens (see
:mod:`loaf.money`); a perp's live state and order limits are exact
:class:`~decimal.Decimal` values (:meth:`MarketResource.get`).

A market is one order book, spot or perp, named by its ticker: its asset's
ticker, ``-PERP`` appended for a perp (``"MUS"``, ``"H100-PERP"``). Every call
that takes a ticker refuses a malformed one (``"mus"``, say) before sending.
"""

from __future__ import annotations

from typing import Any, Iterator

from .. import _served
from ..exceptions import LoafError, LoafServerError
from .base import Resource, _check_market_ticker


def _unusable(path: str, what: str, page: Any) -> LoafServerError:
    """The error of a candle page that cannot be paged on."""
    return LoafServerError(
        f"GET {path} answered {what}; stopped paging", status_code=200, body=page
    )


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
        ``hasL2Market``, ``priceDecimals``, ``quantityDecimals`` and
        ``minOrderValue`` (as :meth:`get`'s ``market``), ``candlesticks``
        (trailing-24h hourly OHLCV sparkline only — use :meth:`candles` for
        real chart history), ``property`` (a property's address, type, supply
        and yield; ``None`` for any other kind), etc.

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

        Returns the ``market``, its ``asset``, a ``marketList`` selector (each
        row's ``marketIndex``, ``ticker``, ``type``, ``status``,
        ``marketPrice``, ``priceDecimals`` and ``quantityDecimals``, and a
        perp's ``maxLeverage`` and ``live`` figures,
        ``{markPrice, openInterest, fundingRateCbp, fundingPeriodMs}`` as in
        :class:`~loaf.models.PerpsMarketLive`, both ``None`` on spot: it maps
        a market index to its ticker. An offering's row, ``type`` ``IPO``,
        stands in for its asset's spot market while that is missing or
        ``PENDING``, with that market's index or ``None``), the current
        ``orderBook`` snapshot (``bids``/``asks`` price levels, the best 200
        a side),
        ``recentTrades``, ``volume24h``, ``dailyReferencePrice`` and
        ``liquidity``. Candle history is NOT included — fetch it from the
        dedicated :meth:`candles` endpoint.

        ``market`` is ``{marketIndex, ticker, type, status, isHalted,
        tradingStartsAt, priceDecimals, quantityDecimals, minOrderValue,
        l2}``, and a perp's ``live``. ``priceDecimals`` and
        ``quantityDecimals``, set at the market's listing, are its tick and
        lot: a price on it has at most ``priceDecimals`` decimal places (a
        tick of 10^-priceDecimals dollars), a quantity at most
        ``quantityDecimals`` (a lot of 10^-quantityDecimals tokens), and the
        SDK refuses an order off them (:mod:`loaf.money`). ``minOrderValue``
        is the least price x quantity of an order, dollars (10), which a full
        close (a SELL of your whole holding) need not reach.
        ``market.l2`` is, by ``type``, a spot market's ``{chainId,
        proxyAddress, l2ChainId, baseAssetId, quoteAssetId,
        signedPriceDecimals, signedQuantityDecimals}``
        (:class:`~loaf.models.MarketL2`: an order signs price x
        10^signedPriceDecimals and quantity x 10^signedQuantityDecimals), or a
        perp's, with its ``status`` and
        terms and no base asset (:class:`~loaf.models.PerpMarketL2`, its order
        limits ``minBase``, tokens, and ``minQuote`` and ``quoteLimit``,
        dollars, exact Decimals): with ``marketIndex``, what every order
        on the market signs (the SDK reads both for you), or ``None``: the
        market cannot be traded now. ``asset`` is ``{ticker, name, kind,
        videoUrl, stakeholders, property}``:
        ``property`` is a property's own facts (``None`` for any other kind). A
        spot market and the perp of one asset carry the same ``asset``. The
        detail has no ``marketPrice``: a spot market's MARKET orders are priced
        from the ``marketList`` entry with the same ``ticker``, a perp's from
        its live mark (``market.live.markPrice``; that entry while the mark is
        0 or not served, ``live`` null).

        The market trades while its ``status`` is ``LIVE``, it is not
        ``isHalted``, its ``l2`` is not ``None`` and its ``tradingStartsAt``
        (unix seconds) has passed (``None``: no wait): orders before then are
        refused (403 ``Trading on this market has not started yet``). A perp
        whose ``l2.status`` is ``EXPIRED`` takes no more orders.

        ``market.live``, a perp's alone, is its live state
        (:class:`~loaf.models.PerpsMarketLive`), the engine's, except that the
        exchange reads an EXPIRED perp's ``tradingState`` as ``HALTED`` and its
        ``stateReason`` as ``MARKET_CLOSED``; ``None`` until the exchange has
        read it (a stalled engine never delays this read: it serves its last
        answer, so bound the age by ``asOfMs``):

        * ``status`` — ``ACTIVE``, or ``EXPIRED``: it takes no orders, and its
          positions leave by exits;
        * ``tradingState`` — ``OPEN``, ``REDUCE_ONLY``, ``PRICE_STALE`` or
          ``HALTED``, least to most restrictive — and ``stateReason``, why it
          refuses an order that adds risk (``None`` while it admits one); both
          ``None`` when the engine holds no book of the market;
        * ``halted``, the engine's trading pause (every new order refused),
          and ``riskOnly``, its risk-only mode: whatever ``tradingState`` says,
          every order that adds risk, and every withdrawal, is answered
          unavailable, so an ``OPEN`` market admits only an order that reduces;
        * ``asOfMs``, the engine clock those four are as of: bound their age
          by it;
        * ``markPrice``, what your positions are valued at, margin is checked
          at, stops and takes fire on and the market's band (``l2.band``)
          centres on (within 2 % of the index; 0 before the first report, when
          no band applies), and ``indexPrice``, the oracle's accepted index
          (0 before the first report), as of ``observedAtMs``, both dollars,
          exact Decimals that can fall between ticks (to
          ``l2.signedPriceDecimals`` places); ``openInterest``, tokens, a
          Decimal (K1's two-sided sum: longs plus shorts, so one side's is
          half); and ``fundingIndex``, F, the engine's integer of extended USDC
          units per lot, a decimal string: a position owes its lots, base x
          10^``quantityDecimals``, x (F - its ``fundingIndex``), signed;
        * ``nextTickAtMs``, when the next funding tick is due (0: none, an
          expired or unpriced market), and ``fundingPeriodMs``, the period
          ``fundingRateCbp`` (centi-bp of the index: funding is paid on the
          index) is per; it is what a tick would apply now, ``None`` when the
          market is not rated;
        * ``heldPrice``, ``{price, observedAtMs}``: the newest price report
          (dollars, a Decimal), held for an operator's answer, while no order
          that can add to a position is admitted; ``None`` when there is none.

        The ``perps:markets`` channel pushes every perp's row about once a
        second (:meth:`~loaf.ws.client.LoafWebSocketClient.on_perps_markets_live`),
        and its terms when they change
        (:meth:`~loaf.ws.client.LoafWebSocketClient.on_market_terms`).

        ``recentTrades`` holds the newest trades first (at most 20), with no
        ``txHash``: :meth:`trades` and the ``trades_batch`` frames carry it (a
        perp's liquidation take has ``isLiquidation: True``:
        :class:`~loaf.models.TradeTick`). ``orderBook`` is
        ``None`` when the market is not LIVE or no fresh book is known: treat
        ``None`` as unknown, not empty.

        ``market.isHalted`` is the effective trading-halt flag (this market's
        own state OR'd with the platform-wide kill switch), as a :meth:`list`
        row's ``isHalted``. Seed from it, then keep it live with the
        ``market:{ticker}`` WebSocket channel
        (:meth:`loaf.ws.client.LoafWebSocketClient.subscribe_market_status`).

        The exchange serves each of the Decimals above as an exact decimal
        string; one in any other form raises
        :class:`~loaf.exceptions.LoafServerError` naming it, never read in
        another unit.
        """
        _check_market_ticker(ticker)
        detail = self._client.get(f"/trade/{ticker}", auth=False)
        return _served.read(detail, _served.market_detail, f"GET /trade/{ticker}")

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
            count_back: the most candles to return (server default 1000, at
                most 1500). A page coarser than ``1m`` can hold fewer while
                ``hasMore`` is true: page on from its ``oldestTs``.

        Returns ``{resolution, candles, oldestTs, hasMore}`` where ``candles``
        is oldest -> newest, each ``{time, open, high, low, close, volume}``.
        To page back, pass the previous response's ``oldestTs`` as ``to`` while
        ``hasMore`` is true (or use :meth:`iter_candles`).

        Resolutions coarser than ``1m`` may answer 503 ``Candle history is busy.
        Please try again shortly.`` under load. The client retries it up to
        ``max_retries`` (after its ``Retry-After`` when that is at most
        :data:`~loaf.constants.MAX_HINTED_WAIT`, 10 s), then raises
        :class:`~loaf.exceptions.LoafServiceUnavailableError`; at once when its
        ``Retry-After`` asks for longer, with those seconds as its
        ``retry_after``.
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
        :class:`~loaf.exceptions.LoafServiceUnavailableError` mid-iteration
        (its ``retry_after`` the answer's ``Retry-After``), and a page whose
        ``oldestTs`` is not before the ``to`` it was asked for raises
        :class:`~loaf.exceptions.LoafError` instead of paging forever. A
        page that cannot be paged on raises
        :class:`~loaf.exceptions.LoafServerError`: not an object, its
        ``candles`` not a list, a ``hasMore`` that is not a bool (or null), or
        ``hasMore`` true with no candles or an ``oldestTs`` that is not a whole
        number.
        """
        path = f"/trade/{ticker}/candles"
        to: int | None = None
        while True:
            page = self.candles(ticker, resolution, to=to, count_back=page_size)
            if not isinstance(page, dict):
                raise _unusable(path, "a page that is not an object", page)
            candles = page.get("candles")
            if not isinstance(candles, list):
                raise _unusable(path, "a page whose candles is not a list", page)
            more = page.get("hasMore")
            if more is not None and not isinstance(more, bool):
                raise _unusable(path, f"a hasMore that is not a bool ({more!r:.40})", page)
            yield from reversed(candles)
            if not more:
                return
            oldest = page.get("oldestTs")
            if not isinstance(oldest, int) or isinstance(oldest, bool):
                shown = f"{oldest!r:.40}"
                raise _unusable(path, f"an oldestTs that is not a whole number ({shown})", page)
            if not candles:
                raise _unusable(path, "an empty page with hasMore", page)
            if to is not None and not oldest < to:
                raise LoafError(
                    f"GET {path} answered oldestTs {oldest} for to={to}; stopped paging"
                )
            to = oldest

    # -- Trades (the tape's backfill) -------------------------------------- #

    def trades(self, ticker: str, *, after_trade_id: int = 0, limit: int = 100) -> Any:
        """``GET /trade/{ticker}/trades`` — the market's trades after
        ``after_trade_id``, oldest first: what the ``trades:{ticker}`` channel
        pushed, to fill a gap in it.

        Returns ``{"trades": [...]}``: at most ``limit`` (1-1000) trades whose
        ``tradeId`` is above ``after_trade_id`` (0: from the first), each
        ``{tradeId, txHash, aggressorSide, price, quantity, timestamp}`` as
        the channel pushes it (a perp's liquidation take has
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

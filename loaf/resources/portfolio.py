"""Portfolio: what you hold, in one read, and your recent activity, in another.

:meth:`PortfolioResource.summary` is your balances, holdings and perps. The
recent activity lists of :meth:`PortfolioResource.component` are capped:
``openOrders`` at 200 (booked orders + ``PENDING`` / ``ARMED`` conditionals),
``tradeHistory``, ``orderHistory`` and ``fundingHistory`` at 20,
``offeringOrders`` at 50, and ``transfers`` at 50. Use
:mod:`loaf.resources.history` for deeper, paginated history.
:meth:`PortfolioResource.set_perps_leverage` saves your leverage on a perp.
"""

from __future__ import annotations

import numbers
from typing import Any, Mapping

from .._l2 import _whole
from ..constants import MAX_PERP_MARKET_INDEX, MAX_PERPS_LEVERAGE
from ..exceptions import LoafServerError, _client_validation_error
from .base import Resource, _check_market_ticker
from .perps import _ranged


class PortfolioResource(Resource):
    def summary(self) -> Any:
        """``GET /portfolio/summary`` — what you hold.

        Returns ``{balances, holdings, perps}``
        (:class:`~loaf.models.PortfolioSummary`).

        ``balances`` is your whole L2 book, one row per asset with a balance or
        a hold: ``{assetId, symbol, decimals, isQuote, total, hold,
        available}``. The amounts are decimal strings in the token's base
        units: ``loaf.from_base_units(row.available, row.decimals)`` is the
        number of tokens. The ``isQuote`` rows are your cash, and a missing row
        means 0. A spot market's token row is its
        ``market.get(ticker).market.l2.baseAssetId``, its cash row the
        ``quoteAssetId``; ``available`` is what a new order can use.

        ``holdings`` are the tokens you hold, facts only: ``ticker`` (the
        asset's, its spot market's too), ``marketIndex`` (its spot market's,
        which ``mark_price`` frames name; ``None`` without one),
        ``averageEntryPrice`` (``None`` when the cost is unknown),
        ``marketPrice`` (the mark when read: its spot market's for an L2
        holding; an IPO allocation's unit price until it trades) and
        ``isIpoAllocation``, plus ``assetId`` (an L2 holding: its amounts are
        its ``balances`` row) or ``quantity`` (an IPO allocation). Its name,
        supply and image are :meth:`component`'s ``holdingDetails``. Values and
        P&L are yours to compute: to value a
        holding live, match ``mark_price`` frames to it by ``marketIndex``
        (:meth:`loaf.ws.client.LoafWebSocketClient.on_mark_price`).

        ``perps`` is :meth:`loaf.resources.perps.PerpsResource.positions`'
        answer, ``None`` when the exchange could not read it just now, which
        never means you hold none.

        Raises :class:`~loaf.exceptions.LoafServiceUnavailableError` (503
        ``The L2 balance is temporarily unavailable; try again in a moment``)
        while the exchange's balances cannot be read — transient, so retry.
        """
        return self._client.get("/portfolio/summary")

    def set_perps_leverage(self, market: int | str, leverage: int) -> Any:
        """``PUT /portfolio/perps-leverage`` — save your leverage on a perp,
        named by its ``marketIndex`` (0 to 254) or by its ticker (``"MUS-PERP"``,
        whose ``marketIndex`` :meth:`~loaf.resources.market.MarketResource.get`
        reads first).

        ``leverage`` is a whole number from 1 to the market's ``maxLeverage``,
        which the exchange checks. It keeps one a market
        for your account and answers it, ``{marketIndex, leverage}``
        (:class:`~loaf.models.PerpsLeverage`); :meth:`component` lists them as
        ``perpsLeverage``, and your ``portfolio`` channel brings each one saved
        (:meth:`~loaf.ws.client.LoafWebSocketClient.on_perps_leverage`).

        It is the web app's leverage setting, as on Hyperliquid: the web app
        sizes an order's margin by it, and shows a position's margin (its
        notional / leverage) and ROE (its unrealized PnL / (entry notional /
        leverage)) by it. The exchange's margin checks never read it: they use
        the market's terms (its ``risk``, any initial-margin tiers, and a tenth
        of a pool's notional when collateral leaves it), so no order or margin
        move of this SDK reads it.

        Raises:
            LoafConfigError: no trading key.
            LoafValidationError: refused locally (``status_code`` 0, nothing
                sent): a leverage or market index out of range, or a spot
                market's ticker; or by the exchange (400), saving nothing:
                ``perps: marketIndex is malformed`` (no perp at that
                index) or ``perps: leverage is malformed`` (above the market's
                most).
            LoafNotFoundError: no market has this ticker.
            LoafAPIError: the exchange refused it otherwise. Nothing was saved.
        """
        whole = isinstance(leverage, numbers.Integral) and not isinstance(leverage, bool)
        if not whole or not 1 <= int(leverage) <= MAX_PERPS_LEVERAGE:
            raise _client_validation_error(
                f"leverage must be a whole number from 1 to {MAX_PERPS_LEVERAGE:,}, and the exchange "
                f"takes at most the market's most, 65,536 // (2 x risk) (got {leverage!r})"
            )
        if isinstance(market, str):
            index = self._perps_index(market)
        else:
            index = _ranged(market, "market", 0, MAX_PERP_MARKET_INDEX, "a perps market index")
        return self._client.request(
            "PUT",
            "/portfolio/perps-leverage",
            json={"marketIndex": index, "leverage": int(leverage)},
        )

    def _perps_index(self, ticker: str) -> int:
        """A perp's ``marketIndex``, as its ``GET /trade/{ticker}`` serves it."""
        _check_market_ticker(ticker)
        if not ticker.endswith("-PERP"):
            raise _client_validation_error(
                f"{ticker} is a spot market: a leverage is saved on a perp, whose ticker ends in -PERP"
            )
        detail = self._client.market.get(ticker)
        market = detail.get("market") if isinstance(detail, Mapping) else None
        index = market.get("marketIndex") if isinstance(market, Mapping) else None
        if not _whole(index, 0, MAX_PERP_MARKET_INDEX):
            raise LoafServerError(
                f"GET /trade/{ticker} answered an unusable market.marketIndex",
                status_code=200,
                body=detail,
            )
        return index

    def component(self) -> Any:
        """``GET /portfolio/component`` — your recent activity, fees, lifetime
        volume, holdings' details and saved leverage, nothing :meth:`summary`
        carries.

        Returns ``{lifetimeVolume, applicableFees, offeringOrders, openOrders,
        tradeHistory, orderHistory, transfers, fundingHistory, holdingDetails,
        holdingSparklines, perpsLeverage}``
        (:class:`~loaf.models.PortfolioComponent`).

        ``applicableFees`` is raw bps, and streams on the private WS channel as
        ``fees_update``; ``lifetimeVolume`` (dollars) also streams on the
        private WS channel as ``lifetime_volume_update``.

        ``openOrders`` interleaves live booked orders with your ``PENDING`` /
        ``ARMED`` conditionals, newest first, before the list is capped, each
        row with its ``pool`` and ``reduceOnly`` on a perp. ``tradeHistory`` is
        every fill of yours
        (:meth:`loaf.resources.history.HistoryResource.trades`, without
        ``proof``). ``transfers`` are your deposits and withdrawals, newest
        first, keyed by ``journalSeq``, each ``amount`` in base units.
        ``fundingHistory`` is your funding payments
        (:meth:`loaf.resources.history.HistoryResource.funding`).
        ``holdingDetails`` is each holding's ``{name, totalTokens, imageUrl}``
        by its ticker (``totalTokens``: a property's supply; ``None`` for any
        other kind). ``holdingSparklines`` is each holding's recent closes by
        its ticker: up to 48, oldest first (draw its entry price, then them,
        then its mark).

        ``perpsLeverage`` is the leverage you saved on each perp
        (:meth:`set_perps_leverage`), ``{marketIndex, leverage}``. A market you
        saved none on is at :data:`~loaf.constants.PERPS_DEFAULT_LEVERAGE` (3),
        and any leverage reads at most the market's ``maxLeverage``.
        """
        return self._client.get("/portfolio/component")

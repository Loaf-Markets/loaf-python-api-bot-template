"""Portfolio: balances, holdings and recent activity, in one read.

The recent activity lists embedded here are capped: ``openOrders`` at 100
(booked orders + ``PENDING`` / ``ARMED`` conditionals), ``tradeHistory`` and
``orderHistory`` at 20, ``offeringOrders`` at 50, and ``transfers`` at 50. Use
:mod:`loaf.resources.history` for deeper, paginated history.
"""

from __future__ import annotations

from typing import Any

from .base import Resource


class PortfolioResource(Resource):
    def component(self) -> Any:
        """``GET /portfolio/component`` — the assembled portfolio.

        Returns ``{balances, holdings, lifetimeVolume, applicableFees,
        offeringOrders, openOrders, tradeHistory, orderHistory, transfers}``.

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
        which ``mark_price`` frames name; ``None`` without one), ``name``,
        ``kind``, ``totalTokens`` (a property's supply; ``None`` for any other
        kind), ``averageEntryPrice`` (``None`` when the cost is unknown),
        ``marketPrice`` (the mark when read: its spot market's for an L2
        holding; an IPO allocation's unit price until it trades),
        ``sparkline``, ``imageUrl`` and ``isIpoAllocation``, plus ``assetId``
        (an L2 holding: its amounts are its ``balances`` row) or ``quantity``
        (an IPO allocation). Values and P&L are yours to compute: to value a
        holding live, match ``mark_price`` frames to it by ``marketIndex``
        (:meth:`loaf.ws.client.LoafWebSocketClient.on_mark_price`).
        ``applicableFees`` is raw bps; ``lifetimeVolume`` (dollars) also streams
        on the private WS channel as ``lifetime_volume_update``.

        ``openOrders`` interleaves live booked orders with your ``PENDING`` /
        ``ARMED`` conditionals, newest first, before the list is capped.
        ``tradeHistory`` rows carry ``txHash`` (the trade's content hash) and
        ``proof`` (see :meth:`loaf.resources.history.HistoryResource.trades`).
        ``transfers`` are your deposits and withdrawals, newest first, keyed by
        ``journalSeq``, each ``amount`` in base units.

        Raises :class:`~loaf.exceptions.LoafServiceUnavailableError` (503
        ``The L2 balance is temporarily unavailable; try again in a moment``)
        while the exchange's balances cannot be read — transient, so retry.
        """
        return self._client.get("/portfolio/component")

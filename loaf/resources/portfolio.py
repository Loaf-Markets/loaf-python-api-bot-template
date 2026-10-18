"""Portfolio: the account's L2 book, holdings and recent activity.

The recent activity lists embedded here are capped: ``openOrders`` at 100 (booked orders and
``PENDING`` / ``ARMED`` conditionals), ``tradeHistory`` and ``orderHistory`` at 20, and
``offeringOrders`` and ``transfers`` at 50. Use :mod:`loaf.resources.history` for deeper,
paginated history.
"""

from __future__ import annotations

from typing import Any

from .base import Resource


class PortfolioResource(Resource):
    def component(self) -> Any:
        """``GET /portfolio/component``: the assembled portfolio (:class:`~loaf.models.PortfolioComponent`).

        * ``balances``: the account's L2 book, one row per asset in ascending ``assetId``, with
          ``symbol``, ``decimals``, ``isQuote`` (the cash) and ``total`` / ``hold`` /
          ``available`` in the token's base units as decimal strings (USDC: 6 decimals). The
          engine admits an order on ``available``.
        * ``positions``: each held property's facts (``ticker``, ``averageEntryPrice``,
          ``marketPrice``, ``sparkline``); an L2 holding's amounts are the ``balances`` row of its
          ``assetId``, an IPO allocation not yet in the L2 book carries its ``quantity``.
        * ``lifetimeVolume``, ``applicableFees`` (raw bps), and recent ``offeringOrders``,
          ``openOrders`` (booked orders and your ``PENDING`` / ``ARMED`` conditionals), ``tradeHistory``
          (each fill with its ``proof``), ``orderHistory`` and ``transfers`` (L2 deposits and withdrawals).

        ``balances`` and ``lifetimeVolume`` stream on the private WebSocket channel as
        ``balances_update`` and ``lifetime_volume_update``.
        """
        return self._client.get("/portfolio/component")

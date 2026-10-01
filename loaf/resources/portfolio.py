"""Portfolio: balances, positions, and PnL.

All currency values are plain dollars and quantities plain tokens. The recent
activity lists embedded here are capped: ``openOrders`` at 100 (booked orders +
``PENDING`` / ``ARMED`` conditionals), ``tradeHistory`` and ``orderHistory`` at
20, ``offeringOrders`` at 50, and ``transfers`` at 50 deposits + 50
withdrawals. Use :mod:`loaf.resources.history` for deeper, paginated history.
"""

from __future__ import annotations

from typing import Any

from .base import Resource


class PortfolioResource(Resource):
    def get(self) -> Any:
        """``GET /portfolio`` — the portfolio page payload: ``{"component": {...}}``.

        ``component`` is identical to :meth:`component`. Prefer :meth:`component`
        unless you specifically want the page wrapper.
        """
        return self._client.get("/portfolio")

    def component(self) -> Any:
        """``GET /portfolio/component`` — the assembled portfolio.

        Returns ``cash``, ``frozen``, ``portfolioValue``, ``portfolioPnl``,
        ``portfolioPnlPercent``, ``lifetimeVolume``, ``positions`` (per-property
        quantity / avg entry / market price / PnL), ``applicableFees`` (raw
        bps), and recent ``offeringOrders`` / ``openOrders`` / ``tradeHistory``
        / ``orderHistory`` / ``transfers``. ``lifetimeVolume`` also streams on
        the private WS channel as ``lifetime_volume_update``.

        ``openOrders`` interleaves live booked orders with your ``PENDING`` /
        ``ARMED`` conditionals, newest first, before the list is capped.

        On ``tradeHistory`` rows ``txHash`` is ``""`` until a batch proof covers
        the trade. Cash, frozen, positions and ``lifetimeVolume`` move when a
        trade matches.
        """
        return self._client.get("/portfolio/component")

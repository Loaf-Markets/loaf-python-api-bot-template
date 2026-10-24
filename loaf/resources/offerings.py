"""Primary market (IPO offerings): browse offerings (public).

Subscribing is not available through the API: each purchase is signed by your
account wallet (the purchase terms and a payment-token permit), which neither
the API key nor the trading key can do — subscribe in the web app.
"""

from __future__ import annotations

from typing import Any

from .base import Resource, _check_ticker


class OfferingsResource(Resource):
    def list(self) -> Any:
        """``GET /offerings`` — every visible offering as a card. (public)

        Returns ``{"ipos": [...]}`` with ``ipoId``, ``ticker``, ``tokenName``
        (the display name), ``unitPrice``, ``totalUnits``, ``unitsAllocated``,
        ``status``, ``opensAt``/``closesAt``, etc.
        """
        return self._client.get("/offerings", auth=False)

    def get(self, ticker: str) -> Any:
        """``GET /offerings/{ticker}`` — full offering page for a property. (public)

        Includes pricing, ``feeBps`` (raw basis points), allocation stats,
        ``recentOrders`` and an ``ipoList`` selector.
        """
        _check_ticker(ticker)
        return self._client.get(f"/offerings/{ticker}", auth=False)

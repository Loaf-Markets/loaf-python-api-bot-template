"""Primary market (IPO offerings): browse, subscribe, pre-approve.

Browsing is public. Subscribing requires wholesale-investor verification (with
two bypasses: private clients, and orders whose notional is >= $500,000, which
still need retail KYC).
"""

from __future__ import annotations

from typing import Any

from .base import Resource


class OfferingsResource(Resource):
    def list(self) -> Any:
        """``GET /offerings`` — every visible offering as a card. (public)

        Returns ``{"ipos": [...]}`` with ``ipoId``, ``tokenName``, ``unitPrice``,
        ``totalUnits``, ``unitsAllocated``, ``status``, ``opensAt``/``closesAt``, etc.
        """
        return self._client.get("/offerings", auth=False)

    def get(self, token_name: str) -> Any:
        """``GET /offerings/{token_name}`` — full offering page for a property. (public)

        Includes pricing, ``feeBps`` (raw basis points), allocation stats,
        ``recentOrders`` and an ``ipoList`` selector.
        """
        return self._client.get(f"/offerings/{token_name}", auth=False)

    def subscribe(
        self,
        ipo_id: int,
        quantity: int,
        *,
        allow_partial: bool = True,
    ) -> Any:
        """``POST /offerings/subscribe`` — subscribe to (buy into) an offering.

        Args:
            ipo_id: the offering id (``ipoId``).
            quantity: whole units to subscribe for (integer; fractional rejected).
            allow_partial: if ``True`` (default) and the offering is near full,
                proceed with the clamped (smaller) amount; if ``False``, a
                shortfall raises a 400.

        Returns ``{success, subscriptionId, allocatedQuantity}``. The HTTP call
        returns as soon as the PENDING order is created; settlement is async —
        track the final outcome (``ALLOCATED`` / ``REJECTED``) on the private
        ``portfolio`` WebSocket channel (``offering_order_update``), keyed by
        ``subscriptionId``.
        """
        body = {
            "ipoId": int(ipo_id),
            "quantity": int(quantity),
            "allowPartial": allow_partial,
        }
        return self._client.post("/offerings/subscribe", json=body)

    def approve(self, ipo_id: int) -> Any:
        """``POST /offerings/approve`` — pre-grant the payment-token allowance.

        Idempotent. Returns ``{approved: true, alreadyApproved: bool}``.
        """
        return self._client.post("/offerings/approve", json={"ipoId": int(ipo_id)})

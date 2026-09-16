"""Paginated order & trade history.

``orders`` and ``trades`` support both offset pagination (``page`` /
``page_size``) and opaque keyset pagination (``cursor``). Prefer the cursor:
take ``nextCursor`` from one response and pass it as ``cursor`` to the next.
Do not derive "has more" from ``total`` (it is capped at 10000 for orders).
"""

from __future__ import annotations

from typing import Any, Iterator

from .base import Resource


class HistoryResource(Resource):
    def orders(
        self,
        *,
        page: int | None = None,
        page_size: int | None = None,
        cursor: str | None = None,
    ) -> Any:
        """``GET /history/orders`` — paginated order history (all statuses), newest first.

        Returns ``{orders, total, page, pageSize, nextCursor}``. ``page_size``
        max is 100 (default 20).

        Conditionals appear here in EVERY status, including the live
        ``PENDING`` / ``ARMED`` ones, so a resting stop shows up in history and
        in ``openOrders`` at once. ``total`` now counts both tables and only
        one is capped — another reason not to derive "has more" from it.
        """
        return self._client.get(
            "/history/orders",
            params={"page": page, "pageSize": page_size, "cursor": cursor},
        )

    def trades(
        self,
        *,
        page: int | None = None,
        page_size: int | None = None,
        cursor: str | None = None,
    ) -> Any:
        """``GET /history/trades`` — paginated executed-trade history, newest first.

        Returns ``{trades, total, page, pageSize, nextCursor}``. ``side`` and
        ``fee`` are reported from this user's perspective.
        """
        return self._client.get(
            "/history/trades",
            params={"page": page, "pageSize": page_size, "cursor": cursor},
        )

    def cancelled_orders(self) -> Any:
        """``GET /history/orders/cancelled`` — all cancel events, newest first (not paginated).

        Returns ``{"cancelledOrders": [{orderId, cancelledAt}]}``. Cancelled
        BOOKED orders only; a cancelled conditional never appears here.
        """
        return self._client.get("/history/orders/cancelled")

    def active_orders(self) -> Any:
        """``GET /history/orders/active`` — currently open / partially-filled orders.

        Returns ``{"activeOrders": [{orderId, quantityLeft, createdAt}]}`` where
        ``quantityLeft`` is the remaining quantity in tokens.

        Booked orders only, and these rows carry no ``type`` to narrow on, so do
        not use this as your "what is live" source once you place stops — use
        ``portfolio.component().openOrders``.
        """
        return self._client.get("/history/orders/active")

    # -- Convenience: iterate every page transparently -------------------- #

    def iter_orders(self, *, page_size: int = 100) -> Iterator[Any]:
        """Yield every order across all pages using cursor pagination."""
        yield from self._iter("/history/orders", "orders", page_size)

    def iter_trades(self, *, page_size: int = 100) -> Iterator[Any]:
        """Yield every trade across all pages using cursor pagination."""
        yield from self._iter("/history/trades", "trades", page_size)

    def _iter(self, path: str, key: str, page_size: int) -> Iterator[Any]:
        cursor: str | None = None
        while True:
            page = self._client.get(path, params={"pageSize": page_size, "cursor": cursor})
            for item in page.get(key, []) or []:
                yield item
            cursor = page.get("nextCursor")
            if not cursor:
                return

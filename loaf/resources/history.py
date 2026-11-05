"""Paginated order & trade history.

``orders`` and ``trades`` support both offset pagination (``page`` /
``page_size``) and opaque keyset pagination (``cursor``). Prefer the cursor:
take ``nextCursor`` from one response and pass it as ``cursor`` to the next.
Pass back only ``nextCursor`` values: a malformed cursor silently returns the
first page. Do not derive "has more" from ``total`` (it is capped at 10000, for
trades too). ``funding`` pages by cursor alone.
"""

from __future__ import annotations

from typing import Any, Iterator

from ..exceptions import LoafError
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
        in ``openOrders`` at once. ``total`` counts both tables and only
        one is capped — another reason not to derive "has more" from it.

        Rows carry ``expiryMs`` (unix ms, the signed expiry): a booked order
        rests until ``expiryMs`` minus 72 h, and the exchange then cancels it; a
        stop or take that fires after then FAILS. ``filledQuantity`` is an
        unrounded float difference (0.2 tokens can read
        ``0.19999999999999998``): compare it with a tolerance, and rely on
        ``status`` for "filled".

        A MARKET order's ``price`` is the worst price it was signed at, and a
        resting MARKET remainder shows as ``OPEN`` / ``PARTIALLY_FILLED``. To
        find the order an :class:`~loaf.exceptions.OrderOutcomeUnknownError`
        was about, match side, price, quantity and ``expiryMs ==
        error.signed_body["l2ExpiryMs"]``.
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

        Every fill of yours, each row a :class:`~loaf.models.TradeHistoryItem`:
        ``source`` is ``TRADE`` (your order matched) or, on a perp,
        ``LIQUIDATION``, ``BACKSTOP_LIQUIDATION``, ``DELEVERAGE``, ``EXIT``,
        ``SETTLE``, ``FORCED`` or ``FORCED_DELEVERAGE``: no trade, so its
        ``txHash`` is ``None``, and its ``orderId`` too unless it filled your
        resting order. ``tradeId`` is always the row's journal seq, a trade's
        id or the perps row's: a deleverage between two of your pools is two
        rows of one ``tradeId``, one each side, so key a row by ``(tradeId,
        side)``. ``role`` off a trade is ``TAKER`` for the pool the row closed,
        ``MAKER`` for the other side (your resting order, a deleverage's
        counterparty, the backstop). ``direction`` is ``BUY`` / ``SELL`` on
        spot, ``OPEN_LONG`` / ``CLOSE_LONG`` / ``OPEN_SHORT`` / ``CLOSE_SHORT``
        on a perp; ``pool`` is a perps fill's margin pool (``None`` on spot);
        ``closedPnl`` is dollars, signed, before fees, ``None`` unless the fill
        reduced a position.

        Returns ``{trades, total, page, pageSize, nextCursor}``. ``side``,
        ``fee`` (dollars, to 0.001), ``orderId`` (your order the fill belongs
        to) and ``role`` (``"MAKER"`` or ``"TAKER"``) are reported from this
        user's perspective.

        Trades are final when they match. ``txHash`` is the trade's content
        hash (``0x`` + 64 hex, never ``""``; ``None`` on a row with no trade),
        not an on-chain transaction. ``proof`` is ``None`` (proofs off, and on
        a row with no trade), ``{"status": "PENDING"}``, or
        ``{"status": "IN_BATCH", batchNumber, l2BlockNumber, rowIndex,
        l2TradeDigest, batchTxHash, final}`` once a proven batch includes the
        trade (:class:`~loaf.models.TradeProofStatus`).
        """
        return self._client.get(
            "/history/trades",
            params={"page": page, "pageSize": page_size, "cursor": cursor},
        )

    def funding(self, *, page_size: int | None = None, cursor: str | None = None) -> Any:
        """``GET /history/funding`` — your funding payments, newest first: one
        row per funding tick and position it charged.

        Returns ``{funding, nextCursor}``. ``page_size`` max is 100 (default
        20). Each row is a :class:`~loaf.models.FundingPayment`, keyed by
        ``(journalSeq, pool)``: ``base`` the position's signed base at the
        tick, ``payment`` what the tick charged it, in the extended USDC units
        of a pool's collateral (a decimal string, positive paid, negative
        received), ``rateCbp`` the rate it applied, centi-bp of the index per
        period (funding is paid on the index). The pool's ``value`` counts
        it at once, and its ``collateral`` moves at its next refresh. What a
        position owes since it last settled is its ``fundingOwed``
        (:meth:`loaf.resources.perps.PerpsResource.positions`).
        """
        return self._client.get(
            "/history/funding", params={"pageSize": page_size, "cursor": cursor}
        )

    def cancelled_orders(self) -> Any:
        """``GET /history/orders/cancelled`` — all cancel events, newest first (not paginated).

        Returns ``{"cancelledOrders": [{orderId, cancelledAt}]}``. Cancelled
        BOOKED orders only; a cancelled conditional never appears here. Includes
        the exchange's own cancels: an expired order or an epoch move.
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
        """Yield every order across all pages using cursor pagination. A
        ``nextCursor`` the server already gave raises
        :class:`~loaf.exceptions.LoafError` instead of paging forever."""
        yield from self._iter("/history/orders", "orders", page_size)

    def iter_trades(self, *, page_size: int = 100) -> Iterator[Any]:
        """Yield every trade across all pages using cursor pagination. A
        ``nextCursor`` the server already gave raises
        :class:`~loaf.exceptions.LoafError` instead of paging forever."""
        yield from self._iter("/history/trades", "trades", page_size)

    def iter_funding(self, *, page_size: int = 100) -> Iterator[Any]:
        """Yield every funding payment across all pages using cursor pagination. A
        ``nextCursor`` the server already gave raises
        :class:`~loaf.exceptions.LoafError` instead of paging forever."""
        yield from self._iter("/history/funding", "funding", page_size)

    def _iter(self, path: str, key: str, page_size: int) -> Iterator[Any]:
        cursor: str | None = None
        followed: set[str] = set()
        while True:
            page = self._client.get(path, params={"pageSize": page_size, "cursor": cursor})
            for item in page.get(key, []) or []:
                yield item
            cursor = page.get("nextCursor")
            if not cursor:
                return
            if cursor in followed:
                raise LoafError(f"GET {path} answered a nextCursor it had already given; stopped paging")
            followed.add(cursor)

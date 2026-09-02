"""Trading: place, cancel, and pre-approve orders.

Placing an order is a single ``POST /orders``; the common case is a one-liner::

    loaf.orders.limit_buy("opera", quantity=10, price=167.49)

Order placement can be rejected with a 403 when:
* a trading-competition round is ACTIVE and your account is not admitted
  (:class:`~loaf.exceptions.CompetitionEligibilityError` — check
  ``loaf.competition.queue_position()``); outside an active round trading is
  unrestricted, or
* trading is halted platform-wide
  (:class:`~loaf.exceptions.TradingHaltedError`).

Three server-side checks reject an order the SDK cannot pre-validate for you:

* **Minimum order value** — price x quantity must be at least 10 USD (400,
  :class:`~loaf.exceptions.LoafValidationError`). A SELL closing your ENTIRE
  available position in a property is exempt, so a dust position can always be
  flattened.
* **Limit price deviation** — a LIMIT price too far from the current market
  reference is refused (400). The ceiling is a deployment setting; the message
  quotes the limit and the reference it used. MARKET orders skip this check —
  their price is slippage-bounded server-side instead.
* **Daily price band** — a separate per-property band around the daily
  reference price (422, :class:`~loaf.exceptions.LoafBusinessRuleError`), whose
  message carries the side, the limit, the reference and the band width.

These endpoints are also per-account rate limited (429 on abuse), on top of the
per-IP limit.

A 200 response means the exchange *accepted* the order into the book — not that
it filled. Fills and cancellations arrive asynchronously on the private
``portfolio`` WebSocket channel.
"""

from __future__ import annotations

import time
from typing import Any

from ..constants import DEFAULT_ORDER_DEADLINE, MARKET_ORDER_PRICE
from ..enums import OrderSide, OrderType, TimeInForce
from ..exceptions import _client_validation_error
from ..money import validate_price, validate_quantity
from .base import Resource


class OrdersResource(Resource):
    def create(
        self,
        token_name: str,
        side: str,
        quantity: float,
        *,
        type: str = OrderType.LIMIT,
        price: float | None = None,
        time_in_force: str = TimeInForce.GTC,
        deadline: int = DEFAULT_ORDER_DEADLINE,
    ) -> Any:
        """``POST /orders`` — place a trading order.

        Args:
            token_name: the property's public ``tokenName`` (e.g. ``"opera"``);
                list the tradeable ones via
                :meth:`loaf.resources.market.MarketResource.properties`.
            side: ``BUY`` or ``SELL`` (:class:`~loaf.enums.OrderSide`).
            quantity: token quantity in human units, at most 1 decimal place.
            type: ``LIMIT`` (default) or ``MARKET`` (:class:`~loaf.enums.OrderType`).
            price: dollars, at most 2 decimals, for ``LIMIT`` orders. Ignored
                (forced to 0) for ``MARKET`` orders.
            time_in_force: ``GTC`` (default), ``IOC``, ``FOK``, or ``GTD``.
            deadline: unix seconds. Must be ``0`` for non-GTD; a future
                timestamp for ``GTD``.

        Notes:
            * Minimum order value is 10 USDL (``price * quantity``), waived only
              for a SELL that closes your whole position.
            * BUY freezes USDL; SELL freezes the property token.
        """
        side = str(side)
        otype = str(type)
        tif = str(time_in_force)

        validate_quantity(quantity)

        if otype == OrderType.MARKET:
            if price not in (None, 0, 0.0):
                raise _client_validation_error("MARKET orders must not set a price (it is forced to 0)")
            price = MARKET_ORDER_PRICE
        elif otype == OrderType.LIMIT:
            if price is None:
                raise _client_validation_error("LIMIT orders require a price")
            if price <= 0:
                raise _client_validation_error("LIMIT orders require a positive price")
            validate_price(price)
        else:
            raise _client_validation_error(f"Unknown order type {otype!r}")

        if tif == TimeInForce.GTD:
            if deadline <= int(time.time()):
                raise _client_validation_error("GTD orders require a future unix-seconds deadline")
        elif deadline != DEFAULT_ORDER_DEADLINE:
            raise _client_validation_error(
                f"Non-GTD orders must use deadline={DEFAULT_ORDER_DEADLINE}"
            )

        body = {
            "tokenName": token_name,
            "price": price,
            "quantity": quantity,
            "side": side,
            "type": otype,
            "timeInForce": tif,
            "deadline": int(deadline),
        }
        return self._client.post("/orders", json=body)

    # -- Convenience wrappers --------------------------------------------- #

    def limit_buy(self, token_name: str, quantity: float, price: float, **kwargs: Any) -> Any:
        """Place a LIMIT BUY. Extra kwargs forwarded to :meth:`create`."""
        return self.create(
            token_name, OrderSide.BUY, quantity, type=OrderType.LIMIT, price=price, **kwargs
        )

    def limit_sell(self, token_name: str, quantity: float, price: float, **kwargs: Any) -> Any:
        """Place a LIMIT SELL. Extra kwargs forwarded to :meth:`create`."""
        return self.create(
            token_name, OrderSide.SELL, quantity, type=OrderType.LIMIT, price=price, **kwargs
        )

    def market_buy(self, token_name: str, quantity: float, **kwargs: Any) -> Any:
        """Place a MARKET BUY (slippage-bounded server-side)."""
        return self.create(token_name, OrderSide.BUY, quantity, type=OrderType.MARKET, **kwargs)

    def market_sell(self, token_name: str, quantity: float, **kwargs: Any) -> Any:
        """Place a MARKET SELL (slippage-bounded server-side)."""
        return self.create(token_name, OrderSide.SELL, quantity, type=OrderType.MARKET, **kwargs)

    # -- Cancel ------------------------------------------------------------ #

    def cancel(self, order_id: int) -> Any:
        """``POST /orders/cancel`` — cancel one open order by id.

        Cancellable only while ``OPEN`` / ``PARTIALLY_FILLED``. Raises
        :class:`~loaf.exceptions.LoafConflictError` (409) if the engine no longer
        holds the order (it likely just filled/cancelled).
        """
        return self._client.post("/orders/cancel", json={"orderId": int(order_id)})

    def cancel_all(self) -> Any:
        """``POST /orders/cancel-all`` — best-effort cancel of every open order.

        Returns ``{requestedCount, cancelledOrderIds, failedOrders}``. A 200 can
        still list ``failedOrders`` (e.g. orders that filled mid-sweep). Your
        "flatten / panic" button.
        """
        return self._client.post("/orders/cancel-all")

    # -- Pre-approval (latency optimisation) ------------------------------- #

    def approve(self, token_name: str) -> Any:
        """``POST /orders/approve`` — pre-grant ERC-20 allowances for a property.

        Idempotent. Optional: removes on-chain approval latency from your first
        BUY/SELL on a property. Orders also approve inline if you skip this.
        """
        return self._client.post("/orders/approve", json={"tokenName": token_name})

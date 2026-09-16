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
* **Limit price deviation** — a LIMIT price too far from the current midprice
  is refused (400). The ceiling is a deployment setting; the message quotes the
  limit and the midprice it used. MARKET orders skip this check — their price is
  slippage-bounded server-side instead.
* **Daily price band** — a separate per-property band around the daily
  reference price (422, :class:`~loaf.exceptions.LoafBusinessRuleError`), whose
  message carries the side, the limit, the reference and the band width.

A **conditional** (stop / take) order goes to ``POST /orders/conditional``
instead. It rests server-side until the mark price reaches your trigger,
then books an ordinary limit order at a price fixed when you placed it. Two
things it adds to the list above:

* **A 200 means the row is ARMED — not that it will ever book.** Every
  trigger-time failure (your holding or cash gone, the daily price band, the
  limit-price deviation, the minimum order value again, an engine rejection) raises
  nothing here: it arrives as ``status: "FAILED"`` with a ``rejectionReason``
  on the private ``portfolio`` WebSocket channel. A ``rejectionReason`` on a
  row that is still ``ARMED`` is the last deferred attempt, not a failure.
* **Trigger already reached** — placement is refused (400,
  :class:`~loaf.exceptions.LoafValidationError`) when the mark is already at or
  through the trigger in the order's direction. The SDK cannot see the mark, so
  it cannot pre-check this.

Conditional rows freeze nothing at placement but do occupy an open-order slot,
counted together with booked orders. ``LoafBusinessRuleError`` (422) is never
raised by conditional placement — the price band runs at trigger time.

These endpoints are also per-account rate limited (429 on abuse), on top of the
per-IP limit.

A 200 response means the exchange *accepted* the order into the book — not that
it filled. Fills and cancellations arrive asynchronously on the private
``portfolio`` WebSocket channel.
"""

from __future__ import annotations

import time
from typing import Any, Mapping

from ..constants import DEFAULT_ORDER_DEADLINE, MARKET_ORDER_PRICE
from ..enums import (
    ConditionalOrderStatus,
    ConditionalOrderType,
    OrderSide,
    OrderType,
    TimeInForce,
    is_conditional_order,
)
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
        tp_price: float | None = None,
        sl_price: float | None = None,
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
            tp_price: dollars, at most 2 decimals. Attaches a take-profit SELL
                leg for this order's FULL quantity. BUY orders only.
            sl_price: dollars, at most 2 decimals. Attaches a stop-loss SELL
                leg for this order's FULL quantity. BUY orders only.

        Notes:
            * Minimum order value is 10 USDL (``price * quantity``), waived only
              for a SELL that closes your whole position.
            * BUY freezes USDL; SELL freezes the property token.
            * ``tp_price`` / ``sl_price`` attach protective legs to a **BUY**
              (spot has no short to protect, so a SELL parent is refused). They
              are the only way to get an OCO pair: when one fires the other is
              cancelled automatically, with no window where both are live. Order is
              ``sl_price < price < tp_price`` on a LIMIT parent.
            * The legs' ids are NOT in the response — only the parent's
              ``orderId``. They arrive on the ``portfolio`` channel as
              ``order_update`` rows and show up in ``openOrders`` as ``PENDING``
              conditionals.
            * Legs arm only on a FULL fill of the parent, a few seconds after
              it, so expect a brief ``PENDING`` window. A parent that ends
              PARTIALLY filled FAILS its legs; one that fills nothing cancels
              them.
            * On a MARKET parent the SDK cannot check a leg against the entry —
              there is no entry yet. The server checks both legs against the
              midprice (``sl_price < midprice < tp_price``).
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

        if tp_price is not None or sl_price is not None:
            if side != OrderSide.BUY:
                raise _client_validation_error(
                    "Take-profit and stop-loss can only be attached to a BUY order"
                )
            if sl_price is not None:
                if sl_price <= 0:
                    raise _client_validation_error("sl_price must be a positive price")
                validate_price(sl_price)
            if tp_price is not None:
                if tp_price <= 0:
                    raise _client_validation_error("tp_price must be a positive price")
                validate_price(tp_price)
            if tp_price is not None and sl_price is not None and tp_price <= sl_price:
                raise _client_validation_error("tp_price must be above sl_price")
            if otype == OrderType.LIMIT:
                if sl_price is not None and sl_price >= price:
                    raise _client_validation_error("sl_price must be below the order price")
                if tp_price is not None and tp_price <= price:
                    raise _client_validation_error("tp_price must be above the order price")

        body = {
            "tokenName": token_name,
            "price": price,
            "quantity": quantity,
            "side": side,
            "type": otype,
            "timeInForce": tif,
            "deadline": int(deadline),
            "tpPrice": tp_price,
            "slPrice": sl_price,
        }
        return self._client.post("/orders", json=body)

    # -- Convenience wrappers --------------------------------------------- #

    def limit_buy(self, token_name: str, quantity: float, price: float, **kwargs: Any) -> Any:
        """Place a LIMIT BUY.

        Extra kwargs forwarded to :meth:`create` (e.g. ``tp_price`` / ``sl_price``).
        """
        return self.create(
            token_name, OrderSide.BUY, quantity, type=OrderType.LIMIT, price=price, **kwargs
        )

    def limit_sell(self, token_name: str, quantity: float, price: float, **kwargs: Any) -> Any:
        """Place a LIMIT SELL. Extra kwargs forwarded to :meth:`create`."""
        return self.create(
            token_name, OrderSide.SELL, quantity, type=OrderType.LIMIT, price=price, **kwargs
        )

    def market_buy(self, token_name: str, quantity: float, **kwargs: Any) -> Any:
        """Place a MARKET BUY (slippage-bounded server-side).

        Extra kwargs forwarded to :meth:`create` (e.g. ``tp_price`` / ``sl_price``).
        """
        return self.create(token_name, OrderSide.BUY, quantity, type=OrderType.MARKET, **kwargs)

    def market_sell(self, token_name: str, quantity: float, **kwargs: Any) -> Any:
        """Place a MARKET SELL (slippage-bounded server-side)."""
        return self.create(token_name, OrderSide.SELL, quantity, type=OrderType.MARKET, **kwargs)

    # -- Conditional orders (stop / take) ---------------------------------- #

    def create_conditional(
        self,
        token_name: str,
        side: str,
        quantity: float,
        *,
        type: str,
        trigger_price: float,
        price: float | None = None,
    ) -> Any:
        """``POST /orders/conditional`` — place a stop / take order.

        The row rests server-side as ``ARMED`` and books an ordinary limit
        order the moment the mark price sits at or through ``trigger_price``.

        Args:
            token_name: the property's public ``tokenName`` (e.g. ``"opera"``);
                list the tradeable ones via
                :meth:`loaf.resources.market.MarketResource.properties`.
            side: ``BUY`` or ``SELL`` (:class:`~loaf.enums.OrderSide`).
            quantity: token quantity in human units, at most 1 decimal place.
                Fixed for the life of the row — it is never clamped at trigger
                time, so a SELL whose holding shrank below it FAILS rather than
                selling what is left.
            type: one of :class:`~loaf.enums.ConditionalOrderType`. Direction
                comes from ``type`` AND ``side``: a stop SELL and a take BUY
                fire on a FALL to the trigger, a stop BUY and a take SELL fire
                on a RISE. :meth:`stop_loss` and :meth:`take_profit` spell the
                two common cases out for you.
            trigger_price: the mark level that fires the row, dollars, at most
                2 decimals. The mark is the order book mid, else the last trade,
                else the last candle close. Refused (400) if the mark is already
                at or through it — triggering is level-based, so a trigger EQUAL
                to the mark is refused too. Round it yourself
                (``round(mark * 0.95, 2)``): a raw float product carries 17
                decimals and fails the 2-decimal check.
            price: dollars, at most 2 decimals, for a ``*_LIMIT`` type. Must be
                omitted for a ``*_MARKET`` type, whose price the server derives
                from the trigger.

        Notes:
            * **A 200 means the row is ARMED — not that it will ever book.** A
              trigger-time failure arrives as ``status: "FAILED"`` with a
              ``rejectionReason`` on the ``portfolio`` channel; a reason on a row
              that is still ``ARMED`` is a deferred retry, not a failure.
            * A ``*_MARKET`` type is NOT a market order: its price is derived from
              the trigger at placement and fixed for life, a *protected limit*, so
              after a gap the booked order can rest away from the market.
            * Cancel with :meth:`cancel_conditional` while ``PENDING`` / ``ARMED``;
              once ``PLACED`` use :meth:`cancel` with the row's ``placedOrderId``
              (:meth:`cancel_row` picks for you). ``timeInForce`` is always
              ``GTC`` and ``deadline`` always ``0`` here.
        """
        side = str(side)
        otype = str(type)

        validate_quantity(quantity)

        if trigger_price <= 0:
            raise _client_validation_error("Conditional orders require a positive trigger_price")
        validate_price(trigger_price)

        if otype in (ConditionalOrderType.STOP_MARKET, ConditionalOrderType.TAKE_MARKET):
            if price not in (None, 0, 0.0):
                raise _client_validation_error(
                    f"{otype} orders must not set a price (it is derived from the trigger)"
                )
            price = MARKET_ORDER_PRICE
        elif otype in (ConditionalOrderType.STOP_LIMIT, ConditionalOrderType.TAKE_LIMIT):
            if price is None:
                raise _client_validation_error(f"{otype} orders require a price")
            if price <= 0:
                raise _client_validation_error(f"{otype} orders require a positive price")
            validate_price(price)
        else:
            raise _client_validation_error(f"Unknown conditional order type {otype!r}")

        body = {
            "tokenName": token_name,
            "price": price,
            "quantity": quantity,
            "side": side,
            "type": otype,
            "timeInForce": TimeInForce.GTC,
            "deadline": DEFAULT_ORDER_DEADLINE,
            "triggerPrice": trigger_price,
        }
        return self._client.post("/orders/conditional", json=body)

    def stop_loss(
        self, token_name: str, quantity: float, trigger_price: float, **kwargs: Any
    ) -> Any:
        """Arm a protective SELL that fires when the mark FALLS to ``trigger_price``.

        ``STOP_MARKET`` SELL — the standalone form of the ``sl_price`` leg
        :meth:`create` attaches to a BUY.

        ``quantity`` is fixed for the life of the row and the SDK never reads
        your position, so if the holding later shrinks below it the row turns
        ``FAILED`` within seconds — long before the mark reaches your trigger,
        leaving you unprotected. Watch for that ``FAILED`` row and re-arm at the
        new size. Extra kwargs go to :meth:`create_conditional`.
        """
        return self.create_conditional(
            token_name,
            OrderSide.SELL,
            quantity,
            type=ConditionalOrderType.STOP_MARKET,
            trigger_price=trigger_price,
            **kwargs,
        )

    def take_profit(
        self, token_name: str, quantity: float, trigger_price: float, **kwargs: Any
    ) -> Any:
        """Arm a SELL that fires when the mark RISES to ``trigger_price``.

        ``TAKE_MARKET`` SELL — the standalone form of the ``tp_price`` leg
        :meth:`create` attaches to a BUY.

        A :meth:`stop_loss` and a :meth:`take_profit` placed separately are
        **not** OCO: neither cancels the other. If the first one's order has
        FILLED the second FAILS on the missing holding; if it is still resting
        the second cancels it to free the tokens and books anyway. For a pair
        that cancels itself, attach ``sl_price`` / ``tp_price`` to the BUY
        (:meth:`create`). Extra kwargs go to :meth:`create_conditional`.
        """
        return self.create_conditional(
            token_name,
            OrderSide.SELL,
            quantity,
            type=ConditionalOrderType.TAKE_MARKET,
            trigger_price=trigger_price,
            **kwargs,
        )

    # -- Cancel ------------------------------------------------------------ #

    def cancel(self, order_id: int) -> Any:
        """``POST /orders/cancel`` — cancel one open order by id.

        Cancellable only while ``OPEN`` / ``PARTIALLY_FILLED``. Raises
        :class:`~loaf.exceptions.LoafConflictError` (409) if the engine no longer
        holds the order (it likely just filled/cancelled).
        """
        return self._client.post("/orders/cancel", json={"orderId": int(order_id)})

    def cancel_conditional(self, order_id: int) -> Any:
        """``POST /orders/conditional/cancel`` — cancel one PENDING / ARMED conditional.

        Pass the CONDITIONAL row's own ``id``; a ``PLACED`` row is cancelled
        with :meth:`cancel` on its ``placedOrderId`` instead (:meth:`cancel_row`
        picks). Each endpoint reads only its own table, so the wrong id is a 404
        like any unknown one. A terminal row raises
        :class:`~loaf.exceptions.LoafValidationError` (400), NOT the
        :class:`~loaf.exceptions.LoafConflictError` (409) :meth:`cancel` raises
        for the same race — an idempotent cancel must catch both.

        This route is database-only, so unlike :meth:`cancel` and
        :meth:`cancel_all` it still works under a platform-wide halt. That makes
        it the only way to stop an ``ARMED`` row firing into the reopen:
        triggering is level-based, so a row whose mark sat through its trigger
        for the whole halt fires as soon as trading resumes.
        """
        return self._client.post("/orders/conditional/cancel", json={"orderId": int(order_id)})

    def cancel_row(self, order: Mapping[str, Any]) -> Any:
        """Cancel one ``openOrders`` / ``order_update`` row, whichever kind it is.

        The two order tables share one id space and each has its own cancel
        route, so cancelling by a bare id is a coin flip. This picks from the
        row's own data, and issues exactly one request:

        * a booked order (``type`` ``MARKET`` / ``LIMIT``, or no ``type``)
          -> :meth:`cancel` with its ``id``;
        * a ``PLACED`` conditional -> :meth:`cancel` with its ``placedOrderId``;
        * any other conditional -> :meth:`cancel_conditional` with its ``id``.

        A terminal row is forwarded too: the server decides, not your snapshot.
        """
        order_id = order.get("id")
        if order_id is None:
            order_id = order.get("orderId")
        if order_id is None:
            raise _client_validation_error("cancel_row needs an order row carrying an id")

        if not is_conditional_order(order):
            return self.cancel(order_id)

        placed = order.get("placedOrderId")
        if str(order.get("status", "")) == ConditionalOrderStatus.PLACED and placed is not None:
            return self.cancel(placed)
        return self.cancel_conditional(order_id)

    def cancel_all(self) -> Any:
        """``POST /orders/cancel-all`` — best-effort cancel of every open order.

        Returns ``{requestedCount, cancelledOrderIds, failedOrders}``. A 200 can
        still list ``failedOrders`` (e.g. orders that filled mid-sweep). Your
        "flatten / panic" button.

        It cancels your ``PENDING`` / ``ARMED`` conditionals FIRST and prepends
        their conditional-row ids to ``cancelledOrderIds`` (and counts them in
        ``requestedCount``), so those ids will 404 against any booked-order
        lookup. It is NOT available under a platform-wide halt —
        :meth:`cancel_conditional` is the only cancel that still works then.
        """
        return self._client.post("/orders/cancel-all")

    # -- Pre-approval (latency optimisation) ------------------------------- #

    def approve(self, token_name: str) -> Any:
        """``POST /orders/approve`` — pre-grant ERC-20 allowances for a property.

        Idempotent. Optional: removes on-chain approval latency from your first
        BUY/SELL on a property. Orders also approve inline if you skip this.
        """
        return self._client.post("/orders/approve", json={"tokenName": token_name})

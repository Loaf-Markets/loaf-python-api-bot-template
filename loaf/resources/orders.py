"""Trading: place and cancel orders, stop & take orders.

Placing an order is a single ``POST /orders``; the common case is a one-liner::

    loaf.orders.limit_buy("OPRA", quantity=10, price=167.49)

**Signing.** Every placement is signed on your machine with your account's L2
trading key (``l2_secret_key=`` / ``$LOAF_L2_SECRET_KEY``; ``python -m
loaf.l2.keys enable`` derives and registers it) and authenticated by that
signature alone: the API key is not sent with the order. It is still needed:
the SDK reads the account's key state and reserves blocks of order indices
with it (each index signs one order), and cancels use it. The property's L2
market comes from its public detail, read at most every 30 s. A refusal that
names an index drops the block, and one that names the epoch, the key or the
signature reads the key state again, before the next order.

**The response.** A 200 from :meth:`OrdersResource.create` is sent after the
exchange commits the order, and carries ``status`` (``OPEN`` /
``PARTIALLY_FILLED`` / ``FILLED`` / ``CANCELLED``), ``quantityLeft`` and
``duplicate``. ``FILLED`` means filled. ``CANCELLED`` on a new BUY means it
filled partly and could not pay for more within what it froze. Later fills and
cancels arrive on the private ``portfolio`` WebSocket channel.

**MARKET orders** take no price. The SDK signs a worst price: the market
reference moved against you by the max slippage (``max_slippage_bps=``, default
``client.max_slippage_bps``). The engine matches it like a LIMIT at that price,
and an unfilled remainder **rests** on the book until it fills or you cancel
it. A BUY freezes worst price x quantity plus the taker fee.

Order placement can be rejected with a 403 when:

* a trading-competition round is ACTIVE and your account is not admitted
  (:class:`~loaf.exceptions.CompetitionEligibilityError` — check
  ``loaf.competition.queue_position()``), the round has not started
  (``COMPETITION_NOT_STARTED``), or the property is not traded in the current
  market mode (``NOT_TRADEABLE_IN_MODE``); outside a round trading is
  unrestricted, and while the market switches modes placement answers a 503
  ``VENUE_SWITCHING`` (nothing was placed),
* trading is halted platform-wide or for the property
  (:class:`~loaf.exceptions.TradingHaltedError`), or
* the market is closed (:class:`~loaf.exceptions.LoafForbiddenError`,
  ``Trading is currently closed``).

Server-side checks reject an order the SDK cannot pre-validate for you (all 400,
:class:`~loaf.exceptions.LoafValidationError`):

* **Minimum order value** — price x quantity must be at least 10 USDL, measured
  at the price you **sign** (a MARKET order's worst price). A SELL closing your
  ENTIRE available position in a property is exempt, so a dust position can
  always be flattened.
* **Limit price deviation** — an order of **any** type, MARKET included, whose
  signed price is too far from the market reference is refused. The ceiling is
  a deployment setting; the message quotes the limit and the reference it used.
* **Daily price band** — a separate per-property band around the daily
  reference price, whose message carries the price, the limit, the reference
  and the band width.
* **Balance and open-order caps** — not enough available balance, or too many
  open orders (resting stops count too).

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
counted together with booked orders.

**Retries.** Placements are re-sent unchanged on timeouts and 5xx; the
exchange keys an order by its account and order index, so a repeat is answered
as a duplicate, never placed twice. If that does not settle it the SDK raises
:class:`~loaf.exceptions.OrderOutcomeUnknownError`: see
:meth:`OrdersResource.resubmit`.

Placements and cancels are also per-account rate limited (429 on abuse), on
top of the per-IP limit.
"""

from __future__ import annotations

import numbers
import time
from collections.abc import Sequence
from decimal import Decimal
from typing import Any, Mapping

from ..enums import (
    CONDITIONAL_ORDER_TYPES,
    ConditionalOrderStatus,
    ConditionalOrderType,
    OrderSide,
    OrderType,
    is_conditional_order,
)
from ..exceptions import LoafAPIError, LoafError, OrderOutcomeUnknownError, _client_validation_error
from ..l2.trading import L2_BUFFER_MS, Leg, OrderIntent
from ..money import (
    _check_side,
    _worst_price,
    validate_price,
    validate_quantity,
    validate_slippage_bps,
)
from .base import Resource, _check_ticker

_LIMIT_CONDITIONALS = (ConditionalOrderType.STOP_LIMIT, ConditionalOrderType.TAKE_LIMIT)


class OrdersResource(Resource):
    def create(
        self,
        ticker: str,
        side: str,
        quantity: float,
        *,
        type: str = OrderType.LIMIT,
        price: float | None = None,
        reference_price: float | None = None,
        max_slippage_bps: int | None = None,
        tp_price: float | None = None,
        sl_price: float | None = None,
        leg_max_slippage_bps: int | None = None,
    ) -> Any:
        """``POST /orders`` — place a trading order, signed with your L2 trading key.

        Args:
            ticker: the property's ``ticker``, exactly as listed (1-4
                uppercase letters or digits, e.g. ``"OPRA"``); pick one from
                :meth:`loaf.resources.market.MarketResource.properties` (not
                every listed row is tradeable; see its docstring).
            side: ``BUY`` or ``SELL`` (:class:`~loaf.enums.OrderSide`).
            quantity: tokens, at most 1 decimal place, up to 1,000,000,000.
            type: ``LIMIT`` (default) or ``MARKET`` (:class:`~loaf.enums.OrderType`).
            price: ``LIMIT`` only: dollars, at most 2 decimals, up to
                1,000,000,000. Refused for ``MARKET``, which signs a worst price
                derived from ``reference_price`` instead.
            reference_price: ``MARKET`` only: the price your max slippage is
                measured from. Default: the exchange's market reference (book
                mid, else last trade, else last candle close, else IPO price),
                read with one extra public request that is served from a shared
                cache, so it can trail the live market by tens of seconds. Pass
                the ``markprice:{ticker}`` WebSocket value for a fresher
                reference (it also saves the request).
            max_slippage_bps: ``MARKET`` only: the most the price may move
                against you, in basis points (default: the client's
                ``max_slippage_bps``, 2% unless configured). The SDK signs
                ``reference_price x (1 ± max_slippage_bps/10,000)`` (+ for a
                BUY, - for a SELL) as your worst fill price, see
                :func:`loaf.worst_price`. For an exact worst price pass
                ``reference_price=X, max_slippage_bps=0``.
            tp_price: the TRIGGER level of an attached take-profit SELL leg for
                this order's FULL quantity. BUY orders only.
            sl_price: the TRIGGER level of an attached stop-loss SELL leg for
                this order's FULL quantity. BUY orders only.
            leg_max_slippage_bps: how far below its trigger each leg may sell
                when it fires (default: the client's ``max_slippage_bps``, never
                this order's ``max_slippage_bps``). Each leg books a LIMIT SELL
                at ``trigger x (1 - leg_max_slippage_bps/10,000)``, so after a
                gap past that price it rests instead of selling.

        Returns:
            ``{success, orderId, status, quantityLeft, duplicate, attachedLegIds}``
            — the order as of the commit that admitted it, and its legs'
            conditional ids (:class:`~loaf.models.OrderResult`).

        Raises:
            OrderOutcomeUnknownError: the order may or may not be live (or
                already filled); :meth:`resubmit` it while you still want it, or
                look for it in ``history.orders()``, never place it afresh.

        Notes:
            * BUY freezes USDC; SELL freezes the property token.
            * ``tp_price`` / ``sl_price`` attach protective legs to a **BUY**
              (spot has no short to protect, so a SELL parent is refused). They
              are the only way to get an OCO pair: when one fires the other is
              cancelled automatically, with no window where both are live. Order is
              ``sl_price < price < tp_price`` on a LIMIT parent.
            * The legs' conditional ids are in ``attachedLegIds`` (empty on a
              duplicate re-send). The legs also arrive on the ``portfolio``
              channel as ``order_update`` rows and show up in ``openOrders`` as
              ``PENDING`` conditionals.
            * Legs arm only on a FULL fill of the parent. A parent that ends
              CANCELLED after a partial fill FAILS its legs, and one with no
              fill cancels them, so **cancelling a resting MARKET remainder
              drops its TP/SL**.
            * On a MARKET parent the server checks the triggers against its
              reference price, not your fill (400 ``Unable to determine market
              price for order`` when it has none).
            * A leg's own price is first checked (deviation, band, minimum
              value) when it fires; a failure FAILS the leg.
            * A duplicate re-send ignores changed legs.
        """
        trader = self._client.l2
        _check_ticker(ticker)
        side = _check_side(side)
        otype = str(type)
        if otype not in (OrderType.LIMIT, OrderType.MARKET):
            raise _client_validation_error(f"Unknown order type {otype!r}")
        qty_d = validate_quantity(quantity)

        limit_d: Decimal | None = None  # a LIMIT's own price; a MARKET's is derived below
        if otype == OrderType.LIMIT:
            if price is None:
                raise _client_validation_error("LIMIT orders require a price")
            limit_d = validate_price(price)
            if reference_price is not None:
                raise _client_validation_error("reference_price only applies to MARKET orders")
            if max_slippage_bps is not None:
                raise _client_validation_error(
                    "max_slippage_bps only applies to MARKET orders "
                    "(a LIMIT order signs its own price)"
                )
        elif price is not None:
            raise _client_validation_error(
                "MARKET orders take no price: the SDK signs reference_price x "
                "(1 ± max_slippage_bps/10,000) as your worst fill price. For an exact worst "
                "price pass reference_price=X, max_slippage_bps=0, or place a LIMIT order."
            )

        legs: list[Leg] = []
        if tp_price is not None or sl_price is not None:
            if side != OrderSide.BUY:
                raise _client_validation_error(
                    "Take-profit and stop-loss can only be attached to a BUY order"
                )
            tp_d = None if tp_price is None else validate_price(tp_price, "tp_price")
            sl_d = None if sl_price is None else validate_price(sl_price, "sl_price")
            if tp_d is not None and sl_d is not None and tp_d <= sl_d:
                raise _client_validation_error("tp_price must be above sl_price")
            if limit_d is not None:
                if sl_d is not None and sl_d >= limit_d:
                    raise _client_validation_error("sl_price must be below the order price")
                if tp_d is not None and tp_d <= limit_d:
                    raise _client_validation_error("tp_price must be above the order price")
            leg_bps = self._tolerance(leg_max_slippage_bps, "leg_max_slippage_bps")
            legs = [
                (key, trigger_d, _worst_price(trigger_d, OrderSide.SELL, leg_bps))
                for key, trigger_d in (("tp", tp_d), ("sl", sl_d))
                if trigger_d is not None
            ]
        elif leg_max_slippage_bps is not None:
            raise _client_validation_error(
                "leg_max_slippage_bps only applies when tp_price or sl_price is set"
            )

        if limit_d is not None:
            price_d = limit_d
        else:
            bps = self._tolerance(max_slippage_bps)
            if reference_price is None:
                reference_price = trader.mark_price(ticker)
            price_d = _worst_price(reference_price, side, bps)

        body = trader.sign(ticker, side, otype, price_d, qty_d, legs=legs)
        return self._place("/orders", body)

    def create_many(
        self, orders: Sequence[OrderIntent], *, max_slippage_bps: int | None = None
    ) -> list[Any]:
        """Sign 1 to 256 LIMIT / MARKET orders under ONE signature and place each.

        K1's batch signature: one root over the orders, each body carrying its
        place in the tree, so a maker quoting many levels signs once instead of
        once per order. Each :class:`~loaf.l2.trading.OrderIntent` is checked and
        priced as :meth:`create` checks and prices it (a MARKET one at its worst
        price off ``reference_price``, default the market reference, with
        ``max_slippage_bps``, default the client's); a batch carries no legs.
        The orders are sent one by one, in the order given.

        Returns:
            One entry per order: the exchange's answer, or the exception it was
            refused with (an :class:`~loaf.exceptions.OrderOutcomeUnknownError`
            among them). One order's refusal does not stop the others.
        """
        trader = self._client.l2
        if not isinstance(orders, Sequence) or not all(isinstance(o, OrderIntent) for o in orders):
            raise _client_validation_error("create_many takes a list of OrderIntent")
        bps = self._tolerance(max_slippage_bps)
        priced: list[tuple[str, str, str, Decimal, Decimal]] = []
        for intent in orders:
            _check_ticker(intent.ticker)
            side = _check_side(intent.side)
            otype = str(intent.type)
            if otype not in (OrderType.LIMIT, OrderType.MARKET):
                raise _client_validation_error(f"A batch signs LIMIT and MARKET orders, got {otype!r}")
            qty_d = validate_quantity(intent.quantity)
            if otype == OrderType.LIMIT:
                if intent.price is None:
                    raise _client_validation_error("LIMIT orders require a price")
                if intent.reference_price is not None:
                    raise _client_validation_error("reference_price only applies to MARKET orders")
                price_d = validate_price(intent.price)
            else:
                if intent.price is not None:
                    raise _client_validation_error(
                        "MARKET orders take no price: the SDK signs reference_price x "
                        "(1 ± max_slippage_bps/10,000) as your worst fill price"
                    )
                reference = intent.reference_price
                if reference is None:
                    reference = trader.mark_price(intent.ticker)
                price_d = _worst_price(reference, side, bps)
            priced.append((intent.ticker, side, otype, price_d, qty_d))
        results: list[Any] = []
        for body in trader.sign_many(priced):
            try:
                results.append(self._place("/orders", body))
            except LoafError as exc:  # each order answers on its own
                results.append(exc)
        return results

    # -- Convenience wrappers --------------------------------------------- #

    def limit_buy(
        self,
        ticker: str,
        quantity: float,
        price: float,
        *,
        tp_price: float | None = None,
        sl_price: float | None = None,
        leg_max_slippage_bps: int | None = None,
    ) -> Any:
        """Place a LIMIT BUY, optionally with take-profit / stop-loss legs (see :meth:`create`)."""
        return self.create(
            ticker,
            OrderSide.BUY,
            quantity,
            type=OrderType.LIMIT,
            price=price,
            tp_price=tp_price,
            sl_price=sl_price,
            leg_max_slippage_bps=leg_max_slippage_bps,
        )

    def limit_sell(self, ticker: str, quantity: float, price: float) -> Any:
        """Place a LIMIT SELL (see :meth:`create`)."""
        return self.create(ticker, OrderSide.SELL, quantity, type=OrderType.LIMIT, price=price)

    def market_buy(
        self,
        ticker: str,
        quantity: float,
        *,
        reference_price: float | None = None,
        max_slippage_bps: int | None = None,
        tp_price: float | None = None,
        sl_price: float | None = None,
        leg_max_slippage_bps: int | None = None,
    ) -> Any:
        """Place a MARKET BUY that pays at most
        ``reference_price x (1 + max_slippage_bps/10,000)``.

        You pass no price: by default the reference is the exchange's market
        price and the max slippage is ``client.max_slippage_bps``. An unfilled
        remainder rests at that price. See :meth:`create`.
        """
        return self.create(
            ticker,
            OrderSide.BUY,
            quantity,
            type=OrderType.MARKET,
            reference_price=reference_price,
            max_slippage_bps=max_slippage_bps,
            tp_price=tp_price,
            sl_price=sl_price,
            leg_max_slippage_bps=leg_max_slippage_bps,
        )

    def market_sell(
        self,
        ticker: str,
        quantity: float,
        *,
        reference_price: float | None = None,
        max_slippage_bps: int | None = None,
    ) -> Any:
        """Place a MARKET SELL that sells at no less than
        ``reference_price x (1 - max_slippage_bps/10,000)``.

        You pass no price: by default the reference is the exchange's market
        price and the max slippage is ``client.max_slippage_bps``. An unfilled
        remainder rests at that price. See :meth:`create`.
        """
        return self.create(
            ticker,
            OrderSide.SELL,
            quantity,
            type=OrderType.MARKET,
            reference_price=reference_price,
            max_slippage_bps=max_slippage_bps,
        )

    # -- Conditional orders (stop / take) ---------------------------------- #

    def create_conditional(
        self,
        ticker: str,
        side: str,
        quantity: float,
        *,
        type: str,
        trigger_price: float,
        price: float | None = None,
        max_slippage_bps: int | None = None,
    ) -> Any:
        """``POST /orders/conditional`` — place a stop / take order, signed with your L2 trading key.

        The row rests server-side as ``ARMED`` and books an ordinary limit
        order the moment the mark price sits at or through ``trigger_price``.

        Args:
            ticker: the property's ``ticker``, exactly as listed (e.g.
                ``"OPRA"``); pick one from
                :meth:`loaf.resources.market.MarketResource.properties` (not
                every listed row is tradeable; see its docstring).
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
            price: ``*_LIMIT`` types only (required): dollars, at most 2
                decimals. Refused for a ``*_MARKET`` type.
            max_slippage_bps: ``*_MARKET`` types only. The SDK signs
                ``trigger_price x (1 ± max_slippage_bps/10,000)`` (+ for a BUY,
                - for a SELL; default: the client's ``max_slippage_bps``) as the
                price the row books at, fixed for its life.

        Returns:
            ``{success, orderId}`` (:class:`~loaf.models.OrderAck`).

        Raises:
            OrderOutcomeUnknownError: the row may or may not be armed (or
                already fired); :meth:`resubmit` it while you still want it, or
                look for it in ``history.orders()``, never place it afresh.

        Notes:
            * **A 200 means the row is ARMED — not that it will ever book.** A
              trigger-time failure arrives as ``status: "FAILED"`` with a
              ``rejectionReason`` on the ``portfolio`` channel; a reason on a row
              that is still ``ARMED`` is a deferred retry, not a failure.
            * A ``*_MARKET`` type is a *protected limit*, not a market order:
              its price is fixed at placement, so after a gap past it the
              booked order rests instead of executing.
            * The minimum order value (10 USDL) is measured at the signed
              price, and so is the engine's balance check when the row fires;
              your balance is not checked when you arm it. A SELL of your whole
              holding is exempt from the minimum.
            * A re-send of the same signed order answers with the same
              ``orderId`` whatever the row's status, but the checks run again
              first.
            * Cancel with :meth:`cancel_conditional` while ``PENDING`` / ``ARMED``;
              once ``PLACED`` use :meth:`cancel` with the row's ``placedOrderId``
              (:meth:`cancel_row` picks for you).
        """
        trader = self._client.l2
        _check_ticker(ticker)
        side = _check_side(side)
        otype = str(type)
        if otype not in CONDITIONAL_ORDER_TYPES:
            raise _client_validation_error(f"Unknown conditional order type {otype!r}")
        qty_d = validate_quantity(quantity)
        trigger_d = validate_price(trigger_price, "trigger_price")

        if otype in _LIMIT_CONDITIONALS:
            if price is None:
                raise _client_validation_error(f"{otype} orders require a price")
            price_d = validate_price(price)
            if max_slippage_bps is not None:
                raise _client_validation_error(
                    "max_slippage_bps only applies to *_MARKET conditionals "
                    "(a *_LIMIT signs its own price)"
                )
        else:
            if price is not None:
                raise _client_validation_error(
                    f"{otype} orders take no price: the SDK signs trigger_price x "
                    "(1 ± max_slippage_bps/10,000) as the price it books at"
                )
            price_d = _worst_price(trigger_d, side, self._tolerance(max_slippage_bps))

        body = trader.sign(ticker, side, otype, price_d, qty_d, trigger=trigger_d)
        return self._place("/orders/conditional", body)

    def stop_loss(
        self,
        ticker: str,
        quantity: float,
        trigger_price: float,
        *,
        max_slippage_bps: int | None = None,
    ) -> Any:
        """Arm a protective SELL that fires when the mark FALLS to ``trigger_price``.

        ``STOP_MARKET`` SELL: when it fires it books a LIMIT SELL at
        ``trigger_price x (1 - max_slippage_bps/10,000)`` (default: the
        client's ``max_slippage_bps``, 2% unless configured), so after a gap
        past that price it rests instead of selling.

        ``quantity`` is fixed for the life of the row and the SDK never reads
        your position, so if the holding later shrinks below it the row turns
        ``FAILED`` within seconds — long before the mark reaches your trigger,
        leaving you unprotected. Watch for that ``FAILED`` row and re-arm at the
        new size. For a take-profit / stop-loss pair that cancels itself (OCO),
        attach ``tp_price`` / ``sl_price`` to the BUY (:meth:`create`).
        """
        return self.create_conditional(
            ticker,
            OrderSide.SELL,
            quantity,
            type=ConditionalOrderType.STOP_MARKET,
            trigger_price=trigger_price,
            max_slippage_bps=max_slippage_bps,
        )

    def take_profit(
        self,
        ticker: str,
        quantity: float,
        trigger_price: float,
        *,
        max_slippage_bps: int | None = None,
    ) -> Any:
        """Arm a SELL that fires when the mark RISES to ``trigger_price``.

        ``TAKE_MARKET`` SELL: when it fires it books a LIMIT SELL at
        ``trigger_price x (1 - max_slippage_bps/10,000)`` (default: the
        client's ``max_slippage_bps``, 2% unless configured).

        A :meth:`stop_loss` and a :meth:`take_profit` placed separately are
        **not** OCO: neither cancels the other. If the first one's order has
        FILLED the second FAILS on the missing holding; if it is still resting
        the second cancels it to free the tokens and books anyway. For a pair
        that cancels itself, attach ``sl_price`` / ``tp_price`` to the BUY
        (:meth:`create`).
        """
        return self.create_conditional(
            ticker,
            OrderSide.SELL,
            quantity,
            type=ConditionalOrderType.TAKE_MARKET,
            trigger_price=trigger_price,
            max_slippage_bps=max_slippage_bps,
        )

    # -- Re-send ------------------------------------------------------------ #

    def resubmit(self, signed_body: Mapping[str, Any]) -> Any:
        """Re-send a signed placement exactly as it was (e.g. the ``signed_body``
        of an :class:`~loaf.exceptions.OrderOutcomeUnknownError`).

        If the first attempt landed, the answer is that same order (on
        ``/orders`` with ``duplicate: True``); if not, it is placed now, at the
        price and quantity signed then, so resubmit only while you still want
        it. It cannot be placed twice: the exchange holds at most one order per
        account and order index, and answers a re-send of the same signed body
        with the order holding them.

        Anything but a 200 raises :class:`~loaf.exceptions.OrderOutcomeUnknownError`
        again, with the reason in ``last_error``: whatever this re-send ran
        into, the first attempt may still be live. A refusal does not prove the
        first attempt failed either: checks run before the duplicate is
        recognised (the open-order cap, for example, counts the order that
        landed). A lasting refusal (a halt, the cap) will not change by
        re-sending, so look for the order in ``history.orders()`` (every
        status) then; an order that filled is not in ``openOrders``.

        Needs neither the trading key nor the API key. Refused locally once the
        order is within a day of its ``l2ExpiryMs``: the exchange refuses an
        order that close to its expiry, so it could neither place it nor
        confirm a duplicate.
        """
        if not isinstance(signed_body, Mapping) or not _is_signed_l2_body(signed_body):
            raise _client_validation_error(
                "resubmit needs a signed placement body (with its L2 order index, expiry and "
                "signature), e.g. OrderOutcomeUnknownError.signed_body"
            )
        if time.time() * 1000 > signed_body["l2ExpiryMs"] - L2_BUFFER_MS:
            raise _client_validation_error(
                "This order expires within a day, so the exchange would refuse it and could no "
                "longer confirm a duplicate. Look for it in history.orders() instead "
                "(openOrders shows only resting orders, not one that already filled)."
            )
        path = "/orders/conditional" if "triggerPrice" in signed_body else "/orders"
        return self._client._send_signed(path, dict(signed_body), ambiguous=True)

    # -- Cancel ------------------------------------------------------------ #

    def cancel(self, order_id: int) -> Any:
        """``POST /orders/cancel`` — cancel one open order by id.

        Cancellable only while ``OPEN`` / ``PARTIALLY_FILLED``. A 200 means the
        cancel has committed. Raises:

        * :class:`~loaf.exceptions.LoafConflictError` (409) if the engine no
          longer holds the order; the message can name its status
          (``Order can no longer be cancelled — it is FILLED.``);
        * :class:`~loaf.exceptions.LoafValidationError` (400) for a terminal
          status the database already knows;
        * :class:`~loaf.exceptions.LoafForbiddenError` (403
          ``Not authorized to cancel this order``) for someone else's order;
        * :class:`~loaf.exceptions.LoafServiceUnavailableError` (503) when the
          trading service was unavailable — a cancel is safe to retry.

        It works under a single-property halt and outside trading hours; only a
        platform-wide halt blocks it (:class:`~loaf.exceptions.TradingHaltedError`).
        """
        return self._client.post("/orders/cancel", json={"orderId": _order_id(order_id)})

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
        return self._client.post("/orders/conditional/cancel", json={"orderId": _order_id(order_id)})

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
        """``POST /orders/cancel-all`` — cancel every open order.

        Returns ``{requestedCount, cancelledOrderIds, failedOrders}``, where
        ``requestedCount`` is ``len(cancelledOrderIds) + len(failedOrders)``.
        Your "flatten / panic" button.

        It cancels your ``PENDING`` / ``ARMED`` conditionals FIRST and prepends
        their conditional-row ids to ``cancelledOrderIds``, so those ids will
        404 against any booked-order lookup. Then one request to the matching
        engine cancels all your resting orders at once, in one database
        transaction: an order placed while it runs may or may not be included,
        one placed after the response is not.

        ``failedOrders`` lists only orders the engine could not cancel because
        the request did not complete on their property (an internal fault, or
        the outcome there is unknown). They stay open, each with the
        ``errorMessage`` "Trading service is temporarily unavailable. Please
        try again in a moment."; calling ``cancel_all()`` again is safe (the
        request is idempotent).

        It is NOT available under a platform-wide halt — it refuses before
        cancelling anything (:class:`~loaf.exceptions.TradingHaltedError`), and
        :meth:`cancel_conditional` is the only cancel that still works then. A
        503 (:class:`~loaf.exceptions.LoafServiceUnavailableError`) means the
        engine did not process the request or its outcome is unknown; it can
        arrive after the conditionals were already cancelled, and retrying is
        safe.
        """
        return self._client.post("/orders/cancel-all")

    # -- Signing & sending ------------------------------------------------- #

    def _tolerance(self, max_slippage_bps: int | None, field: str = "max_slippage_bps") -> int:
        """This order's max slippage, else the client's (re-checked: it is a public attribute)."""
        if max_slippage_bps is None:
            return validate_slippage_bps(
                self._client.max_slippage_bps, "LoafClient.max_slippage_bps"
            )
        return validate_slippage_bps(max_slippage_bps, field)

    def _place(self, path: str, body: dict) -> Any:
        """Send a signed body. A refusal that names the order's index or the account's key
        (:meth:`~loaf.l2.trading.L2Trader.after_refusal`) changes what the next order signs."""
        try:
            return self._client._send_signed(path, body)
        except OrderOutcomeUnknownError as exc:
            self._after_refusal(exc.last_error, body)
            raise
        except LoafAPIError as exc:
            self._after_refusal(exc, body)
            raise

    def _after_refusal(self, error: BaseException, body: Mapping[str, Any]) -> None:
        if isinstance(error, LoafAPIError):
            self._client.l2.after_refusal(error.message, body.get("l2AccountIndex"))


def _whole(value: Any) -> bool:
    return isinstance(value, numbers.Integral) and not isinstance(value, bool)


def _order_id(value: Any) -> int:
    """An order id exactly as given: a positive whole number (``7``, ``7.0`` or ``"7"``).
    ``int()`` would turn ``3.9`` into 3 and ``True`` into 1, and cancel another order."""
    if isinstance(value, str) and value.isdigit() and value.isascii():
        value = int(value)
    elif isinstance(value, float) and value.is_integer():
        value = int(value)
    elif isinstance(value, Decimal) and value.is_finite() and value == value.to_integral_value():
        value = int(value)
    if not _whole(value) or value < 1:
        raise _client_validation_error(f"order_id must be a positive whole number, got {value!r}")
    return int(value)


def _is_signed_l2_body(body: Mapping[str, Any]) -> bool:
    """The fields that make a body one signed L2 order: its account, index, expiry and signature."""
    return (
        all(_whole(body.get(k)) for k in ("l2AccountIndex", "l2OrderIndex", "l2ExpiryMs"))
        and all(isinstance(body.get(k), str) and body.get(k) for k in ("l2SigS", "l2SigE"))
    )

"""Trading: place and cancel orders, stop & take orders.

Placing an order is a single ``POST /orders``; the common case is a one-liner::

    loaf.orders.limit_buy("OPRA", quantity=10, price=167.49)

**Signing.** Every placement is signed on your machine with your trading key
(``secret_key=`` / ``$LOAF_SECRET_KEY``) and authenticated by that
signature alone: the API key is not sent with it. The API key is still needed,
for cancels and for the account reads below.

**What the SDK reads for you.** An order signs the property's L2 market
(``property.l2Market`` of ``GET /trade/{ticker}``), your trading account's
index and epoch (``GET /l2/keys``), and an order number reserved to your
account (``POST /l2/order-indices``), reserved in blocks that grow from what
your first order needs to 64. All of it is cached in memory and refreshed when
a refusal shows it stale. The order numbers of an order that was definitely not
placed are reused by later orders; a restart leaves the unused rest of a block
unused.

**Order life.** Every order expires. ``ttl_ms=`` sets how long it may rest
(default 7 days, from one minute to about 47.9 days): the SDK signs the expiry
``l2ExpiryMs`` = now + 24 hours + ``ttl_ms`` (``expiryMs`` on order rows), and
the exchange stops filling the order 24 hours before it and cancels it (a
``CANCELLED`` you did not request). TP/SL legs share their
parent's life. An ARMED stop, take or leg is not cancelled when its life ends:
it keeps its open-order slot until you cancel it or it fires, and FAILS if it
fires later, so cancel and re-arm it before then. After an epoch
move — a Reset trading key in the web app, a cancel-all sent on chain, or any
placement refused ``l2 epoch: …`` — the exchange has cancelled your resting
orders, but the stops, takes and TP/SL legs signed before it stay ARMED and
FAIL when they fire: cancel and re-arm them, a TP/SL leg as a separate
:meth:`OrdersResource.stop_loss` / :meth:`OrdersResource.take_profit` (not OCO).

**The response.** A 200 from :meth:`OrdersResource.create` is sent after the
exchange commits the order, and carries ``status`` (``OPEN`` /
``PARTIALLY_FILLED`` / ``FILLED`` / ``CANCELLED``), ``quantityLeft``,
``duplicate`` and ``attachedLegIds``. ``FILLED`` means filled. ``CANCELLED`` on
a new order means it stopped early: a BUY whose hold could not pay for its next
fill, or a SELL stopped at K1's bound. Later fills and cancels arrive on the
private ``portfolio`` WebSocket channel.

**MARKET orders** take no price. The SDK signs a worst price: the market
reference moved against you by the max slippage (``max_slippage_bps=``, default
``client.max_slippage_bps``), rounded toward the reference to the cent. The
engine matches it like a LIMIT at that price, and an unfilled remainder
**rests** on the book until it fills, expires or you cancel it. A BUY holds
worst price x quantity x (1 + :data:`~loaf.constants.L2_FEE_RATE_CAP` /
1,000,000) of your cash.

Order placement can be rejected with a 403 when:

* the current market mode does not take it
  (:class:`~loaf.exceptions.VenueError`, by ``code``): during a competition
  round only the round's market trades (``NOT_TRADEABLE_IN_MODE``), it opens
  when the round goes ACTIVE (``COMPETITION_NOT_STARTED``), and only seat
  holders trade it (``NOT_COMPETITION_PARTICIPANT`` — check
  ``loaf.competition.queue_position().participant``; without a seat you have no
  trading account on its market, so an order there normally raises
  :class:`~loaf.exceptions.LoafConfigError` before anything is sent); while the
  mode is switching the answer is a 503
  :class:`~loaf.exceptions.VenueSwitchingError` instead,
* trading is halted platform-wide or for the property
  (:class:`~loaf.exceptions.TradingHaltedError`), or
* the market is closed (:class:`~loaf.exceptions.LoafForbiddenError`,
  ``Trading is currently closed``; real markets only).

Server-side checks reject an order the SDK cannot pre-validate for you (all 400,
:class:`~loaf.exceptions.LoafValidationError`):

* **Minimum order value** — price x quantity must be at least $10, measured
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
* **Fee-rate cap** — ``l2 fee: the signed fee-rate cap is below the account's
  fee rate``: your account's fee rate is above
  :data:`~loaf.constants.L2_FEE_RATE_CAP`, which every order signs.

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

Conditional rows hold nothing at placement, and your cash and holding are
checked only when they fire, but they do occupy an open-order slot, counted
together with booked orders. The exchange checks a row's price grid, order
number and its range only when the row fires, so those mistakes — and an order
number made stale if the exchange resets order numbers — FAIL the row then: watch
``on_order_update`` for ``status: "FAILED"`` (``order_status`` frames carry
booked orders only). Each stop or take holds its order number until it fires,
and one cancelled or expired unfired keeps counting against your account's
allowance (below) for about 49.5 days: move a trailing stop on meaningful
moves, never on every tick.

**Retries.** Placements are re-sent unchanged on timeouts and 5xx; a repeat is
answered as a duplicate, never placed twice. Two rules cover the rest:

* any error other than :class:`~loaf.exceptions.OrderOutcomeUnknownError`
  means the order was not placed: place it again if you still want it;
* on :class:`~loaf.exceptions.OrderOutcomeUnknownError`,
  ``resubmit(error.signed_body)`` while it can still rest, or find it in
  ``history.orders()`` by its ``expiryMs``, where a miss proves nothing while
  it can still rest. Never place a fresh order for the same intent while one
  may be live.

**Order-number allowance.** A reservation refused ``l2 order_index: the account
would hold more than … reserved indices no order uses`` means the account has
used up its allowance of reserved-but-unused order numbers: no order can be
placed on it, the web app included, until old reservations age out (up to about
49.5 days). It takes frequent restarts (each can leave up to 64 unused), many
stops that never fire, or many processes on one account.

Placements and cancels are also rate limited per account (429), on top of the
per-IP limit.
"""

from __future__ import annotations

import copy
import numbers
import time
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from functools import partial
from typing import TYPE_CHECKING, Any, Callable, Mapping, Sequence

from .. import k1
from .._l2 import AccountState, L2State, Market, TradingKey, _is_int, market_of
from ..constants import (
    DEFAULT_ORDER_TTL_MS,
    ENV_API_KEY,
    ENV_SECRET_KEY,
    L2_BUFFER_MS,
    L2_FEE_RATE_CAP,
    MAX_ORDER_TTL_MS,
    MIN_ORDER_TTL_MS,
)
from ..enums import (
    CONDITIONAL_ORDER_TYPES,
    ConditionalOrderStatus,
    ConditionalOrderType,
    OrderSide,
    OrderType,
    TimeInForce,
    is_conditional_order,
)
from ..exceptions import (
    LoafAPIError,
    LoafConfigError,
    LoafError,
    OrderOutcomeUnknownError,
    _client_validation_error,
)
from ..money import (
    _DECIMAL_CONTEXT,
    _check_side,
    _worst_price,
    validate_price,
    validate_quantity,
    validate_slippage_bps,
)
from .base import Resource, _check_ticker

if TYPE_CHECKING:
    from ..client import LoafClient

_LIMIT_CONDITIONALS = (ConditionalOrderType.STOP_LIMIT, ConditionalOrderType.TAKE_LIMIT)

_SIGNED_BODY_STRS = ("l2SigS", "l2SigE")
_SIGNED_BODY_INTS = ("l2OrderIndex", "l2AccountIndex", "l2ExpiryMs")


class OrdersResource(Resource):
    def __init__(self, client: LoafClient) -> None:
        super().__init__(client)
        #: The markets, account and order numbers your orders sign, refreshed on refusals.
        self._l2 = L2State(client)

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
        ttl_ms: int | None = None,
    ) -> Any:
        """``POST /orders`` — place a trading order, signed with your trading key.

        Every :class:`~loaf.exceptions.LoafAPIError` and
        :class:`~loaf.exceptions.LoafConnectionError` it raises means the order
        was not placed: anything that might have landed raises
        :class:`~loaf.exceptions.OrderOutcomeUnknownError` instead.

        Args:
            ticker: the property's ``ticker``, exactly as listed (1-4
                uppercase letters or digits, e.g. ``"OPRA"``); pick one from
                :meth:`loaf.resources.market.MarketResource.properties` (not
                every listed row is tradeable; see its docstring).
            side: ``BUY`` or ``SELL`` (:class:`~loaf.enums.OrderSide`).
            quantity: tokens, at most 1 decimal place, up to
                28,147,497,671,065.5.
            type: ``LIMIT`` (default) or ``MARKET`` (:class:`~loaf.enums.OrderType`).
            price: ``LIMIT`` only: dollars, at most 2 decimals, up to
                4,294,967.29. Refused for ``MARKET``, which signs a worst price
                derived from ``reference_price`` instead.
            reference_price: ``MARKET`` only: the price your max slippage is
                measured from. Default: the exchange's market reference (book
                mid, else last trade, else last candle close, else IPO price),
                read with one extra public request, which can lag the live
                market. Pass
                the ``markprice:{ticker}`` WebSocket value for a fresher
                reference (it also saves the request once the SDK has read the
                property's market).
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
            ttl_ms: how long the order may rest, in whole milliseconds, from
                60,000 (one minute) to 4,141,458,432 (about 47.9 days); default
                604,800,000 (7 days). The exchange then stops filling it and
                cancels it; its TP/SL legs share its life. A bot's quotes
                should die with the bot: give them a short one.

        Returns:
            ``{success, orderId, status, quantityLeft, duplicate,
            attachedLegIds}`` (:class:`~loaf.models.OrderResult`): a new order
            as of the commit that admitted it, a duplicate as it is now (it may
            have filled or been cancelled since); ``attachedLegIds`` are the
            legs' conditional ids, take-profit first (``[]`` without legs).
            When ``duplicate`` is true (an identical re-send of an order that
            had landed), the legs are not compared, and any legs of the first
            admission exist although ``attachedLegIds`` is ``[]``: find them in
            ``portfolio.component().openOrders``, never attach them again (a
            second stop means a second sell).

        Raises:
            LoafConfigError: no trading key or API key; your trading key is not
                the one in force; no trading account; trading not enabled; this
                machine's clock hours ahead. Act in the web app or fix the
                setup, then restart.
            LoafAuthError: ``Invalid credentials``: your API key expired or was
                revoked (cancels fail too): create a new one in the web app and
                restart. Otherwise the exchange refused the order's signature or
                key; the next order re-reads your account.
            VenueError: the current market mode does not take this order.
            VenueSwitchingError: the market mode is switching (the exchange's
                answer, or the SDK's own while the switch settles): place it
                again in a few seconds if you still want it.
            TradingHaltedError: trading is halted.
            LoafValidationError: refused locally (``status_code`` 0) or by the
                exchange (400), including its ``l2 …`` texts.
            OrderOutcomeUnknownError: the order may or may not be live (or
                already filled); :meth:`resubmit` it while you still want it, or
                look for it in ``history.orders()``, never place it afresh.

        Notes:
            * A resting BUY holds price x quantity x (1 +
              :data:`~loaf.constants.L2_FEE_RATE_CAP` / 1,000,000) of your
              cash; a SELL holds the property's tokens.
            * ``tp_price`` / ``sl_price`` attach protective legs to a **BUY**
              (spot has no short to protect, so a SELL parent is refused). They
              are the only way to get an OCO pair: when one fires the other is
              cancelled automatically, with no window where both are live. Order is
              ``sl_price < price < tp_price`` on a LIMIT parent.
            * The legs arrive on the ``portfolio`` channel as ``order_update``
              rows and show up in ``openOrders`` as ``PENDING`` conditionals.
            * Legs arm only on a FULL fill of the parent. A parent that ends
              CANCELLED after a partial fill FAILS its legs, and one with no
              fill cancels them, so **cancelling a resting MARKET remainder
              drops its TP/SL**.
            * On a MARKET parent the server checks the triggers against its
              reference price, not your fill (400 ``Unable to determine market
              price for order`` when it has none).
            * A leg's own price is first checked (deviation, band, minimum
              value) when it fires; a failure FAILS the leg.
        """
        key = self._credentials()
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

        legs: list[tuple[str, Decimal, Decimal]] = []
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
                (leg, trigger_d, _worst_price(trigger_d, OrderSide.SELL, leg_bps))
                for leg, trigger_d in (("tp", tp_d), ("sl", sl_d))
                if trigger_d is not None
            ]
        elif leg_max_slippage_bps is not None:
            raise _client_validation_error(
                "leg_max_slippage_bps only applies when tp_price or sl_price is set"
            )

        order_price: Decimal | Callable[[Any], Decimal]
        if limit_d is not None:
            order_price = limit_d
        else:
            bps = self._tolerance(max_slippage_bps)
            if reference_price is None:  # the exchange's, read with the order's market
                order_price = partial(_worst_price, side=side, max_slippage_bps=bps)
            else:
                order_price = _worst_price(reference_price, side, bps)
        ttl = _ttl(ttl_ms)
        return self._place(key, ticker, side, otype, qty_d, order_price, ttl, legs=legs)

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
        ttl_ms: int | None = None,
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
            ttl_ms=ttl_ms,
        )

    def limit_sell(
        self, ticker: str, quantity: float, price: float, *, ttl_ms: int | None = None
    ) -> Any:
        """Place a LIMIT SELL (see :meth:`create`)."""
        return self.create(
            ticker, OrderSide.SELL, quantity, type=OrderType.LIMIT, price=price, ttl_ms=ttl_ms
        )

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
        ttl_ms: int | None = None,
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
            ttl_ms=ttl_ms,
        )

    def market_sell(
        self,
        ticker: str,
        quantity: float,
        *,
        reference_price: float | None = None,
        max_slippage_bps: int | None = None,
        ttl_ms: int | None = None,
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
            ttl_ms=ttl_ms,
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
        ttl_ms: int | None = None,
    ) -> Any:
        """``POST /orders/conditional`` — place a stop / take order, signed with your trading key.

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
            ttl_ms: the life of the order the row books, as in :meth:`create`
                (default 7 days): a row that fires after it FAILS, and one that
                has not fired by then stays ``ARMED``, in its open-order slot,
                until it fires or you cancel it. So cancel and re-arm a stop
                before then.

        Returns:
            ``{success, orderId}`` (:class:`~loaf.models.OrderAck`).

        Raises:
            OrderOutcomeUnknownError: the row may or may not be armed (or
                already fired); :meth:`resubmit` it while you still want it, or
                look for it in ``history.orders()``, never place it afresh.
            LoafError: the others :meth:`create` raises, with the same meaning:
                the row was not placed.

        Notes:
            * **A 200 means the row is ARMED — not that it will ever book.** A
              trigger-time failure arrives as ``status: "FAILED"`` with a
              ``rejectionReason`` on the ``portfolio`` channel; a reason on a row
              that is still ``ARMED`` is a deferred retry, not a failure.
            * A ``*_MARKET`` type is a *protected limit*, not a market order:
              its price is fixed at placement, so after a gap past it the
              booked order rests instead of executing.
            * The minimum order value ($10) is measured at the signed price; a
              SELL of your whole holding is exempt. Your cash and holding are
              checked only when the row fires.
            * A re-send of the same signed order answers with the same
              ``orderId`` whatever the row's status, but the checks run again
              first.
            * Cancel with :meth:`cancel_conditional` while ``PENDING`` / ``ARMED``;
              once ``PLACED`` use :meth:`cancel` with the row's ``placedOrderId``
              (:meth:`cancel_row` picks for you).
        """
        key = self._credentials()
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
        ttl = _ttl(ttl_ms)
        return self._place(key, ticker, side, otype, qty_d, price_d, ttl, trigger_d=trigger_d)

    def stop_loss(
        self,
        ticker: str,
        quantity: float,
        trigger_price: float,
        *,
        max_slippage_bps: int | None = None,
        ttl_ms: int | None = None,
    ) -> Any:
        """Arm a protective SELL that fires when the mark FALLS to ``trigger_price``.

        ``STOP_MARKET`` SELL: when it fires it books a LIMIT SELL at
        ``trigger_price x (1 - max_slippage_bps/10,000)`` (default: the
        client's ``max_slippage_bps``, 2% unless configured), so after a gap
        past that price it rests instead of selling.

        ``quantity`` is fixed for the life of the row, and your holding is not
        checked until it fires: a stop that fires short of tokens cancels your
        resting SELLs on this property and tries once more, and if the holding
        is still below ``quantity`` the row turns ``FAILED`` and sells nothing.
        Nothing warns you earlier, so when you sell part of the position,
        cancel the stop (:meth:`cancel_conditional`) and re-arm it at the new
        size. For a take-profit / stop-loss pair that cancels itself (OCO),
        attach ``tp_price`` / ``sl_price`` to the BUY (:meth:`create`).
        """
        return self.create_conditional(
            ticker,
            OrderSide.SELL,
            quantity,
            type=ConditionalOrderType.STOP_MARKET,
            trigger_price=trigger_price,
            max_slippage_bps=max_slippage_bps,
            ttl_ms=ttl_ms,
        )

    def take_profit(
        self,
        ticker: str,
        quantity: float,
        trigger_price: float,
        *,
        max_slippage_bps: int | None = None,
        ttl_ms: int | None = None,
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
            ttl_ms=ttl_ms,
        )

    # -- Re-send ------------------------------------------------------------ #

    def resubmit(self, signed_body: Mapping[str, Any]) -> Any:
        """Re-send a signed placement exactly as it was (e.g. the ``signed_body``
        of an :class:`~loaf.exceptions.OrderOutcomeUnknownError`).

        If the first attempt landed, the answer is that same order as it is
        now (on ``/orders`` with ``duplicate: True``). A stop or take is
        answered with just the ``orderId`` of its row, whatever its status: a
        re-send never re-arms one that was cancelled (as a competition round
        does on the real markets), fired or FAILED. So read that row's
        ``status`` in ``history.orders()`` before relying on it, and arm a new
        one if it is ``CANCELLED`` or ``FAILED``. If the first attempt did not
        land, the order is placed now, at the price and quantity signed then,
        so resubmit only while you still want it. It cannot be placed twice:
        the exchange admits one order per account and order number.

        Anything but a 200 raises :class:`~loaf.exceptions.OrderOutcomeUnknownError`
        again, with the reason in ``last_error``: whatever this re-send ran
        into, the first attempt may still be live. A refusal does not prove the
        first attempt failed either: checks run before the duplicate is
        recognised (the open-order cap, for example, counts the order that
        landed). A lasting refusal (a halt, the cap) will not change by
        re-sending, so look for the order in ``history.orders()`` (every
        status) then; an order that filled is not in ``openOrders``.

        After an epoch move (a Reset trading key, or a cancel-all sent on chain)
        the exchange refuses it (``l2 epoch: …``) and has cancelled any resting
        order of the old epoch. While a competition round is PREPARED or ACTIVE,
        a real market's body is refused (``NOT_TRADEABLE_IN_MODE``, so
        ``OrderOutcomeUnknownError`` again) and its history is not served: keep
        the body and resubmit it after the round ends, while it can still rest.

        Needs neither the trading key nor the API key. Once the order can no
        longer rest (its ``l2ExpiryMs`` minus 24 hours has passed) nothing is
        sent, since the exchange refuses it then: the
        ``OrderOutcomeUnknownError`` has ``attempts`` 0 and the reason in
        ``last_error``. Stop resubmitting it: find it in ``history.orders()`` by
        its ``expiryMs``, and cancel any stop, take or TP/SL leg of it that is
        still ``ARMED``.
        """
        body = signed_body if isinstance(signed_body, Mapping) else {}
        if not (
            all(isinstance(body.get(field), str) for field in _SIGNED_BODY_STRS)
            and all(_is_int(body.get(field)) for field in _SIGNED_BODY_INTS)
        ):
            raise _client_validation_error(
                "resubmit needs a signed placement body (its l2 fields and signature), "
                "e.g. OrderOutcomeUnknownError.signed_body"
            )
        expiry_ms = body["l2ExpiryMs"]
        rests_until_ms = expiry_ms - L2_BUFFER_MS
        if time.time_ns() // 1_000_000 >= rests_until_ms:
            until = datetime.fromtimestamp(rests_until_ms // 1000, tz=timezone.utc)
            refusal = _client_validation_error(
                f"This order could rest only until {until.isoformat()} (its l2ExpiryMs minus 24 "
                "hours): the exchange refuses it now. If it landed, any part of it still resting "
                "is cancelled, but a stop or take, or a TP/SL leg, stays ARMED, holding an "
                "open-order slot, until it fires and FAILS: find it in history.orders() "
                f"(expiryMs {expiry_ms}) and cancel it."
            )
            # Nothing is sent, but the first attempt may have landed, and filled: still unknown.
            raise OrderOutcomeUnknownError(
                signed_body=copy.deepcopy(dict(body)), attempts=0, last_error=refusal
            ) from refusal
        path = "/orders/conditional" if "triggerPrice" in body else "/orders"
        return self._client._send_signed(path, dict(body), ambiguous=True)

    # -- Cancel ------------------------------------------------------------ #

    def cancel(self, order_id: int) -> Any:
        """``POST /orders/cancel`` — cancel one open order by id.

        Cancellable only while ``OPEN`` / ``PARTIALLY_FILLED``. A 200 means the
        cancel has committed. Raises:

        * :class:`~loaf.exceptions.LoafConflictError` (409) if the engine no
          longer holds the order; the message can name its status
          (``Order can no longer be cancelled — it is FILLED.``);
        * :class:`~loaf.exceptions.LoafValidationError` (400) for an order
          already known to be in a terminal status, or (``status_code`` 0,
          nothing sent) for an ``order_id`` that is not a whole number from 1;
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
        for the same race — an idempotent cancel must catch both. A
        ``status_code`` 0 :class:`~loaf.exceptions.LoafValidationError` does not
        mean a terminal row: the ``order_id`` is not a whole number from 1, so
        nothing was sent and the order may still be live.

        This route is database-only, so unlike :meth:`cancel` and
        :meth:`cancel_all` it still works under a platform-wide halt. That makes
        it the only way to stop an ``ARMED`` row firing into the reopen:
        triggering is level-based, so a row whose mark sat through its trigger
        for the whole halt fires as soon as trading resumes.
        """
        return self._client.post(
            "/orders/conditional/cancel", json={"orderId": _order_id(order_id)}
        )

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

        It cancels your ``PENDING`` / ``ARMED`` conditionals FIRST and prepends
        their conditional-row ids to ``cancelledOrderIds``, so those ids will
        404 against any booked-order lookup. Then one request to the matching
        engine cancels all your resting orders at once, atomically: an order
        placed while it runs may or may not be included, one placed after the
        response is not.

        ``failedOrders`` lists only orders the engine could not cancel because
        the request did not complete on their property (it failed there, or
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

    def _credentials(self) -> TradingKey:
        """The trading key that signs, once both keys a placement needs are set."""
        key = self._client._l2_key
        if key is None:
            raise LoafConfigError(
                f"Placing orders needs your trading key: set ${ENV_SECRET_KEY} (the web app's "
                "Show trading key gives it; see the README) or pass secret_key=..."
            )
        if not self._client.api_key:
            raise LoafConfigError(
                "Placing orders needs your API key too: the SDK reads your trading account and "
                f"reserves order numbers with it. Pass api_key=... or set ${ENV_API_KEY}."
            )
        return key

    def _tolerance(self, max_slippage_bps: int | None, field: str = "max_slippage_bps") -> int:
        """This order's max slippage, else the client's (re-checked: it is a public attribute)."""
        if max_slippage_bps is None:
            return validate_slippage_bps(
                self._client.max_slippage_bps, "LoafClient.max_slippage_bps"
            )
        return validate_slippage_bps(max_slippage_bps, field)

    def _place(
        self,
        key: TradingKey,
        ticker: str,
        side: str,
        otype: str,
        qty_d: Decimal,
        price: Decimal | Callable[[Any], Decimal],
        ttl: int,
        trigger_d: Decimal | None = None,
        legs: Sequence[tuple[str, Decimal, Decimal]] = (),
    ) -> Any:
        """Read what the order signs, take its order numbers, sign it and send it.

        ``price`` is the order's price, or (a MARKET order without
        ``reference_price``) the function that derives it from the exchange's
        reference; ``trigger_d`` makes it a conditional; ``legs`` are its TP/SL
        legs as ``("tp" | "sl", trigger, price)``.
        """
        l2 = self._l2
        span = 1 + len(legs)
        account = block = None
        first = 0
        try:
            market = l2.market(ticker)
            account = None if market is None else l2.cached_account(market)
            if market is None or account is None or not isinstance(price, Decimal):
                # The account is read only right after a detail read of the order's ticker whose
                # venue check passed; a MARKET order reads the detail for its reference anyway.
                detail = self._client._request(
                    "GET", f"/trade/{ticker}", auth=False, before_order=True
                )
                l2.note_mode(detail)  # before the venue check: a refused read shows the mode too
                market = market_of(detail, ticker)
                l2.remember(ticker, market)
                account = l2.account(market, key.public_key, ticker)
                if not isinstance(price, Decimal):
                    price = price(_reference_from(detail, ticker))
            first, block, expiry_ms = l2.take(market, account, span, ttl)

            body: dict[str, Any] = {
                "ticker": ticker,
                "price": float(price),
                "quantity": float(qty_d),
                "side": side,
                "type": otype,
            }
            if trigger_d is not None:  # a conditional: the order it books when the mark gets there
                body["triggerPrice"] = float(trigger_d)
            body.update({
                "timeInForce": TimeInForce.GTC.value,
                "l2ChainId": market.l2_chain_id,
                "l2AccountIndex": account.account_index,
                "l2MarketIndex": market.market_index,
                "l2OrderIndex": first,
                "l2Epoch": account.epoch,
                "l2FeeRateCap": L2_FEE_RATE_CAP,
                "l2ExpiryMs": expiry_ms,
                "l2BaseAssetId": market.base_asset_id,
                "l2QuoteAssetId": market.quote_asset_id,
                **_signed(key, market, account, first, expiry_ms, side, price, qty_d),
            })
            # Each leg is a SELL of the parent's quantity at its own price, with the parent's
            # expiry and the next order number: tp, then sl.
            for index, (leg, trigger, leg_price) in enumerate(legs, first + 1):
                body[leg] = {
                    "triggerPrice": float(trigger),
                    "price": float(leg_price),
                    "l2OrderIndex": index,
                    "l2ExpiryMs": expiry_ms,
                    **_signed(
                        key, market, account, index, expiry_ms, OrderSide.SELL, leg_price, qty_d
                    ),
                }
            path = "/orders/conditional" if trigger_d is not None else "/orders"
            return self._client._send_signed(path, body)
        except OrderOutcomeUnknownError as exc:
            # It may be live, so its order numbers are never given back: only a resubmit of this
            # very body (a duplicate at worst) may ever carry them.
            if isinstance(exc.last_error, LoafAPIError):
                l2.after_refusal(ticker, account, block, exc.last_error)
            raise
        except LoafError as exc:
            # Definite: nothing this call sent can be live (it signed at most one body, and every
            # attempt carrying it was refused or never arrived), so a later order may sign the
            # same numbers (L2State.give_back). Never re-signed here: one call, one signed body.
            if isinstance(exc, LoafAPIError):
                l2.after_refusal(ticker, account, block, exc)
            if block is not None:
                l2.give_back(block, first, span)
            raise


def _ttl(ttl_ms: Any) -> int:
    """``ttl_ms=`` checked, or the default life when omitted."""
    if ttl_ms is None:
        return DEFAULT_ORDER_TTL_MS
    whole = isinstance(ttl_ms, numbers.Integral) and not isinstance(ttl_ms, bool)
    if not whole or not MIN_ORDER_TTL_MS <= int(ttl_ms) <= MAX_ORDER_TTL_MS:
        raise _client_validation_error(
            f"ttl_ms must be a whole number of milliseconds from {MIN_ORDER_TTL_MS:,} (one "
            f"minute) to {MAX_ORDER_TTL_MS:,} (about 47.9 days) (got {ttl_ms!r})"
        )
    return int(ttl_ms)


def _order_id(order_id: Any) -> int:
    """``order_id`` checked: refused unless a whole number from 1, never rounded or read from
    text (3.9 or ``True`` would name another order)."""
    whole = isinstance(order_id, numbers.Integral) and not isinstance(order_id, bool)
    if not whole or int(order_id) < 1:
        # A mapping shows only its type: a signed body passed by mistake must stay out of logs.
        shown = type(order_id).__name__ if isinstance(order_id, Mapping) else repr(order_id)
        raise _client_validation_error(
            f"order_id must be an order's id, a whole number from 1 (got {shown})"
        )
    return int(order_id)


def _reference_from(detail: Any, ticker: str) -> Any:
    for item in detail.get("propertyList") or []:
        if item.get("ticker") == ticker:
            price = item.get("marketPrice")
            if isinstance(price, (int, float)) and not isinstance(price, bool) and price > 0:
                return price
            break
    raise _client_validation_error(
        f"No market reference price for {ticker!r} right now; pass reference_price= "
        "(e.g. the markprice WebSocket value) or place a LIMIT order"
    )


def _signed(
    key: TradingKey,
    market: Market,
    account: AccountState,
    index: int,
    expiry_ms: int,
    side: str,
    price: Decimal,
    quantity: Decimal,
) -> dict[str, str]:
    """``l2SigS`` / ``l2SigE`` over one order's 12 signed terms: an order, a leg or a
    conditional (the order it books)."""
    with localcontext(_DECIMAL_CONTEXT):
        price_scaled, quantity_scaled = int(price * 1000), int(quantity * 10)  # exact on the grid
    try:
        message = k1.order_message(
            l2_chain_id=market.l2_chain_id,
            l2_market_index=market.market_index,
            l2_base_asset_id=market.base_asset_id,
            l2_quote_asset_id=market.quote_asset_id,
            is_ask=side == OrderSide.SELL,
            price=price_scaled,
            l2_fee_rate_cap=L2_FEE_RATE_CAP,
            l2_account_index=account.account_index,
            l2_order_index=index,
            l2_epoch=account.epoch,
            quantity=quantity_scaled,
            l2_expiry_ms=expiry_ms,
        )
    except k1.K1Error as exc:
        # The engine's own text, e.g. K1's bound on price x quantity; nothing was sent.
        raise _client_validation_error(str(exc)) from None
    s, e = key.sign(message)
    return {"l2SigS": s, "l2SigE": e}

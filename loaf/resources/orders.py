"""Trading: place and cancel orders, stop & take orders.

Placing an order is a single ``POST /orders``; the common case is a one-liner::

    loaf.orders.limit_buy("OPRA", quantity=10, price=167.49)

**Signing.** Every placement is signed on your machine with your agent key
(``agent_private_key=`` / ``$LOAF_AGENT_PRIVATE_KEY``) and authenticated by that
signature alone: the API key is not sent. Cancels still use the API key. The
first order on a property reads its token contract once (a public request); if
a competition round has since redeployed it, the SDK notices the refusal,
re-reads the contract and re-signs once.

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

* a trading-competition round is ACTIVE and your account is not admitted, or a
  round is switching (:class:`~loaf.exceptions.CompetitionEligibilityError` —
  check ``loaf.competition.queue_position()``); outside a round trading is
  unrestricted,
* trading is halted platform-wide or for the property
  (:class:`~loaf.exceptions.TradingHaltedError`), or
* the market is closed (:class:`~loaf.exceptions.LoafForbiddenError`,
  ``Trading is currently closed``).

Server-side checks reject an order the SDK cannot pre-validate for you (all 400,
:class:`~loaf.exceptions.LoafValidationError`):

* **Minimum order value** — price x quantity must be at least 10 USD, measured
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

**Retries.** Placements are re-sent unchanged on timeouts and 5xx; a repeat is
answered as a duplicate, never placed twice. If that does not settle it the SDK
raises :class:`~loaf.exceptions.OrderOutcomeUnknownError`: see
:meth:`OrdersResource.resubmit`.

Placements and cancels are also per-account rate limited (429 on abuse), on
top of the per-IP limit.
"""

from __future__ import annotations

import re
import time
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Callable, Mapping

from ..constants import DEFAULT_ORDER_DEADLINE, ORDER_NONCE_WINDOW_MS
from ..enums import (
    CONDITIONAL_ORDER_TYPES,
    ConditionalOrderStatus,
    ConditionalOrderType,
    OrderSide,
    OrderType,
    TimeInForce,
    is_conditional_order,
)
from ..exceptions import LoafAuthError, _client_validation_error
from ..money import (
    _check_side,
    _worst_price,
    validate_price,
    validate_quantity,
    validate_slippage_bps,
)
from .base import Resource

if TYPE_CHECKING:
    from ..client import LoafClient
    from ..signing import OrderSigner

_TICKER_RE = re.compile(r"[A-Z0-9]{1,4}")
_NONCE_RE = re.compile(r"[0-9a-f]{32}")

# The signer 401s a stale token contract produces: it recovers a stranger's address.
_STALE_CONTRACT_401S = frozenset({
    "Signer is not authorized to trade for any account",
    "Every signature in the request must be by the same signer",
})

_LIMIT_CONDITIONALS = (ConditionalOrderType.STOP_LIMIT, ConditionalOrderType.TAKE_LIMIT)


class OrdersResource(Resource):
    def __init__(self, client: LoafClient) -> None:
        super().__init__(client)
        #: ticker -> the property's token contract, as served. Filled lazily; healed on a
        #: signer 401 (competition rounds redeploy the contracts).
        self._contracts: dict[str, str] = {}

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
        """``POST /orders`` — place a trading order, signed with your agent key.

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
            ``{success, orderId, status, quantityLeft, duplicate}`` — the order
            as of the commit that admitted it (:class:`~loaf.models.OrderResult`).

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
            * The legs' ids are NOT in the response — only the parent's
              ``orderId``. They arrive on the ``portfolio`` channel as
              ``order_update`` rows and show up in ``openOrders`` as ``PENDING``
              conditionals.
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
        signer = self._client._require_signer()
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
                (key, trigger_d, _worst_price(trigger_d, OrderSide.SELL, leg_bps))
                for key, trigger_d in (("tp", tp_d), ("sl", sl_d))
                if trigger_d is not None
            ]
        elif leg_max_slippage_bps is not None:
            raise _client_validation_error(
                "leg_max_slippage_bps only applies when tp_price or sl_price is set"
            )

        fetched = False  # whether this call has already read the property (and its contract)
        if limit_d is not None:
            price_d = limit_d
        else:
            bps = self._tolerance(max_slippage_bps)
            if reference_price is None:
                reference_price = _reference_from(self._fetch_property(ticker), ticker)
                fetched = True
            price_d = _worst_price(reference_price, side, bps)

        def build(contract: str) -> dict:
            body = {
                "ticker": ticker,
                "price": float(price_d),
                "quantity": float(qty_d),
                "side": side,
                "type": otype,
                "timeInForce": TimeInForce.GTC.value,
                "deadline": DEFAULT_ORDER_DEADLINE,
                **_signed(signer, contract, side, price_d, qty_d),
            }
            for key, trigger_d, leg_price_d in legs:
                body[key] = {
                    "triggerPrice": float(trigger_d),
                    "price": float(leg_price_d),
                    **_signed(signer, contract, OrderSide.SELL.value, leg_price_d, qty_d),
                }
            return body

        return self._place("/orders", ticker, build, fetched=fetched)

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
        """``POST /orders/conditional`` — place a stop / take order, signed with your agent key.

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
            * The minimum order value (10 USDC) and a BUY's cash check are
              measured at the signed price. A SELL of your whole holding is
              exempt from the minimum.
            * A re-send of the same signed order answers with the same
              ``orderId`` whatever the row's status, but the checks run again
              first.
            * Cancel with :meth:`cancel_conditional` while ``PENDING`` / ``ARMED``;
              once ``PLACED`` use :meth:`cancel` with the row's ``placedOrderId``
              (:meth:`cancel_row` picks for you).
        """
        signer = self._client._require_signer()
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

        def build(contract: str) -> dict:
            return {
                "ticker": ticker,
                "price": float(price_d),
                "quantity": float(qty_d),
                "side": side,
                "type": otype,
                "triggerPrice": float(trigger_d),
                "timeInForce": TimeInForce.GTC.value,
                "deadline": DEFAULT_ORDER_DEADLINE,
                **_signed(signer, contract, side, price_d, qty_d),
            }

        return self._place("/orders/conditional", ticker, build)

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
        it. It cannot be placed twice, unless a competition round switch (a
        round being prepared or ending) has happened since it was signed. A
        switch deletes every order, and with them the record that makes a
        re-send a duplicate, so a body signed before a switch would simply be
        placed again. Never resubmit one; compare your positions and cash to
        see whether it landed.

        Anything but a 200 raises :class:`~loaf.exceptions.OrderOutcomeUnknownError`
        again, with the reason in ``last_error``: whatever this re-send ran
        into, the first attempt may still be live. A refusal does not prove the
        first attempt failed either: checks run before the duplicate is
        recognised (the open-order cap, for example, counts the order that
        landed). A lasting refusal (a halt, the cap) will not change by
        re-sending, so look for the order in ``history.orders()`` (every
        status) then; an order that filled is not in ``openOrders``.

        Needs neither the agent key nor the API key. Refused locally when the
        order was signed more than 24 hours ago, which the exchange no longer
        accepts.
        """
        nonce = signed_body.get("nonce") if isinstance(signed_body, Mapping) else None
        if (
            not isinstance(nonce, str)
            or not _NONCE_RE.fullmatch(nonce)
            or not isinstance(signed_body.get("signature"), str)
        ):
            raise _client_validation_error(
                "resubmit needs a signed placement body (with its nonce and signature), "
                "e.g. OrderOutcomeUnknownError.signed_body"
            )
        if abs(time.time() * 1000 - int(nonce[:12], 16)) > ORDER_NONCE_WINDOW_MS:
            raise _client_validation_error(
                "This order was signed more than 24 hours ago, so the exchange would refuse it and "
                "could no longer confirm a duplicate. Look for it in history.orders() instead "
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
        lookup. It is NOT available under a platform-wide halt — it refuses
        before cancelling anything, and :meth:`cancel_conditional` is the only
        cancel that still works then. A 503 can arrive after the conditionals
        were already cancelled.
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

    def _fetch_property(self, ticker: str) -> Any:
        # market.property(), minus waiting out a 429 (a late order is stale); a 404 propagates.
        detail = self._client._request("GET", f"/trade/{ticker}", auth=False, before_order=True)
        self._contracts[ticker] = _contract_from(detail, ticker)
        return detail

    def _place(
        self, path: str, ticker: str, build: Callable[[str], dict], *, fetched: bool = False
    ) -> Any:
        """Sign with the property's contract and send. ``fetched``: the caller read the
        property during this call, so its cached contract is fresh."""
        if ticker not in self._contracts:
            self._fetch_property(ticker)
            fetched = True
        contract = self._contracts[ticker]
        try:
            return self._client._send_signed(path, build(contract))
        except LoafAuthError as exc:
            # A cached contract a competition round has since replaced: re-read it and, if it
            # changed, re-sign with fresh nonces once. Safe only because no earlier attempt of
            # this body can have landed: after an ambiguous attempt _send_signed raises
            # OrderOutcomeUnknownError instead (even for a 401), which must never be caught here.
            if fetched or exc.message not in _STALE_CONTRACT_401S:
                raise
            self._fetch_property(ticker)
            current = self._contracts[ticker]
            if current.lower() == contract.lower():
                raise
            return self._client._send_signed(path, build(current))


def _check_ticker(ticker: Any) -> None:
    if not isinstance(ticker, str) or not _TICKER_RE.fullmatch(ticker):
        raise _client_validation_error(
            "ticker must be 1-4 uppercase letters or digits (a property's ticker, e.g. 'OPRA'), "
            f"got {ticker!r}"
        )


def _contract_from(detail: Any, ticker: str) -> str:
    prop = detail.get("property") if isinstance(detail, dict) else None
    address = prop.get("contractAddress") if isinstance(prop, dict) else None
    if not address:
        raise _client_validation_error(
            f"Trading unavailable: {ticker!r} has no deployed token contract yet, "
            "so an order for it cannot be signed"
        )
    return str(address)


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
    signer: OrderSigner, contract: str, side: str, price: Decimal, quantity: Decimal
) -> dict[str, str]:
    """A fresh nonce and the signature over it: every signed order and leg gets its own."""
    from .. import signing  # lazy: eth-account is importable once a signer exists

    nonce = signing.new_order_nonce()
    signature = signer.sign_order(
        contract_address=contract, side=side, price=price, quantity=quantity, nonce=nonce
    )
    return {"nonce": nonce, "signature": signature}

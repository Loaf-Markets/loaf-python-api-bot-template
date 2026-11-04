"""Trading: place and cancel orders, stop & take orders.

Placing an order is a single ``POST /orders``; the common case is a one-liner::

    loaf.orders.limit_buy("OPRA", quantity=10, price=167.49)

You name an order's market by its ticker (``"OPRA"``; a perp's ends in
``-PERP``); the order itself carries only the market index it signs.

**Signing.** Every placement is signed on your machine with your trading key
(``trading_key=`` / ``$LOAF_TRADING_KEY``) and authenticated by that
signature alone: no login token is sent with it. Cancels and the account
requests below are authenticated by a login token the trading key signs.

**What the SDK reads for you.** An order signs its market (``market.marketIndex``
and ``market.l2`` of ``GET /trade/{ticker}``), your trading account's index (your
trading key's) and its epoch (``GET /l2/keys``), and an order number reserved to
your account (``POST /l2/order-indices``), reserved in blocks that grow from what
your first order needs to 64. All of it is cached in memory and refreshed when a
refusal shows it stale (a placement refused as no market, or as one that is not
listed, or on a perp as outside its band with a price the SDK derived there,
reads the ticker's market again). A perp's terms come with its market: its band
bounds the prices the SDK derives there (**The perps band**). The order numbers
of an order that was definitely not placed are reused by later orders; a
restart leaves the unused rest of a block unused. An order on a market of
another L2 chain than your trading key's is refused before anything is signed.

**Order life.** Every order expires. ``ttl_ms=`` sets how long it may rest
(default 7 days, from one minute to about 45.9 days): the SDK signs the expiry
``l2ExpiryMs`` = now + 72 hours + ``ttl_ms`` (``expiryMs`` on order rows), and
the exchange stops filling the order 72 hours before it and cancels it (a
``CANCELLED`` you did not request). TP/SL legs share their
parent's life. The exchange FAILS an ARMED stop, take or leg once its life ends
(its ``l2ExpiryMs`` less 72 hours) or its account's epoch moves, within about a
second (it sweeps them every second), whether or not it has fired (a fired one
the band holds stays ARMED, below): it does not wait for the row to fire. So
cancel and re-arm one you want kept before its life ends. After an epoch
move — a Reset trading key in the web app, a cancel-all sent on chain, or any
placement refused ``SETTLE_EPOCH_MISMATCH`` — the exchange has cancelled your
resting orders and FAILS the stops, takes and TP/SL legs signed before it, within
about a second: place them again, a TP/SL leg as a separate
:meth:`OrdersResource.stop_loss` / :meth:`OrdersResource.take_profit` (not OCO).

**The response.** A 200 from :meth:`OrdersResource.create` is sent after the
exchange commits the order, and carries ``status`` (``OPEN`` /
``PARTIALLY_FILLED`` / ``FILLED`` / ``CANCELLED``), ``quantityLeft``,
``duplicate`` and ``attachedLegIds``. ``FILLED`` means filled. ``CANCELLED`` on
a new order means it stopped early: a BUY whose hold could not pay for its next
fill, or a SELL stopped at K1's bound. Later fills and cancels arrive on the
private ``portfolio`` WebSocket channel.

**MARKET orders** take no price. The SDK signs a worst price: the market
reference (a perp's live mark) moved against you by the max slippage
(``max_slippage_bps=``, default ``client.max_slippage_bps``; on a perp held
within its band, see **The perps band**), rounded toward the reference to the
cent. The engine matches it up to that price and **never rests it**: what it
cannot fill at once is cancelled, and one that fills nothing is refused. A BUY
holds worst price x quantity x (1 + :data:`~loaf.constants.L2_FEE_RATE_CAP` /
1,000,000) of your cash while it matches.

**Time in force.** ``time_in_force=`` (:class:`~loaf.enums.TimeInForce`) on
:meth:`OrdersResource.create`, :meth:`OrdersResource.limit_buy` and
:meth:`OrdersResource.limit_sell`: ``GTC`` (the default) rests what does not
fill at once; ``IOC`` cancels it, and is refused when it fills nothing; ``FOK``
fills the whole quantity at once or is refused; ``POST_ONLY``, a LIMIT order's
alone, is refused (:class:`~loaf.exceptions.PostOnlyWouldTakeError`) when it
would trade at once, so it only ever rests. A stop or take, and a TP/SL leg,
books a GTC order.

**Perps.** A perp (its ticker ends in ``-PERP``; its ``market.type`` is
``"PERP"``) trades a perpetual future settled in USDC: a BUY opens or grows a
long, or shrinks a short, and a SELL the other way round. Each order trades
from one margin pool, ``pool=``: 0, the cross pool (the default), or an
isolated pool, 1 to 7 (fund it first, :meth:`~loaf.resources.perps.PerpsResource.add_margin`).
``reduce_only=True`` lets its fills only shrink the pool's position. The SDK
signs both, and refuses either on a spot market. TP/SL legs attach to a perp's
BUY or SELL (a SELL's mirrored: its take-profit below, its stop-loss above),
never to a reduce-only one; each leg is a reduce-only order on the other side,
in the order's pool. An expired perp takes no orders: close a position in it
with :meth:`~loaf.resources.perps.PerpsResource.exit`.

**The perps band.** Each perp has a band, ``market.l2.band`` basis points (0:
none), centred on its mark (``market.live.markPrice``; a mark of 0, before the
market's first price report, bands nothing). The exchange refuses a perps order
of any kind (LIMIT, MARKET, IOC, a reduce-only close, the order a stop books
when it fires) whose signed price lies outside ``mark x (1 ± band/10,000)``,
both edges admitted, and, when it is placed, a perps stop, take or TP/SL leg
whose limit lies outside the band around its own trigger (``|limit - trigger|
x 10,000 > band x trigger``), each with 400 ``PRICE_BAND``. So on a perp a
price the SDK derives starts from its reference moved
``min(max slippage, band // 2)`` basis points against you, as the web app
prices, rounded toward the reference to the cent: a MARKET order's worst price
(off the live mark, unless you pass ``reference_price``), a ``*_MARKET``
stop's or take's booked price and a TP/SL leg's limit (off their trigger). The
other half of the band is left for the mark to move before the order is
admitted. That price is signed if it did not cross the reference and lies
inside the band, which it does unless the $0.01 floor lifted it: a MARKET
BUY's off a reference under one cent is $0.01, inside the band only from
``10,000 x (0.01 - reference) / reference`` bp (10,000 off a mark of $0.005).
Rounding toward a reference off the cent grid can cross it (a SELL's price
above it, a BUY's below it): a mark is in tenths of a cent, while a trigger is
on the grid, so a stop's or a leg's price never crosses. A MARKET order whose
price crossed signs instead the cent on the reference's near side (a SELL's
below it, a BUY's above it: against you) when that cent is positive, inside
the band (``|cent - reference| x 10,000 <= band x reference``) and within your
max slippage of the reference (all of it, not half the band); else the
crossing price, when it lies inside the band. An order left with no such price
is refused locally (:class:`~loaf.exceptions.LoafValidationError`,
``status_code`` 0: nothing is reserved, signed or sent). At a mark of $1.009
and a band of 100 bp, say, a MARKET SELL at the default 2% signs 1.00, not the
crossing 1.01. A band of 0 changes nothing. A price you give exactly is signed
as given, never moved or refused for the band: a LIMIT order's, a ``*_LIMIT``
stop's or take's, and a MARKET order's ``reference_price`` with a max slippage
of 0 (to the cent: off the grid, a BUY's rounds down and a SELL's up). The band
is read with the market and kept, and read again once an order that signed a
price the SDK derived on it (its own, or a TP/SL leg's limit) is refused
``PRICE_BAND``; an order whose prices were all given keeps it.

**Refusals name a code.** Every refusal of a placement or a cancel carries
``code`` (:attr:`~loaf.exceptions.LoafAPIError.code`), one of
:class:`~loaf.enums.OrderErrorCode`: branch on it, never on the message, which
is for people and may change. The exchange may add codes; treat one you do not
know by its status (its exception class). The SDK picks the exception class
from the status and the code.

Order placement can be rejected with a 403 when:

* trading is halted, platform-wide (``TRADING_HALTED``) or for the market
  (``MARKET_HALTED``) (:class:`~loaf.exceptions.TradingHaltedError`),
* the market does not trade (``MARKET_NOT_LIVE``,
  :class:`~loaf.exceptions.LoafForbiddenError`): before it is listed, before
  its trading starts, or once it is delisted, or
* the market's own state refuses it (:class:`~loaf.exceptions.MarketStateError`,
  its ``state`` the reason): halted (:class:`~loaf.exceptions.MarketHaltedError`),
  outside its trading hours (``MARKET_CLOSED``), reduce-only (``REDUCE_ONLY``),
  its price stale (``PRICE_STALE``), or reduce-only while its latest price
  waits for review (``PRICE_HELD``, :class:`~loaf.exceptions.PriceHeldError`).
  In the last three an order that reduces your position is still taken.

Server-side checks reject an order the SDK cannot pre-validate for you (all 400,
:class:`~loaf.exceptions.LoafValidationError`):

* **Minimum order value** (``BELOW_MIN_ORDER_VALUE``) — price x quantity must
  be at least $10, measured at the price you **sign** (a MARKET order's worst
  price). A SELL closing your ENTIRE available holding of the asset is exempt,
  so a dust holding can always be flattened.
* **Price band** (``PRICE_BAND``) — an order of **any** type, MARKET included,
  whose signed price is too far from the market reference, or outside the
  market's daily band around its daily reference price (deployment and market
  settings, which the message quotes). On a perp, a price outside the market's
  band around its mark (``l2 perps: the price is outside the market's band
  around its mark``), and a stop's, take's or TP/SL leg's limit outside the band
  around its own trigger (**The perps band**, above).
* **Balance and open-order caps** (``INSUFFICIENT_BALANCE``,
  ``OPEN_ORDER_LIMIT``) — not enough available balance, or too many open
  orders (resting stops count too).
* **Fee-rate cap** (``FEE_RATE_CAP_TOO_LOW``) — your account's fee rate is
  above :data:`~loaf.constants.L2_FEE_RATE_CAP`, which every order signs.
* **Time in force** — an IOC or MARKET order that fills nothing
  (``NOT_FILLED``), a FOK order that cannot fill in full (``FOK_NOT_FILLED``),
  and a POST_ONLY order that would trade at once (``POST_ONLY_WOULD_TAKE``,
  :class:`~loaf.exceptions.PostOnlyWouldTakeError`).

An order that would trade with a resting order of your own is not refused: the
exchange cancels the resting one (``order_status`` ``CANCELLED``) and the new
order goes on matching.

**Order controls.** :meth:`OrdersResource.create` and its four wrappers (not
the stops and takes) take three controls that are sent with the order but not
signed:

* ``order_tag=``, 1 to 4,294,967,295: your label for a group of orders. The
  order keeps it while it rests; :meth:`OrdersResource.cancel_all` and
  :meth:`OrdersResource.cancel_after` can select by it, and its rows and its
  ``order_status`` / ``order_update`` frames carry it as ``orderTag``, with its
  ``l2OrderIndex``.
* ``send_by_ms=``, unix ms: the latest time the exchange may decide the order.
  Arriving later, or decided later, it is refused
  (:class:`~loaf.exceptions.SendByPassedError`): nothing is placed, and the
  order number is not spent. A quote delayed in transit never lands late.
* ``replaces_order_id=``: your order resting on the same market, which the
  exchange cancels in the batch that decides the new order, before it: the two
  never rest together, the old order's hold is free for the new one's checks,
  and the open-order cap counts one order less. If the old order no longer
  rests there, the new one is refused
  (:class:`~loaf.exceptions.ReplaceTargetGoneError`) and nothing changes. If
  the cancel went through and the new order is then refused for another
  reason, the old one stays cancelled, and the error's
  ``replaced_order_cancelled`` is ``True``.

**Cancels.** :meth:`OrdersResource.cancel` cancels one order by id,
:meth:`OrdersResource.cancel_conditional` one stop or take, and
:meth:`OrdersResource.cancel_all` every order, or one market's, or one tag's.
:meth:`OrdersResource.cancel_after` is the dead man's switch: the exchange
cancels a scope's orders unless you call it again before its deadline (call it
every 15 to 30 s with a 60 to 120 s timeout, from the loop that quotes).
:meth:`OrdersResource.cancel_by_index` settles an order by the order number it
signed, with no read: an order whose placement's answer you lost
(:class:`~loaf.exceptions.OrderOutcomeUnknownError`) is cancelled if it rests,
reported if it ended, and voided if it never arrived.

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
  A fired perps row whose order the band around the mark refuses (the mark
  moved past the band of its limit) is not failed: it stays ``ARMED`` and the
  exchange tries it again every second until the mark lets it in, or until its
  order's life ends or its account's epoch moves (``FAILED``), or its position
  closes (``CANCELLED``). Only a row
  whose limit lies outside the band around its own trigger (placed before the
  exchange checked that, or outside it since the market's band was narrowed)
  is ``FAILED``, with ``l2 perps: the price is outside the market's band
  around its trigger price`` as its ``rejectionReason``.
* **Trigger already reached** — placement is refused (400,
  :class:`~loaf.exceptions.LoafValidationError`) when the mark is already at or
  through the trigger in the order's direction. The SDK cannot see the mark, so
  it cannot pre-check this.
* **A perps limit outside the band around its trigger** — placement is refused
  (400 ``PRICE_BAND``, ``l2 perps: the price is outside the market's band
  around its trigger price``) when a perps row's limit lies outside the
  market's band around its trigger. The SDK keeps the ``*_MARKET`` price it
  derives inside it (**The perps band**); a ``*_LIMIT`` row's ``price`` is
  signed as given.

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
  ``resubmit(error.signed_body)`` while you still want it and it can still
  rest; once you do not, ``cancel_by_index(error.signed_body["l2OrderIndex"])``
  settles it. Never place a fresh order for the same intent while one may be
  live.

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
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, localcontext
from typing import TYPE_CHECKING, Any, Mapping, NamedTuple, Sequence

from .. import k1
from .._l2 import (
    _MAX_ORDER_INDEX,
    _MAX_SAFE_INT,
    AccountState,
    L2State,
    Market,
    TradingKey,
    _is_int,
    _whole,
    market_of,
)
from ..constants import (
    CANCEL_AFTER_MAX_MS,
    CANCEL_AFTER_MIN_MS,
    DEFAULT_ORDER_TTL_MS,
    ENV_BASE_URL,
    ENV_TRADING_KEY,
    L2_BUFFER_MS,
    L2_FEE_RATE_CAP,
    MAX_MARKET_INDEX,
    MAX_ORDER_TTL_MS,
    MAX_POOL,
    MIN_ORDER_TTL_MS,
    ORDER_TAG_MAX,
    SEND_BY_MS_MAX,
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
    LoafServerError,
    OrderOutcomeUnknownError,
    SendByPassedError,
    _client_validation_error,
)
from ..money import (
    _DECIMAL_CONTEXT,
    _PRICE_TICK,
    _check_side,
    _to_decimal,
    _worst_price,
    validate_price,
    validate_quantity,
    validate_slippage_bps,
)
from .base import Resource, _check_market_ticker

if TYPE_CHECKING:
    from ..client import LoafClient

_LIMIT_CONDITIONALS = (ConditionalOrderType.STOP_LIMIT, ConditionalOrderType.TAKE_LIMIT)
_TIMES_IN_FORCE = frozenset(member.value for member in TimeInForce)

_SIGNED_BODY_STRS = ("l2SigS", "l2SigE")
_SIGNED_BODY_INTS = ("l2OrderIndex", "l2AccountIndex", "l2ExpiryMs")

_TAG_RULE = f"a whole number from 1 to {ORDER_TAG_MAX:,}"
_SEND_BY_RULE = "a unix time in milliseconds, a whole number from 1 to 2^48 - 1"


class OrdersResource(Resource):
    def __init__(self, client: LoafClient) -> None:
        super().__init__(client)
        #: The markets, account and order numbers your orders sign, refreshed on refusals.
        self._l2 = L2State(client)
        #: The market index a cancel's ``ticker=`` names, as last read.
        self._market_indices: dict[str, int] = {}

    def warm(self, *tickers: str) -> None:
        """Do now what a first placement would do first: read each ticker's market and your
        account, and reserve order numbers. The next placement on them sends only the order.

        Raises what those reads raise when a placement makes them.
        """
        key = self._credentials()
        for ticker in tickers:
            _check_market_ticker(ticker)
            detail = self._client._request(
                "GET", f"/trade/{ticker}", auth=False, before_order=True
            )
            market = market_of(detail, ticker)
            self._check_chain(market, key, ticker)
            self._l2.remember(ticker, market)
        self._l2.warm(self._l2.account(key.l2_account_index))

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
        pool: int | None = None,
        reduce_only: bool = False,
        time_in_force: str = TimeInForce.GTC,
        order_tag: int | None = None,
        send_by_ms: int | None = None,
        replaces_order_id: int | None = None,
    ) -> Any:
        """``POST /orders`` — place a trading order, signed with your trading key.

        Every :class:`~loaf.exceptions.LoafAPIError` and
        :class:`~loaf.exceptions.LoafConnectionError` it raises means the order
        was not placed: anything that might have landed raises
        :class:`~loaf.exceptions.OrderOutcomeUnknownError` instead.

        Args:
            ticker: the market's ``ticker``, exactly as listed (e.g.
                ``"OPRA"``); pick one from
                :meth:`loaf.resources.market.MarketResource.list` (not every
                listed row is tradeable; see its docstring).
            side: ``BUY`` or ``SELL`` (:class:`~loaf.enums.OrderSide`).
            quantity: tokens, at most 1 decimal place, up to
                28,147,497,671,065.5.
            type: ``LIMIT`` (default) or ``MARKET`` (:class:`~loaf.enums.OrderType`).
            price: ``LIMIT`` only: dollars, at most 2 decimals, up to
                4,294,967.29. Refused for ``MARKET``, which signs a worst price
                derived from ``reference_price`` instead.
            reference_price: ``MARKET`` only: the price your max slippage is
                measured from. Default, read with one extra public request
                that can lag the live market: on a perp, its live mark
                (``market.live.markPrice``), the centre of its band; on spot,
                and on a perp whose mark is 0 (no price report yet) or not
                served (``market.live`` null, while the exchange may still band
                the order around its mark), the exchange's market reference
                (book mid, else last trade, else last candle close, else IPO
                price). Pass the
                ``markprice:{ticker}`` WebSocket value (a perp's: its mark)
                for a fresher reference (it also saves the request once the
                SDK has read the market).
            max_slippage_bps: ``MARKET`` only: the most the price may move
                against you, in basis points (default: the client's
                ``max_slippage_bps``, 2% unless configured). The SDK signs
                ``reference_price x (1 ± max_slippage_bps/10,000)`` (+ for a
                BUY, - for a SELL) as your worst fill price, see
                :func:`loaf.worst_price`. On a perp the SDK first moves it at
                most half the market's band, ``min(max_slippage_bps,
                band // 2)``; where rounding that to the cent crosses an
                off-grid reference (a SELL's above it, a BUY's below it), it
                signs the cent on the reference's near side if the band and
                ``max_slippage_bps`` admit it, else the crossing price if the
                band admits that, else refuses the order locally (the module
                docs, **The perps band**). A BUY off a reference under one
                cent starts at $0.01, the lowest price, and is refused so too
                unless the band admits that cent. For an exact worst price pass
                ``reference_price=X, max_slippage_bps=0``: ``X``, to the cent (a
                BUY's down, a SELL's up), is signed as given, never moved or
                refused for the band.
            tp_price: the TRIGGER level of an attached take-profit leg for this
                order's FULL quantity, on the other side. A BUY's on a spot
                market; a BUY's or a SELL's on a perp (not a
                reduce-only order's).
            sl_price: the TRIGGER level of an attached stop-loss leg, likewise.
            leg_max_slippage_bps: how far past its trigger each leg may trade
                when it fires (default: the client's ``max_slippage_bps``, never
                this order's ``max_slippage_bps``). A BUY's leg books a LIMIT
                SELL at ``trigger x (1 - leg_max_slippage_bps/10,000)``, a
                SELL's a LIMIT BUY at ``trigger x (1 + ...)``, so after a gap
                past that price it rests instead of trading. On a perp the
                move is at most half the market's band, which keeps each
                leg's limit inside the band around its trigger.
            time_in_force: ``GTC`` (default), ``IOC``, ``FOK`` or ``POST_ONLY``
                (:class:`~loaf.enums.TimeInForce`). ``GTC`` rests what does not
                fill at once (a MARKET order never rests: the exchange runs it as
                ``IOC``); ``IOC`` cancels it, and is refused (400) when it fills
                nothing; ``FOK`` fills the whole quantity at once or is refused
                (400) and fills nothing; ``POST_ONLY`` (LIMIT only: refused
                locally on a MARKET order) is refused
                (:class:`~loaf.exceptions.PostOnlyWouldTakeError`) when it would
                trade at once. Order rows show a post-only order as ``GTC``.
            ttl_ms: how long the order may rest, in whole milliseconds, from
                60,000 (one minute) to 3,968,658,432 (about 45.9 days); default
                604,800,000 (7 days). The exchange then stops filling it and
                cancels it; its TP/SL legs share its life. A bot's quotes
                should die with the bot: give them a short one.
            pool: perps only: the margin pool the order trades from,
                0 (the cross pool, the default) to 7 (an isolated pool).
                Refused on a spot market.
            reduce_only: perps only: ``True`` lets the order's fills
                only shrink the pool's position in the market, never open or
                flip one. Refused on a spot market.
            order_tag: your label for the order, a whole number from 1 to
                4,294,967,295 (default: none). :meth:`cancel_all` and
                :meth:`cancel_after` can select the orders carrying it, and
                its rows and frames show it as ``orderTag``.
            send_by_ms: the latest time, unix ms, the exchange may decide the
                order (default: none). Later, it is refused
                (:class:`~loaf.exceptions.SendByPassedError`) and nothing is
                placed. The exchange compares it with its own clock.
            replaces_order_id: the id of your order resting on the same
                market, to cancel in the same step, before this one is
                matched (default: none). See the module docs, **Order
                controls**.

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
            LoafConfigError: no trading key; a market on another L2 chain than
                your trading key's (it is another deployment's); this machine's
                clock hours ahead. Fix the setup, then restart.
            LoafAuthError: the exchange refused the login token of your account
                read or reservation: your trading key is not the one in force on
                its account (reset, not active yet, or another deployment's),
                which the message names, and cancels fail too: set the key in
                force and restart. Otherwise the exchange refused the order's
                signature or key; the next order re-reads your account.
            TradingHaltedError: trading is halted.
            MarketStateError: the market's own state does not take the order
                now (``state``); :class:`~loaf.exceptions.PriceHeldError` while
                its latest price waits for review.
            PostOnlyWouldTakeError: a ``POST_ONLY`` order would have traded at
                once (400); nothing was placed.
            SendByPassedError: ``send_by_ms`` passed before the exchange
                decided the order (400); nothing was placed.
            ReplaceTargetGoneError: the order ``replaces_order_id`` names does
                not rest on the market (409); nothing changed.
            LoafValidationError: refused locally (``status_code`` 0) or by the
                exchange (400); its ``code`` says why.
            OrderOutcomeUnknownError: the order may or may not be live (or
                already filled); :meth:`resubmit` it while you still want it, or
                look for it in ``history.orders()``, never place it afresh.

        Notes:
            * On a spot market a resting BUY holds price x quantity x (1 +
              :data:`~loaf.constants.L2_FEE_RATE_CAP` / 1,000,000) of your
              cash; a SELL holds the tokens it sells.
            * ``tp_price`` / ``sl_price`` attach protective legs to a **BUY**
              on a spot market (spot has no short to protect, so a SELL parent
              is refused once the market is read), and to a BUY or a SELL on a
              perp, where each leg is reduce-only in the order's pool
              (a reduce-only order takes none: it opens nothing to protect). They
              are the only way to get an OCO pair: when one fires the other is
              cancelled automatically, with no window where both are live. Order is
              ``sl_price < price < tp_price`` on a LIMIT BUY parent, and
              ``tp_price < price < sl_price`` on a LIMIT SELL one.
            * The legs arrive on the ``portfolio`` channel as ``order_update``
              rows and show up in ``openOrders`` as ``PENDING`` conditionals.
            * On a spot market legs arm only on a FULL fill of the parent. A
              parent that ends CANCELLED after a partial fill (the unfilled rest
              of a MARKET or IOC order, your cancel, its expiry) FAILS its legs,
              and one with no fill cancels them. On a perp they arm on the
              parent's first fill, at its quantity (reduce-only, they close no
              more than the position); only a parent that fills nothing drops
              them.
            * On a MARKET parent the server checks the triggers against its
              reference price, not your fill (400 ``Unable to determine market
              price for order`` when it has none).
            * A leg's own price is checked (deviation, band, minimum value)
              when it fires; a failure FAILS the leg, except that a perps leg
              whose order the band around the mark refuses stays ``ARMED`` and
              is tried again every second until the mark lets it in, or until
              its order's life ends or its account's epoch moves
              (``FAILED``), or its position closes (``CANCELLED``). On a
              perp the exchange also checks each leg's limit when the order is
              placed: one outside the market's band around the leg's trigger
              refuses the order (400 ``PRICE_BAND``, ``attached_legs[<i>]: l2
              perps: the price is outside the market's band around its trigger
              price``; nothing is placed). The limits the SDK derives lie
              inside it.
        """
        key = self._credentials()
        _check_market_ticker(ticker)
        side = _check_side(side)
        otype = str(type)
        if otype not in (OrderType.LIMIT, OrderType.MARKET):
            raise _client_validation_error(f"Unknown order type {otype!r}")
        tif = _time_in_force(time_in_force, otype)
        qty_d = validate_quantity(quantity)
        perps = _perps_terms(pool, reduce_only)
        controls = _order_controls(order_tag, send_by_ms, replaces_order_id)

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

        legs: list[tuple[str, _Derived]] = []
        if tp_price is not None or sl_price is not None:
            if reduce_only:
                raise _client_validation_error(
                    "Take-profit and stop-loss cannot be attached to a reduce-only order: it "
                    "opens nothing for them to close"
                )
            tp_d = None if tp_price is None else validate_price(tp_price, "tp_price")
            sl_d = None if sl_price is None else validate_price(sl_price, "sl_price")
            # A SELL's legs (a perps short's: a spot SELL takes none, refused once its market is
            # read) mirror a BUY's: its take-profit fires on a fall and its stop-loss on a rise.
            short = side == OrderSide.SELL
            up, down = ("below", "above") if short else ("above", "below")

            def beyond(a: Decimal, b: Decimal) -> bool:
                return a < b if short else a > b

            if tp_d is not None and sl_d is not None and not beyond(tp_d, sl_d):
                raise _client_validation_error(f"tp_price must be {up} sl_price")
            if limit_d is not None:
                if sl_d is not None and not beyond(limit_d, sl_d):
                    raise _client_validation_error(f"sl_price must be {down} the order price")
                if tp_d is not None and not beyond(tp_d, limit_d):
                    raise _client_validation_error(f"tp_price must be {up} the order price")
            leg_bps = self._tolerance(leg_max_slippage_bps, "leg_max_slippage_bps")
            leg_side = OrderSide.BUY if short else OrderSide.SELL
            # Each leg's limit: its trigger moved by the leg slippage, within a perp's band.
            legs = [
                (leg, _derived(trigger_d, leg_side, leg_bps))
                for leg, trigger_d in (("tp", tp_d), ("sl", sl_d))
                if trigger_d is not None
            ]
        elif leg_max_slippage_bps is not None:
            raise _client_validation_error(
                "leg_max_slippage_bps only applies when tp_price or sl_price is set"
            )

        order_price: Decimal | _Derived
        if limit_d is not None:
            order_price = limit_d
        else:
            bps = self._tolerance(max_slippage_bps)
            if reference_price is not None and bps == 0:
                # A price you give exactly: reference_price to the cent (a BUY's down, a SELL's
                # up), signed as given, never moved or refused for a perp's band; a PRICE_BAND
                # refusal of it shows no band stale (_place's derived, L2State.after_refusal).
                order_price = _worst_price(reference_price, side, 0)
            else:
                # Off reference_price, or without one off the market's (a perp's mark), read with
                # the market.
                order_price = _derived(reference_price, side, bps)
        ttl = _ttl(ttl_ms)
        return self._place(
            key,
            ticker,
            side,
            otype,
            qty_d,
            order_price,
            ttl,
            perps,
            legs=legs,
            time_in_force=tif,
            controls=controls,
        )

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
        pool: int | None = None,
        reduce_only: bool = False,
        time_in_force: str = TimeInForce.GTC,
        order_tag: int | None = None,
        send_by_ms: int | None = None,
        replaces_order_id: int | None = None,
    ) -> Any:
        """Place a LIMIT BUY, optionally with take-profit / stop-loss legs, ``GTC`` unless
        ``time_in_force=`` says otherwise (see :meth:`create`)."""
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
            pool=pool,
            reduce_only=reduce_only,
            time_in_force=time_in_force,
            order_tag=order_tag,
            send_by_ms=send_by_ms,
            replaces_order_id=replaces_order_id,
        )

    def limit_sell(
        self,
        ticker: str,
        quantity: float,
        price: float,
        *,
        tp_price: float | None = None,
        sl_price: float | None = None,
        leg_max_slippage_bps: int | None = None,
        ttl_ms: int | None = None,
        pool: int | None = None,
        reduce_only: bool = False,
        time_in_force: str = TimeInForce.GTC,
        order_tag: int | None = None,
        send_by_ms: int | None = None,
        replaces_order_id: int | None = None,
    ) -> Any:
        """Place a LIMIT SELL, on a perp optionally with take-profit / stop-loss legs, ``GTC``
        unless ``time_in_force=`` says otherwise (see :meth:`create`)."""
        return self.create(
            ticker,
            OrderSide.SELL,
            quantity,
            type=OrderType.LIMIT,
            price=price,
            tp_price=tp_price,
            sl_price=sl_price,
            leg_max_slippage_bps=leg_max_slippage_bps,
            ttl_ms=ttl_ms,
            pool=pool,
            reduce_only=reduce_only,
            time_in_force=time_in_force,
            order_tag=order_tag,
            send_by_ms=send_by_ms,
            replaces_order_id=replaces_order_id,
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
        pool: int | None = None,
        reduce_only: bool = False,
        order_tag: int | None = None,
        send_by_ms: int | None = None,
        replaces_order_id: int | None = None,
    ) -> Any:
        """Place a MARKET BUY that pays at most
        ``reference_price x (1 + max_slippage_bps/10,000)``.

        You pass no price: by default the reference is the exchange's market
        price (a perp's live mark) and the max slippage is
        ``client.max_slippage_bps`` (on a perp held within its band). It never
        rests: what cannot fill at once is cancelled. See :meth:`create`.
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
            pool=pool,
            reduce_only=reduce_only,
            order_tag=order_tag,
            send_by_ms=send_by_ms,
            replaces_order_id=replaces_order_id,
        )

    def market_sell(
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
        pool: int | None = None,
        reduce_only: bool = False,
        order_tag: int | None = None,
        send_by_ms: int | None = None,
        replaces_order_id: int | None = None,
    ) -> Any:
        """Place a MARKET SELL that sells at no less than
        ``reference_price x (1 - max_slippage_bps/10,000)``.

        You pass no price: by default the reference is the exchange's market
        price (a perp's live mark) and the max slippage is
        ``client.max_slippage_bps`` (on a perp held within its band). It never
        rests: what cannot fill at once is cancelled. TP/SL legs on a perp only.
        See :meth:`create`.
        """
        return self.create(
            ticker,
            OrderSide.SELL,
            quantity,
            type=OrderType.MARKET,
            reference_price=reference_price,
            max_slippage_bps=max_slippage_bps,
            tp_price=tp_price,
            sl_price=sl_price,
            leg_max_slippage_bps=leg_max_slippage_bps,
            ttl_ms=ttl_ms,
            pool=pool,
            reduce_only=reduce_only,
            order_tag=order_tag,
            send_by_ms=send_by_ms,
            replaces_order_id=replaces_order_id,
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
        pool: int | None = None,
        reduce_only: bool = False,
    ) -> Any:
        """``POST /orders/conditional`` — place a stop / take order, signed with your trading key.

        The row rests server-side as ``ARMED`` and books an ordinary limit
        order the moment the mark price sits at or through ``trigger_price``.

        Args:
            ticker: the market's ``ticker``, exactly as listed (e.g.
                ``"OPRA"``); pick one from
                :meth:`loaf.resources.market.MarketResource.list` (not every
                listed row is tradeable; see its docstring).
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
                else the last candle close. On a perp it is
                ``market.get(ticker).market.live.markPrice`` (dollars x 1000),
                which the ``perps:markets`` channel pushes. Refused (400) if the
                mark is already at or through it — triggering is level-based, so
                a trigger EQUAL to the mark is refused too. Round it yourself
                (``round(mark * 0.95, 2)``): a raw float product carries 17
                decimals and fails the 2-decimal check.
            price: ``*_LIMIT`` types only (required): dollars, at most 2
                decimals. Refused for a ``*_MARKET`` type. On a perp it must
                lie within the market's band around ``trigger_price`` (400
                ``PRICE_BAND`` otherwise); the SDK signs it as given.
            max_slippage_bps: ``*_MARKET`` types only. The SDK signs
                ``trigger_price x (1 ± max_slippage_bps/10,000)`` (+ for a BUY,
                - for a SELL; default: the client's ``max_slippage_bps``) as the
                price the row books at, fixed for its life. On a perp the move
                is at most half the market's band, ``min(max_slippage_bps,
                band // 2)``, so the price lies inside the band around the
                trigger, which the exchange checks at placement.
            ttl_ms: the life of the order the row books, as in :meth:`create`
                (default 7 days): once it ends the exchange FAILS the row if it
                is still ``ARMED``, fired or not, within about a second (it
                does not wait for it to fire). So cancel and re-arm a stop
                before then.
            pool: perps only: the margin pool the order it books
                trades from, as in :meth:`create`.
            reduce_only: perps only, as in :meth:`create`: a stop or
                take that protects a perps position should be reduce-only, so
                that it never opens a position once that one is closed.

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
            * On a perp the order a fired row books is admitted only within
              the market's band around the mark. One the band refuses (the
              mark moved past the band of its limit) waits: the row stays
              ``ARMED`` and is tried again every second until the mark lets it
              in, or until its order's life ends or its account's epoch moves
              (``FAILED``), or its position closes (``CANCELLED``). A row
              whose limit lies outside the band around its own
              trigger (the market's band was narrowed since it was placed) is
              ``FAILED``, with ``l2 perps: the price is outside the market's
              band around its trigger price`` as its ``rejectionReason``.
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
        _check_market_ticker(ticker)
        side = _check_side(side)
        otype = str(type)
        if otype not in CONDITIONAL_ORDER_TYPES:
            raise _client_validation_error(f"Unknown conditional order type {otype!r}")
        qty_d = validate_quantity(quantity)
        trigger_d = validate_price(trigger_price, "trigger_price")
        perps = _perps_terms(pool, reduce_only)

        price_d: Decimal | _Derived
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
            # The trigger moved by the max slippage, within a perp's band.
            price_d = _derived(trigger_d, side, self._tolerance(max_slippage_bps))
        ttl = _ttl(ttl_ms)
        return self._place(
            key, ticker, side, otype, qty_d, price_d, ttl, perps, trigger_d=trigger_d
        )

    def stop_loss(
        self,
        ticker: str,
        quantity: float,
        trigger_price: float,
        *,
        max_slippage_bps: int | None = None,
        ttl_ms: int | None = None,
        pool: int | None = None,
        reduce_only: bool = False,
    ) -> Any:
        """Arm a protective SELL that fires when the mark FALLS to ``trigger_price``.

        ``STOP_MARKET`` SELL: when it fires it books a LIMIT SELL at
        ``trigger_price x (1 - max_slippage_bps/10,000)`` (default: the
        client's ``max_slippage_bps``, 2% unless configured; on a perp at most
        half the market's band), so after a gap past that price it rests
        instead of selling.

        ``quantity`` is fixed for the life of the row, and your holding is not
        checked until it fires: a stop that fires short of tokens (a resting
        SELL holds the tokens it sells) turns ``FAILED``, with the refusal's
        text as its ``rejectionReason``, sells nothing and cancels none of
        your orders. Nothing warns you earlier, so when you sell part of the
        holding or rest a SELL of it, cancel the stop
        (:meth:`cancel_conditional`) and re-arm it at the new size. For a
        take-profit / stop-loss pair that cancels itself (OCO), attach
        ``tp_price`` / ``sl_price`` to the BUY (:meth:`create`).

        On a perp this SELL protects a long in ``pool``: pass
        ``reduce_only=True``. A short's stop is a BUY (:meth:`create_conditional`).
        If the mark gaps past the band of its price, it waits ``ARMED`` until
        the mark comes back within it, or until its order's life ends or its
        account's epoch moves (``FAILED``), or its position closes
        (``CANCELLED``) (:meth:`create_conditional`).
        """
        return self.create_conditional(
            ticker,
            OrderSide.SELL,
            quantity,
            type=ConditionalOrderType.STOP_MARKET,
            trigger_price=trigger_price,
            max_slippage_bps=max_slippage_bps,
            ttl_ms=ttl_ms,
            pool=pool,
            reduce_only=reduce_only,
        )

    def take_profit(
        self,
        ticker: str,
        quantity: float,
        trigger_price: float,
        *,
        max_slippage_bps: int | None = None,
        ttl_ms: int | None = None,
        pool: int | None = None,
        reduce_only: bool = False,
    ) -> Any:
        """Arm a SELL that fires when the mark RISES to ``trigger_price``.

        ``TAKE_MARKET`` SELL: when it fires it books a LIMIT SELL at
        ``trigger_price x (1 - max_slippage_bps/10,000)`` (default: the
        client's ``max_slippage_bps``, 2% unless configured; on a perp at most
        half the market's band).

        A :meth:`stop_loss` and a :meth:`take_profit` placed separately are
        **not** OCO: neither cancels the other. Once the first one's order has
        FILLED, or while it rests holding the tokens, the second fires short of
        them and FAILS, with the refusal's text as its ``rejectionReason``,
        cancelling none of your orders. For a pair that cancels itself, attach
        ``sl_price`` / ``tp_price`` to the BUY (:meth:`create`).

        On a perp this SELL takes a long's profit in ``pool``: pass
        ``reduce_only=True``. A short's take is a BUY (:meth:`create_conditional`).
        """
        return self.create_conditional(
            ticker,
            OrderSide.SELL,
            quantity,
            type=ConditionalOrderType.TAKE_MARKET,
            trigger_price=trigger_price,
            max_slippage_bps=max_slippage_bps,
            ttl_ms=ttl_ms,
            pool=pool,
            reduce_only=reduce_only,
        )

    # -- Re-send ------------------------------------------------------------ #

    def resubmit(self, signed_body: Mapping[str, Any], *, send_by_ms: int | None = None) -> Any:
        """Re-send a signed placement exactly as it was (e.g. the ``signed_body``
        of an :class:`~loaf.exceptions.OrderOutcomeUnknownError`), with a new
        ``sendByMs`` when ``send_by_ms=`` is given (it is not signed).

        If the first attempt landed, the answer is that same order as it is
        now (on ``/orders`` with ``duplicate: True``). A stop or take is
        answered with just the ``orderId`` of its row, whatever its status: a
        re-send never re-arms one that was cancelled, fired or FAILED. So read
        that row's ``status`` in ``history.orders()`` before relying on it, and
        arm a new one if it is ``CANCELLED`` or ``FAILED``. If the first
        attempt did not land, the order is placed now, at the price and
        quantity signed then, so resubmit only while you still want it. It
        cannot be placed twice: the exchange admits one order per account and
        order number.

        Anything but a 200 raises :class:`~loaf.exceptions.OrderOutcomeUnknownError`
        again, with the reason in ``last_error``: whatever this re-send ran
        into, the first attempt may still be live. A refusal does not prove the
        first attempt failed either: checks run before the duplicate is
        recognised (the open-order cap, for example, counts the order that
        landed). A lasting refusal (a halt, the cap) will not change by
        re-sending, so look for the order in ``history.orders()`` (every
        status) then; an order that filled is not in ``openOrders``.

        After an epoch move (a Reset trading key, or a cancel-all sent on chain)
        the exchange refuses it (``SETTLE_EPOCH_MISMATCH``), has cancelled any resting
        order of the old epoch and FAILS its stops, takes and TP/SL legs.

        Needs no trading key. Once the order can no longer rest (its
        ``l2ExpiryMs`` minus 72 hours has passed) nothing is sent, since the
        exchange refuses it then: the ``OrderOutcomeUnknownError`` has
        ``attempts`` 0 and the reason in ``last_error``. Stop resubmitting it:
        settle it with :meth:`cancel_by_index`, or find it in
        ``history.orders()`` by its ``expiryMs``. If it landed, it is over: the
        exchange has cancelled what of it still rested, and FAILS a stop, take
        or TP/SL leg of it still ``ARMED`` within about a second of that time.

        A body whose ``sendByMs`` has passed is not sent either (``attempts``
        0, ``last_error`` a :class:`~loaf.exceptions.SendByPassedError`): the
        exchange refuses it before it looks for the first attempt, so the
        answer would say nothing. Settle it with :meth:`cancel_by_index`, or,
        to have it placed late after all, resubmit it with a later
        ``send_by_ms=``.
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
        conditional = "triggerPrice" in body
        body = dict(body)
        if send_by_ms is not None:
            if conditional:
                raise _client_validation_error(
                    "send_by_ms applies to orders only: a stop or take takes no send-by time"
                )
            body["sendByMs"] = _whole_number(
                send_by_ms, "send_by_ms", 1, SEND_BY_MS_MAX, _SEND_BY_RULE
            )
        now_ms = time.time_ns() // 1_000_000
        expiry_ms = body["l2ExpiryMs"]
        rests_until_ms = expiry_ms - L2_BUFFER_MS
        send_by = body.get("sendByMs")
        refusal: LoafError | None = None
        if now_ms >= rests_until_ms:
            until = datetime.fromtimestamp(rests_until_ms // 1000, tz=timezone.utc)
            refusal = _client_validation_error(
                f"This order could rest only until {until.isoformat()} (its l2ExpiryMs minus "
                f"{L2_BUFFER_MS // 3_600_000} hours): the exchange refuses it now. If it landed, "
                "it is over: any part of it still resting is cancelled, and a stop or take, or a "
                "TP/SL leg, still ARMED is FAILED within about a second of that time, fired or "
                f"not. Find it in history.orders() (expiryMs {expiry_ms})."
            )
        elif _is_int(send_by) and now_ms > send_by:
            refusal = SendByPassedError(
                f"This order's sendByMs {send_by} has passed: the exchange refuses it before it "
                "looks for an earlier attempt, so a re-send would say nothing. Settle it with "
                f"orders.cancel_by_index({body['l2OrderIndex']}), or resubmit it with a later "
                "send_by_ms= to have it placed now if it never was.",
                status_code=0,
                code="SEND_BY_PASSED",
            )
        if refusal is not None:
            # Nothing is sent, but the first attempt may have landed, and filled: still unknown.
            raise OrderOutcomeUnknownError(
                signed_body=copy.deepcopy(body), attempts=0, last_error=refusal
            ) from refusal
        path = "/orders/conditional" if conditional else "/orders"
        return self._client._send_signed(path, body, ambiguous=True)

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

        It works under a single-market halt; only a platform-wide halt blocks it
        (:class:`~loaf.exceptions.TradingHaltedError`).
        """
        return self._client.post("/orders/cancel", json={"orderId": _order_id(order_id)})

    def cancel_conditional(self, order_id: int) -> Any:
        """``POST /orders/conditional/cancel`` — cancel one PENDING / ARMED conditional.

        Pass the CONDITIONAL row's own ``id``; a ``PLACED`` row is cancelled
        with :meth:`cancel` on its ``placedOrderId`` instead (:meth:`cancel_row`
        picks). Each endpoint reads only its own rows, so the wrong id is a 404
        like any unknown one. A terminal row raises
        :class:`~loaf.exceptions.LoafValidationError` (400); a row the engine
        was already ending (firing or failing) as the cancel arrived raises
        :class:`~loaf.exceptions.LoafConflictError` (409), as :meth:`cancel`
        does for the same race — an idempotent cancel must catch both. A 503
        (:class:`~loaf.exceptions.LoafServiceUnavailableError`) means the
        engine did not answer; the row may still be live, and retrying is safe.
        A ``status_code`` 0 :class:`~loaf.exceptions.LoafValidationError` does
        not mean a terminal row: the ``order_id`` is not a whole number from 1,
        so nothing was sent and the order may still be live.

        The matching engine holds the row and cancels it, and never refuses the
        cancel for the market's state, a pause or a halt; the backend's
        platform-wide kill switch stops :meth:`cancel` and :meth:`cancel_all`
        only. So it still works under a platform-wide halt, which makes it the
        only way to stop an ``ARMED`` row firing into the reopen: triggering is
        level-based, so a row whose mark sat through its trigger for the whole
        halt fires as soon as trading resumes.
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

    def cancel_all(self, ticker: str | None = None, *, order_tag: int | None = None) -> Any:
        """``POST /orders/cancel-all`` — cancel your open orders: every one, or
        one market's (``ticker=``), or the ones carrying one ``order_tag=``, or
        both.

        Returns ``{requestedCount, cancelledOrderIds, failedOrders}``
        (:class:`~loaf.models.CancelAllResult`), where ``requestedCount`` is
        ``len(cancelledOrderIds) + len(failedOrders)``.

        One request to the matching engine cancels the scope's resting orders
        at once, atomically: an order placed while it runs may or may not be
        included, one placed after the response is not. With no
        ``order_tag=`` it also cancels the scope's ``PENDING`` / ``ARMED``
        stops and takes (a stop or take carries no tag), and their
        conditional-row ids come first in ``cancelledOrderIds``, so those ids
        will 404 against any booked-order lookup.

        ``failedOrders`` lists only orders the engine could not cancel because
        the request did not complete on their market (it failed there, or
        the outcome there is unknown). They stay open, each with the
        ``errorMessage`` "Trading service is temporarily unavailable. Please
        try again in a moment."; calling it again is safe (the request is
        idempotent).

        No epoch moves: an order signed earlier with an unused order number
        can still be sent and fill. To make sure one never lands, void its
        number with :meth:`cancel_by_index`.

        It is NOT available under a platform-wide halt — it refuses before
        cancelling anything (:class:`~loaf.exceptions.TradingHaltedError`), and
        :meth:`cancel_conditional` is the only cancel that still works then. A
        503 (:class:`~loaf.exceptions.LoafServiceUnavailableError`) means the
        engine did not process the request or its outcome is unknown, and
        retrying is safe. ``ticker=`` costs one public read the first time
        it names a market no order of yours was placed on here.
        """
        scope = self._scope(ticker, order_tag)
        if not scope:
            return self._client.post("/orders/cancel-all")
        return self._client.post("/orders/cancel-all", json=scope)

    def cancel_after(
        self, timeout_ms: int, *, ticker: str | None = None, order_tag: int | None = None
    ) -> Any:
        """``POST /orders/cancel-after`` — the dead man's switch: unless you call
        again within ``timeout_ms``, the exchange cancels the scope's orders.

        The scope is every market or one (``ticker=``), and every order or the
        ones carrying one ``order_tag=``, as :meth:`cancel_all` names them;
        each scope has its own timer, and each call replaces its scope's
        deadline. When the deadline passes, the exchange cancels the scope's
        orders as ``cancel_all`` with that scope would (an untagged scope's
        stops and takes too), and the timer stays off until you set it again.
        ``timeout_ms`` 0 turns the scope's timer off.

        Call it every 15 to 30 s with a ``timeout_ms`` of 60,000 to 120,000,
        from the loop that quotes, never from a thread of its own: the timer
        is there to fire when that loop stops. The deadline is the exchange's
        and durable: one that passes while its engine is down fires as it
        starts, before any order.

        Args:
            timeout_ms: 0 (off), or a whole number of milliseconds from 10,000
                to 120,000.
            ticker: the market of the scope (default: every market).
            order_tag: the tag of the scope (default: every order).

        Returns:
            ``{deadlineMs}`` (:class:`~loaf.models.CancelAfterResult`): when
            the scope's orders are cancelled, unix ms of the exchange's clock;
            0 when ``timeout_ms`` was 0.

        Raises:
            LoafRateLimitError: more than one call a second (429). The limit
                is per account, across every scope.
            LoafServiceUnavailableError: 503. ``code`` ``NOT_PROCESSED``:
                nothing changed (the scope's previous deadline stands);
                ``SERVICE_UNAVAILABLE``: the exchange did not answer. Either
                way, call again: a repeat sets the same scope.

        It is taken under a platform-wide halt, which freezes the other
        cancels: a timer that could not be renewed then would fire during the
        halt.
        """
        whole = isinstance(timeout_ms, numbers.Integral) and not isinstance(timeout_ms, bool)
        timeout = int(timeout_ms) if whole else None
        if timeout is None or not (
            timeout == 0 or CANCEL_AFTER_MIN_MS <= timeout <= CANCEL_AFTER_MAX_MS
        ):
            raise _client_validation_error(
                f"timeout_ms must be 0 (off) or a whole number of milliseconds from "
                f"{CANCEL_AFTER_MIN_MS:,} to {CANCEL_AFTER_MAX_MS:,} (got {timeout_ms!r})"
            )
        body = {"timeoutMs": timeout, **self._scope(ticker, order_tag)}
        return self._client.post("/orders/cancel-after", json=body)

    def cancel_by_index(self, order_index: int) -> Any:
        """``POST /orders/cancel-by-index`` — settle an order by the order number
        it signed (``l2OrderIndex``), with no read.

        For an order whose placement's answer was lost
        (:class:`~loaf.exceptions.OrderOutcomeUnknownError`: pass its
        ``signed_body["l2OrderIndex"]``) once you no longer want it. The answer
        (:class:`~loaf.models.CancelByIndexResult`) is final:

        * ``CANCELLED`` (:class:`~loaf.enums.CancelByIndexOutcome`): the order
          was resting, and is cancelled now, as :meth:`cancel` cancels it;
        * ``ENDED``: the order was placed and no longer rests (``status`` says
          how it ended: ``FILLED``, ``CANCELLED``); nothing changed;
        * ``VOIDED``: no order was placed with the number, and none ever will
          be: an attempt still on its way is refused ``ORDER_INDEX_USED``
          whenever it arrives. ``orderId`` and ``status`` are ``None``.

        A repeat answers ``ENDED`` or ``VOIDED``. The number is your trading
        key's account's; the SDK never signs it again.

        Raises:
            LoafValidationError: 400. ``code`` ``ORDER_INDEX_NOT_RESERVED``: no
                range reserved to your account holds the number;
                ``INVALID_REQUEST``: a placed stop or take holds it, and the
                message names its id (cancel it with
                :meth:`cancel_conditional`, since voiding it would leave it
                unable to fire). Nothing changed. Locally (``status_code`` 0,
                nothing sent): ``order_index`` is not a whole number from 1 to
                2^40 - 1.
            LoafForbiddenError: 403 ``ORDER_NOT_OWNED``: the number is another
                account's. Nothing changed.
            TradingHaltedError: a platform-wide halt (403), as for every
                cancel but :meth:`cancel_conditional`.
            LoafServiceUnavailableError: 503 (``NOT_PROCESSED`` or
                ``SERVICE_UNAVAILABLE``): call it again; a repeat settles it.
        """
        key = self._credentials()
        index = _whole_number(
            order_index, "order_index", 1, _MAX_ORDER_INDEX,
            "an order number, a whole number from 1 to 2^40 - 1",
        )
        # Whatever the answer, a number you asked to settle is never signed again: voided, a later
        # order on it would be refused as used.
        self._l2.spent(index)
        return self._client.post(
            "/orders/cancel-by-index",
            json={"l2AccountIndex": key.l2_account_index, "l2OrderIndex": index},
        )

    def _scope(self, ticker: str | None, order_tag: Any) -> dict[str, int]:
        """A cancel's scope: ``ticker``'s market index and the tag, each when given."""
        scope: dict[str, int] = {}
        if order_tag is not None:  # checked before the read a ticker can cost
            scope["orderTag"] = _whole_number(order_tag, "order_tag", 1, ORDER_TAG_MAX, _TAG_RULE)
        if ticker is not None:
            scope = {"marketIndex": self._market_index(ticker), **scope}
        return scope

    def _market_index(self, ticker: str) -> int:
        """``ticker``'s market index: the one an order here signed, else read once."""
        _check_market_ticker(ticker)
        market = self._l2.market(ticker)
        if market is not None:
            return market.market_index
        index = self._market_indices.get(ticker)
        if index is not None:
            return index
        detail = self._client._request("GET", f"/trade/{ticker}", auth=False)
        values = detail.get("market") if isinstance(detail, Mapping) else None
        index = values.get("marketIndex") if isinstance(values, Mapping) else None
        if not _whole(index, 0, MAX_MARKET_INDEX):
            raise LoafServerError(
                f"GET /trade/{ticker} answered no market index", status_code=200, body=detail
            )
        self._market_indices[ticker] = index
        return index

    # -- Signing & sending ------------------------------------------------- #

    def _credentials(self) -> TradingKey:
        """The trading key that signs, once it is set."""
        key = self._client._l2_key
        if key is None:
            raise LoafConfigError(
                f"Placing orders needs your trading key: set ${ENV_TRADING_KEY} to the web app's "
                "Show trading key (README §2), or pass trading_key=..."
            )
        return key

    def _tolerance(self, max_slippage_bps: int | None, field: str = "max_slippage_bps") -> int:
        """This order's max slippage, else the client's (re-checked: it is a public attribute)."""
        if max_slippage_bps is None:
            return validate_slippage_bps(
                self._client.max_slippage_bps, "LoafClient.max_slippage_bps"
            )
        return validate_slippage_bps(max_slippage_bps, field)

    def _check_chain(self, market: Market, key: TradingKey, ticker: str) -> None:
        if market.l2_chain_id != key.l2_chain_id:
            raise LoafConfigError(
                f"{ticker}'s market is on L2 chain {market.l2_chain_id} and your trading key "
                f"is for L2 chain {key.l2_chain_id}: {ENV_TRADING_KEY} is from another Loaf "
                f"deployment than the one at {self._client.base_url} ({ENV_BASE_URL}). Set "
                f"{ENV_TRADING_KEY} to the trading key this deployment's web app shows "
                f"(README §2), or {ENV_BASE_URL} to the key's deployment."
            )

    def _place(
        self,
        key: TradingKey,
        ticker: str,
        side: str,
        otype: str,
        qty_d: Decimal,
        price: Decimal | _Derived,
        ttl: int,
        perps: tuple[int | None, bool],
        trigger_d: Decimal | None = None,
        legs: Sequence[tuple[str, _Derived]] = (),
        time_in_force: str = TimeInForce.GTC.value,
        controls: Mapping[str, int] | None = None,
    ) -> Any:
        """Read what the order signs, take its order numbers, sign it and send it.

        ``price`` is the order's price as given (a LIMIT order's, a ``*_LIMIT``
        conditional's, a MARKET order's ``reference_price`` with no slippage), or
        the one the SDK derives (:class:`_Derived`: a MARKET order's worst price,
        off its ``reference_price`` or, without one, off the market's reference,
        read here; a ``*_MARKET`` conditional's, off its trigger), priced once
        the market is known (and refused then, before any order number is
        taken, when no cent fits a perp's band and the max slippage:
        :meth:`_Derived.on`);
        ``perps`` its ``(pool, reduce_only)`` as passed; ``trigger_d`` makes
        it a conditional (always GTC); ``legs`` are its TP/SL legs as
        ``("tp" | "sl", limit)``, each limit derived off the leg's trigger;
        ``controls`` its unsigned order controls' body keys
        (:func:`_order_controls`).
        """
        l2 = self._l2
        span = 1 + len(legs)
        account = block = None
        first = 0
        # Whether it signs a price the SDK derives within the band kept with the market (its own,
        # or a leg's): a PRICE_BAND refusal shows that band possibly stale (L2State.after_refusal).
        # A price given exactly, a MARKET order's reference_price with no slippage among them, owes
        # the band nothing.
        derived = isinstance(price, _Derived) or bool(legs)
        try:
            market = l2.market(ticker)
            reference = None  # the market's, for a MARKET order without reference_price
            unpriced = isinstance(price, _Derived) and price.reference is None
            if market is None or unpriced:
                # A MARKET order reads the detail for its reference anyway.
                detail = self._client._request(
                    "GET", f"/trade/{ticker}", auth=False, before_order=True
                )
                market = market_of(detail, ticker)
                l2.remember(ticker, market)
                if unpriced:
                    reference = _reference_from(detail, ticker, market)
            if isinstance(price, _Derived):
                price = price.on(market, ticker, reference)
            self._check_chain(market, key, ticker)
            market_keys, base_field, leg_field = _market_keys(market, ticker, side, perps, legs)
            account = l2.account(key.l2_account_index)
            first, block, expiry_ms = l2.take(account, span, ttl)

            body: dict[str, Any] = {
                "price": float(price),
                "quantity": float(qty_d),
                "side": side,
                "type": otype,
            }
            if trigger_d is not None:  # a conditional: the order it books when the mark gets there
                body["triggerPrice"] = float(trigger_d)
            body.update({
                "timeInForce": time_in_force,
                "l2ChainId": market.l2_chain_id,
                "l2AccountIndex": account.account_index,
                "l2MarketIndex": market.market_index,
                "l2OrderIndex": first,
                "l2Epoch": account.epoch,
                "l2FeeRateCap": L2_FEE_RATE_CAP,
                "l2ExpiryMs": expiry_ms,
                **market_keys,
                "l2QuoteAssetId": market.quote_asset_id,
                **_signed(key, market, base_field, account, first, expiry_ms, side, price, qty_d),
                **(controls or {}),  # sent beside the signed fields, not signed
            })
            # Each leg is an order of the parent's quantity on the other side, at its own price,
            # with the parent's expiry and the next order number (tp, then sl), and its own
            # base-asset field (a perps leg's is reduce-only, see _market_keys).
            leg_side = OrderSide.BUY if side == OrderSide.SELL else OrderSide.SELL
            for index, (leg, limit) in enumerate(legs, first + 1):
                leg_price = limit.on(market, ticker)
                body[leg] = {
                    "triggerPrice": float(limit.reference),  # a leg's reference is its trigger
                    "price": float(leg_price),
                    "l2OrderIndex": index,
                    "l2ExpiryMs": expiry_ms,
                    **_signed(
                        key,
                        market,
                        leg_field,
                        account,
                        index,
                        expiry_ms,
                        leg_side,
                        leg_price,
                        qty_d,
                    ),
                }
            path = "/orders/conditional" if trigger_d is not None else "/orders"
            return self._client._send_signed(path, body)
        except OrderOutcomeUnknownError as exc:
            # It may be live, so its order numbers are never given back: only a resubmit of this
            # very body (a duplicate at worst) may ever carry them.
            if isinstance(exc.last_error, LoafAPIError):
                l2.after_refusal(ticker, account, block, exc.last_error, derived)
            raise
        except LoafError as exc:
            # Definite: nothing this call sent can be live (it signed at most one body, and every
            # attempt carrying it was refused or never arrived), so a later order may sign the
            # same numbers (L2State.give_back). Never re-signed here: one call, one signed body.
            if isinstance(exc, LoafAPIError):
                l2.after_refusal(ticker, account, block, exc, derived)
            if block is not None:
                l2.give_back(block, first, span)
            raise


def _time_in_force(time_in_force: Any, otype: str) -> str:
    """``time_in_force=`` checked against the exchange's times in force, POST_ONLY on a
    LIMIT order alone (the exchange's own text for it)."""
    if not isinstance(time_in_force, str) or time_in_force not in _TIMES_IN_FORCE:
        raise _client_validation_error(
            f"time_in_force must be GTC, IOC, FOK or POST_ONLY (got {time_in_force!r})"
        )
    if time_in_force == TimeInForce.POST_ONLY and otype != OrderType.LIMIT:
        raise _client_validation_error("time_in_force: POST_ONLY requires a LIMIT order")
    return TimeInForce(time_in_force).value


def _perps_terms(pool: Any, reduce_only: Any) -> tuple[int | None, bool]:
    """``pool=`` and ``reduce_only=`` checked; whether the market takes them is checked once it
    is read (:func:`_market_keys`)."""
    whole = isinstance(pool, numbers.Integral) and not isinstance(pool, bool)
    if pool is not None and not (whole and 0 <= int(pool) <= MAX_POOL):
        raise _client_validation_error(
            f"pool must be a margin pool, a whole number from 0 (the cross pool) to {MAX_POOL} "
            f"(got {pool!r})"
        )
    if not isinstance(reduce_only, bool):
        raise _client_validation_error(f"reduce_only must be True or False (got {reduce_only!r})")
    return (None if pool is None else int(pool)), reduce_only


def _market_keys(
    market: Market,
    ticker: str,
    side: str,
    perps: tuple[int | None, bool],
    legs: Sequence[tuple[str, _Derived]],
) -> tuple[dict[str, Any], int, int]:
    """The body keys an order's market decides, its signed base-asset field and its legs':
    a spot market's base asset (``l2BaseAssetId``), or a perp order's pool and reduce-only bit
    (``l2Pool``, ``l2ReduceOnly``), which it signs as its pool byte. A perp's leg closes what
    its order opens: it signs the order's pool with the reduce-only bit."""
    pool, reduce_only = perps
    if market.type == "SPOT":
        if pool is not None or reduce_only:
            raise _client_validation_error(
                f"{ticker} is a spot market: pool= and reduce_only= apply to perps only (their "
                "tickers end in -PERP)"
            )
        if legs and side != OrderSide.BUY:
            raise _client_validation_error(
                f"{ticker} is a spot market: take-profit and stop-loss can only be attached to "
                "a BUY order there (spot has no short to protect)"
            )
        return {"l2BaseAssetId": market.base_asset_id}, market.base_asset_id, market.base_asset_id
    if market.l2_status != "ACTIVE":
        raise _client_validation_error(
            f"{ticker} (perps market {market.market_index}) has expired and takes no orders: "
            "close a position in it with perps.exit()"
        )
    pool = 0 if pool is None else pool
    return (
        {"l2Pool": pool, "l2ReduceOnly": reduce_only},
        k1.perps_base_field(pool, reduce_only),
        k1.perps_base_field(pool, True),
    )


def _ttl(ttl_ms: Any) -> int:
    """``ttl_ms=`` checked, or the default life when omitted."""
    if ttl_ms is None:
        return DEFAULT_ORDER_TTL_MS
    whole = isinstance(ttl_ms, numbers.Integral) and not isinstance(ttl_ms, bool)
    if not whole or not MIN_ORDER_TTL_MS <= int(ttl_ms) <= MAX_ORDER_TTL_MS:
        raise _client_validation_error(
            f"ttl_ms must be a whole number of milliseconds from {MIN_ORDER_TTL_MS:,} (one "
            f"minute) to {MAX_ORDER_TTL_MS:,} (about {MAX_ORDER_TTL_MS / 86_400_000:.1f} days) "
            f"(got {ttl_ms!r})"
        )
    return int(ttl_ms)


def _whole_number(value: Any, field: str, low: int, high: int, rule: str) -> int:
    """``value`` checked as a whole number from ``low`` to ``high``, never rounded or read from
    text; refused locally with ``field must be {rule}`` otherwise."""
    whole = isinstance(value, numbers.Integral) and not isinstance(value, bool)
    if not whole or not low <= int(value) <= high:
        # A mapping shows only its type: a signed body passed by mistake must stay out of logs.
        shown = type(value).__name__ if isinstance(value, Mapping) else repr(value)
        raise _client_validation_error(f"{field} must be {rule} (got {shown})")
    return int(value)


def _order_controls(order_tag: Any, send_by_ms: Any, replaces_order_id: Any) -> dict[str, int]:
    """An order's unsigned controls, checked, as its body keys: only the ones given."""
    controls: dict[str, int] = {}
    if order_tag is not None:
        controls["orderTag"] = _whole_number(order_tag, "order_tag", 1, ORDER_TAG_MAX, _TAG_RULE)
    if send_by_ms is not None:
        controls["sendByMs"] = _whole_number(
            send_by_ms, "send_by_ms", 1, SEND_BY_MS_MAX, _SEND_BY_RULE
        )
    if replaces_order_id is not None:
        controls["replacesOrderId"] = _whole_number(
            replaces_order_id, "replaces_order_id", 1, _MAX_SAFE_INT,
            "an order's id, a whole number from 1",
        )
    return controls


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


class _Derived(NamedTuple):
    """A price the SDK derives rather than signs as given: a MARKET order's worst price, a
    ``*_MARKET`` stop's or take's booked price, a TP/SL leg's limit. ``reference`` (a MARKET order's
    ``reference_price``, or the trigger of a stop, take or leg; ``None``: the market's reference,
    read with its market) moved ``bps`` against an order of ``side``, the move held within half a
    perp's band (:func:`_band_bps`) once the market is known, a price rounded across an off-grid
    reference given way to the cent on its near side where that may be signed, and an order left
    with no price inside the band refused (:meth:`on`)."""

    reference: Any
    side: str
    bps: int

    def on(self, market: Market, ticker: str, reference: Any = None) -> Decimal:
        """The price on ``market`` (``ticker``'s), off ``reference`` when this one has none.

        Today's price first: the reference moved ``bps`` against the order, at most half a perp's
        band (:func:`_band_bps`), rounded toward the reference to the cent, and at least $0.01. On
        a perp with a band, a price that crossed the reference (a SELL's above it, a BUY's below
        it), which only rounding toward a reference off the cent grid does (a mark is in tenths of
        a cent; a trigger is on the grid, so a stop's or a leg's price never crosses), gives way to
        the cent on the reference's near side (a SELL's floor, a BUY's ceiling: against the order)
        when that cent is positive, inside the band (``|cent - reference| x 10,000 <= band x
        reference``, the exchange's rule, both edges in) and within ``bps`` of the reference (the
        whole max slippage, not the half-band cap). Else today's price, across the reference or
        not, is kept while it lies inside the band, which one that did not cross always does
        unless the $0.01 floor lifted it: a BUY off a reference under one cent signs $0.01 only
        where the band is at least ``10,000 x (0.01 - reference) / reference`` bp (10,000 off
        $0.005). Else no cent lies inside the band and within ``bps`` against the order, and the
        order is refused here, before an order number is taken or anything is signed or sent. A
        band of 0 changes nothing.
        """
        start = reference if self.reference is None else self.reference
        price = _worst_price(start, self.side, _band_bps(self.bps, market))
        band = market.band if market.type == "PERP" else 0
        if band <= 0:
            return price
        sell = self.side == OrderSide.SELL
        with localcontext(_DECIMAL_CONTEXT):
            ref = _to_decimal(start, "reference_price")  # exact: what the band is measured from
            if (price > ref) if sell else (price < ref):
                # Across an off-grid reference: the cent on its near side, against the order.
                near = ref.quantize(_PRICE_TICK, rounding=ROUND_FLOOR if sell else ROUND_CEILING)
                reach = abs(near - ref) * 10_000
                # The exchange's rule and the max slippage, both edges in.
                if near >= _PRICE_TICK and reach <= band * ref and reach <= self.bps * ref:
                    return near
            # Today's price, across the reference or not, while it lies inside the band (the
            # exchange's rule, both edges in): one that did not cross lies outside it only when the
            # $0.01 floor lifted it, a BUY's off a reference under one cent.
            if abs(price - ref) * 10_000 <= band * ref:
                return price
        raise _client_validation_error(
            f"No price on the $0.01 grid lies within {ticker}'s band of {band} bp around {ref} and "
            f"within max_slippage_bps={self.bps} of it: place a LIMIT order"
        )


def _derived(reference: Any, side: str, bps: int) -> _Derived:
    """A derived price (:class:`_Derived`), checked now when its reference is given, so that one no
    order could sign is refused before anything is read: a band only brings it nearer (the near
    cent :meth:`_Derived.on` may sign lies within ``bps`` of the reference too)."""
    if reference is not None:
        _worst_price(reference, side, bps)
    return _Derived(reference, side, bps)


def _band_bps(bps: int, market: Market) -> int:
    """How far a price the SDK derives on ``market`` first moves from its reference: ``bps``, but at
    most half a perp's band (0: no band, which bounds nothing), as the web app prices. The exchange
    refuses a perps order whose price lies outside the band around the mark, and a perps stop, take
    or leg whose limit lies outside it around its own trigger: within half of it, the price is
    inside, with the other half left for the mark to move before the order is admitted. Rounded to
    the cent across an off-grid reference, it may give way to the cent on the reference's near side;
    lifted to the $0.01 floor (a BUY's off a reference under one cent), it may lie outside the band,
    which refuses it (:meth:`_Derived.on`)."""
    if market.type == "PERP" and market.band > 0:
        return min(bps, market.band // 2)
    return bps


def _reference_from(detail: Any, ticker: str, market: Market) -> Any:
    """A MARKET order's default reference: on a perp, its live mark (``market.live.markPrice``,
    dollars x 1000), the centre of its band; else, and on a perp whose mark is 0 (no price report
    yet, so no band either) or not served (``market.live`` null: the exchange may still band the
    order around its mark), the ``marketPrice`` of ``ticker``'s ``marketList`` entry (the book
    mid, else the last trade, else the last candle close, else the IPO price)."""
    if market.type == "PERP":
        live = detail["market"].get("live")  # market_of checked that "market" is an object
        mark = live.get("markPrice") if isinstance(live, Mapping) else None
        if _whole(mark, 1, _MAX_SAFE_INT):
            return Decimal(f"{mark}E-3")  # exact, whatever the decimal context
    for item in detail.get("marketList") or []:
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
    base_field: int,
    account: AccountState,
    index: int,
    expiry_ms: int,
    side: str,
    price: Decimal,
    quantity: Decimal,
) -> dict[str, str]:
    """``l2SigS`` / ``l2SigE`` over one order's 12 signed terms: an order, a leg or a
    conditional (the order it books). ``base_field`` is its signed base-asset field
    (:func:`_market_keys`)."""
    with localcontext(_DECIMAL_CONTEXT):
        price_scaled, quantity_scaled = int(price * 1000), int(quantity * 10)  # exact on the grid
    try:
        message = k1.order_message(
            l2_chain_id=market.l2_chain_id,
            l2_market_index=market.market_index,
            l2_base_asset_id=base_field,
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

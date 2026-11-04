"""Exception hierarchy for the Loaf SDK.

Every error the API can return is mapped to a specific exception so a bot can
``except`` precisely:

    LoafError                       (base — catch this to catch everything)
    ├── LoafConfigError             misconfiguration (e.g. no trading key or a malformed one, a
    │                               market on another deployment's chain, a clock hours ahead)
    ├── LoafConnectionError         network failure / timeout / a body that failed to decompress
    ├── OrderOutcomeUnknownError    a signed order may be live; the SDK could not confirm it
    ├── PerpsOutcomeUnknownError    a signed perps allocate or exit may have been applied
    ├── WithdrawalOutcomeUnknownError  a signed withdrawal may have been journaled
    └── LoafAPIError                the server returned an HTTP error (.code: why, when it says)
        ├── LoafAuthError                 401  a login token, or an order's signature or key, refused
        ├── LoafForbiddenError            403  generic forbidden
        │   ├── TradingHaltedError        403  "…halted…", or the exchange's engine's own pause
        │   │   └── TradingPausedError    503  the engine paused trading (a 503 error too)
        │   └── MarketStateError          403  the market's state refuses the order (.state)
        │       ├── MarketHaltedError     403  the market is halted (a TradingHaltedError too)
        │       └── PriceHeldError        403  reduce-only while its latest price waits for review
        ├── LoafValidationError           400  validation or order-rule refusal (or local, status 0)
        │   ├── PostOnlyWouldTakeError    400  a POST_ONLY order would have traded at once
        │   └── SendByPassedError         400  the order's send_by_ms passed before it was decided
        ├── LoafNotFoundError             404
        ├── LoafConflictError             409  (e.g. a cancel that lost the race)
        │   └── ReplaceTargetGoneError    409  the order a replace names is not resting
        ├── LoafRateLimitError            429  (carries .retry_after seconds)
        └── LoafServerError               5xx  (503 -> LoafServiceUnavailableError)
            └── LoafServiceUnavailableError 503

An order route's refusal, and a withdrawal's, names why in
:attr:`LoafAPIError.code`, one of :class:`~loaf.enums.OrderErrorCode`'s values,
which never change meaning (the message is for people and may). The SDK picks
the exception class from the status and that code, never from the message.
"""

from __future__ import annotations

import html
import re
from typing import Any

_PRE_RE = re.compile(r"<pre>(.*?)</pre>", re.IGNORECASE | re.DOTALL)


class LoafError(Exception):
    """Base class for every error raised by this SDK."""

    def __reduce__(self) -> Any:
        # The default rebuilds with cls(*args), which keyword-only __init__s refuse; restore the
        # attributes instead, so errors survive pickle (process pools) and copy.
        return (_rebuild_error, (type(self), self.args), self.__dict__)


def _rebuild_error(cls: type[LoafError], args: tuple) -> LoafError:
    return cls.__new__(cls, *args)


class LoafConfigError(LoafError):
    """The client is misconfigured: no trading key (``$LOAF_TRADING_KEY``) for
    an authenticated request or an order, a malformed trading key, an invalid
    ``LoafClient(max_slippage_bps=)`` / ``$LOAF_MAX_SLIPPAGE_BPS`` or
    ``websocket(reconnect_delay=)``, or a server that does not run the exchange.

    From an order placement (nothing was placed):

    * the market is on another L2 chain than your trading key: the key is
      another Loaf deployment's than ``$LOAF_API_BASE_URL``;
    * this machine's clock is hours ahead of the exchange's: synchronize it.

    Fix the setup, then restart.
    """


class LoafConnectionError(LoafError):
    """The request never received an HTTP response (network error / timeout), or
    received one whose compressed body could not be decompressed."""


class OrderOutcomeUnknownError(LoafError):
    """A signed order may or may not have been placed; the SDK could not find out.

    Raised by order placement after a timeout or a server-side failure that the
    SDK's own identical re-sends did not settle, and by
    :meth:`~loaf.resources.orders.OrdersResource.resubmit` on anything but a
    200. The order may be live, and may already have filled, so never place the
    same intent afresh. While you still want it, and while it can still rest
    (until its ``l2ExpiryMs`` minus :data:`~loaf.constants.L2_BUFFER_MS`),
    re-send it with ``orders.resubmit(error.signed_body)``.

    Once you no longer want it, settle it in one call with
    ``orders.cancel_by_index(error.signed_body["l2OrderIndex"])``
    (:meth:`~loaf.resources.orders.OrdersResource.cancel_by_index`): it
    answers ``CANCELLED`` (it was resting, and is cancelled now), ``ENDED``
    (it was placed and no longer rests: ``status`` says how it ended) or
    ``VOIDED`` (it was never placed, and never will be). A stop or take that
    was placed is refused there instead, with its row's id in the message:
    cancel it with :meth:`~loaf.resources.orders.OrdersResource.cancel_conditional`.
    Or look for the order in ``history.orders()`` by its ``expiryMs``; a miss
    there is no proof while it can still rest, since an attempt the server
    still holds can be admitted, and its row recorded, until then.

    After an epoch move (a Reset trading key, or a cancel-all sent on chain)
    the exchange has cancelled any resting order of the old epoch, and refuses
    the body. :meth:`~loaf.resources.orders.OrdersResource.resubmit` documents
    what each answer means.

    Deliberately NOT a :class:`LoafConnectionError`: code that re-places an
    order on a connection error must not catch this one.

    Attributes:
        signed_body: The exact body that was sent (its order number, expiry and
            signatures). It holds no key, but placing it needs none: anyone who
            has it can place this order until it can no longer rest, so keep it
            out of logs and alerts (the message leaves the signatures out).
        attempts: How many times the SDK tried to send it; 0 when
            :meth:`~loaf.resources.orders.OrdersResource.resubmit` did not try
            because the exchange would refuse it unread: the order can no
            longer rest, or its ``sendByMs`` has passed (``last_error`` says
            which).
        last_error: The last attempt's error (also ``__cause__``).
    """

    def __init__(
        self, *, signed_body: dict[str, Any], attempts: int, last_error: Exception
    ) -> None:
        self.signed_body = signed_body
        self.attempts = attempts
        self.last_error = last_error
        super().__init__(
            f"Could not confirm whether order {signed_body.get('l2OrderIndex')} of account "
            f"{signed_body.get('l2AccountIndex')} was placed after {attempts} attempt(s) (last "
            f"error: {last_error}). It may be live, and may already have filled. Never place it "
            "afresh: while you still want it and it can still rest, re-send it unchanged with "
            "orders.resubmit(error.signed_body) (the same order if it landed; if not, it is "
            "placed now at the signed price). Once you no longer want it, settle it with "
            f"orders.cancel_by_index({signed_body.get('l2OrderIndex')}): CANCELLED, ENDED or "
            "VOIDED (never placed, and never will be). Or find it in history.orders() by its "
            f"expiryMs {signed_body.get('l2ExpiryMs')} (every status; openOrders shows only "
            "resting orders), where a miss proves nothing while it can still rest (until its "
            "expiryMs minus 72 hours)."
        )


class PerpsOutcomeUnknownError(LoafError):
    """A signed perps allocate or exit may or may not have been applied; the SDK
    could not find out.

    Raised by :meth:`~loaf.resources.perps.PerpsResource.allocate` (and its
    ``add_margin`` / ``return_margin``) and
    :meth:`~loaf.resources.perps.PerpsResource.exit` after a timeout or a
    server-side failure that the SDK's own identical re-sends did not settle,
    and by :meth:`~loaf.resources.perps.PerpsResource.resubmit` on anything but
    a 202. Never sign the same intent afresh while it may have been applied: a
    fresh op signs the next nonce, so both could apply.

    Re-send it unchanged with ``perps.resubmit(error.signed_body)`` instead.
    The exchange applies one op per nonce and answers a re-send of an applied
    one as its duplicate (``duplicate: True``) whatever its expiry, so a 202
    settles it: applied before, or applied now. A 422 to a re-send made once
    the op's ``l2ExpiryMs`` has passed (allow a few minutes for clocks)
    settles it the other way: it was not applied and never will be. Before
    then an earlier attempt may still be on its way.

    Deliberately NOT a :class:`LoafConnectionError`, nor an
    :class:`OrderOutcomeUnknownError` (whose ``signed_body`` is an order).

    Attributes:
        signed_body: The exact body that was sent (its nonce, expiry and
            signatures). It holds no key, but sending it needs nothing beyond
            a login token: keep it out of logs and alerts (the message leaves
            the signatures out).
        attempts: How many times the SDK tried to send it.
        last_error: The last attempt's error (also ``__cause__``).
    """

    def __init__(
        self, *, signed_body: dict[str, Any], attempts: int, last_error: Exception
    ) -> None:
        self.signed_body = signed_body
        self.attempts = attempts
        self.last_error = last_error
        kind = "allocate" if "fromSide" in signed_body else "exit"
        super().__init__(
            f"Could not confirm whether the {kind} of account "
            f"{signed_body.get('l2AccountIndex')} at nonce {signed_body.get('l2Nonce')} was "
            f"applied after {attempts} attempt(s) (last error: {last_error}). It may have been "
            "applied. Never sign it afresh: re-send it unchanged with "
            "perps.resubmit(error.signed_body), whose 202 settles it (duplicate: applied before; "
            f"otherwise applied now). Once its l2ExpiryMs {signed_body.get('l2ExpiryMs')} has "
            "passed, a 422 to a resubmit means it was never applied."
        )


class WithdrawalOutcomeUnknownError(LoafError):
    """A signed withdrawal may or may not have been journaled; the SDK could not
    find out.

    Raised by :meth:`~loaf.resources.funds.FundsResource.withdraw` after a
    timeout or a server-side failure that the SDK's own identical re-sends did
    not settle, and by :meth:`~loaf.resources.funds.FundsResource.resubmit` on
    anything but a 202 or a 422 ``ORDER_INDEX_USED``. Never sign the same
    withdrawal afresh while it may have been journaled: a fresh one signs the
    next nonce, so both could be paid.

    Re-send it unchanged with ``funds.resubmit(error.signed_body)`` instead:
    the exchange journals one withdrawal per account and nonce and answers a
    re-send of a journaled one as its duplicate (``duplicate: True``), so a 202
    settles it. It answers that before it checks the nonce, so a re-send
    refused 422 ``ORDER_INDEX_USED`` (raised as that
    :class:`LoafAPIError`) settles it too: no attempt was journaled, and none
    will be (its nonce went to another op): withdraw again. It takes the body
    only until its ``l2ExpiryMs`` minus
    :data:`~loaf.constants.L2_BUFFER_MS` (the withdrawal's ``ttl_ms`` after it
    was signed), by its own clock. Once it refuses a re-send for that
    (``ORDER_EXPIRY_INVALID``: the expiry is inside the buffer), your account's
    next nonce settles it (``GET /l2/keys``' ``withdrawalNonce``): still
    ``l2Nonce``, it was never journaled and never will be; past it, this
    withdrawal or another op you signed at that nonce was.

    Attributes:
        signed_body: The exact body that was sent (its nonce, expiry and
            signatures). It holds no key, but sending it needs nothing beyond
            a login token: keep it out of logs and alerts (the message leaves
            the signatures out). It can only ever pay your own wallet.
        attempts: How many times the SDK tried to send it.
        last_error: The last attempt's error (also ``__cause__``).
    """

    def __init__(
        self, *, signed_body: dict[str, Any], attempts: int, last_error: Exception
    ) -> None:
        self.signed_body = signed_body
        self.attempts = attempts
        self.last_error = last_error
        super().__init__(
            f"Could not confirm whether the withdrawal of account "
            f"{signed_body.get('l2AccountIndex')} at nonce {signed_body.get('l2Nonce')} was "
            f"journaled after {attempts} attempt(s) (last error: {last_error}). It may have been. "
            "Never sign it afresh: re-send it unchanged with funds.resubmit(error.signed_body), "
            "whose 202 settles it (duplicate: journaled before; otherwise journaled now), as does "
            "a 422 ORDER_INDEX_USED (never journaled: withdraw again). The "
            f"exchange takes it until its l2ExpiryMs {signed_body.get('l2ExpiryMs')} minus 72 "
            "hours; once it refuses a re-send for that, your account's next nonce settles it "
            f"(GET /l2/keys withdrawalNonce): still {signed_body.get('l2Nonce')}, never "
            "journaled; past it, this or another op at that nonce was."
        )


class LoafAPIError(LoafError):
    """The server returned an HTTP error status.

    Attributes:
        status_code: HTTP status code.
        message: Human-readable error message (the API's ``error`` field; a
            refused login token's is followed by what it means).
        code: Machine-readable code when present. On the order routes (placements,
            cancels, ``cancel_after``, ``cancel_by_index``) and on a withdrawal
            every refusal has one, an :class:`~loaf.enums.OrderErrorCode` value:
            compare with it, never with the message. The list grows: treat a
            code you do not know by its status.
        details: List of field-level messages on a 400 validation error.
        request_id: The ``X-Request-Id`` for support correlation, if any.
        body: The raw parsed response body.
        replaced_order_cancelled: ``True`` when this refuses an order sent with
            ``replaces_order_id`` AFTER its batch cancelled the order it
            replaces: the old order stays cancelled although the new one was
            not placed. ``False`` otherwise: a refused replace that changed
            nothing (:class:`ReplaceTargetGoneError` included) leaves the old
            order as it was.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        code: str | None = None,
        details: list[str] | None = None,
        request_id: str | None = None,
        body: Any = None,
        replaced_order_cancelled: bool = False,
    ) -> None:
        self.status_code = status_code
        self.message = message
        self.code = code
        self.details = details or []
        self.request_id = request_id
        self.body = body
        self.replaced_order_cancelled = replaced_order_cancelled
        parts = [f"HTTP {status_code}: {message}"]
        if code:
            parts.append(f"(code={code})")
        if self.details:
            parts.append("[" + "; ".join(self.details) + "]")
        if request_id:
            parts.append(f"(request_id={request_id})")
        super().__init__(" ".join(parts))


class LoafAuthError(LoafAPIError):
    """401 — your trading key's login token, or an order's signature or key, was
    refused.

    Reads, cancels, the account read and order-number reservation an order
    placement makes, and perps allocates and exits and withdrawals (and their
    nonce read) are authenticated by a login token your trading key signs.
    A 401 to one is followed, in the message, by what it means (the server's
    own text stays in :attr:`body`):

    * your trading key is not the one in force on its account here: it was
      reset in the web app, the account's key is not active yet, or the key is
      another Loaf deployment's (``l2 signature: …``, ``l2 key: …``,
      ``l2 account: …``, or ``l2 login: the token is for another exchange's L2
      chain``). Every request fails the same way until you set the key in force
      (README §2) and restart; after a reset, place your stops and takes again:
      the exchange FAILS the ones signed before it;
    * this machine's clock is off (``l2 login: the token has expired``, or
      ``… expires more than 15 minutes ahead``): synchronize it, to within about
      5 minutes of the exchange's.

    The next request signs a fresh login token. The WebSocket never raises it:
    while ``portfolio`` is subscribed, it drops a connection whose login token
    the server does not accept and retries, reporting each attempt to
    ``on_disconnect``.

    Otherwise, from an order placement, the exchange refused the order's
    signature or key — ``l2 signature: …`` (prefixed ``tp: `` / ``sl: `` for a
    leg), ``l2 key: …``, or ``l2 account: no trading account has this account
    index``. Nothing was placed. The next order re-reads your trading account,
    whose login token is refused, with the cause named, if the key changed.

    From a withdrawal, a 401 with ``code`` ``INVALID_SIGNATURE`` refuses the
    withdrawal's own signature or key (``l2 signature: …``, ``l2 key: …``, or
    ``l2 money: the signing key is not the key the journal set for the api
    key``): nothing was journaled, and the message names your trading key not
    in force, as for a login token.
    """


class LoafForbiddenError(LoafAPIError):
    """403 — authenticated but not permitted."""


class TradingHaltedError(LoafForbiddenError):
    """Trading is halted: every new order is refused while it lasts, and nothing
    was placed.

    403 ``TRADING_HALTED``, the platform-wide halt, or ``MARKET_HALTED``, that
    market's halt (raised as :class:`MarketHaltedError`, which is a
    ``TradingHaltedError`` too). A platform-wide halt also blocks
    :meth:`~loaf.resources.orders.OrdersResource.cancel`, ``cancel_all`` and
    ``cancel_by_index`` (a single-market halt does not). The exceptions are
    :meth:`~loaf.resources.orders.OrdersResource.cancel_conditional` and
    ``cancel_after``, which the exchange takes under any halt — during a
    platform halt the first is your only way to pull a resting stop before the
    reopen.

    Or the exchange's matching engine's own pause, which refuses orders but
    still admits cancels: :class:`TradingPausedError` (503 ``TRADING_PAUSED``).

    Back off and retry later; there is no client-side fix.
    """


class MarketStateError(LoafForbiddenError):
    """403 — the market's own state refuses the order now; nothing was placed.

    ``state`` is the exchange engine's name for it, the refusal's ``code`` but
    for a held price's:

    * ``MARKET_HALTED`` — the market is halted (:class:`MarketHaltedError`,
      also a :class:`TradingHaltedError`): no order is taken.
    * ``MARKET_CLOSED`` — the market is outside its trading hours: no order is
      taken until it opens.
    * ``REDUCE_ONLY`` — the market is reduce-only; or, as a
      :class:`PriceHeldError` (code ``PRICE_HELD``), reduce-only while its
      latest price waits for review.
    * ``PRICE_STALE`` — the market's price is stale, until a fresh price
      arrives: reduce-only.

    In the last two an order that only reduces your position (a perps
    ``reduce_only=True`` order) is still taken. Back off and retry later;
    there is no client-side fix.

    Attributes:
        state: ``MARKET_HALTED``, ``MARKET_CLOSED``, ``REDUCE_ONLY`` or
            ``PRICE_STALE``.
    """

    def __init__(self, *args: Any, state: str, **kwargs: Any) -> None:
        self.state = state
        super().__init__(*args, **kwargs)


class MarketHaltedError(MarketStateError, TradingHaltedError):
    """403 ``MARKET_HALTED``: the market's own halt (``state`` ``MARKET_HALTED``).
    A :class:`TradingHaltedError` too, so code that backs off on halts catches it
    either way."""


class PriceHeldError(MarketStateError):
    """403 ``PRICE_HELD``: the market's newest price is held for an operator's
    review, and until it is signed off the market is reduce-only (``state``
    ``REDUCE_ONLY``). A ``reduce_only=True`` order is still taken; retry the
    rest later."""


class LoafValidationError(LoafAPIError):
    """400 — request validation failed. See :attr:`details` for field errors.

    Also raised client-side (with ``status_code=0``) for inputs this SDK can
    reject before sending, e.g. a limit price with too many decimal places.
    """


class PostOnlyWouldTakeError(LoafValidationError):
    """400 ``POST_ONLY_WOULD_TAKE``: a ``POST_ONLY`` order crossed the book's best
    resting order of the other side. Nothing was placed: re-price it behind the
    touch, or send it again."""


class SendByPassedError(LoafValidationError):
    """400 ``SEND_BY_PASSED``: the order's ``send_by_ms`` passed before the
    exchange decided it. Nothing was placed and its order number is not spent
    (a later order may sign it): place the order again if you still want it.

    The exchange checks ``send_by_ms`` before anything else, so a re-send of a
    body past it learns nothing about an earlier attempt:
    :meth:`~loaf.resources.orders.OrdersResource.resubmit` sends none unless
    given a later ``send_by_ms=``, and
    :meth:`~loaf.resources.orders.OrdersResource.cancel_by_index` settles it.
    """


class LoafNotFoundError(LoafAPIError):
    """404 — the resource does not exist."""


class LoafConflictError(LoafAPIError):
    """409 — conflict (e.g. a cancel that lost the race to a fill, a stop /
    take whose order number another order holds)."""


class ReplaceTargetGoneError(LoafConflictError):
    """409 ``REPLACE_TARGET_GONE``: the order a replace names
    (``replaces_order_id``) is not resting on that market: it filled, was
    cancelled or expired, or is another market's. Nothing changed: the new order
    was not placed, and its order number is not spent. Read your open orders
    before you place it plainly."""


class LoafRateLimitError(LoafAPIError):
    """429 — too many requests.

    Attributes:
        retry_after: Seconds to wait before retrying, derived from the
            ``RateLimit-Reset`` / ``Retry-After`` headers when present.
    """

    def __init__(self, *args: Any, retry_after: float | None = None, **kwargs: Any) -> None:
        self.retry_after = retry_after
        super().__init__(*args, **kwargs)


class LoafServerError(LoafAPIError):
    """5xx — the server failed to process the request.

    Also raised with a 2xx ``status_code`` for an answer the SDK cannot use,
    e.g. a body labelled JSON that is not JSON.
    """


class LoafServiceUnavailableError(LoafServerError):
    """503 — a service is temporarily unavailable, e.g. the trading service,
    candle history under load (``Candle history is busy…``), or your perps read
    through ``loaf.perps`` (a summary's ``perps`` is ``None``).

    Transient: the client retries reads, and re-sends signed placements (see
    :meth:`~loaf.resources.orders.OrdersResource.create`), before raising it.
    From a placement it means the order was not placed: a 503 that might have
    placed it raises :class:`OrderOutcomeUnknownError` instead.
    """


class TradingPausedError(TradingHaltedError, LoafServiceUnavailableError):
    """503 ``TRADING_PAUSED``: the exchange's matching engine has paused trading.
    Nothing was placed (or allocated, exited or withdrawn). Cancels are still
    taken.

    A :class:`TradingHaltedError` and a :class:`LoafServiceUnavailableError`
    both, so code that backs off on halts and code that retries a 503 each
    catch it. A signed placement refused so was re-sent first, like any 503
    that placed nothing (:meth:`~loaf.resources.orders.OrdersResource.create`).

    Attributes:
        retry_after: The seconds the exchange's ``Retry-After`` asked for, or
            ``None`` without one.
    """

    def __init__(self, *args: Any, retry_after: float | None = None, **kwargs: Any) -> None:
        self.retry_after = retry_after
        super().__init__(*args, **kwargs)


# The refusals that raise a class of their own, by status and code (the code is the contract; the
# message is for people). Any other answer raises its status's class.
_MARKET_STATES: dict[str, tuple[type[MarketStateError], str]] = {
    "MARKET_HALTED": (MarketHaltedError, "MARKET_HALTED"),
    "MARKET_CLOSED": (MarketStateError, "MARKET_CLOSED"),
    "REDUCE_ONLY": (MarketStateError, "REDUCE_ONLY"),
    "PRICE_STALE": (MarketStateError, "PRICE_STALE"),
    # A held price: the market is reduce-only while its latest price waits for review.
    "PRICE_HELD": (PriceHeldError, "REDUCE_ONLY"),
}
_CODED: dict[tuple[int, str], type[LoafAPIError]] = {
    (403, "TRADING_HALTED"): TradingHaltedError,
    (400, "POST_ONLY_WOULD_TAKE"): PostOnlyWouldTakeError,
    (400, "SEND_BY_PASSED"): SendByPassedError,
    (409, "REPLACE_TARGET_GONE"): ReplaceTargetGoneError,
}


def _client_validation_error(message: str, details: list[str] | None = None) -> LoafValidationError:
    """Build a validation error for input rejected locally (no HTTP round-trip)."""
    return LoafValidationError(message, status_code=0, details=details)


def error_from_response(
    status_code: int,
    body: Any,
    *,
    request_id: str | None = None,
    retry_after: float | None = None,
) -> LoafAPIError:
    """Map an HTTP error response to the most specific :class:`LoafAPIError`: by
    its status, and on the order routes by its ``code`` too, never by its text.

    ``retry_after`` is the answer's own hint, in seconds: carried by a 429 and a
    503 ``TRADING_PAUSED``.
    """
    message = "Unknown error"
    code: Any = None
    details: list[str] | None = None
    replaced_order_cancelled = False

    if isinstance(body, dict):
        message = str(body.get("error") or message)
        code = body.get("code")
        raw_details = body.get("details")
        if isinstance(raw_details, list):
            details = [str(d) for d in raw_details]
        replaced_order_cancelled = body.get("replacedOrderCancelled") is True
    elif isinstance(body, str) and body.strip():
        # A route that does not exist answers with an HTML page: keep just its <pre> text.
        pre = _PRE_RE.search(body)
        message = html.unescape(pre.group(1)).strip() if pre else body.strip()

    kwargs: dict[str, Any] = dict(
        status_code=status_code,
        code=code,
        details=details,
        request_id=request_id,
        body=body,
        replaced_order_cancelled=replaced_order_cancelled,
    )

    if isinstance(code, str):
        if status_code == 403 and code in _MARKET_STATES:
            cls, state = _MARKET_STATES[code]
            return cls(message, state=state, **kwargs)
        if status_code == 503 and code == "TRADING_PAUSED":
            return TradingPausedError(message, retry_after=retry_after, **kwargs)
        coded = _CODED.get((status_code, code))
        if coded is not None:
            return coded(message, **kwargs)
    if status_code == 401:
        return LoafAuthError(message, **kwargs)
    if status_code == 403:
        return LoafForbiddenError(message, **kwargs)
    if status_code == 400:
        return LoafValidationError(message, **kwargs)
    if status_code == 404:
        return LoafNotFoundError(message, **kwargs)
    if status_code == 409:
        return LoafConflictError(message, **kwargs)
    if status_code == 429:
        return LoafRateLimitError(message, retry_after=retry_after, **kwargs)
    if status_code == 503:
        return LoafServiceUnavailableError(message, **kwargs)
    if status_code >= 500:
        return LoafServerError(message, **kwargs)
    return LoafAPIError(message, **kwargs)

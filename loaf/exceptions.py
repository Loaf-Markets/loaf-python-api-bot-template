"""Exception hierarchy for the Loaf SDK.

Every error the API can return is mapped to a specific exception so a bot can
``except`` precisely:

    LoafError                       (base — catch this to catch everything)
    ├── LoafConfigError             misconfiguration (e.g. missing API key or trading key, a trading
    │                               key not in force, a clock hours ahead)
    ├── LoafConnectionError         network failure / timeout / a body that failed to decompress
    ├── OrderOutcomeUnknownError    a signed order may be live; the SDK could not confirm it
    └── LoafAPIError                the server returned an HTTP error
        ├── LoafAuthError                 401  bad/expired credentials, or an order's signature or key
        ├── LoafForbiddenError            403  generic forbidden
        │   └── TradingHaltedError        403  "…halted…", or 400 from the exchange's engine
        ├── LoafValidationError           400  validation or order-rule refusal (or local, status 0)
        ├── LoafNotFoundError             404
        ├── LoafConflictError             409  (e.g. a cancel that lost the race, a prize already claimed)
        ├── LoafRateLimitError            429  (carries .retry_after seconds)
        └── LoafServerError               5xx  (503 -> LoafServiceUnavailableError)
            └── LoafServiceUnavailableError 503
"""

from __future__ import annotations

import html
import re
from typing import Any

_PRE_RE = re.compile(r"<pre>(.*?)</pre>", re.IGNORECASE | re.DOTALL)

# The exchange engine's own halts, which the API passes through as 400 with the engine's text.
_ENGINE_HALTS = ("trading halted:", "L1_RECHECK_PENDING:", "E_L2_ORDERS_WITHOUT_JOURNAL:")


class LoafError(Exception):
    """Base class for every error raised by this SDK."""

    def __reduce__(self) -> Any:
        # The default rebuilds with cls(*args), which keyword-only __init__s refuse; restore the
        # attributes instead, so errors survive pickle (process pools) and copy.
        return (_rebuild_error, (type(self), self.args), self.__dict__)


def _rebuild_error(cls: type[LoafError], args: tuple) -> LoafError:
    return cls.__new__(cls, *args)


class LoafConfigError(LoafError):
    """The client is misconfigured: no API key for an authed request, a missing
    or malformed API key or trading key, an invalid
    ``LoafClient(max_slippage_bps=)`` / ``$LOAF_MAX_SLIPPAGE_BPS`` or
    ``websocket(reconnect_delay=)``, or ``LOAF_WALLET_PRIVATE_KEY`` set in the
    bot's environment.

    From an order placement (nothing was placed):

    * your trading key is not the one in force on the market (it was reset in
      the web app, or it belongs to another vault): get the key in force again
      (README §2), restart, and cancel and re-arm every stop and take, because
      those signed with the old key stay ARMED and FAIL when they fire;
    * trading is not enabled on your account, or you have no trading account
      yet: act in the web app (deposit first);
    * this machine's clock is hours ahead of the exchange's: synchronize it.

    Act in the web app or fix the setup, then restart.
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
    re-send it with ``orders.resubmit(error.signed_body)``. Otherwise look for
    it in ``history.orders()`` by its ``expiryMs``; a miss is no proof while it
    can still rest, since an attempt the server still holds can be admitted,
    and its row recorded, until then.

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
            because the order can no longer rest (``last_error`` says so).
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
            "afresh: while it can still rest, re-send it unchanged with "
            "orders.resubmit(error.signed_body) (the same order if it landed; if not, it is "
            "placed now at the signed price), or find it in history.orders() by its expiryMs "
            f"{signed_body.get('l2ExpiryMs')} (every status; openOrders shows only resting "
            "orders). A history miss proves nothing while it can still rest (until its expiryMs "
            "minus 72 hours)."
        )


class LoafAPIError(LoafError):
    """The server returned an HTTP error status.

    Attributes:
        status_code: HTTP status code.
        message: Human-readable error message (the API's ``error`` field).
        code: Machine-readable code when present.
        details: List of field-level messages on a 400 validation error.
        request_id: The ``X-Request-Id`` for support correlation, if any.
        body: The raw parsed response body.
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
    ) -> None:
        self.status_code = status_code
        self.message = message
        self.code = code
        self.details = details or []
        self.request_id = request_id
        self.body = body
        parts = [f"HTTP {status_code}: {message}"]
        if code:
            parts.append(f"(code={code})")
        if self.details:
            parts.append("[" + "; ".join(self.details) + "]")
        if request_id:
            parts.append(f"(request_id={request_id})")
        super().__init__(" ".join(parts))


class LoafAuthError(LoafAPIError):
    """401 — credentials, or an order's signature or key, were refused.

    ``Invalid credentials``: the API key is unknown, expired or deleted. Reads
    and cancels raise it, and so do order placements, which read your trading
    account and reserve order numbers with the API key: every request it
    authenticates fails the same way until you create a new key in the web app
    and restart. The WebSocket never raises it: while ``portfolio`` is
    subscribed, it drops a connection whose key the server does not accept and
    retries, reporting each attempt to ``on_disconnect``.

    Otherwise, from an order placement, the exchange refused the order's
    signature or key — ``l2 signature: …`` (prefixed ``tp: `` / ``sl: `` for a
    leg), ``l2 key: …``, or ``l2 account: no trading account has this account
    index``. Nothing was placed. The next order re-reads your trading account
    and names the cause. After a key change, stops and takes signed before it
    FAIL when they fire: re-arm them.
    """


class LoafForbiddenError(LoafAPIError):
    """403 — authenticated but not permitted."""


class TradingHaltedError(LoafForbiddenError):
    """Trading is halted: every new order is refused while it lasts, and nothing
    was placed.

    403 ``Trading is currently halted`` (platform-wide) or ``Trading halted for
    this market``. A platform-wide halt also blocks
    :meth:`~loaf.resources.orders.OrdersResource.cancel` and ``cancel_all`` (a
    single-market halt does not). The one exception is
    :meth:`~loaf.resources.orders.OrdersResource.cancel_conditional`, which is
    database-only and stays available — during a platform halt it is your only
    way to pull a resting stop before the reopen.

    Or 400 from the exchange's engine, whose own halts refuse orders but still
    admit cancels.

    Back off and retry later; there is no client-side fix.
    """


class LoafValidationError(LoafAPIError):
    """400 — request validation failed. See :attr:`details` for field errors.

    Also raised client-side (with ``status_code=0``) for inputs this SDK can
    reject before sending, e.g. a limit price with too many decimal places.
    """


class LoafNotFoundError(LoafAPIError):
    """404 — the resource does not exist."""


class LoafConflictError(LoafAPIError):
    """409 — conflict (e.g. a cancel that lost the race to a fill, a stop /
    take whose order number another order holds, payout details already
    submitted)."""


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
    """503 — a service is temporarily unavailable, e.g. the trading service, or
    candle history under load (``Candle history is busy…``).

    Transient: the client retries reads, and re-sends signed placements (see
    :meth:`~loaf.resources.orders.OrdersResource.create`), before raising it.
    From a placement it means the order was not placed: a 503 that might have
    placed it raises :class:`OrderOutcomeUnknownError` instead.
    """


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
    """Map an HTTP error response to the most specific :class:`LoafAPIError`."""
    message = "Unknown error"
    code: str | None = None
    details: list[str] | None = None

    if isinstance(body, dict):
        message = str(body.get("error") or message)
        code = body.get("code")
        raw_details = body.get("details")
        if isinstance(raw_details, list):
            details = [str(d) for d in raw_details]
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
    )

    if status_code == 401:
        return LoafAuthError(message, **kwargs)
    if status_code == 403:
        # The halt rejection carries no machine code — match on the message.
        if "halted" in message.lower():
            return TradingHaltedError(message, **kwargs)
        return LoafForbiddenError(message, **kwargs)
    if status_code == 400:
        if message.startswith(_ENGINE_HALTS):
            return TradingHaltedError(message, **kwargs)
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

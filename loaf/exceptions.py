"""Exception hierarchy for the Loaf SDK.

Every error the API can return is mapped to a specific exception so a bot can
``except`` precisely:

    LoafError                       (base — catch this to catch everything)
    ├── LoafConfigError             misconfiguration (e.g. missing API key)
    ├── LoafConnectionError         network failure / timeout (no HTTP response)
    ├── OrderOutcomeUnknownError    a signed order may be live; the SDK could not confirm it
    └── LoafAPIError                the server returned an HTTP error
        ├── LoafAuthError                 401  bad/expired credentials, or an order signature the exchange refused
        ├── LoafForbiddenError            403  generic forbidden
        │   ├── TradingHaltedError        403  "Trading is currently halted" / "Trading halted for this property"
        │   └── CompetitionEligibilityError 403 code=NOT_COMPETITION_PARTICIPANT
        ├── LoafValidationError           400  validation or order-rule refusal (incl. the daily price band)
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


class LoafError(Exception):
    """Base class for every error raised by this SDK."""

    def __reduce__(self) -> Any:
        # The default rebuilds with cls(*args), which keyword-only __init__s refuse; restore the
        # attributes instead, so errors survive pickle (process pools) and copy.
        return (_rebuild_error, (type(self), self.args), self.__dict__)


def _rebuild_error(cls: type[LoafError], args: tuple) -> LoafError:
    return cls.__new__(cls, *args)


class LoafConfigError(LoafError):
    """The client is misconfigured: no API key for an authed request, a
    missing or malformed agent private key when placing an order, or an
    invalid ``LoafClient(max_slippage_bps=)`` / ``$LOAF_MAX_SLIPPAGE_BPS``."""


class LoafConnectionError(LoafError):
    """The request never received an HTTP response (network error / timeout)."""


class OrderOutcomeUnknownError(LoafError):
    """A signed order may or may not have been placed; the SDK could not find out.

    Raised by order placement after a timeout or a server-side failure that the
    SDK's own identical re-sends did not settle, and by
    :meth:`~loaf.resources.orders.OrdersResource.resubmit` on anything but a
    200. The order may be live, and may already have filled, so never place the
    same intent afresh. While you still want it, re-send it with
    ``orders.resubmit(error.signed_body)``: within 24 hours of signing, and
    never after a competition round switch. Otherwise look for it in
    ``history.orders()``.
    :meth:`~loaf.resources.orders.OrdersResource.resubmit` documents what each
    answer means.

    Deliberately NOT a :class:`LoafConnectionError`: code that re-places an
    order on a connection error must not catch this one.

    Attributes:
        signed_body: The exact body that was sent (nonce and signatures; no secrets).
        attempts: How many times it was sent.
        last_error: The last attempt's error (also ``__cause__``).
    """

    def __init__(
        self, *, signed_body: dict[str, Any], attempts: int, last_error: Exception
    ) -> None:
        self.signed_body = signed_body
        self.attempts = attempts
        self.last_error = last_error
        super().__init__(
            f"Could not confirm whether order {self.nonce} was placed after {attempts} "
            f"attempt(s) (last error: {last_error}). It may be live, and may already have "
            "filled: re-send it unchanged with orders.resubmit(error.signed_body) within 24 "
            "hours of signing and before any round switch (the same order if it landed; if "
            "not, it is placed now at the signed price), or look for it in "
            "history.orders(), which lists every status. openOrders shows only resting orders."
        )

    @property
    def nonce(self) -> str:
        """The order's nonce (``signed_body["nonce"]``)."""
        return str(self.signed_body.get("nonce", ""))


class LoafAPIError(LoafError):
    """The server returned an HTTP error status.

    Attributes:
        status_code: HTTP status code.
        message: Human-readable error message (the API's ``error`` field).
        code: Machine-readable code when present (e.g. ``NOT_COMPETITION_PARTICIPANT``).
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
    """401 — credentials or an order signature were refused.

    On reads and cancels: the API key is unknown, expired or deleted
    (``Invalid credentials``). The WebSocket never raises it: a refused key
    leaves the socket anonymous, and a ``portfolio`` subscription is refused
    with an ``error`` frame (code ``UNAUTHORIZED``) sent to ``on_error``.

    On order placement, which never sends the API key: the exchange refused the
    order's signature. ``Signer is not authorized to trade for any account``
    means the agent key is not approved for any account; it expires and is
    revoked together with its API key. Stop placing and alert.
    """


class LoafForbiddenError(LoafAPIError):
    """403 — authenticated but not permitted."""


class TradingHaltedError(LoafForbiddenError):
    """403 "Trading is currently halted" (platform-wide) or "Trading halted for
    this property".

    Order placement rejects while the halt lasts. A platform-wide halt also
    blocks :meth:`~loaf.resources.orders.OrdersResource.cancel` and
    ``cancel_all`` (a single-property halt does not). The one exception is
    :meth:`~loaf.resources.orders.OrdersResource.cancel_conditional`, which is
    database-only and stays available — during a platform halt it is your only
    way to pull a resting stop before the reopen. Otherwise back off and retry
    later; there is no client-side fix.
    """


class CompetitionEligibilityError(LoafForbiddenError):
    """403 ``NOT_COMPETITION_PARTICIPANT`` — not admitted to the active round.

    Raised on order placement while a competition round is ACTIVE and your
    account has not been admitted, and also while the market switches modes,
    while a round is being prepared, and while it is finalizing. Check your
    standing with
    :meth:`loaf.resources.competition.CompetitionResource.queue_position`.
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
    take nonce already used by a different order, payout details already
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
    """5xx — the server failed to process the request."""


class LoafServiceUnavailableError(LoafServerError):
    """503 — a service is temporarily unavailable, e.g. the trading service, or
    candle history under load (``Candle history is busy…``).

    Transient: the client retries reads, and re-sends signed placements (see
    :meth:`~loaf.resources.orders.OrdersResource.create`), before raising it.
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
        if code == "NOT_COMPETITION_PARTICIPANT":
            return CompetitionEligibilityError(message, **kwargs)
        # The halt rejection carries no machine code — match on the message.
        if "halted" in message.lower():
            return TradingHaltedError(message, **kwargs)
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

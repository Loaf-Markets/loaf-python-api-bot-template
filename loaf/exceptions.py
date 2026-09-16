"""Exception hierarchy for the Loaf SDK.

Every error the API can return is mapped to a specific exception so a bot can
``except`` precisely:

    LoafError                       (base — catch this to catch everything)
    ├── LoafConfigError             misconfiguration (e.g. missing API key)
    ├── LoafConnectionError         network failure / timeout (no HTTP response)
    └── LoafAPIError                the server returned an HTTP error
        ├── LoafAuthError                 401  bad/expired/missing credentials
        ├── LoafForbiddenError            403  generic forbidden
        │   ├── KycRequiredError          403  retail/wholesale KYC required
        │   ├── TradingHaltedError        403  "Trading is currently halted" (platform-wide or per-property)
        │   └── CompetitionEligibilityError 403 code=NOT_COMPETITION_PARTICIPANT
        ├── LoafValidationError           400  body/query/param validation failed
        ├── LoafNotFoundError             404
        ├── LoafConflictError             409  (e.g. handle taken, already referred)
        ├── LoafBusinessRuleError         422  (e.g. price-band violation)
        ├── LoafRateLimitError            429  (carries .retry_after seconds)
        └── LoafServerError               5xx  (503 -> LoafServiceUnavailableError)
            └── LoafServiceUnavailableError 503
"""

from __future__ import annotations

from typing import Any


class LoafError(Exception):
    """Base class for every error raised by this SDK."""


class LoafConfigError(LoafError):
    """The client is misconfigured (e.g. no API key for an authed request)."""


class LoafConnectionError(LoafError):
    """The request never received an HTTP response (network error / timeout)."""


class LoafAPIError(LoafError):
    """The server returned an HTTP error status.

    Attributes:
        status_code: HTTP status code.
        message: Human-readable error message (the API's ``error`` field).
        code: Machine-readable code when present (e.g. ``REFERRAL_REQUIRED``).
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
    """401 — credentials are missing, invalid, or expired."""


class LoafForbiddenError(LoafAPIError):
    """403 — authenticated but not permitted."""


class KycRequiredError(LoafForbiddenError):
    """403 — retail or wholesale KYC verification is required for this action."""


class TradingHaltedError(LoafForbiddenError):
    """403 "Trading is currently halted" — a platform-wide or per-property halt.

    Order placement and offering subscriptions reject while the halt lasts, as
    do :meth:`~loaf.resources.orders.OrdersResource.cancel` and ``cancel_all``.
    The one exception is
    :meth:`~loaf.resources.orders.OrdersResource.cancel_conditional`, which is
    database-only and stays available — during a platform halt it is your only
    way to pull a resting stop before the reopen. Otherwise back off and retry
    later; there is no client-side fix.
    """


class CompetitionEligibilityError(LoafForbiddenError):
    """403 ``NOT_COMPETITION_PARTICIPANT`` — not admitted to the active round.

    Raised on order placement while a competition round is ACTIVE and your
    account has not been admitted. Check your standing with
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
    """409 — conflict (e.g. handle already taken, referral already redeemed)."""


class LoafBusinessRuleError(LoafAPIError):
    """422 — a business rule rejected the request (e.g. price-band violation)."""


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
    """503 — the trading service is temporarily unavailable (transient; retry)."""


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
        message = str(body.get("error") or body.get("message") or message)
        code = body.get("code")
        raw_details = body.get("details")
        if isinstance(raw_details, list):
            details = [str(d) for d in raw_details]
    elif isinstance(body, str) and body.strip():
        message = body.strip()

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
        if "kyc" in message.lower() or "wholesale" in message.lower():
            return KycRequiredError(message, **kwargs)
        return LoafForbiddenError(message, **kwargs)
    if status_code == 400:
        return LoafValidationError(message, **kwargs)
    if status_code == 404:
        return LoafNotFoundError(message, **kwargs)
    if status_code == 409:
        return LoafConflictError(message, **kwargs)
    if status_code == 422:
        return LoafBusinessRuleError(message, **kwargs)
    if status_code == 429:
        return LoafRateLimitError(message, retry_after=retry_after, **kwargs)
    if status_code == 503:
        return LoafServiceUnavailableError(message, **kwargs)
    if status_code >= 500:
        return LoafServerError(message, **kwargs)
    return LoafAPIError(message, **kwargs)

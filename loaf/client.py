"""The synchronous Loaf API client."""

from __future__ import annotations

import copy
import json as _json
import numbers
import os
import random
import re
import ssl
import time
from typing import TYPE_CHECKING, Any

import httpx

from ._l2 import _SECRET_RE, TradingKey
from ._object import parse
from ._version import __version__
from .constants import (
    DEFAULT_BASE_URL,
    DEFAULT_MAX_RETRIES,
    DEFAULT_MAX_SLIPPAGE_BPS,
    DEFAULT_PLACEMENT_TIMEOUT,
    DEFAULT_TIMEOUT,
    ENV_API_KEY,
    ENV_BASE_URL,
    ENV_SECRET_KEY,
    ENV_MAX_SLIPPAGE_BPS,
    ENV_WALLET_PRIVATE_KEY,
    ENV_WS_URL,
    USER_AGENT,
)
from .exceptions import (
    LoafAPIError,
    LoafConfigError,
    LoafConnectionError,
    LoafServerError,
    OrderOutcomeUnknownError,
    error_from_response,
)

if TYPE_CHECKING:
    from .resources.competition import CompetitionResource
    from .resources.history import HistoryResource
    from .resources.leaderboard import LeaderboardResource
    from .resources.market import MarketResource
    from .resources.offerings import OfferingsResource
    from .resources.orders import OrdersResource
    from .resources.portfolio import PortfolioResource
    from .ws.client import LoafWebSocketClient

_IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "DELETE"})

_PRIVATE_KEY_SHAPE = re.compile(r"0[xX][0-9a-fA-F]{64}")
# A space or anything but printable ASCII, which no API key (64 hex) or JWT holds.
_STRAY = re.compile(r"[^\x21-\x7e]")


def _api_key_refusal(api_key: str) -> str | None:
    """Why ``api_key`` must not be sent as the API key: it holds what looks like a trading or
    wallet key, which must never leave this machine, or a character no API key has (a second
    line, say), with which httpx would quote the whole value in its error and the WebSocket
    send it in every auth frame. ``None`` for any other value."""
    try:
        # Searched for, not matched whole: a .env loader can keep quotes, a comment or the pasted
        # line's own name around the key. An API key is 64 hex, so it holds neither shape.
        if _SECRET_RE.search(api_key):  # 80 hex in a row are a trading key
            return (
                f"{ENV_API_KEY} holds what looks like your trading key (80 hex), which must never "
                f"leave your machine: it belongs in {ENV_SECRET_KEY} only. {ENV_API_KEY} is "
                "the API key: 64 hex characters, no prefix."
            )
        if _PRIVATE_KEY_SHAPE.search(api_key):
            return (
                f"{ENV_API_KEY} holds what looks like a wallet private key (0x + 64 hex), which "
                f"must never leave your machine. {ENV_API_KEY} is the API key: 64 hex characters, "
                "no prefix."
            )
        if _STRAY.search(api_key.strip()):  # the WebSocket client checks its key unstripped
            return (
                f"{ENV_API_KEY} has a line break, space or other stray character inside it. "
                f"{ENV_API_KEY} is the API key alone: 64 hex characters, no prefix."
            )
        return None
    finally:
        api_key = ""  # crash reporters record frame locals, and a str pattern raises on bytes


# Failures before the request left this machine (a proxy that refused the tunnel included):
# re-sending them is always safe.
_NOT_SENT_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError)

# 503s the exchange sends only when nothing was placed (safe to treat as a definite answer),
# lowercased: the exchange writes them in both cases.
_NOT_PLACED_503_TEXTS = (
    "your order was not placed",
    "nothing was done",
    "the l2 balance is temporarily unavailable",
)


def _definitely_not_placed(error: LoafAPIError) -> bool:
    return error.status_code == 503 and any(
        t in error.message.lower() for t in _NOT_PLACED_503_TEXTS
    )


# $LOAF_MAX_SLIPPAGE_BPS: a plain whole number from 0 to 9999 (leading zeros allowed).
_BPS_TEXT = re.compile(r"0*([0-9]{1,4})")
_BPS_RULE = "must be a whole number of basis points from 0 to 9999 (100 = 1%)"


def _max_slippage_bps(value: Any) -> int:
    """``max_slippage_bps=``, else ``$LOAF_MAX_SLIPPAGE_BPS``, else the default."""
    if value is not None:
        whole = isinstance(value, numbers.Integral) and not isinstance(value, bool)
        if not whole or not 0 <= int(value) < 10_000:
            raise LoafConfigError(f"max_slippage_bps {_BPS_RULE}, got {value!r}")
        return int(value)
    raw = (os.environ.get(ENV_MAX_SLIPPAGE_BPS) or "").strip()
    if not raw:
        return DEFAULT_MAX_SLIPPAGE_BPS
    match = _BPS_TEXT.fullmatch(raw)
    if match is None:
        raise LoafConfigError(f"${ENV_MAX_SLIPPAGE_BPS} {_BPS_RULE}, got {raw!r}")
    return int(match.group(1))


def _placement_timeout(timeout: Any) -> httpx.Timeout:
    """The client's timeout, with its read wait raised to at least DEFAULT_PLACEMENT_TIMEOUT."""
    t = httpx.Timeout(timeout)
    read = None if t.read is None else max(t.read, DEFAULT_PLACEMENT_TIMEOUT)
    return httpx.Timeout(connect=t.connect, read=read, write=t.write, pool=t.pool)


class LoafClient:
    """Entry point for talking to the Loaf API.

    Example::

        from loaf import LoafClient

        with LoafClient(api_key="...") as loaf:
            print(loaf.portfolio.component().balances)
            print(loaf.market.list())

    Args:
        api_key: The API key from the Loaf web app (API -> Generate API Token),
            sent as ``Authorization: Bearer`` on reads, cancels, the
            WebSocket, and the two account requests an order needs
            (``GET /l2/keys``, ``POST /l2/order-indices``). It is never sent on
            an order placement. If omitted, read from ``$LOAF_API_KEY``.
            Public endpoints work without one; other authenticated calls raise
            :class:`LoafConfigError`.
        base_url: REST base URL including the ``/api`` suffix. If omitted, read
            from ``$LOAF_API_BASE_URL``, else :data:`~loaf.constants.DEFAULT_BASE_URL`.
        secret_key: Your trading key, which signs every order locally and is
            never sent: the 80 hex characters (no ``0x``) the web app shows
            under Show trading key (README §2). If omitted, read from
            ``$LOAF_SECRET_KEY`` (prefer the environment variable: an
            explicit ``secret_key=`` argument can show up in tools that
            capture call frames). Without it everything works except placing
            orders.
        ws_url: Override the WebSocket URL. If omitted it is derived from
            ``base_url`` (``https://host/api`` -> ``wss://host/ws``) or read from
            ``$LOAF_WS_URL``.
        max_slippage_bps: The most a price the SDK works out for you may move
            against you, in basis points (100 = 1%), a whole number from 0 to
            9999. A MARKET order signs the market reference x (1 ± this) as
            its worst fill price, a ``*_MARKET`` stop / take books at its
            trigger ± this, and a TP/SL leg sells no lower than its trigger
            minus this. If omitted, read from ``$LOAF_MAX_SLIPPAGE_BPS``, else
            :data:`~loaf.constants.DEFAULT_MAX_SLIPPAGE_BPS` (200, 2%). Kept as
            ``client.max_slippage_bps``; one order overrides it with
            ``max_slippage_bps=`` / ``leg_max_slippage_bps=``.
        timeout: Per-request timeout in seconds (or anything httpx accepts).
            Order placements wait at least
            :data:`~loaf.constants.DEFAULT_PLACEMENT_TIMEOUT` (45 s) for the
            answer, and follow this argument even with your own ``http_client``.
        max_retries: Automatic retries for transient failures. Reads retry 429,
            503 and network errors. Signed placements retry network errors and
            5xx by re-sending the identical signed order after a short pause,
            which cannot place twice; each attempt can wait out the placement
            timeout, so lower this (e.g. ``1``) for a latency-sensitive loop.
            Cancels and other POSTs are never retried.
        verify: TLS verification for REST and the WebSocket (only the WebSocket
            with your own ``http_client``): an ``ssl.SSLContext``, such as
            ``ssl.create_default_context(cafile=...)``, trusts your own CAs. Set
            ``False`` ONLY for a trusted local dev server using a self-signed
            certificate — it disables MITM protection, so never use it against a
            remote host.
        http_client: Provide your own ``httpx.Client`` (advanced). Its response
            hooks must not raise (no ``raise_for_status`` hook): the SDK reads
            every answer itself, and a hook that raises bypasses its error
            handling, so the order numbers of a refused order are not reused
            and what the SDK cached is not refreshed.
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        *,
        secret_key: str | None = None,
        ws_url: str | None = None,
        max_slippage_bps: int | None = None,
        timeout: float | tuple | httpx.Timeout | None = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        verify: bool | ssl.SSLContext = True,
        http_client: httpx.Client | None = None,
    ) -> None:
        try:
            api_key = (api_key or os.environ.get(ENV_API_KEY) or "").strip()
            refusal = _api_key_refusal(api_key)
            if refusal:
                raise LoafConfigError(refusal)
            self.api_key = api_key or None
            if (os.environ.get(ENV_WALLET_PRIVATE_KEY) or "").strip():
                raise LoafConfigError(
                    f"{ENV_WALLET_PRIVATE_KEY} is set in this process's environment. The wallet "
                    "key must never reach your bot: remove it from your environment and from any "
                    ".env file your bot loads (only python -m loaf.secret_key reads it, for one "
                    "command; README §2)."
                )
            #: Default max slippage (basis points) for the prices the SDK derives for your orders.
            self.max_slippage_bps: int = _max_slippage_bps(max_slippage_bps)
            raw = (secret_key or os.environ.get(ENV_SECRET_KEY) or "").strip()
            #: Signs placements, and keeps the secret to itself; None without a trading key.
            self._l2_key: TradingKey | None = TradingKey(raw) if raw else None
        finally:
            # Crash reporters and debuggers record frame locals: whatever fails in __init__, no
            # trading key (nor a wallet or trading key passed as the API key) is left in them.
            raw = secret_key = api_key = None

        self.base_url = (base_url or os.environ.get(ENV_BASE_URL) or DEFAULT_BASE_URL).rstrip("/")
        self._ws_url_override = ws_url or os.environ.get(ENV_WS_URL) or None
        self._placement_timeout = _placement_timeout(timeout)
        self.max_retries = max(0, int(max_retries))
        self._verify = verify

        #: Most recent rate-limit snapshot from response headers, e.g.
        #: ``{"limit": 100, "remaining": 97, "reset": 873.0}`` (reset in seconds).
        self.last_rate_limit: dict[str, float] = {}

        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        }
        self._http = http_client or httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            headers=headers,
            verify=verify,
            # httpx's default drops a connection idle for 5 s, so a bot acting every few seconds
            # would pay a new TCP + TLS handshake on every order. A connection the far end has
            # closed meanwhile is noticed and replaced before it is reused.
            limits=httpx.Limits(
                max_connections=100, max_keepalive_connections=20, keepalive_expiry=30.0
            ),
        )
        self._owns_http = http_client is None

        # Lazily import to avoid an import cycle at module load.
        from .resources.competition import CompetitionResource
        from .resources.history import HistoryResource
        from .resources.leaderboard import LeaderboardResource
        from .resources.market import MarketResource
        from .resources.offerings import OfferingsResource
        from .resources.orders import OrdersResource
        from .resources.portfolio import PortfolioResource

        #: Public market data: markets, candles, trades, info pages.
        self.market: MarketResource = MarketResource(self)
        #: Primary market (IPO offerings): list, detail.
        self.offerings: OfferingsResource = OfferingsResource(self)
        #: Trading: place (signed with your trading key) and cancel orders, stop & take orders.
        self.orders: OrdersResource = OrdersResource(self)
        #: Balances, holdings, and recent activity.
        self.portfolio: PortfolioResource = PortfolioResource(self)
        #: Paginated order & trade history.
        self.history: HistoryResource = HistoryResource(self)
        #: Competition leaderboard.
        self.leaderboard: LeaderboardResource = LeaderboardResource(self)
        #: Trading competition: rounds info, queue position, payout details.
        self.competition: CompetitionResource = CompetitionResource(self)

    # ------------------------------------------------------------------ #
    # Public convenience verbs (return parsed bodies)
    # ------------------------------------------------------------------ #

    def get(self, path: str, *, params: dict | None = None, auth: bool = True) -> Any:
        return self.request("GET", path, params=params, auth=auth)

    def post(
        self, path: str, *, json: dict | None = None, params: dict | None = None, auth: bool = True
    ) -> Any:
        return self.request("POST", path, json=json, params=params, auth=auth)

    # ------------------------------------------------------------------ #
    # Core request logic
    # ------------------------------------------------------------------ #

    def request(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        params: dict | None = None,
        auth: bool = True,
    ) -> Any:
        """Send a request and return the parsed body (a :class:`LoafObject`,
        a list, or ``None``).

        Handles auth headers, JSON (de)serialisation, error mapping, and
        automatic retries for transient failures. You normally call the typed
        resource methods rather than this directly.
        """
        return self._request(method, path, json=json, params=params, auth=auth)

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        params: dict | None = None,
        auth: bool = True,
        before_order: bool = False,
    ) -> Any:
        """:meth:`request`, plus ``before_order``: set for the reads an order
        placement makes before signing, so a 429 raises at once and a 503 is
        retried after only a short jittered pause, never the rate-limit hints.
        """
        headers: dict[str, str] = {}
        if auth:
            if not self.api_key:
                raise LoafConfigError(
                    f"{method} {path} requires authentication but no API key is set. "
                    f"Pass api_key=... or set ${ENV_API_KEY}."
                )
            headers["Authorization"] = f"Bearer {self.api_key}"

        # Drop params/body keys whose value is None so we never send "null".
        clean_params = {k: v for k, v in (params or {}).items() if v is not None}
        clean_json = None if json is None else {k: v for k, v in json.items() if v is not None}

        idempotent = method.upper() in _IDEMPOTENT_METHODS
        attempt = 0
        while True:
            try:
                response = self._http.request(
                    method,
                    path,
                    params=clean_params or None,
                    json=clean_json,
                    headers=headers,
                )
            except httpx.RequestError as exc:
                if idempotent and attempt < self.max_retries:
                    self._sleep_backoff(attempt)
                    attempt += 1
                    continue
                raise LoafConnectionError(f"{method} {path} failed: {exc}") from exc

            self._record_rate_limit(response)

            if response.is_success:
                try:
                    return self._parse_body(response)
                except ValueError as exc:  # labelled JSON but not JSON
                    raise LoafServerError(
                        f"{method} {path} answered a body that is not JSON",
                        status_code=response.status_code,
                        request_id=response.headers.get("x-request-id"),
                        body=response.text,
                    ) from exc

            # Error path — decide whether to retry. Only idempotent (read) requests
            # are retried here. Signed placements go through _send_signed, which
            # re-sends the identical signed body. Other POSTs (cancels) are never retried.
            should_retry = (
                attempt < self.max_retries
                and idempotent
                and response.status_code in ((503,) if before_order else (429, 503))
            )
            if should_retry:
                # Before signing an order, never wait out rate-limit hints: a late order is stale.
                self._sleep_backoff(attempt, None if before_order else response)
                attempt += 1
                continue

            raise self._build_error(response)

    # ------------------------------------------------------------------ #
    # Signed order placement
    # ------------------------------------------------------------------ #

    @property
    def public_key(self) -> str | None:
        """The public key of your trading key, ``0x`` + 80 lowercase hex (what ``GET /l2/keys``
        names ``journaledKey``), or ``None`` when no ``LOAF_SECRET_KEY`` is set."""
        return self._l2_key.public_key if self._l2_key is not None else None

    def _send_signed(self, path: str, body: dict, *, ambiguous: bool = False) -> Any:
        """POST a signed placement, re-sending the identical bytes on transient failures.

        No ``Authorization`` header: the signature authenticates. Network errors
        and 5xx are re-sent up to ``max_retries`` times, after a short jittered
        pause; the exchange admits one order per account and order number, so a
        repeat is answered as a duplicate, never a second order. 4xx (429
        included) is final. If any attempt may have landed and no 200 settled
        it, raises :class:`~loaf.exceptions.OrderOutcomeUnknownError`.

        ``ambiguous=True`` (resubmit): an earlier attempt may already have
        landed, so anything but a 200 raises ``OrderOutcomeUnknownError``.
        """
        content = _json.dumps(body, separators=(",", ":"), allow_nan=False).encode()
        headers = {"Content-Type": "application/json"}
        timeout = self._placement_timeout
        attempt = 0
        while True:
            retryable = True
            try:
                response = self._http.post(path, content=content, headers=headers, timeout=timeout)
            except httpx.RequestError as exc:
                if not isinstance(exc, _NOT_SENT_ERRORS):  # may have been received
                    ambiguous = True
                error: Exception = LoafConnectionError(f"POST {path} failed: {exc}")
                error.__cause__ = exc
            except Exception as exc:
                # E.g. another thread closed the client, or a response hook of your http_client
                # raised on a 5xx answer: a body that may have been placed is never lost.
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code >= 500:
                    ambiguous = True
                error, retryable = exc, False
            else:
                self._record_rate_limit(response)
                if response.is_success:
                    try:
                        result = self._parse_body(response)
                    except ValueError:  # labelled JSON but not JSON
                        result = response.text
                    if isinstance(result, dict) and "orderId" in result:
                        return result
                    error = LoafServerError(
                        "Unexpected response to an order placement",
                        status_code=response.status_code,
                        body=result,
                    )
                    ambiguous, retryable = True, False
                else:
                    error = self._build_error(response)
                    retryable = response.status_code >= 500
                    if retryable and not _definitely_not_placed(error):
                        ambiguous = True
            if not retryable or attempt >= self.max_retries:
                break
            # Jitter only: every reply carries RateLimit-Reset, and a signed price goes stale.
            self._sleep_backoff(attempt)
            attempt += 1
        if ambiguous:
            raise OrderOutcomeUnknownError(
                signed_body=copy.deepcopy(body), attempts=attempt + 1, last_error=error
            ) from error
        raise error

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _parse_body(response: httpx.Response) -> Any:
        if response.status_code == 204 or not response.content:
            return None
        ctype = response.headers.get("content-type", "")
        if "application/json" in ctype:
            return parse(response.json())
        return response.text

    def _build_error(self, response: httpx.Response) -> LoafAPIError:
        try:
            body: Any = response.json()
        except ValueError:
            body = response.text
        return error_from_response(
            response.status_code,
            body,
            request_id=response.headers.get("x-request-id"),
            retry_after=self._retry_after_seconds(response),
        )

    def _record_rate_limit(self, response: httpx.Response) -> None:
        snapshot: dict[str, float] = {}
        for header, key in (
            ("ratelimit-limit", "limit"),
            ("ratelimit-remaining", "remaining"),
            ("ratelimit-reset", "reset"),
        ):
            value = response.headers.get(header)
            if value is not None:
                try:
                    snapshot[key] = float(value)
                except ValueError:
                    pass
        if snapshot:
            self.last_rate_limit = snapshot

    @staticmethod
    def _retry_after_seconds(response: httpx.Response) -> float | None:
        for header in ("retry-after", "ratelimit-reset"):
            value = response.headers.get(header)
            if value is not None:
                try:
                    return max(0.0, float(value))
                except ValueError:
                    pass
        return None

    def _sleep_backoff(self, attempt: int, response: httpx.Response | None = None) -> None:
        if response is not None:
            hinted = self._retry_after_seconds(response)
            if hinted is not None and hinted > 0:  # a hint of 0 or less would retry at once
                time.sleep(min(hinted, 60.0))
                return
        # Exponential backoff with full jitter, capped at 30s; a capped exponent never overflows.
        delay = min(30.0, 0.5 * 2 ** min(attempt, 6))
        time.sleep(random.uniform(0, delay))

    # ------------------------------------------------------------------ #
    # WebSocket
    # ------------------------------------------------------------------ #

    def websocket(self, **kwargs: Any) -> LoafWebSocketClient:
        """Create a :class:`~loaf.ws.client.LoafWebSocketClient` bound to this
        client's credentials and host. See that class for usage."""
        from .ws.client import LoafWebSocketClient

        try:
            return LoafWebSocketClient(self, **kwargs)
        finally:
            kwargs.clear()  # crash reporters record frame locals: no refused api_key stays here

    @property
    def ws_url(self) -> str:
        from .ws.client import derive_ws_url

        return self._ws_url_override or derive_ws_url(self.base_url)

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> LoafClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __repr__(self) -> str:
        # getattr: crash reporters repr a client whose __init__ raised part-way.
        authed = "authenticated" if getattr(self, "api_key", None) else "anonymous"
        key = getattr(self, "_l2_key", None)
        public_key = f" public_key={key.public_key}" if key is not None else ""
        base_url = getattr(self, "base_url", None)
        return f"<LoafClient base_url={base_url!r} {authed}{public_key} v{__version__}>"

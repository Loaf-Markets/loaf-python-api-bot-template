"""The synchronous Loaf API client."""

from __future__ import annotations

import os
import random
import time
from typing import TYPE_CHECKING, Any

import httpx

from ._object import parse
from ._version import __version__
from .constants import (
    DEFAULT_BASE_URL,
    DEFAULT_MAX_RETRIES,
    DEFAULT_TIMEOUT,
    ENV_API_KEY,
    ENV_BASE_URL,
    ENV_WS_URL,
    USER_AGENT,
)
from .exceptions import (
    LoafAPIError,
    LoafConfigError,
    LoafConnectionError,
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


class LoafClient:
    """Entry point for talking to the Loaf API.

    Example::

        from loaf import LoafClient

        with LoafClient(api_key="...") as loaf:
            print(loaf.portfolio.component().cash)
            print(loaf.market.properties())

    Args:
        api_key: A Loaf **user API key** (used as the ``Authorization: Bearer``
            token). Mint one from the Loaf web app (Settings -> API keys). If
            omitted, read from ``$LOAF_API_KEY``. Public endpoints work without
            one; authenticated endpoints raise :class:`LoafConfigError`.
        base_url: REST base URL including the ``/api`` suffix. If omitted, read
            from ``$LOAF_API_BASE_URL``, else :data:`~loaf.constants.DEFAULT_BASE_URL`.
        ws_url: Override the WebSocket URL. If omitted it is derived from
            ``base_url`` (``https://host/api`` -> ``wss://host/ws``) or read from
            ``$LOAF_WS_URL``.
        timeout: Per-request timeout in seconds.
        max_retries: Automatic retries for transient failures (429 and, for
            idempotent requests, 503 / network errors).
        verify: TLS verification, forwarded to httpx. Set ``False`` ONLY for a
            trusted local dev server using a self-signed certificate — it disables
            MITM protection, so never use it against a remote host.
        http_client: Provide your own ``httpx.Client`` (advanced).
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        *,
        ws_url: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        verify: bool | str = True,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.api_key = api_key or os.environ.get(ENV_API_KEY) or None
        self.base_url = (base_url or os.environ.get(ENV_BASE_URL) or DEFAULT_BASE_URL).rstrip("/")
        self._ws_url_override = ws_url or os.environ.get(ENV_WS_URL) or None
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

        #: Public market data: properties, candles, info pages.
        self.market: MarketResource = MarketResource(self)
        #: Primary market (IPO offerings): list, detail, subscribe, approve.
        self.offerings: OfferingsResource = OfferingsResource(self)
        #: Trading: place/cancel orders, pre-approve.
        self.orders: OrdersResource = OrdersResource(self)
        #: Balances, positions, and PnL.
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

    def delete(self, path: str, *, params: dict | None = None, auth: bool = True) -> Any:
        return self.request("DELETE", path, params=params, auth=auth)

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
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                # On a non-idempotent request the server MAY have processed the write
                # before the response was lost, so we deliberately do not retry (that
                # could place the order twice). Surface an error that says so, so the
                # caller reconciles against GET /orders or openOrders instead of blindly
                # re-sending.
                if idempotent and attempt < self.max_retries:
                    self._sleep_backoff(attempt)
                    attempt += 1
                    continue
                if idempotent:
                    raise LoafConnectionError(f"{method} {path} failed: {exc}") from exc
                raise LoafConnectionError(
                    f"{method} {path} failed: {exc}. The request may still have been "
                    f"processed server-side - reconcile against the order/history "
                    f"endpoints before retrying, or you may act on it twice."
                ) from exc

            self._record_rate_limit(response)

            if response.is_success:
                return self._parse_body(response)

            # Error path — decide whether to retry. Only idempotent (read) requests
            # are retried: re-sending a non-idempotent POST (e.g. /orders) could place
            # the order twice, so we surface the error and let the caller decide.
            should_retry = (
                attempt < self.max_retries
                and idempotent
                and response.status_code in (429, 503)
            )
            if should_retry:
                self._sleep_backoff(attempt, response)
                attempt += 1
                continue

            raise self._build_error(response)

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _parse_body(response: httpx.Response) -> Any:
        if response.status_code == 204 or not response.content:
            return None
        ctype = response.headers.get("content-type", "")
        if "application/json" in ctype:
            # A server (or proxy) that promises JSON but sends a malformed body used to
            # raise a raw httpx/json.JSONDecodeError out of the SDK, breaking the promise
            # that every failure is a LoafError. Wrap it.
            try:
                return parse(response.json())
            except ValueError as exc:
                raise LoafAPIError(
                    f"HTTP {response.status_code}: response claimed JSON but did not parse "
                    f"({exc})",
                    status_code=response.status_code,
                    body=response.text[:2048],
                ) from exc
        return response.text

    def _build_error(self, response: httpx.Response) -> Exception:
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
        # Honour the server's hint first, but only when it is actually positive: a
        # `RateLimit-Reset: 0` (already-reset window) otherwise collapsed the whole backoff
        # to zero and the client hammered a rate limiter in a tight loop.
        if response is not None:
            hinted = self._retry_after_seconds(response)
            if hinted is not None and hinted > 0:
                time.sleep(min(hinted, 60.0))
                return
        # Exponential backoff with full jitter, capped at 30s. Full jitter (a uniform
        # draw from [0, delay]) is what stops a fleet of clients that were throttled
        # together from retrying in lockstep and re-triggering the limit.
        delay = min(30.0, (2**attempt) * 0.5)
        time.sleep(random.uniform(delay / 2, delay))

    # ------------------------------------------------------------------ #
    # WebSocket
    # ------------------------------------------------------------------ #

    def websocket(self, **kwargs: Any) -> LoafWebSocketClient:
        """Create a :class:`~loaf.ws.client.LoafWebSocketClient` bound to this
        client's credentials and host. See that class for usage."""
        from .ws.client import LoafWebSocketClient

        return LoafWebSocketClient(self, **kwargs)

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
        authed = "authenticated" if self.api_key else "anonymous"
        return f"<LoafClient base_url={self.base_url!r} {authed} v{__version__}>"

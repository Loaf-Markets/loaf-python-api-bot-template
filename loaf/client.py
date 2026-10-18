"""The synchronous Loaf API client."""

from __future__ import annotations

import copy
import json as _json
import numbers
import os
import random
import re
import time
from typing import TYPE_CHECKING, Any

import httpx

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
    ENV_L2_SECRET_KEY,
    ENV_MAX_SLIPPAGE_BPS,
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
    from .l2.signer import L2Signer
    from .l2.trading import L2Config, L2Trader
    from .resources.competition import CompetitionResource
    from .resources.history import HistoryResource
    from .resources.leaderboard import LeaderboardResource
    from .resources.market import MarketResource
    from .resources.offerings import OfferingsResource
    from .resources.orders import OrdersResource
    from .resources.portfolio import PortfolioResource
    from .ws.client import LoafWebSocketClient

_IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "DELETE"})

# The account's L2 trading key: 80 lowercase hex (an API token is 64).
_L2_KEY_SHAPE = re.compile(r"[0-9a-f]{80}")

# Failures before the request left this machine: re-sending them is always safe.
_NOT_SENT_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)

# 503s the exchange sends only when nothing was placed (safe to treat as a definite answer),
# matched in any case (Loaf-Backend at the K1 head): "Your order was not placed" (ordersService.ts,
# l2OrderFields.ts, the venue read), "nothing was done" (the account read, l2OrderFields.ts), the
# eligibility read (auth.ts), and the code of a market-mode switch (competitionVenue.ts).
_NOT_PLACED_503_TEXTS = (
    "your order was not placed",
    "nothing was done",
    "trading temporarily unavailable. please try again shortly.",
)
_NOT_PLACED_503_CODES = frozenset({"VENUE_SWITCHING"})


def _definitely_not_placed(error: LoafAPIError) -> bool:
    if error.status_code != 503:
        return False
    if error.code in _NOT_PLACED_503_CODES:
        return True
    message = error.message.lower()
    return any(t in message for t in _NOT_PLACED_503_TEXTS)


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

        with LoafClient(api_key="...", l2_secret_key="...") as loaf:
            print(loaf.portfolio.component().balances)
            loaf.orders.limit_buy("OPRA", quantity=10, price=167.49)

    Args:
        api_key: The API token from the Loaf web app (Settings -> API keys),
            sent as ``Authorization: Bearer`` on reads, cancels, the WebSocket
            and the account reads an order needs (its key state, its order
            indices). It is never sent with the order itself. If omitted, read
            from ``$LOAF_API_KEY``. Public endpoints work without one; other
            authenticated calls raise :class:`LoafConfigError`.
        base_url: REST base URL including the ``/api`` suffix. If omitted, read
            from ``$LOAF_API_BASE_URL``, else :data:`~loaf.constants.DEFAULT_BASE_URL`.
        l2_secret_key: The account's L2 trading key, 80 lowercase hex. It signs
            every order locally and is never sent. If omitted, read from
            ``$LOAF_L2_SECRET_KEY`` (prefer the environment variable: an
            explicit ``l2_secret_key=`` argument can show up in tools that
            capture call frames). ``python -m loaf.l2.keys enable`` derives it
            from the wallet and registers it. Without it everything works
            except placing orders.
        l2_config: L2 signing settings (:class:`~loaf.l2.trading.L2Config`); if
            omitted, read from the ``LOAF_L2_*`` variables, else the defaults.
        l2_signer: The native L2 signer (:class:`~loaf.l2.signer.L2Signer`);
            if omitted, the vendored library for this platform.
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
        l2_secret_key: str | None = None,
        l2_config: L2Config | None = None,
        l2_signer: L2Signer | None = None,
        ws_url: str | None = None,
        max_slippage_bps: int | None = None,
        timeout: float | tuple | httpx.Timeout | None = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        verify: bool | str = True,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.api_key = (api_key or os.environ.get(ENV_API_KEY) or "").strip() or None
        if self.api_key and _L2_KEY_SHAPE.fullmatch(self.api_key.lower()):  # in any letter case
            raise LoafConfigError(
                f"{ENV_API_KEY} looks like an L2 trading key (80 hex). Put the API token in "
                f"{ENV_API_KEY} and the trading key in {ENV_L2_SECRET_KEY}."
            )
        #: Default max slippage (basis points) for the prices the SDK derives for your orders.
        self.max_slippage_bps: int = _max_slippage_bps(max_slippage_bps)
        #: The trading key the L2 signer gets on first use (:attr:`l2`); None without one.
        self._l2_secret_key: str | None = None
        raw_l2 = None
        try:
            raw_l2 = (l2_secret_key or os.environ.get(ENV_L2_SECRET_KEY) or "").strip()
            if raw_l2:
                if not _L2_KEY_SHAPE.fullmatch(raw_l2):
                    raise LoafConfigError(
                        f"{ENV_L2_SECRET_KEY} must be the account's L2 trading key, 80 lowercase "
                        "hex (python -m loaf.l2.keys enable derives and registers it)."
                    )
                self._l2_secret_key = raw_l2
        finally:
            # Crash reporters and debuggers record frame locals: leave no key in them.
            raw_l2 = l2_secret_key = None
        self._l2_config = l2_config
        self._l2_signer = l2_signer
        self._l2: L2Trader | None = None

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

        #: Public market data: properties, candles, info pages.
        self.market: MarketResource = MarketResource(self)
        #: Primary market (IPO offerings): list, detail, subscribe.
        self.offerings: OfferingsResource = OfferingsResource(self)
        #: Trading: place (signed with your L2 trading key) and cancel orders, stop & take orders.
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
        """:meth:`request`, plus ``before_order``: set for the read an order
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
            except (httpx.TimeoutException, httpx.TransportError) as exc:
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
                    raise LoafAPIError(
                        f"The {method} {path} response did not parse as JSON",
                        status_code=response.status_code,
                        request_id=response.headers.get("x-request-id"),
                        body=response.text[:1000],
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
    def l2_public_key(self) -> str | None:
        """The public key of the trading key that signs your orders (``0x`` and 80 hex, as
        ``GET /l2/keys`` names it), or ``None`` without a trading key."""
        if self._l2 is None and not self._l2_secret_key:
            return None
        return self.l2.public_key

    @property
    def l2(self) -> L2Trader:
        """The L2 signer of this client's orders (:class:`~loaf.l2.trading.L2Trader`), made on
        first use from the trading key; :class:`LoafConfigError` without one."""
        if self._l2 is None:
            if not self._l2_secret_key:
                raise LoafConfigError(
                    "Placing an order needs the account's L2 trading key: pass l2_secret_key=... "
                    f"or set ${ENV_L2_SECRET_KEY} (python -m loaf.l2.keys enable derives and "
                    "registers it)."
                )
            from .l2.trading import L2Trader

            self._l2 = L2Trader(
                self, self._l2_secret_key, config=self._l2_config, signer=self._l2_signer
            )
            self._l2_secret_key = None  # the trader holds the one copy now
        return self._l2

    def _send_signed(self, path: str, body: dict, *, ambiguous: bool = False) -> Any:
        """POST a signed placement, re-sending the identical bytes on transient failures.

        No ``Authorization`` header: the signature authenticates. Network errors
        and 5xx are re-sent up to ``max_retries`` times, after a short jittered
        pause; the exchange keys an order by its account and order index, so a
        repeat is a duplicate, never a second order. 4xx (429 included) is
        final. If any attempt may have landed and no 200 settled it, raises
        :class:`~loaf.exceptions.OrderOutcomeUnknownError`.

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
            if hinted is not None and hinted > 0:
                time.sleep(min(hinted, 60.0))
                return
        # Exponential backoff with full jitter, capped at 30s.
        delay = min(30.0, (2**attempt) * 0.5)
        time.sleep(random.uniform(0, delay))

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
        # getattr: crash reporters repr a client whose __init__ raised part-way.
        authed = "authenticated" if getattr(self, "api_key", None) else "anonymous"
        trader = getattr(self, "_l2", None)
        if trader is not None:
            l2 = f" l2_key={trader.public_key}"
        else:
            l2 = " l2_key=set" if getattr(self, "_l2_secret_key", None) else ""
        base_url = getattr(self, "base_url", None)
        return f"<LoafClient base_url={base_url!r} {authed}{l2} v{__version__}>"

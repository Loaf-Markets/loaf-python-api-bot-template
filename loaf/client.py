"""The synchronous Loaf API client."""

from __future__ import annotations

import copy
import json as _json
import math
import numbers
import os
import random
import re
import ssl
import sys
import threading
import time
import traceback
from time import monotonic as _monotonic
from typing import TYPE_CHECKING, Any

import httpx

from ._l2 import LoginTokens, TradingKey
from ._object import parse
from ._version import __version__
from .constants import (
    DEFAULT_BASE_URL,
    DEFAULT_MAX_RETRIES,
    DEFAULT_MAX_SLIPPAGE_BPS,
    DEFAULT_PLACEMENT_TIMEOUT,
    DEFAULT_TIMEOUT,
    ENV_BASE_URL,
    ENV_MAX_SLIPPAGE_BPS,
    ENV_TRADING_KEY,
    ENV_WS_URL,
    L2_ORDER_INDEX_BLOCK,
    L2_ORDER_INDEX_BLOCK_MAX,
    MAX_HINTED_WAIT,
    MAX_RESPONSE_BYTES,
    MAX_RETRY_AFTER,
    USER_AGENT,
)
from .exceptions import (
    LoafAPIError,
    LoafAuthError,
    LoafConfigError,
    LoafConnectionError,
    LoafServerError,
    OrderOutcomeUnknownError,
    PerpsOutcomeUnknownError,
    WithdrawalOutcomeUnknownError,
    error_from_response,
)

if TYPE_CHECKING:
    from .resources.funds import FundsResource
    from .resources.history import HistoryResource
    from .resources.market import MarketResource
    from .resources.orders import OrdersResource
    from .resources.perps import PerpsResource
    from .resources.portfolio import PortfolioResource
    from .ws.client import LoafWebSocketClient

_IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "DELETE"})

# What a 401 refusing a login token means, by its text (the backend's): the trading key is not the
# one in force on its account here, or this machine's clock is off. A 401 refusing a signed op's
# signature or key carries code INVALID_SIGNATURE, which means the key whatever its text (a
# withdrawal's "l2 money: the signing key is not the key the journal set for the api key", say).
# Any other 401 keeps its text.
_KEY_REFUSALS = (
    "l2 signature:",
    "l2 key:",
    "l2 account:",
    "l2 login: the token is for another exchange's L2 chain",
)
_CLOCK_REFUSALS = ("l2 login: the token has expired", "l2 login: the token expires more than")
_KEY_NOT_IN_FORCE = (
    f"{ENV_TRADING_KEY} is not the trading key in force on its account here: it was reset in the "
    "web app, the account's key is not active yet (enable trading on a market's Trade page and "
    f"wait for it to take effect), or it is another Loaf deployment's key (check {ENV_BASE_URL}). "
    f"Set {ENV_TRADING_KEY} to the web app's Show trading key (README §2) and restart; after a "
    "reset, also place your stops and takes again: the exchange FAILS the ones signed before it."
)
_CLOCK_OFF = (
    "this machine's clock is off: the exchange takes a login token only before its expiry and at "
    "most 15 minutes ahead of its own clock, and the SDK signs ones that live 10 minutes from this "
    "machine's, so the two clocks must agree within about 5 minutes. Synchronize the clock (NTP)."
)


def _login_refusal(error: LoafAPIError) -> LoafAPIError:
    """A 401 refusing a login token, or a signed op's signature or key, with what it
    means appended to its message (the server's own text stays in ``body``)."""
    if error.code == "INVALID_SIGNATURE" or error.message.startswith(_KEY_REFUSALS):
        meaning = _KEY_NOT_IN_FORCE
    elif error.message.startswith(_CLOCK_REFUSALS):
        meaning = _CLOCK_OFF
    else:
        return error
    return LoafAuthError(
        f"{error.message} — {meaning}",
        status_code=error.status_code,
        code=error.code,
        details=error.details,
        request_id=error.request_id,
        body=error.body,
    )


# Failures before the request left this machine (a proxy that refused the tunnel included):
# re-sending them is always safe.
_NOT_SENT_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError)

# 503s the exchange sends only when nothing was done, so as definite an answer as a 4xx: any with
# one of these codes (the engine paused, or it never took the order), and, of the ones it sends with
# no code, those whose JSON error (its own envelope, never a page that is not JSON) carries one of
# their op's texts, lowercased (the exchange writes them in both cases): the engine without the
# order's L2 fields, its answer that it journaled no perps op, or the account (an op's, or a login
# token's) unread before anything reached the engine. Any other 503 may follow a journaled op.
_NOT_DONE_503_CODES = frozenset({"TRADING_PAUSED", "NOT_PROCESSED", "MARKET_UNAVAILABLE"})
_NOT_DONE_503_TEXTS = {
    "order": ("your order was not placed", "nothing was done"),
    "perps": ("nothing was journaled", "nothing was done"),
    "withdrawal": ("nothing was done",),
}

# Each signed op: what an unexpected answer to it names, and the error an unknown outcome raises
# (each takes the body sent, the attempts and the last error).
_SIGNED_OPS: dict[
    str,
    tuple[
        str,
        type[OrderOutcomeUnknownError | PerpsOutcomeUnknownError | WithdrawalOutcomeUnknownError],
    ],
] = {
    "order": ("an order placement", OrderOutcomeUnknownError),
    "perps": ("a perps op", PerpsOutcomeUnknownError),
    "withdrawal": ("a withdrawal", WithdrawalOutcomeUnknownError),
}


def _definitely_not_done(error: LoafAPIError, op: str) -> bool:
    """Whether a 503 answering signed ``op`` says nothing was done (a code counts only as a
    string, a text only as the error of a JSON object)."""
    if error.status_code != 503:
        return False
    if isinstance(error.code, str) and error.code in _NOT_DONE_503_CODES:
        return True
    text = error.body.get("error") if isinstance(error.body, dict) else None
    return isinstance(text, str) and any(
        done in text.lower() for done in _NOT_DONE_503_TEXTS[op]
    )


# Refusals of a re-send that speak for every attempt: the exchange answers a re-send of a journaled
# withdrawal as its duplicate before any other check, so a 422 ORDER_INDEX_USED to one (its nonce
# spent by another op, or its idempotency key held by another withdrawal) says no attempt was
# journaled, and none will be: a nonce only moves up. A perps op's spent nonce carries no code.
_NEVER_JOURNALED_CODES = {"withdrawal": frozenset({"ORDER_INDEX_USED"})}


def _never_journaled(error: LoafAPIError, op: str) -> bool:
    """Whether a refusal of signed ``op`` says no attempt of it was journaled, nor ever will be."""
    return (
        error.status_code == 422
        and isinstance(error.code, str)
        and error.code in _NEVER_JOURNALED_CODES.get(op, frozenset())
    )


def _settles(answer: Any, settled_by: str) -> bool:
    """Whether a 2xx ``answer`` settles a signed op: a JSON object whose ``success`` is not false
    and whose ``settled_by`` (an order's ``orderId``, a perps op's or a withdrawal's
    ``journalSeq``) is a whole number from 0 up."""
    if not isinstance(answer, dict) or answer.get("success") is False:
        return False
    settled = answer.get(settled_by)
    return isinstance(settled, int) and not isinstance(settled, bool) and settled >= 0


class _Unread(Exception):
    """An answer whose body the client stopped reading, or never began to: past
    MAX_RESPONSE_BYTES, still arriving past the request's read timeout, or in an encoding it
    does not read."""


# The Content-Encoding an answer may name (or none): one encoding whose expansion a network read
# bounds, about a thousandfold. httpx decodes whatever an answer names, a list of encodings in
# turn (each multiplying the last) and brotli or zstd when their packages are installed: one read
# of those decodes to any size.
_READ_ENCODINGS = frozenset({"identity", "gzip", "x-gzip", "deflate"})

# What every request the client sends asks for, through a caller's own http_client too (httpx
# also asks for brotli and zstd when their packages are installed).
_ACCEPT_ENCODING = "gzip, deflate"

# The most characters of a refused Content-Encoding that go into the error.
_ENCODING_TEXT_LIMIT = 100


def _finite(value: str | None) -> float | None:
    """A header's number, when it is a finite one."""
    if value is None:
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _without_credentials(request: httpx.Request) -> httpx.Request:
    """A copy of ``request`` without its ``Authorization`` header."""
    headers = [(k, v) for k, v in request.headers.raw if k.lower() != b"authorization"]
    try:
        content: bytes | None = request.content
    except httpx.RequestNotRead:
        content = None
    return httpx.Request(
        request.method, request.url, headers=headers, content=content,
        extensions=request.extensions,
    )


def _scrub(error: BaseException, handled: BaseException | None) -> None:
    """Take the login token out of ``error`` and every error it chains (``__cause__``,
    ``__context__``): the locals of each finished frame of their tracebacks (httpx's and
    httpcore's hold the request's headers), and the ``Authorization`` header of the request an
    httpx error carries, which is replaced by a copy without it. Crash reporters and debuggers
    record both.

    ``handled`` is the error the caller was handling when it called into the SDK (Python chains
    it as the ``__context__`` of what the SDK raises): it, and what it chains, are the caller's
    own, and left as they are."""
    seen: set[int] = set() if handled is None else {id(handled)}
    chained: list[BaseException | None] = [error]
    while chained:
        exc = chained.pop()
        if exc is None or id(exc) in seen:
            continue
        seen.add(id(exc))
        traceback.clear_frames(exc.__traceback__)  # a frame still running keeps its locals
        if isinstance(exc, httpx.HTTPError):
            try:
                request: httpx.Request | None = exc.request
            except RuntimeError:  # raised with no request
                request = None
            if request is not None:
                exc.request = _without_credentials(request)
                if isinstance(exc, httpx.HTTPStatusError):
                    exc.response.request = exc.request
        chained += (exc.__cause__, exc.__context__)


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


def _placement_timeout(timeout: Any, placement_timeout: float | None) -> httpx.Timeout:
    """The client's timeout, with its read wait set to ``placement_timeout``, else raised to at
    least DEFAULT_PLACEMENT_TIMEOUT."""
    t = httpx.Timeout(timeout)
    if placement_timeout is None:
        read = None if t.read is None else max(t.read, DEFAULT_PLACEMENT_TIMEOUT)
    elif (
        isinstance(placement_timeout, (int, float))
        and not isinstance(placement_timeout, bool)
        and 0 < placement_timeout < float("inf")
    ):
        read = placement_timeout
    else:
        raise LoafConfigError(
            f"placement_timeout must be a number of seconds above 0, got {placement_timeout!r}"
        )
    return httpx.Timeout(connect=t.connect, read=read, write=t.write, pool=t.pool)


def _order_numbers(block: int, start: int) -> tuple[int, int]:
    """``(order_number_block, order_number_start)`` once each is usable."""
    for name, value in (("order_number_block", block), ("order_number_start", start)):
        if not isinstance(value, int) or isinstance(value, bool):
            raise LoafConfigError(f"{name} must be a whole number, got {value!r}")
        if not 1 <= value <= L2_ORDER_INDEX_BLOCK_MAX:
            raise LoafConfigError(
                f"{name} must be from 1 to {L2_ORDER_INDEX_BLOCK_MAX}, got {value!r}"
            )
    if start > block:
        raise LoafConfigError(
            f"order_number_start ({start}) cannot exceed order_number_block ({block})"
        )
    return block, start


class LoafClient:
    """Entry point for talking to the Loaf API.

    Example::

        from loaf import LoafClient

        with LoafClient() as loaf:   # reads $LOAF_TRADING_KEY
            print(loaf.portfolio.summary().balances)
            print(loaf.market.list())

    Args:
        trading_key: Your trading key, your one credential: the one string the
            web app's API page gives under Show trading key,
            ``loaf_tk_<chain>_<account>_<80 hex>`` (README §2). It signs every
            order and perps allocate and exit locally, and the short-lived
            login tokens sent as ``Authorization: Bearer`` on reads, cancels,
            the two account requests an order needs (``GET /l2/keys``,
            ``POST /l2/order-indices``), perps allocates and exits, and the
            WebSocket; the key itself is never sent. If omitted, read from
            ``$LOAF_TRADING_KEY`` (prefer the environment variable: an explicit
            ``trading_key=`` argument can show up in tools that capture call
            frames). Without it only the public endpoints work; other calls
            raise :class:`LoafConfigError`.
        base_url: REST base URL including the ``/api`` suffix. If omitted, read
            from ``$LOAF_API_BASE_URL``, else :data:`~loaf.constants.DEFAULT_BASE_URL`.
            An ``https://`` URL, or ``http://`` to this machine (``localhost``,
            127.0.0.0/8 or ``[::1]``), with no user name or password in it;
            anything else raises :class:`LoafConfigError`. Its scheme and host
            are lowercased.
        ws_url: Override the WebSocket URL. If omitted, read from
            ``$LOAF_WS_URL``, else derived from ``base_url`` (``https://host/api``
            -> ``wss://host/ws``). A ``wss://`` URL, or ``ws://`` to this machine.
            One on another host than ``base_url``'s warns (``UserWarning``): the
            login token is sent to that host as well.
        allow_http: Also take ``http://`` and ``ws://`` URLs to other hosts, for a
            dev server on your own network, in a container network or on a
            ``*.localhost`` name; never for
            a remote host: login tokens and signed orders then travel
            unencrypted. Default ``False``. No environment variable sets it.
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
            An answer's whole body must also arrive within the read timeout of
            its headers, and be at most
            :data:`~loaf.constants.MAX_RESPONSE_BYTES` (32 MiB) decoded: past
            either, a read raises :class:`~loaf.exceptions.LoafConnectionError`
            (not retried), and a signed op's outcome is unknown (not re-sent).
            Reading stops once what has been decoded passes the size, so the
            most held is that plus one decoded network read: the client the SDK
            builds asks only for gzip and deflate, and an answer in any other
            ``Content-Encoding`` (two stacked among them, or brotli) is refused
            the same way before any of it is read, with your own
            ``http_client`` too.
        placement_timeout: Seconds each attempt of a signed placement (and a
            perps allocate or exit) waits for its answer, in place of that 45 s
            floor. The exchange answers only once the order is committed, so a
            shorter wait can give up on an order that then lands: past it the SDK
            re-sends (``max_retries``), then raises
            :class:`~loaf.exceptions.OrderOutcomeUnknownError`.
        max_retries: Automatic retries for transient failures. Reads retry 429,
            503 and network errors: after the answer's ``Retry-After`` (a 429's
            ``RateLimit-Reset`` without one) when that is at most
            :data:`~loaf.constants.MAX_HINTED_WAIT` (10 s), else after a short
            jittered backoff; an answer that asks for a longer wait is raised at
            once, its ``retry_after`` the seconds it asked for (a 429's
            :class:`~loaf.exceptions.LoafRateLimitError`, a 503's
            :class:`~loaf.exceptions.LoafServiceUnavailableError`). The reads a placement
            makes before it signs never wait out a hint, and raise a 429 at
            once. Signed placements (and perps allocates and exits, and
            withdrawals) retry network errors and 5xx by re-sending the
            identical signed body after a short pause, which cannot place twice;
            each attempt can wait out the placement timeout, so lower this (e.g.
            ``1``) for a latency-sensitive loop.
            Cancels and other POSTs are never retried.
        order_number_block: The most order numbers one reservation asks for, 1
            to :data:`~loaf.constants.L2_ORDER_INDEX_BLOCK_MAX`. The exchange takes
            one reservation a second per account, so this caps your orders a
            second. Default :data:`~loaf.constants.L2_ORDER_INDEX_BLOCK` (64). The
            numbers a process has reserved when it stops stay unused against your
            allowance (README, "The order-number allowance").
        order_number_start: The size of the first reservation, from which they
            double up to ``order_number_block``. Default 1, so a process that
            stops after a few orders leaves few numbers unused.
        reserve_ahead: Reserve the next block on a background thread once the
            current one is half used, so a placement waits on neither that
            request nor its one-a-second limit. One that outruns the thread still
            reserves for itself. Default ``False``: no thread.
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
            and what the SDK cached is not refreshed. Its answers are read as
            the SDK's own client's are (see ``timeout``): every request sent
            through it asks only for gzip and deflate, whatever its own
            ``Accept-Encoding``, and an answer in any other
            ``Content-Encoding`` is refused.
    """

    def __init__(
        self,
        trading_key: str | None = None,
        base_url: str | None = None,
        *,
        ws_url: str | None = None,
        allow_http: bool = False,
        max_slippage_bps: int | None = None,
        timeout: float | tuple | httpx.Timeout | None = DEFAULT_TIMEOUT,
        placement_timeout: float | None = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        order_number_block: int = L2_ORDER_INDEX_BLOCK,
        order_number_start: int = 1,
        reserve_ahead: bool = False,
        verify: bool | ssl.SSLContext = True,
        http_client: httpx.Client | None = None,
    ) -> None:
        try:
            #: Default max slippage (basis points) for the prices the SDK derives for your orders.
            self.max_slippage_bps: int = _max_slippage_bps(max_slippage_bps)
            raw = (trading_key or os.environ.get(ENV_TRADING_KEY) or "").strip()
            #: Signs placements and login tokens, and keeps the secret to itself; None without a
            #: trading key.
            self._l2_key: TradingKey | None = TradingKey(raw) if raw else None
        finally:
            # Crash reporters and debuggers record frame locals: whatever fails in __init__, no
            # trading key is left in them.
            raw = trading_key = None
        #: The login tokens the trading key signs for every authenticated request and the
        #: WebSocket; None without a trading key.
        self._login_tokens = LoginTokens(self._l2_key) if self._l2_key is not None else None
        #: Held from the nonce read to the answer of each perps op and withdrawal: they spend one
        #: nonce counter, so this client sends them one at a time.
        self._op_lock = threading.Lock()

        from . import _urls

        #: Whether http:// and ws:// URLs to other hosts than this machine are taken.
        self._allow_http = bool(allow_http)
        base_source = "base_url" if base_url else f"${ENV_BASE_URL}"
        self.base_url = _urls.rest_url(
            base_url or os.environ.get(ENV_BASE_URL) or DEFAULT_BASE_URL,
            source=base_source,
            allow_http=self._allow_http,
        )
        ws_source = "ws_url" if ws_url else f"${ENV_WS_URL}"
        ws_override = ws_url or os.environ.get(ENV_WS_URL)
        if ws_override:
            self._ws_url = _urls.ws_url(ws_override, source=ws_source, allow_http=self._allow_http)
            _urls.warn_other_host(self._ws_url, self.base_url, source=ws_source)
        else:
            self._ws_url = _urls.derive_ws_url(self.base_url)
        self._placement_timeout = _placement_timeout(timeout, placement_timeout)
        self._order_numbers = _order_numbers(order_number_block, order_number_start)
        self._reserve_ahead = bool(reserve_ahead)
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
        from .resources.funds import FundsResource
        from .resources.history import HistoryResource
        from .resources.market import MarketResource
        from .resources.orders import OrdersResource
        from .resources.perps import PerpsResource
        from .resources.portfolio import PortfolioResource

        #: Public market data: markets, candles, trades.
        self.market: MarketResource = MarketResource(self)
        #: Trading: place (signed with your trading key) and cancel orders, stop & take orders.
        self.orders: OrdersResource = OrdersResource(self)
        #: Perps: your pools and positions, margin moves and exits (signed like orders).
        self.perps: PerpsResource = PerpsResource(self)
        #: Balances, holdings, and recent activity.
        self.portfolio: PortfolioResource = PortfolioResource(self)
        #: Withdrawals of USDC to your own wallet (signed like orders).
        self.funds: FundsResource = FundsResource(self)
        #: Paginated order, trade & funding history.
        self.history: HistoryResource = HistoryResource(self)

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
        """Send a request and return its parsed JSON body (a :class:`LoafObject`,
        a list or another JSON value), or ``None`` for a 204 or an empty body.

        Handles authentication (``auth=True``: a login token your trading key
        signs), JSON (de)serialisation, error mapping, and automatic retries for
        transient failures. You normally call the typed resource methods rather
        than this directly.

        An idempotent request (``GET``, ``DELETE``) is retried up to
        ``max_retries`` times on a network error, a 429 and a 503: after the
        answer's ``Retry-After`` (a 429's ``RateLimit-Reset`` without one) when
        that is at most :data:`~loaf.constants.MAX_HINTED_WAIT` (10 s), else
        after a short jittered backoff. An answer that asks for a longer wait is
        raised at once, with the seconds it asked for as its ``retry_after``
        (:class:`~loaf.exceptions.LoafRateLimitError`, or the 503's
        :class:`~loaf.exceptions.LoafServiceUnavailableError`), so you decide
        what to do with the time. Other requests (``POST``: cancels) are never
        retried.

        A 2xx whose body is not JSON, whatever its content type says (a CDN's or
        a captive portal's page, say), raises
        :class:`~loaf.exceptions.LoafServerError`. A body larger than
        :data:`~loaf.constants.MAX_RESPONSE_BYTES`, or still arriving past the
        read timeout, raises :class:`~loaf.exceptions.LoafConnectionError` and
        is not retried.
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
        handled = sys.exc_info()[1]  # the caller's own, never scrubbed
        tokens = self._login_tokens if auth else None
        if auth and tokens is None:
            raise LoafConfigError(
                f"{method} {path} needs your trading key, which signs the login token that "
                f"authenticates it: set ${ENV_TRADING_KEY} to the web app's Show trading key "
                "(README §2), or pass trading_key=..."
            )

        # Drop params/body keys whose value is None so we never send "null".
        clean_params = {k: v for k, v in (params or {}).items() if v is not None}
        clean_json = None if json is None else {k: v for k, v in json.items() if v is not None}

        idempotent = method.upper() in _IDEMPOTENT_METHODS
        attempt = 0
        token = ""
        request: httpx.Request | None = None
        response: httpx.Response | None = None
        try:
            while True:
                # Taken for each attempt: a backoff can outlast the token's half-life.
                token = tokens.token() if tokens is not None else ""
                try:
                    request = self._http.build_request(
                        method,
                        path,
                        params=clean_params or None,
                        json=clean_json,
                        headers={
                            "Accept-Encoding": _ACCEPT_ENCODING,
                            **({"Authorization": f"Bearer {token}"} if token else {}),
                        },
                    )
                    response, content = self._receive(request)
                except httpx.RequestError as exc:
                    if idempotent and attempt < self.max_retries:
                        self._sleep_backoff(attempt)
                        attempt += 1
                        continue
                    raise LoafConnectionError(f"{method} {path} failed: {exc}") from exc
                except _Unread as exc:  # the same answer again would cost as much
                    raise LoafConnectionError(f"{method} {path} failed: {exc}") from None

                self._record_rate_limit(response)

                if response.is_success:
                    try:
                        return self._parse_body(response, content)
                    except Exception as exc:  # not JSON, or too deeply nested to read
                        raise LoafServerError(
                            f"{method} {path} answered a body that is not JSON",
                            status_code=response.status_code,
                            request_id=response.headers.get("x-request-id"),
                            body=self._text(response, content),
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
                    # Before signing an order, never wait out rate-limit hints: a late order is
                    # stale. Nor, ever, one longer than MAX_HINTED_WAIT: that answer is raised.
                    hinted = None if before_order else self._retry_after_seconds(response)
                    if hinted is None or hinted <= MAX_HINTED_WAIT:
                        self._sleep_backoff(attempt, hinted)
                        attempt += 1
                        continue

                error = self._build_error(response, content)
                if tokens is not None and response.status_code == 401:
                    tokens.forget(token)  # refused: the next request signs a fresh one
                    error = _login_refusal(error)
                raise error
        except BaseException as exc:
            _scrub(exc, handled)
            raise
        finally:
            # Crash reporters and debuggers record frame locals: a login token is a credential, the
            # request (and the response, which holds it) carried it, and tokens holds the current
            # one.
            token = ""
            tokens = request = response = handled = None

    # ------------------------------------------------------------------ #
    # Signed order placement
    # ------------------------------------------------------------------ #

    @property
    def public_key(self) -> str | None:
        """The public key of your trading key, ``0x`` + 80 lowercase hex (what ``GET /l2/keys``
        names ``journaledKey`` while it is in force), or ``None`` when no trading key is set."""
        return self._l2_key.public_key if self._l2_key is not None else None

    def _send_signed(
        self, path: str, body: dict, *, ambiguous: bool = False, op: str = "order"
    ) -> Any:
        """POST signed ``op``, re-sending the identical bytes on transient failures.

        ``op="order"``: an order placement. It carries no ``Authorization``
        header: its signature authenticates. Network errors and 5xx are re-sent
        up to ``max_retries`` times, after a short jittered pause; the exchange
        admits one order per account and order number, so a repeat is answered
        as a duplicate, never a second order. 4xx (429 included) is final. If
        any attempt may have landed and no 200 settled it, raises
        :class:`~loaf.exceptions.OrderOutcomeUnknownError`.

        ``ambiguous=True`` (resubmit): an earlier attempt may already have
        landed, so anything but a 2xx raises the op's unknown-outcome error,
        except a withdrawal's 422 ``ORDER_INDEX_USED``, which says none landed
        (:func:`_never_journaled`): it is raised as itself.

        ``op="perps"`` or ``op="withdrawal"``: a signed perps allocate or exit,
        or a withdrawal. It carries a login token too (the exchange checks the
        account is yours) and is settled by a 202 ``{journalSeq, duplicate,
        ...}``; the exchange journals one op per account and nonce and answers a
        repeat of a journaled one as its duplicate. It raises
        :class:`~loaf.exceptions.PerpsOutcomeUnknownError` or
        :class:`~loaf.exceptions.WithdrawalOutcomeUnknownError` instead.

        A 2xx settles the op only as a JSON object whose ``success`` is not
        false and whose ``orderId`` (an order) or ``journalSeq`` is a whole
        number (:func:`_settles`). Any other 2xx is an unknown outcome, not
        re-sent. So is an answer whose body was not read to its end (past
        :data:`~loaf.constants.MAX_RESPONSE_BYTES` or the read timeout),
        whatever its status. An answer whose body arrived but could not be
        interpreted is an unknown outcome too, unless its status refuses the op
        (a 4xx): that is a refusal.
        """
        handled = sys.exc_info()[1]  # the caller's own, never scrubbed
        what, unknown = _SIGNED_OPS[op]
        content = _json.dumps(body, separators=(",", ":"), allow_nan=False).encode()
        headers = {"Content-Type": "application/json", "Accept-Encoding": _ACCEPT_ENCODING}
        tokens = None if op == "order" else self._login_tokens
        if op != "order" and tokens is None:
            raise LoafConfigError(
                f"POST {path} needs your trading key, which signs the op and the login token "
                f"that authenticates it: set ${ENV_TRADING_KEY} to the web app's Show trading "
                "key (README §2), or pass trading_key=..."
            )
        settled_by = "orderId" if op == "order" else "journalSeq"
        timeout = self._placement_timeout
        attempt = 0
        token = ""
        request: httpx.Request | None = None
        response: httpx.Response | None = None
        try:
            while True:
                retryable = True
                try:
                    if tokens is not None:
                        # Taken for each attempt, as a read takes it: a re-send carries a live
                        # token. A failure to sign one is this attempt's error, so an earlier
                        # attempt that may have landed still ends in an unknown outcome.
                        token = tokens.token()
                        headers["Authorization"] = f"Bearer {token}"
                    request = self._http.build_request(
                        "POST", path, content=content, headers=headers, timeout=timeout
                    )
                    response, answer = self._receive(request)
                except httpx.RequestError as exc:
                    if not isinstance(exc, _NOT_SENT_ERRORS):  # may have been received
                        ambiguous = True
                    error: Exception = LoafConnectionError(f"POST {path} failed: {exc}")
                    error.__cause__ = exc
                except _Unread as exc:
                    # Sent and answered, but what the answer says was not read: it may have
                    # landed, and the same answer again would cost as much.
                    error = LoafConnectionError(f"POST {path} failed: {exc}")
                    ambiguous, retryable = True, False
                except Exception as exc:
                    # E.g. another thread closed the client, or a response hook of your
                    # http_client raised on a 5xx answer: a body that may have been placed is
                    # never lost.
                    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code >= 500:
                        ambiguous = True
                    error, retryable = exc, False
                else:
                    try:
                        self._record_rate_limit(response)
                        if response.is_success:
                            try:
                                result = self._parse_body(response, answer)
                            except Exception:  # not JSON, or too deeply nested to read
                                result = self._text(response, answer)
                            if _settles(result, settled_by):
                                return result
                            error = LoafServerError(
                                f"Unexpected response to {what}",
                                status_code=response.status_code,
                                body=result,
                            )
                            ambiguous, retryable = True, False
                        else:
                            error = self._build_error(response, answer)
                            if tokens is not None and response.status_code == 401:
                                tokens.forget(token)  # refused: the next request signs a fresh one
                                error = _login_refusal(error)
                            retryable = response.status_code >= 500
                            if retryable and not _definitely_not_done(error, op):
                                ambiguous = True
                            elif _never_journaled(error, op):
                                ambiguous = False  # it settles every attempt, a resubmit's too
                    except Exception as exc:
                        # An answer this could not read is no answer, unless its status alone
                        # refuses the op (below 500 and no 2xx): anything else may have landed.
                        status = response.status_code
                        if not response.is_success and status < 500:
                            error = error_from_response(
                                status, None, request_id=response.headers.get("x-request-id")
                            )
                        else:
                            error = LoafServerError(
                                f"Unexpected response to {what}", status_code=status
                            )
                            ambiguous = True
                        error.__cause__ = exc
                        retryable = False
                if not retryable or attempt >= self.max_retries:
                    break
                # Jitter only: every reply carries RateLimit-Reset, and a signed price goes stale.
                self._sleep_backoff(attempt)
                attempt += 1
            if ambiguous:
                raise unknown(
                    signed_body=copy.deepcopy(body), attempts=attempt + 1, last_error=error
                ) from error
            raise error
        except BaseException as exc:
            _scrub(exc, handled)
            raise
        finally:
            # Crash reporters and debuggers record frame locals: a login token is a credential, the
            # request (and the response, which holds it) carried it, and tokens holds the current
            # one.
            token = ""
            headers.pop("Authorization", None)
            tokens = request = response = handled = None

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _receive(self, request: httpx.Request) -> tuple[httpx.Response, bytes]:
        """Send ``request`` and read its answer's body, decompressed: until what has been decoded
        passes MAX_RESPONSE_BYTES (so at most that plus one decoded network read is held), and
        only while the request's read timeout (when it has one) has not passed since its headers
        arrived; past either, :class:`_Unread`. An answer whose ``Content-Encoding`` names
        anything but ``identity`` or one of gzip and deflate is :class:`_Unread` before any of
        its body is read, whichever ``http_client`` sent it. httpx's own errors (a body that
        broke off, or failed to decompress) raise as they are."""
        response = self._http.send(request, stream=True)
        try:
            named = [
                name.strip().lower()
                for name in response.headers.get_list("content-encoding", split_commas=True)
                if name.strip()
            ]
            if len(named) > 1 or (named and named[0] not in _READ_ENCODINGS):
                encoding = ", ".join(named)
                if len(encoding) > _ENCODING_TEXT_LIMIT:
                    encoding = encoding[:_ENCODING_TEXT_LIMIT] + "..."
                raise _Unread(
                    f"its answer's body is encoded {encoding!r}, which the client does not "
                    "read (only gzip or deflate, once)"
                )
            wait = request.extensions.get("timeout", {}).get("read")
            deadline = None if wait is None else _monotonic() + wait
            chunks: list[bytes] = []
            size = 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise _Unread(f"its answer's body passed {MAX_RESPONSE_BYTES} bytes")
                chunks.append(chunk)
                if deadline is not None and _monotonic() > deadline:
                    raise _Unread(
                        f"its answer's body was still arriving past the read timeout ({wait} s)"
                    )
            return response, b"".join(chunks)
        finally:
            response.close()

    @staticmethod
    def _text(response: httpx.Response, content: bytes) -> str:
        """``content`` as text, in the answer's charset (UTF-8 without a known one)."""
        try:
            return content.decode(response.charset_encoding or "utf-8", errors="replace")
        except LookupError:  # a charset Python does not know
            return content.decode("utf-8", errors="replace")

    @staticmethod
    def _parse_body(response: httpx.Response, content: bytes) -> Any:
        """``content`` parsed as JSON, whatever the answer's content type says, or ``None`` for a
        204 or an empty body; a body that is not JSON raises (a ``ValueError``, or a
        ``RecursionError`` for one nested too deep)."""
        if response.status_code == 204 or not content:
            return None
        return parse(_json.loads(content))

    def _build_error(self, response: httpx.Response, content: bytes) -> LoafAPIError:
        text = self._text(response, content)
        try:
            body: Any = _json.loads(content)
        except Exception:  # not JSON, or too deeply nested to read
            body = text
        request_id = response.headers.get("x-request-id")
        retry_after = self._retry_after_seconds(response)
        try:
            return error_from_response(
                response.status_code, body, request_id=request_id, retry_after=retry_after
            )
        except Exception:  # a JSON error too deeply nested to read: its text
            return error_from_response(
                response.status_code, text, request_id=request_id, retry_after=retry_after
            )

    def _record_rate_limit(self, response: httpx.Response) -> None:
        """Keep the answer's rate-limit headers in :attr:`last_rate_limit`: each that is a finite
        number from 0 up (any other is left out)."""
        snapshot: dict[str, float] = {}
        for header, key in (
            ("ratelimit-limit", "limit"),
            ("ratelimit-remaining", "remaining"),
            ("ratelimit-reset", "reset"),
        ):
            value = _finite(response.headers.get(header))
            if value is not None and value >= 0:
                snapshot[key] = value
        if snapshot:
            self.last_rate_limit = snapshot

    @staticmethod
    def _retry_after_seconds(response: httpx.Response) -> float | None:
        """The wait an answer asks for, in seconds: its Retry-After, else, for a 429 alone, its
        RateLimit-Reset (every answer carries one, the time left in the rate-limit window, which
        says nothing about when a 503's cause clears). Only a finite number counts, read as 0
        below 0 and as MAX_RETRY_AFTER above it; ``None`` without one."""
        if response.status_code == 429:
            headers: tuple[str, ...] = ("retry-after", "ratelimit-reset")
        else:
            headers = ("retry-after",)
        for header in headers:
            value = _finite(response.headers.get(header))
            if value is not None:
                return min(max(0.0, value), MAX_RETRY_AFTER)
        return None

    def _sleep_backoff(self, attempt: int, hinted: float | None = None) -> None:
        """Wait before retry ``attempt``: the ``hinted`` seconds an answer asked for, else (a hint
        of 0 would retry at once) an exponential backoff with full jitter."""
        if hinted is not None and hinted > 0:
            time.sleep(hinted)
            return
        # Exponential backoff with full jitter, capped at 30s; a capped exponent never overflows.
        delay = min(30.0, 0.5 * 2 ** min(attempt, 6))
        time.sleep(random.uniform(0, delay))

    # ------------------------------------------------------------------ #
    # WebSocket
    # ------------------------------------------------------------------ #

    def websocket(self, **kwargs: Any) -> LoafWebSocketClient:
        """Create a :class:`~loaf.ws.client.LoafWebSocketClient` bound to this
        client's trading key, host and ``allow_http``. See that class for usage."""
        from .ws.client import LoafWebSocketClient

        return LoafWebSocketClient(self, **kwargs)

    @property
    def ws_url(self) -> str:
        """The WebSocket URL: ``ws_url=``, else ``$LOAF_WS_URL``, else derived from
        :attr:`base_url`, checked and lowercased like it."""
        return self._ws_url

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def close(self) -> None:
        self.orders._l2.close()
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> LoafClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __repr__(self) -> str:
        # getattr: crash reporters repr a client whose __init__ raised part-way.
        key = getattr(self, "_l2_key", None)
        authed = "anonymous" if key is None else f"authenticated public_key={key.public_key}"
        base_url = getattr(self, "base_url", None)
        return f"<LoafClient base_url={base_url!r} {authed} v{__version__}>"

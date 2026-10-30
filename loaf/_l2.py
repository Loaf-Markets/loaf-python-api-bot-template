"""What an order is signed with: your trading key, its market, your trading
account and the order numbers reserved to it; and the login tokens your trading
key signs, which authenticate every other request you make.

Internal. :class:`~loaf.client.LoafClient` keeps one :class:`TradingKey` and its
:class:`LoginTokens`; :class:`~loaf.resources.orders.OrdersResource` keeps one
:class:`L2State`; :class:`~loaf.resources.perps.PerpsResource` signs its allocates
and exits with the :class:`TradingKey` at the nonce :func:`read_signing_account` reads.
"""

from __future__ import annotations

import os
import re
import threading
import time
import traceback
from typing import TYPE_CHECKING, Any, Mapping, NamedTuple

from . import k1
from .constants import (
    ENV_BASE_URL,
    ENV_TRADING_KEY,
    L2_BUFFER_MS,
    L2_LOGIN_TOKEN_LIFE_MS,
    L2_LOGIN_TOKEN_PREFIX,
    L2_MAX_ORDER_LIFE_MS,
    L2_ORDER_INDEX_BLOCK,
    L2_ORDER_LIFE_MARGIN_MS,
    PERPS_QUOTE_ASSET_ID,
    TRADING_KEY_PREFIX,
)
from .exceptions import (
    LoafAPIError,
    LoafConfigError,
    LoafError,
    LoafServerError,
    TradingHaltedError,
    _client_validation_error,
)

if TYPE_CHECKING:
    from typing import TypeGuard  # Python 3.10+: annotations only

    from .client import LoafClient

# A trading key: the L2 chain id (1 to the KoalaBear modulus less one) and the account's index
# (48 bits) in decimal with no leading zero, then the secret key, 40 bytes little-endian as
# lowercase hex.
_TRADING_KEY_RE = re.compile(
    re.escape(TRADING_KEY_PREFIX) + r"([1-9][0-9]{0,9})_(0|[1-9][0-9]{0,14})_([0-9a-f]{80})"
)
# One text whatever is wrong with the key: it names nothing it was given.
_BAD_TRADING_KEY = (
    f"{ENV_TRADING_KEY} (or trading_key=) is not a trading key from the Loaf web app: expected "
    "the one string its Show trading key gives, loaf_tk_<chain>_<account>_<80 hex>, whole and "
    "unchanged."
)
_ADDRESS_RE = re.compile(r"0x[0-9a-fA-F]{40}")
# A market's ticker: its asset's, -PERP appended for a perp ('MUS', 'H100-PERP').
_MARKET_TICKER_RE = re.compile(r"[A-Z0-9]{1,10}(-PERP)?")

_MAX_48 = 2**48 - 1
_MAX_ORDER_INDEX = 2**40 - 1
_MAX_SAFE_INT = 2**53 - 1
_KB_MODULUS = 2_130_706_433  # an L2 chain id is a KoalaBear element, below this prime
# A perp's l2.status: K1's; an EXPIRED perp takes no more orders.
_PERP_L2_STATUSES = ("ACTIVE", "EXPIRED")

_RESERVED_TOO_SOON = "l2 order_index: the account's last reservation is less than 1 s old"
# A 403 for a market that is the ticker's still (listed, its start time not reached yet).
_NOT_STARTED = "Trading on this market has not started yet"

# Refusals that show a cached market or account stale (matched in prefixed leg forms too).
_CONTEXT_TEXTS = (
    "l2 chain id:",
    "l2 market:",
    "l2 asset:",
    "l2 epoch:",
    "l2 key:",
    "l2 signature:",
    "l2 account:",
)
# Refusals that show an order-index block unusable.
_INDEX_TEXTS = (
    "l2 order_index: already used by the account; re-sign with the next index",
    "l2 order_index: not inside a range reserved to the account; reserve one with "
    "ReserveOrderIndices",
    "l2 order_index: the range was reserved more than K1's longest order life before the expiry; "
    "reserve a new one",
)


class TradingKey:
    """Your trading key, the one string the web app's Show trading key gives: the
    secret of its account's K1 api key 0, with that account and its L2 chain. Signs
    orders, perps allocates and exits, and login tokens; the secret never leaves it."""

    __slots__ = ("_secret", "public_key", "l2_chain_id", "l2_account_index")

    def __init__(self, trading_key: str) -> None:
        # Crash reporters and debuggers record frame locals: no local holds the secret when this
        # raises, and the refusal is raised outside the except (no __context__).
        text, trading_key = trading_key, ""
        chain_id = account_index = 0
        secret = b""
        # Matched in the condition, so no local keeps the match (its string is the whole key). The
        # chain id and the account lie between the prefix and the secret, the last 80 characters.
        # Anything but a str is refused, and so is a chain id or an account out of range: b"" is
        # refused below like any other value that is not a scalar.
        if isinstance(text, str) and _TRADING_KEY_RE.fullmatch(text):
            chain_id, account_index = map(int, text[len(TRADING_KEY_PREFIX) : -81].split("_"))
            if chain_id < _KB_MODULUS and account_index <= _MAX_48:
                secret = bytes.fromhex(text[-80:])
        text = ""
        try:
            public = k1.public_key(secret)
        except k1.K1Error:  # not 40 bytes of a scalar in [1, n)
            public = None
        except BaseException as exc:  # a Ctrl-C while the first call builds K1's table, say
            secret = b""
            traceback.clear_frames(exc.__traceback__)  # k1's finished frames hold the secret
            raise
        if public is None:
            secret = b""
            raise LoafConfigError(_BAD_TRADING_KEY)
        self._secret = secret
        #: ``0x`` + 80 lowercase hex: ``GET /l2/keys``'s ``journaledKey`` while it is in force.
        self.public_key = "0x" + public.hex()
        #: The L2 chain of the exchange whose web app showed the key: an order is signed on it.
        self.l2_chain_id = chain_id
        #: Your trading account's index: every order and login token is signed for it.
        self.l2_account_index = account_index

    def sign(self, message: bytes) -> tuple[str, str]:
        """``(s, e)`` of a K1 message (an order's, a perps allocate's or exit's, or a login
        token's), 80 lowercase hex each (32 fresh random bytes per signature, so two
        signatures of the same terms differ)."""
        try:
            s, e = k1.sign(self._secret, message, os.urandom(32))
        except Exception as exc:
            failure = f"{type(exc).__name__}: {exc}"
        else:
            return s.hex(), e.hex()
        # Raised outside the except: neither the traceback nor __context__ reaches k1.sign's frame,
        # which holds the secret scalar and the nonce.
        raise LoafError(f"Signing with your trading key failed ({failure}); nothing was sent.")

    def login_token(self, expiry_ms: int) -> str:
        """A login token of the key's account that expires at ``expiry_ms`` (unix ms):
        ``loaf_lt_<l2ChainId>_<l2AccountIndex>_<l2ExpiryMs>_<l2SigS><l2SigE>``, the
        signature over K1's login message of those three fields."""
        s, e = self.sign(
            k1.login_message(
                l2_chain_id=self.l2_chain_id,
                l2_account_index=self.l2_account_index,
                l2_expiry_ms=expiry_ms,
            )
        )
        return (
            f"{L2_LOGIN_TOKEN_PREFIX}{self.l2_chain_id}_{self.l2_account_index}_{expiry_ms}_{s}{e}"
        )

    def __repr__(self) -> str:
        # getattr: crash reporters repr the key whose __init__ refused it, which has neither.
        return (
            f"TradingKey(public_key={getattr(self, 'public_key', None)!r}, "
            f"l2_account_index={getattr(self, 'l2_account_index', None)!r})"
        )

    def __getstate__(self) -> Any:
        # pickle and copy ask for the state here, at every protocol, and so do serializers that
        # would otherwise read the slots (jsonpickle).
        raise TypeError("TradingKey holds a secret key and cannot be pickled or copied")


class LoginTokens:
    """The login tokens a trading key signs: the ``Authorization: Bearer`` of every
    authenticated request, and the WebSocket's ``auth`` frame. A login token is a
    bearer credential until it expires, so it is kept here, in memory only, and never
    logged or put in an error.

    One token is kept and shared. It lives :data:`~loaf.constants.L2_LOGIN_TOKEN_LIFE_MS`,
    and the first caller to find less than half of that left signs a fresh one, under
    the lock, so concurrent callers share one signature.
    """

    __slots__ = ("_key", "_lock", "_token", "_renew_at_ms")

    def __init__(self, key: TradingKey) -> None:
        self._key = key
        self._lock = threading.Lock()
        self._token: str | None = None
        self._renew_at_ms = 0  # unix ms: when the kept token has half its life left

    def token(self) -> str:
        """A login token with more than half its life left: the one kept, else one
        signed now."""
        return self.token_and_renewal()[0]

    def token_and_renewal(self) -> tuple[str, float]:
        """:meth:`token`, and the seconds until it has only half its life left: when a
        holder that keeps it (a WebSocket connection) sends a fresh one."""
        with self._lock:
            now_ms = time.time_ns() // 1_000_000
            if self._token is None or now_ms >= self._renew_at_ms:
                expiry_ms = now_ms + L2_LOGIN_TOKEN_LIFE_MS
                self._token = self._key.login_token(expiry_ms)
                self._renew_at_ms = expiry_ms - L2_LOGIN_TOKEN_LIFE_MS // 2
            return self._token, (self._renew_at_ms - now_ms) / 1000

    def forget(self, token: str) -> None:
        """Drop ``token``, which the server refused, so that the next caller signs a
        fresh one: unless another caller has signed a newer one since, which is kept."""
        with self._lock:
            if self._token == token:
                self._token = None

    def __getstate__(self) -> Any:
        raise TypeError("LoginTokens holds a trading key and cannot be pickled or copied")


class Market(NamedTuple):
    """A market's ``marketIndex``, ``type`` and ``l2``: the vault and market every order on it
    signs."""

    chain_id: int
    proxy_address: str
    l2_chain_id: int
    market_index: int
    #: A spot market's base asset; 0 on a perp, which has none (an order signs its pool).
    base_asset_id: int
    quote_asset_id: int
    #: ``"SPOT"`` or ``"PERP"``.
    type: str
    #: A perp's ``l2.status``: ``"ACTIVE"``, or ``"EXPIRED"`` (it takes no more orders).
    #: ``None`` on a spot market, whose ``l2`` is served only while it is active.
    l2_status: str | None


class AccountState(NamedTuple):
    """Your trading account: what every order signs."""

    account_index: int
    epoch: int


class _Block:
    """Reserved order indices: ``next`` up to ``end`` (exclusive), and the ``(first, span)``
    definite refusals gave back below ``next``, for orders that expire by ``latest_expiry_ms``."""

    __slots__ = ("next", "end", "latest_expiry_ms", "returned")

    def __init__(self, next: int, end: int, latest_expiry_ms: int) -> None:
        self.next = next
        self.end = end
        self.latest_expiry_ms = latest_expiry_ms
        self.returned: list[tuple[int, int]] = []

    def serves(self, span: int, expiry_ms: int) -> bool:
        return self.next + span <= self.end and expiry_ms <= self.latest_expiry_ms


def market_of(detail: Any, ticker: str) -> Market:
    """The market an order on ``ticker`` signs, from its ``GET /trade/{ticker}`` answer:
    its ``marketIndex`` and its ``l2``.

    Raises:
        LoafConfigError: the server does not run the exchange this SDK trades on.
        LoafValidationError: the market has no L2 market right now (local).
        LoafServerError: the answer is no JSON object (a web page), or an unusable market.
    """
    if not isinstance(detail, Mapping):  # the web app's page, an edge challenge
        raise LoafServerError(
            f"GET /trade/{ticker} answered no market", status_code=200, body=detail
        )
    market = detail.get("market")
    if not isinstance(market, Mapping):  # an older server's answer, or another API's
        raise LoafConfigError(
            f"GET /trade/{ticker} answered no market: this server does not run the exchange this "
            f"SDK trades on (check {ENV_BASE_URL})."
        )
    if "l2" in market and market["l2"] is None:
        raise _client_validation_error(
            f"{ticker} has no L2 market right now (market.l2 is null), so it cannot be traded"
        )
    field = _unusable_market_field(market)
    if field is not None:
        raise LoafServerError(
            f"GET /trade/{ticker} answered an unusable market.{field}",
            status_code=200,
            body=market,
        )
    l2 = market["l2"]
    custody = (l2["chainId"], l2["proxyAddress"], l2["l2ChainId"], market["marketIndex"])
    if market["type"] == "PERP":
        return Market(*custody, 0, l2["quoteAssetId"], "PERP", l2["status"])
    return Market(*custody, l2["baseAssetId"], l2["quoteAssetId"], "SPOT", None)


def epoch_of(answer: Any, account_index: int) -> int:
    """``GET /l2/keys``'s ``epoch``, once its ``accountIndex`` is ``account_index``,
    your trading key's account.

    The login token that read it was signed by the key in force on that account, so
    the account has an epoch.

    Raises:
        LoafServerError: another account, or an epoch no order could be signed at.
    """
    values = answer if isinstance(answer, Mapping) else {}
    index = values.get("accountIndex")
    epoch = values.get("epoch")
    if not (_is_int(index) and index == account_index):
        field = "accountIndex"
    elif not _whole(epoch, 0, _MAX_48):
        field = "epoch"
    else:
        return epoch
    raise LoafServerError(
        f"GET /l2/keys answered an unusable {field}", status_code=200, body=answer
    )


def read_signing_account(client: LoafClient, account_index: int) -> int:
    """``GET /l2/keys`` — the nonce a signed allocate or exit names: api key 0's next
    nonce (``withdrawalNonce``: your withdrawals spend the same counter), once its
    ``accountIndex`` is ``account_index``, your trading key's account.

    The login token that read it was signed by the key in force on that account.

    Raises:
        LoafAuthError: the exchange refused the read's login token: your trading key
            is not the one in force on its account.
        LoafServerError: the answer names another account, or a nonce no op could be
            signed with.
    """
    answer = client._request("GET", "/l2/keys", params={"apiKeyIndex": 0}, before_order=True)
    values = answer if isinstance(answer, Mapping) else {}
    index = values.get("accountIndex")
    nonce = values.get("withdrawalNonce")
    if not (_is_int(index) and index == account_index):
        field = "accountIndex"
    elif not _whole(nonce, 0, _MAX_48):
        field = "withdrawalNonce"
    else:
        return nonce
    raise LoafServerError(
        f"GET /l2/keys answered an unusable {field}", status_code=200, body=answer
    )


class L2State:
    """What an orders resource keeps between placements: each ticker's market, your
    account and the block of order indices being handed out. In memory only, and
    refreshed only when a refusal shows it stale.

    One lock guards the account and the block, and is held across the account read
    and a reservation, so concurrent placements wait for and share the one in flight.
    Signing and sending happen outside it.
    """

    def __init__(self, client: LoafClient) -> None:
        self._client = client
        self._lock = threading.Lock()
        # Each access is one atomic dict operation; losing a race costs one detail read.
        self._markets: dict[str, Market] = {}
        self._account: AccountState | None = None
        self._block: _Block | None = None
        self._last_count = 0

    def market(self, ticker: str) -> Market | None:
        """``ticker``'s market as last read, if any."""
        return self._markets.get(ticker)

    def remember(self, ticker: str, market: Market) -> None:
        """Keep ``ticker``'s market as just read."""
        self._markets[ticker] = market

    def account(self, account_index: int) -> AccountState:
        """Your account, ``account_index`` (your trading key's): the one kept, else its
        epoch read with ``GET /l2/keys``.

        Nothing is kept on a failure, so the next placement reads again.

        Raises:
            LoafAuthError: the exchange refused the read's login token: your trading key
                is not the one in force on its account.
            LoafServerError: the answer names another account, or no usable epoch.
        """
        with self._lock:
            if self._account is None:
                # The login token names the account; api key 0 is the one orders are signed by.
                answer = self._client._request(
                    "GET", "/l2/keys", params={"apiKeyIndex": 0}, before_order=True
                )
                self._account = AccountState(account_index, epoch_of(answer, account_index))
            return self._account

    def take(self, account: AccountState, span: int, ttl_ms: int) -> tuple[int, _Block, int]:
        """``span`` consecutive order indices for an order (and its legs) that may rest
        ``ttl_ms``, reserving more when needed: ``(first, block, expiry_ms)``, where
        ``expiry_ms`` is its signed ``l2ExpiryMs``, from the placement's one clock reading.

        Raises:
            LoafConfigError: even a fresh reservation cannot take that expiry: this
                machine's clock is hours ahead.
        """
        with self._lock:
            # Read here, so waiting for the lock or a reservation never shortens the order's life.
            expiry_ms = time.time_ns() // 1_000_000 + L2_BUFFER_MS + ttl_ms
            block = self._block
            if block is not None and expiry_ms <= block.latest_expiry_ms:
                for i, (first, size) in enumerate(block.returned):  # given back: signed first
                    if size == span:
                        del block.returned[i]
                        return first, block, expiry_ms
            if block is None or not block.serves(span, expiry_ms):
                block = self._reserve(account, span)
                expiry_ms = time.time_ns() // 1_000_000 + L2_BUFFER_MS + ttl_ms
                if not block.serves(span, expiry_ms):
                    # The block stays: it serves shorter lives.
                    raise LoafConfigError(
                        f"This order would expire at {expiry_ms} (unix ms), later than the "
                        "exchange accepts for an order signed now: this machine's clock is hours "
                        "ahead of the exchange's. Synchronize it (NTP) and place the order again."
                    )
            # Advanced before signing, so no two signings in flight share an index.
            first = block.next
            block.next += span
            return first, block, expiry_ms

    def give_back(self, block: _Block, first: int, span: int) -> None:
        """Return a definitely refused order's indices, so a later order signs them,
        unless ``block`` was dropped or replaced since (nothing takes from it then)."""
        # Safe after a definite refusal only (never after an unknown outcome): every attempt that
        # carried these indices was refused or never arrived, and a refused body leaves no trace,
        # so the next body signing them is the only one that can be admitted on them; were the
        # refused one admitted after all, the exchange would refuse the next as "already used".
        with self._lock:
            if block.next == first + span:
                block.next = first
            else:  # another order took the indices after them since
                block.returned.append((first, span))

    def after_refusal(
        self,
        ticker: str,
        account: AccountState | None,
        block: _Block | None,
        error: LoafAPIError,
    ) -> None:
        """Forget what a refusal of an order on ``ticker`` shows stale: the order
        started with ``account`` and took its indices from ``block`` (``None`` until
        they are reserved: a refusal that comes with one answered the placement)."""
        # Texts count on refusals only: the 503s for an unreadable account or an unchecked
        # signature carry the l2 prefixes too, and show nothing stale.
        message = error.message if error.status_code < 500 else ""
        context = any(text in message for text in _CONTEXT_TEXTS)
        # Only the placement's own answer shows the ticker's market stale: the account read and the
        # reservation never name it, and a refusal of their login token ('l2 key: …', say) shows
        # your trading key not in force. A placement names its market by the signed index alone:
        # refused as no market (404) or as one that does not trade (403: delisted, say), it shows
        # the market stale, since the ticker may name another market by now. A halt or a start not
        # reached shows nothing.
        market_stale = block is not None and (
            context
            or error.status_code == 404
            or (
                error.status_code == 403
                and not isinstance(error, TradingHaltedError)
                and _NOT_STARTED not in message
            )
        )
        if market_stale:
            self._markets.pop(ticker, None)
        with self._lock:
            if context and self._account == account:  # by value: a fresh, different read survives
                self._account = None
            if self._block is block and any(text in message for text in _INDEX_TEXTS):
                self._block = None  # by identity: a block another thread just reserved survives

    def _reserve(self, account: AccountState, span: int) -> _Block:
        """``POST /l2/order-indices`` for at least ``span`` indices (under the lock)."""
        # The exchange counts every reserved index no order uses against the account's allowance
        # for about 49.5 days, and an in-memory block's tail is lost at every restart: so the
        # count grows from the first order's span, doubling up to L2_ORDER_INDEX_BLOCK.
        count = min(L2_ORDER_INDEX_BLOCK, max(span, 2 * self._last_count))
        try:
            answer = self._client._request("POST", "/l2/order-indices", json={"count": count})
        except LoafAPIError as exc:
            if _RESERVED_TOO_SOON not in exc.message:  # too soon after the last reservation
                raise
            time.sleep(1.0)
            answer = self._client._request("POST", "/l2/order-indices", json={"count": count})
        values = answer if isinstance(answer, Mapping) else {}
        index = values.get("accountIndex")
        first = values.get("firstIndex")
        got = values.get("count")
        reserved_at = values.get("reservedAtMs")
        if (
            _is_int(index)
            and index == account.account_index
            and _whole(first, 1, _MAX_ORDER_INDEX)
            and _whole(got, 1, _MAX_ORDER_INDEX)
            and first + got - 1 <= _MAX_ORDER_INDEX
            and _whole(reserved_at, 1, _MAX_SAFE_INT)
        ):
            # The exchange admits an order on the range only if it expires by reservedAtMs plus
            # the longest order life; one margin covers this machine's clock.
            latest = reserved_at + L2_MAX_ORDER_LIFE_MS - L2_ORDER_LIFE_MARGIN_MS
            self._block = _Block(first, first + got, latest)
            self._last_count = got
            return self._block
        shown = dict(answer) if isinstance(answer, Mapping) else answer
        raise LoafServerError(
            f"POST /l2/order-indices answered a range no order can be signed in: {shown!r}",
            status_code=201,
            body=answer,
        )


def _is_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _whole(value: object, low: int, high: int) -> TypeGuard[int]:
    return _is_int(value) and low <= value <= high


def _unusable_market_field(market: Mapping[str, Any]) -> str | None:
    """The first field of a ``market`` no order can sign, in schema order: its
    ``marketIndex`` and ``type``, then its ``l2``'s: the custody, then a spot market's
    assets, or a perp's quote asset (USDC) and status. A perp's other terms are not
    signed."""
    if not _whole(market.get("marketIndex"), 0, 4095):
        return "marketIndex"
    kind = market.get("type")
    if kind not in ("SPOT", "PERP"):
        return "type"
    l2 = market.get("l2")
    if not isinstance(l2, Mapping):
        return "l2"
    proxy = l2.get("proxyAddress")
    checks = [
        ("chainId", _whole(l2.get("chainId"), 1, _MAX_SAFE_INT)),
        ("proxyAddress", isinstance(proxy, str) and _ADDRESS_RE.fullmatch(proxy) is not None),
        ("l2ChainId", _whole(l2.get("l2ChainId"), 1, _KB_MODULUS - 1)),
    ]
    quote = l2.get("quoteAssetId")
    if kind == "PERP":
        checks += [
            ("quoteAssetId", _is_int(quote) and quote == PERPS_QUOTE_ASSET_ID),
            ("status", l2.get("status") in _PERP_L2_STATUSES),
        ]
    else:
        base = l2.get("baseAssetId")
        checks += [
            ("baseAssetId", _whole(base, 1, 62)),
            ("quoteAssetId", _whole(quote, 1, 62) and quote != base),
        ]
    return next((f"l2.{field}" for field, ok in checks if not ok), None)

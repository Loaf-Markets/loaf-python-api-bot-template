"""What an order is signed with: your trading key, its market, your trading
account and the order numbers reserved to it; and the login tokens your trading
key signs, which authenticate every other request you make.

Internal. :class:`~loaf.client.LoafClient` keeps one :class:`TradingKey` and its
:class:`LoginTokens`; :class:`~loaf.resources.orders.OrdersResource` keeps one
:class:`L2State`; :class:`~loaf.resources.perps.PerpsResource` signs its allocates
and exits, and :class:`~loaf.resources.funds.FundsResource` its withdrawals, with the
:class:`TradingKey` at the nonce :func:`read_signing_account` reads
(:func:`submit_signed_op`).
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
import traceback
from typing import TYPE_CHECKING, Any, Callable, Mapping, NamedTuple

from . import k1
from .constants import (
    ENV_BASE_URL,
    ENV_TRADING_KEY,
    L2_BUFFER_MS,
    L2_LOGIN_TOKEN_LIFE_MS,
    L2_LOGIN_TOKEN_PREFIX,
    L2_MAX_ORDER_LIFE_MS,
    L2_ORDER_LIFE_MARGIN_MS,
    PERPS_QUOTE_ASSET_ID,
    TRADING_KEY_PREFIX,
)
from .exceptions import (
    LoafAPIError,
    LoafConfigError,
    LoafError,
    LoafServerError,
    MarketStateError,
    TradingHaltedError,
    _client_validation_error,
)

if TYPE_CHECKING:
    # typing has it from Python 3.10; type checkers ship typing_extensions, and annotations only
    # use it, so the package needs no runtime dependency.
    from typing_extensions import TypeGuard

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
logger = logging.getLogger(__name__)

# The longest wait between background reservation attempts after failures.
_AHEAD_MAX_PAUSE_S = 30.0
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
# The order routes' codes for some of the refusals above, which hold whatever the text says: the
# account's epoch or key, and an order number used or voided. The texts stay for the refusals no
# code names apart (INVALID_REQUEST covers several).
_CONTEXT_CODES = frozenset({"SETTLE_EPOCH_MISMATCH", "INVALID_SIGNATURE"})
_INDEX_CODES = frozenset({"ORDER_INDEX_USED"})


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
    signs, and a perp's band, which bounds the prices the SDK derives on it."""

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
    #: A perp's band, ``l2.band`` basis points as last read: the exchange holds a perps order's
    #: price within it around the market's mark, and a perps stop's, take's or leg's limit within
    #: it around its own trigger. 0 (no band) on a spot market, and on a perp whose band is 0 or
    #: not served as a whole number.
    band: int = 0


class AccountState(NamedTuple):
    """Your trading account: what every order signs."""

    account_index: int
    epoch: int


class _Block:
    """Reserved order indices: ``next`` up to ``end`` (exclusive), and the ``(first, span)``
    definite refusals gave back below ``next``, for orders that expire by ``latest_expiry_ms``."""

    __slots__ = ("next", "end", "size", "latest_expiry_ms", "returned")

    def __init__(self, next: int, end: int, latest_expiry_ms: int) -> None:
        self.next = next
        self.end = end
        self.size = end - next
        self.latest_expiry_ms = latest_expiry_ms
        self.returned: list[tuple[int, int]] = []

    def serves(self, span: int, expiry_ms: int) -> bool:
        return self.next + span <= self.end and expiry_ms <= self.latest_expiry_ms

    def half_used(self) -> bool:
        return 2 * (self.end - self.next) <= self.size


def market_of(detail: Any, ticker: str) -> Market:
    """The market an order on ``ticker`` signs, from its ``GET /trade/{ticker}`` answer:
    its ``marketIndex`` and its ``l2`` (a perp's band among its terms).

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
        # Not signed, so never refused: a band that is not a whole number bounds nothing, as in the
        # web app.
        band = l2.get("band")
        band = band if _is_int(band) and band > 0 else 0
        return Market(*custody, 0, l2["quoteAssetId"], "PERP", l2["status"], band)
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


def submit_signed_op(
    client: LoafClient,
    key: TradingKey,
    path: str,
    terms: dict[str, Any],
    lead_ms: int,
    message: Callable[[int, int, int, int], bytes],
    op: str,
) -> Any:
    """Sign a perps allocate or exit, or a withdrawal, at your account's next nonce
    (:func:`read_signing_account`) and expiry now + ``lead_ms``, and send it
    (``client._send_signed``, ``op`` its kind). ``message(chain_id, account_index,
    nonce, expiry_ms)`` builds the K1 message it signs, and ``terms`` are its body's
    own fields.

    One op at a time per client (``client._op_lock``): the three spend one counter, so
    no two sign the same nonce. Your trading key names the L2 chain: another
    deployment's is refused at the nonce read's login."""
    with client._op_lock:
        chain_id, account_index = key.l2_chain_id, key.l2_account_index
        nonce = read_signing_account(client, account_index)
        expiry_ms = time.time_ns() // 1_000_000 + lead_ms
        try:
            signed = message(chain_id, account_index, nonce, expiry_ms)
        except k1.K1Error as exc:
            # The engine's own text, e.g. an account index past an op's width; nothing was sent.
            raise _client_validation_error(str(exc)) from None
        s, e = key.sign(signed)
        body = {
            "l2ChainId": chain_id,
            "l2AccountIndex": account_index,
            **terms,
            "l2Nonce": nonce,
            "l2ExpiryMs": expiry_ms,
            "l2SigS": s,
            "l2SigE": e,
        }
        return client._send_signed(path, body, op=op)


class L2State:
    """What an orders resource keeps between placements: each ticker's market, your
    account and the block of order indices being handed out. In memory only, and
    refreshed only when a refusal shows it stale.

    One lock guards the account and the block, and is held across the account read
    and a reservation, so concurrent placements wait for and share the one in flight.
    Signing and sending happen outside it.

    With ``reserve_ahead``, a background thread reserves the next block (``_standby``) once
    the current one is half used, outside the lock, so a placement with numbers left never
    waits for it. A placement that runs out waits for a reservation already in flight rather
    than sending a second, which the one-a-second limit would refuse.
    """

    def __init__(self, client: LoafClient) -> None:
        self._client = client
        self._lock = threading.Lock()
        # Each access is one atomic dict operation; losing a race costs one detail read.
        self._markets: dict[str, Market] = {}
        self._account: AccountState | None = None
        self._block: _Block | None = None
        self._last_count = 0
        self._max_count, self._first_count = client._order_numbers
        self._ahead = client._reserve_ahead
        self._standby: _Block | None = None
        self._ahead_account: AccountState | None = None
        self._ahead_thread: threading.Thread | None = None
        self._ahead_wake = threading.Event()
        self._ahead_busy = False  # a background reservation is in flight
        self._ahead_done = threading.Condition(self._lock)
        self._reserved_ns = 0  # when the last reservation was answered, on the clock below
        self._closed = False

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
                block = self._next_block(account, span, expiry_ms)
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
            if self._ahead:
                self._wake_ahead(account)
            return first, block, expiry_ms

    def warm(self, account: AccountState) -> None:
        """Reserve the first block now, so the next placement sends only the order."""
        with self._lock:
            if self._block is None:
                self._next_block(account, 1, 0)
            if self._ahead:
                self._wake_ahead(account)

    def close(self) -> None:
        """Stop the background reservations. What is reserved stays unused."""
        with self._lock:
            self._closed = True
            self._ahead_wake.set()

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

    def spent(self, order_index: int) -> None:
        """Never sign ``order_index`` again: ``cancel_by_index`` settled it (an order carries
        it, or it is voided), and the exchange would refuse a later order on it as used."""
        with self._lock:
            for block in (self._block, self._standby):
                if block is None:
                    continue
                # Given back by a definite refusal: the whole span goes, its legs' numbers with it.
                block.returned = [
                    (first, span)
                    for first, span in block.returned
                    if not first <= order_index < first + span
                ]
                if block.next <= order_index < block.end:  # not handed out yet: end before it
                    block.end = order_index

    def after_refusal(
        self,
        ticker: str,
        account: AccountState | None,
        block: _Block | None,
        error: LoafAPIError,
        derived: bool = False,
    ) -> None:
        """Forget what a refusal of an order on ``ticker`` shows stale: the order
        started with ``account`` and took its indices from ``block`` (``None`` until
        they are reserved: a refusal that comes with one answered the placement), and
        ``derived`` says whether it signed a price the SDK derived within the band kept
        with the market (its own, a MARKET order's or a ``*_MARKET`` row's, or a TP/SL
        leg's limit); a MARKET order's ``reference_price`` with no slippage is a price
        given, not derived."""
        # Texts and codes count on refusals only: the 503s for an unreadable account or an
        # unchecked signature carry the l2 prefixes too, and show nothing stale.
        refusal = error.status_code < 500
        message = error.message if refusal else ""
        code = error.code if refusal and isinstance(error.code, str) else None
        context = any(text in message for text in _CONTEXT_TEXTS) or code in _CONTEXT_CODES
        # Only the placement's own answer shows the ticker's market stale: the account read and the
        # reservation never name it, and a refusal of their login token ('l2 key: …', say) shows
        # your trading key not in force. A placement names its market by the signed index alone:
        # refused as no market (404) or as one that does not trade (403: delisted, say), it shows
        # the market stale, since the ticker may name another market by now. Refused for a perp's
        # band (PRICE_BAND) with a price the SDK derived within the band kept with the market
        # (``derived``), it shows that band possibly stale (narrowed since it priced the order), so
        # the next order reads the market's terms again; a price signed as given (a LIMIT, IOC or
        # reduce-only order's, a *_LIMIT row's, a MARKET order's reference_price with no slippage)
        # owes the band nothing, and the market is kept. A halt, the market's own state (closed,
        # reduce-only, its price stale or held) and a start not reached say what the market takes
        # now, and show nothing.
        cached = self._markets.get(ticker)
        banded = (
            derived and code == "PRICE_BAND" and cached is not None and cached.type == "PERP"
        )
        market_stale = block is not None and (
            context
            or banded
            or error.status_code == 404
            or (
                error.status_code == 403
                and not isinstance(error, (TradingHaltedError, MarketStateError))
                and _NOT_STARTED not in message
            )
        )
        if market_stale:
            self._markets.pop(ticker, None)
        with self._lock:
            if context and self._account == account:  # by value: a fresh, different read survives
                self._account = None
            if self._block is block and (
                any(text in message for text in _INDEX_TEXTS) or code in _INDEX_CODES
            ):
                self._block = None  # by identity: a block another thread just reserved survives

    def _next_block(self, account: AccountState, span: int, expiry_ms: int) -> _Block:
        """The block a placement takes from once its own cannot serve it (under the lock): one a
        background reservation just brought, the one reserved ahead, else one reserved now."""
        while self._ahead_busy:
            self._ahead_done.wait()
        block = self._block
        if block is not None and block.serves(span, expiry_ms):
            return block
        # One that cannot serve this order is let go like a block's tail: left in place, it would
        # keep the background from reserving another.
        standby, self._standby = self._standby, None
        if standby is not None and standby.serves(span, expiry_ms):
            self._block = standby
            return standby
        return self._reserve(account, span)

    def _reserve(self, account: AccountState, span: int) -> _Block:
        """``POST /l2/order-indices`` for at least ``span`` indices (under the lock)."""
        block = self._reserved(account, self._post_reservation(self._next_count(span)))
        self._block = block
        return block

    def _next_count(self, span: int) -> int:
        # The exchange counts every reserved index no order uses against the account's allowance
        # for about 49.5 days, and an in-memory block's tail is lost at every restart: so the
        # count grows from order_number_start, doubling up to order_number_block.
        grown = 2 * self._last_count if self._last_count else self._first_count
        return max(span, min(self._max_count, grown))

    def _post_reservation(self, count: int) -> Any:
        try:
            return self._client._request("POST", "/l2/order-indices", json={"count": count})
        except LoafAPIError as exc:
            if _RESERVED_TOO_SOON not in exc.message:  # too soon after the last reservation
                raise
            time.sleep(1.0)
            return self._client._request("POST", "/l2/order-indices", json={"count": count})

    def _reserved(self, account: AccountState, answer: Any) -> _Block:
        """The block a reservation's answer gives, noted as the last one (under the lock)."""
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
            self._last_count = got
            self._reserved_ns = time.time_ns()
            return _Block(first, first + got, latest)
        shown = dict(answer) if isinstance(answer, Mapping) else answer
        raise LoafServerError(
            f"POST /l2/order-indices answered a range no order can be signed in: {shown!r}",
            status_code=201,
            body=answer,
        )

    def _wake_ahead(self, account: AccountState) -> None:
        """Start the next block's reservation once this one is half used (under the lock)."""
        block = self._block
        if (
            self._closed
            or self._standby is not None
            or self._ahead_busy
            or not (block is None or block.half_used())
        ):
            return
        self._ahead_account = account
        self._ahead_wake.set()
        if self._ahead_thread is None:
            self._ahead_thread = threading.Thread(
                target=self._run_ahead, name="loaf-order-numbers", daemon=True
            )
            self._ahead_thread.start()

    def _run_ahead(self) -> None:
        pause = 1.0
        while True:
            self._ahead_wake.wait()
            self._ahead_wake.clear()
            if self._closed:
                return
            try:
                self._reserve_ahead()
                pause = 1.0
            except Exception as exc:  # the thread outlives any one failure
                # Your placements still reserve for themselves, and they raise what this hides.
                logger.warning("Reserving order numbers ahead failed: %s", exc)
                time.sleep(pause)
                pause = min(2 * pause, _AHEAD_MAX_PAUSE_S)
                self._ahead_wake.set()

    def _reserve_ahead(self) -> None:
        """One background reservation: the request outside the lock, its block kept under it."""
        with self._lock:
            account = self._ahead_account
            block = self._block
            if (
                account is None
                or self._closed
                or self._standby is not None
                or not (block is None or block.half_used())
            ):
                return
            self._ahead_busy = True
            count = self._next_count(1)
            since_s = (time.time_ns() - self._reserved_ns) / 1e9
        try:
            if since_s < 1.0:
                time.sleep(1.0 - since_s)  # sent now, the limit would refuse it
            answer = self._post_reservation(count)
            with self._lock:
                fresh = self._reserved(account, answer)
                if self._block is None:
                    self._block = fresh
                else:
                    self._standby = fresh
        finally:
            with self._lock:
                self._ahead_busy = False
                self._ahead_done.notify_all()


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

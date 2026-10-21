"""What an order is signed with: your trading key, the property's L2 market, your
trading account and the order numbers reserved to it.

Internal. :class:`~loaf.resources.orders.OrdersResource` keeps one
:class:`L2State`; ``python -m loaf.secret_key`` shares :func:`market_of` and
:func:`read_account`.
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
    L2_BUFFER_MS,
    L2_MAX_ORDER_LIFE_MS,
    L2_ORDER_INDEX_BLOCK,
    L2_ORDER_LIFE_MARGIN_MS,
)
from .exceptions import (
    LoafAPIError,
    LoafConfigError,
    LoafError,
    LoafNotFoundError,
    LoafServerError,
    VenueError,
    VenueSwitchingError,
    _client_validation_error,
)

if TYPE_CHECKING:
    from typing import TypeGuard  # Python 3.10+: annotations only

    from .client import LoafClient

#: ``GET /l2/keys``'s 404 text while you have no trading account on the ledger it reads.
NO_ACCOUNT = "l2 account: no L2 account for this user"

_SECRET_RE = re.compile(r"[0-9a-fA-F]{80}")
_BAD_TRADING_KEY = (
    "LOAF_SECRET_KEY (or secret_key=) is not a trading key: expected the 80 hex "
    "characters the web app's Show trading key gives, with no 0x prefix."
)
_ADDRESS_RE = re.compile(r"0x[0-9a-fA-F]{40}")
_PUBLIC_KEY_RE = re.compile(r"0x[0-9a-f]{80}")
_TICKER_RE = re.compile(r"[A-Z0-9]{1,4}")
# The journaledKey of an account whose trading an L1 key change disabled.
_DISABLED_KEY = "0x" + "0" * 80

_MAX_48 = 2**48 - 1
_MAX_ORDER_INDEX = 2**40 - 1
_MAX_SAFE_INT = 2**53 - 1
_KB_MODULUS = 2_130_706_433  # an L2 chain id is a KoalaBear element, below this prime

# How long after the SDK last saw the market mode switch an account that contradicts the trading
# key is taken for the switch still settling (seconds).
_VENUE_SETTLE_S = 120.0

_SWITCHED = (
    "The market mode switched while this order was being prepared: nothing was placed; try again "
    "in a few seconds."
)
_RESERVED_TOO_SOON = "l2 order_index: the account's last reservation is less than 1 s old"

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
    """The account's trading key (K1 api key 0 on one vault): signs orders; the secret
    never leaves it."""

    __slots__ = ("_secret", "public_key")

    def __init__(self, secret_hex: str) -> None:
        # Crash reporters and debuggers record frame locals: no local holds the secret when this
        # raises, and the refusal is raised outside the except (no __context__).
        text, secret_hex = secret_hex, ""
        # A 0x prefix is refused on purpose (0x + 80 hex is the shape of a public key), and so is
        # anything but a str; b"" is refused below like any other value that is not a scalar.
        secret = (
            bytes.fromhex(text) if isinstance(text, str) and _SECRET_RE.fullmatch(text) else b""
        )
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

    def sign(self, message: bytes) -> tuple[str, str]:
        """``(l2SigS, l2SigE)`` of a K1 order message, 80 lowercase hex each (32 fresh random
        bytes per signature, so two signatures of the same terms differ)."""
        try:
            s, e = k1.sign(self._secret, message, os.urandom(32))
        except Exception as exc:
            failure = f"{type(exc).__name__}: {exc}"
        else:
            return s.hex(), e.hex()
        # Raised outside the except: neither the traceback nor __context__ reaches k1.sign's frame,
        # which holds the secret scalar and the nonce.
        raise LoafError(f"Signing the order failed ({failure}); nothing was sent.")

    def __repr__(self) -> str:
        # getattr: crash reporters repr the key whose __init__ refused it, which has no public key.
        return f"TradingKey(public_key={getattr(self, 'public_key', None)!r})"

    def __getstate__(self) -> Any:
        # pickle and copy ask for the state here, at every protocol, and so do serializers that
        # would otherwise read the slots (jsonpickle).
        raise TypeError("TradingKey holds a secret key and cannot be pickled or copied")


class Market(NamedTuple):
    """A property's ``l2Market``: the vault and market every order on it signs."""

    chain_id: int
    proxy_address: str
    l2_chain_id: int
    market_index: int
    base_asset_id: int
    quote_asset_id: int

    @property
    def vault(self) -> tuple[str, int]:
        """``(proxy address lowercased, L2 chain id)``: the ledger the market settles on."""
        return (self.proxy_address.lower(), self.l2_chain_id)


class AccountState(NamedTuple):
    """Your trading account on one vault: what every order on it signs."""

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
    """The market an order on ``ticker`` signs, from its ``GET /trade/{ticker}`` answer.

    Raises:
        VenueError: the property does not trade in the current market mode (local,
            code ``NOT_TRADEABLE_IN_MODE``).
        LoafConfigError: the server does not run the exchange this SDK trades on.
        LoafValidationError: the property, a real one, has no L2 market right now (local).
        LoafServerError: the answer has no property, or an unusable ``l2Market``.
    """
    prop = detail.get("property") if isinstance(detail, Mapping) else None
    if not isinstance(prop, Mapping):
        raise LoafServerError(
            f"GET /trade/{ticker} answered no property", status_code=200, body=detail
        )
    # Your account and order numbers are read from the current market mode's ledger: read for a
    # property that mode does not trade, they would name another vault's account. During a round
    # only the round's own market has an l2Market: another competition property's is null.
    mode = bool(detail.get("competitionModeActive"))
    outside_its_round = mode and "l2Market" in prop and prop["l2Market"] is None
    if bool(prop.get("isCompetition")) != mode or outside_its_round:
        raise VenueError(
            f"{ticker} is not tradeable in the current market mode: "
            + (
                "a competition round is on, and only its market trades"
                if mode
                else "it is a competition market, which trades only during its round"
            ),
            status_code=0,
            code="NOT_TRADEABLE_IN_MODE",
        )
    if "l2Market" not in prop:
        raise LoafConfigError(
            f"GET /trade/{ticker} carries no l2Market: this server does not run the exchange this "
            f"SDK trades on (check {ENV_BASE_URL})."
        )
    raw = prop["l2Market"]
    if raw is None:
        raise _client_validation_error(
            f"{ticker} has no L2 market right now (property.l2Market is null), so it cannot be "
            "traded"
        )
    field = _unusable_market_field(raw)
    if field is not None:
        raise LoafServerError(
            f"GET /trade/{ticker} answered an unusable property.l2Market ({field})",
            status_code=200,
            body=raw,
        )
    return Market(
        raw["chainId"],
        raw["proxyAddress"],
        raw["l2ChainId"],
        raw["marketIndex"],
        raw["baseAssetId"],
        raw["quoteAssetId"],
    )


def parse_keys(answer: Any) -> tuple[int, int | None, str | None]:
    """``GET /l2/keys``'s ``(accountIndex, epoch, journaledKey)``, each checked.

    Raises:
        LoafServerError: a field no order could be signed with.
    """
    values = answer if isinstance(answer, Mapping) else {}
    account_index = values.get("accountIndex")
    epoch = values.get("epoch")
    journaled = values.get("journaledKey")
    if not _whole(account_index, 0, _MAX_48):
        field = "accountIndex"
    elif epoch is not None and not _whole(epoch, 0, _MAX_48):
        field = "epoch"
    elif journaled is not None and not (
        isinstance(journaled, str) and _PUBLIC_KEY_RE.fullmatch(journaled)
    ):
        field = "journaledKey"
    else:
        return account_index, epoch, journaled
    raise LoafServerError(
        f"GET /l2/keys answered an unusable {field}", status_code=200, body=answer
    )


def read_account(client: LoafClient, ticker: str) -> tuple[int, int, str]:
    """``GET /l2/keys`` — your trading account on the current market mode's ledger:
    ``(account_index, epoch, journaled_key)``, once a trading key is in force on it.

    ``ticker`` names the market in the refusals.

    Raises:
        LoafConfigError: you have no trading account there yet (on a competition
            round's market: no seat, or the round has not funded it yet), or no
            trading key is in force on it.
    """
    try:
        # No publicKey: the caller compares journaledKey with its own key itself.
        answer = client._request("GET", "/l2/keys", params={"apiKeyIndex": 0}, before_order=True)
    except LoafNotFoundError as exc:
        if exc.message != NO_ACCOUNT:
            raise
        raise LoafConfigError(
            f"You have no trading account on {ticker}'s market yet: on a real market, deposit in "
            "the Loaf web app first; a competition round's market has accounts only for the "
            "round's participants, which the round funds (check "
            "loaf.competition.queue_position().participant; with a seat, wait for the funding)."
        ) from exc
    account_index, epoch, journaled = parse_keys(answer)
    if epoch is None or journaled is None or journaled == _DISABLED_KEY:
        raise LoafConfigError(
            f"Trading is not enabled on your account on {ticker}'s market (no trading key is in "
            f"force): open {ticker} on the Loaf web app's Trade page, enable trading there and "
            "wait for it to take effect, then set LOAF_SECRET_KEY to the key in force: the web "
            "app's Show trading key (README §2), or on a competition round's market "
            f"python -m loaf.secret_key --ticker {ticker} (README, Competition rounds)."
        )
    return account_index, epoch, journaled


class L2State:
    """What an orders resource keeps between placements: each ticker's market, your
    account as read on one vault (a trading key is in force on one vault only), and
    the block of order indices being handed out. In memory only, and refreshed only
    when a refusal shows it stale.

    One lock guards the account and the block, and is held across the account read
    and a reservation, so concurrent placements wait for and share the one in flight.
    Signing and sending happen outside it, and so does noting a market-mode switch,
    which an account read in flight must see.
    """

    def __init__(self, client: LoafClient) -> None:
        self._client = client
        self._lock = threading.Lock()
        # Each access is one atomic dict operation; losing a race costs one detail read.
        self._markets: dict[str, Market] = {}
        self._vault: tuple[str, int] | None = None  # the vault _account was read on
        self._account: AccountState | None = None
        self._block: _Block | None = None
        self._last_count = 0
        self._mode: bool | None = None  # competitionModeActive of the last property read
        self._switch_seen_at = float("-inf")  # time.monotonic() the mode was last seen switching

    def market(self, ticker: str) -> Market | None:
        """``ticker``'s market as last read, if any."""
        return self._markets.get(ticker)

    def remember(self, ticker: str, market: Market) -> None:
        """Keep ``ticker``'s market as just read."""
        self._markets[ticker] = market

    def note_mode(self, detail: Any) -> None:
        """Note the market mode a placement's ``GET /trade/{ticker}`` answer shows: one
        other than the last read's is a switch, which opens the window of :meth:`account`."""
        if not isinstance(detail, Mapping):
            return
        mode = bool(detail.get("competitionModeActive"))
        # Not under the lock, which an account read in flight holds: it must count this switch.
        # Reads racing here can at worst note one switch twice.
        previous, self._mode = self._mode, mode
        if previous is not None and mode != previous:
            self._switch_seen_at = time.monotonic()

    def cached_account(self, market: Market) -> AccountState | None:
        """The account read on ``market``'s vault, if one is kept (no I/O)."""
        with self._lock:
            return self._account if self._vault == market.vault else None

    def account(self, market: Market, public_key: str, ticker: str) -> AccountState:
        """Your account on ``market``'s vault: the one kept, else read with ``GET /l2/keys``.

        Call it only right after a detail read of ``ticker`` whose venue check passed.
        Nothing is kept on a failure, so the next placement reads again.

        Raises:
            LoafConfigError: no trading account, trading not enabled, or ``public_key``
                is not the trading key in force.
            VenueSwitchingError: one of those within two minutes of the SDK seeing the
                market mode switch (local, code ``VENUE_SWITCHING``).
        """
        with self._lock:
            if self._vault == market.vault and self._account is not None:
                return self._account
            try:
                account_index, epoch, journaled = read_account(self._client, ticker)
                # The web app derives a distinct key per vault, so a match also proves the answer
                # is this vault's account.
                if journaled != public_key:
                    raise LoafConfigError(
                        f"LOAF_SECRET_KEY is not the trading key in force on {ticker}'s "
                        "market: it was reset in the web app, or this key belongs to another "
                        "market's vault (a competition round's market has its own). Set "
                        "LOAF_SECRET_KEY to the key in force (the web app's Show trading key; on "
                        f"a round's market, python -m loaf.secret_key --ticker {ticker}) and "
                        "restart; then cancel and re-arm your stops and takes, which stay "
                        "ARMED but FAIL when they fire once the key changed."
                    )
            except LoafConfigError as exc:
                # Around a round transition consecutive reads can land on different ledgers (the
                # detail behind the venue check can be stale), so a contradiction then is no
                # configuration mistake.
                if time.monotonic() - self._switch_seen_at < _VENUE_SETTLE_S:
                    raise VenueSwitchingError(
                        "The market mode is switching (a competition round is starting or "
                        f"ending), so your trading account on {ticker}'s market cannot be read "
                        "reliably yet: nothing was placed; try again in a few seconds.",
                        status_code=0,
                        code="VENUE_SWITCHING",
                    ) from exc
                raise
            if self._vault != market.vault:
                self._vault, self._block = market.vault, None  # a block never serves another vault
            self._account = AccountState(account_index, epoch)
            return self._account

    def take(
        self, market: Market, account: AccountState, span: int, ttl_ms: int
    ) -> tuple[int, _Block, int]:
        """``span`` consecutive order indices for an order (and its legs) that may rest
        ``ttl_ms``, reserving more when needed: ``(first, block, expiry_ms)``, where
        ``expiry_ms`` is its signed ``l2ExpiryMs``, from the placement's one clock reading.

        Raises:
            VenueSwitchingError: ``market`` is not the vault the account was read on,
                or the reservation was answered from another ledger (local, code
                ``VENUE_SWITCHING``).
            LoafConfigError: even a fresh reservation cannot take that expiry: this
                machine's clock is hours ahead.
        """
        with self._lock:
            if self._vault != market.vault:
                # Another thread read an account on another vault since this placement read its own.
                raise VenueSwitchingError(_SWITCHED, status_code=0, code="VENUE_SWITCHING")
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
        started with ``account`` and took its indices from ``block``."""
        # Texts count on refusals only: the 503s for an unreadable account or an unchecked
        # signature carry the l2 prefixes too, and show nothing stale.
        message = error.message if error.status_code < 500 else ""
        # The market mode's refusals, the exchange's or the SDK's own, at any status.
        venue = isinstance(error, (VenueError, VenueSwitchingError))
        context = venue or any(text in message for text in _CONTEXT_TEXTS)
        if context:
            self._markets.pop(ticker, None)
        # The exchange's market-mode refusals show a switch, noted before the lock as note_mode
        # does. The SDK's own (status 0) do not: a local one repeats for as long as a mode lasts
        # (note_mode sees the switch itself), so a real configuration error surfaces once the
        # window has passed.
        if venue and error.status_code:
            self._switch_seen_at = time.monotonic()
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
            try:
                answer = self._client._request("POST", "/l2/order-indices", json={"count": count})
            except LoafAPIError as exc:
                if _RESERVED_TOO_SOON not in exc.message:  # too soon after the last reservation
                    raise
                time.sleep(1.0)
                answer = self._client._request("POST", "/l2/order-indices", json={"count": count})
        except LoafNotFoundError as exc:
            if exc.message != NO_ACCOUNT:
                raise
            raise self._switched() from exc  # a ledger that holds no account of yours answered
        values = answer if isinstance(answer, Mapping) else {}
        index = values.get("accountIndex")
        first = values.get("firstIndex")
        got = values.get("count")
        reserved_at = values.get("reservedAtMs")
        if _is_int(index) and index != account.account_index:
            raise self._switched()  # another account: another ledger answered
        if (
            _is_int(index)
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

    def _switched(self) -> VenueSwitchingError:
        """A reservation answered from another ledger than the account was read on:
        the market mode switched in between (under the lock)."""
        self._account = None  # read it again
        self._switch_seen_at = time.monotonic()
        return VenueSwitchingError(_SWITCHED, status_code=0, code="VENUE_SWITCHING")


def _is_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _whole(value: object, low: int, high: int) -> TypeGuard[int]:
    return _is_int(value) and low <= value <= high


def _unusable_market_field(raw: Any) -> str | None:
    """The first field of a ``property.l2Market`` no order can sign, in schema order."""
    if not isinstance(raw, Mapping):
        return "l2Market"
    proxy = raw.get("proxyAddress")
    base, quote = raw.get("baseAssetId"), raw.get("quoteAssetId")
    checks = (
        ("chainId", _whole(raw.get("chainId"), 1, _MAX_SAFE_INT)),
        ("proxyAddress", isinstance(proxy, str) and _ADDRESS_RE.fullmatch(proxy) is not None),
        ("l2ChainId", _whole(raw.get("l2ChainId"), 1, _KB_MODULUS - 1)),
        ("marketIndex", _whole(raw.get("marketIndex"), 0, 4095)),
        ("baseAssetId", _whole(base, 1, 62)),
        ("quoteAssetId", _whole(quote, 1, 62) and quote != base),
    )
    return next((field for field, ok in checks if not ok), None)

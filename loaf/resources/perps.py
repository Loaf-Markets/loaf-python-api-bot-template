"""Perps: perps markets, your margin pools and positions, margin moves and exits.

A perps market trades a perpetual future settled in USDC (its property's
``l2Market`` has ``kind`` ``"perps"``). Its orders are placed like any other,
with ``pool=`` and ``reduce_only=``
(:class:`~loaf.resources.orders.OrdersResource`); this resource reads the
markets and your positions, and moves your margin::

    loaf.perps.add_margin(5_000, pool=0)           # 5,000 USDC: spot balance -> pool 0
    loaf.orders.limit_buy("PRP1", quantity=2, price=101.5, pool=0)
    loaf.perps.position(market=4, pool=0)

**Pools.** Your perps margin is USDC held in pools: pool 0, the cross pool,
and the isolated pools 1 to 7. Each holds at most 8 positions, one per market,
margined by that pool's collateral alone, and an order trades from the pool it
names (``pool=``, 0 by default). USDC moves between your spot balance and a
pool, or between two pools, by an allocate (:meth:`PerpsResource.add_margin`,
:meth:`PerpsResource.return_margin`, :meth:`PerpsResource.allocate`). Once a
market has expired, a position in it is closed by an exit
(:meth:`PerpsResource.exit`).

**Signing.** An allocate or an exit is signed on your machine with your
trading key over the exchange's message, as an order is, and is sent with your
API key too (the exchange checks the account is yours). Each spends your
account's next nonce, one counter your withdrawals spend too: the SDK reads it
(``GET /l2/keys``) for every op, and sends one op at a time per client. An op
sent elsewhere (the web app, another process) between that read and the send
makes the exchange refuse this one, ``l2 money: the nonce is not the api key's
nonce …`` (422): send it again, and the SDK reads the new nonce.

**Retries.** As for orders: a timeout or a 5xx re-sends the same signed body,
which the exchange applies once at most (it answers a repeat of an applied op
``duplicate: True``). Any error other than
:class:`~loaf.exceptions.PerpsOutcomeUnknownError` means the op was not applied
(its nonce stays unspent); on that one, ``resubmit(error.signed_body)``, and
never sign the same intent afresh while it may have been applied.

**Units.** An allocate's ``amount`` is in USDC (``12.5`` is 12.5 USDC). The
exchange moves whole ticks of it: the SDK turns the amount into ticks with the
collateral ``GET /l2/perps/markets`` serves (:meth:`PerpsResource.collateral`:
ticks = amount x 10^``decimals`` / ``tickSize``), and refuses an amount that is
not a whole number of ticks, or a reply without a usable collateral (it never
assumes one; an exit, which moves no amount, never needs it). The reads are the
engine's units, not dollars: every amount of 64 bits or more is a decimal
string, and :meth:`PerpsResource.positions` lists their units.
"""

from __future__ import annotations

import numbers
import threading
import time
from decimal import Context, Decimal, localcontext
from fractions import Fraction
from typing import TYPE_CHECKING, Any, Callable, Mapping, NamedTuple

from .. import k1
from .._l2 import _KB_MODULUS, _is_int, _whole, read_signing_account
from .._object import LoafObject
from ..constants import (
    DEFAULT_PERPS_OP_TTL_MS,
    ENV_API_KEY,
    ENV_SECRET_KEY,
    MAX_PERPS_OP_TTL_MS,
    MAX_POOL,
    MIN_ORDER_TTL_MS,
    PERPS_LAST,
    PERPS_QUOTE_ASSET_ID,
    PERPS_SPOT_SIDE,
)
from ..exceptions import (
    LoafAPIError,
    LoafConfigError,
    LoafServerError,
    _client_validation_error,
)
from ..money import _BASE_UNITS_RE, _to_decimal
from .base import Resource

if TYPE_CHECKING:
    from .._l2 import TradingKey
    from ..client import LoafClient

_ALLOCATE_LIMIT = 2**60  # an allocate's amount, in ticks, is below it
_TICK_SIZE_LIMIT = 2**128  # the collateral's tickSize is below it
_EXTENSION_LIMIT = 2**56  # and its extensionMultiplier
_EXACT = Context(prec=100)  # every collateral bound below fits in it


class _Collateral(NamedTuple):
    """``GET /l2/perps/markets``' collateral, checked: ``row`` as served, ``tick`` one tick in
    the token's units and ``most`` the largest allocate (2^60 - 1 ticks), both exact."""

    row: Mapping[str, Any]
    symbol: str
    decimals: int
    tick_size: int
    tick: Decimal
    most: Decimal

_SIGNED_OP_STRS = ("l2SigS", "l2SigE")
_SIGNED_OP_INTS = ("l2ChainId", "l2AccountIndex", "l2Nonce", "l2ExpiryMs")
_ALLOCATE_KEYS = ("fromSide", "toSide", "amount")
_EXIT_KEYS = ("pool", "market")


class PerpsResource(Resource):
    def __init__(self, client: LoafClient) -> None:
        super().__init__(client)
        # One op at a time: each signs the nonce GET /l2/keys serves, which only an applied op moves.
        self._lock = threading.Lock()
        #: The L2 chain id every op signs and the perps markets' reply it came from (whose
        #: collateral only an allocate reads), read once (under ``_terms_lock``); dropped on the
        #: chain id's refusal, or when an allocate finds no usable collateral in it.
        self._terms_lock = threading.Lock()
        self._market_terms: tuple[int, Any] | None = None

    # -- Markets ------------------------------------------------------------ #

    def markets(self) -> Any:
        """``GET /l2/perps/markets`` — every perps market, ascending by index. Public.

        Returns ``{"markets": [...], "collateral": {...}}``
        (:class:`~loaf.models.PerpsMarkets`); :meth:`collateral` reads the
        second. Each row is the market as a property's ``l2Market`` serves it
        (:class:`~loaf.models.L2PerpsMarket`): ``marketIndex`` (0 to 254),
        ``status`` (``ACTIVE``, or ``EXPIRED``: it takes no orders and its
        positions leave by exits), the custody (``chainId``, ``proxyAddress``,
        ``l2ChainId``), ``quoteAssetId`` (3, USDC), and the terms its orders
        are admitted against: ``takerFee`` / ``makerFee`` (per 1,000,000),
        ``risk`` (the maintenance margin factor per 65,536; initial margin is
        twice it), ``band``, ``flags``, ``oiCap``, ``minBase``, ``minQuote``,
        ``quoteLimit``, ``sem`` and ``qem``.

        The exchange's edge caches it for 5 s: a market that has just expired
        can still show ``ACTIVE`` that long. A market's live state (its index
        price, funding rate and open interest) is not served yet.
        """
        return self._client.get("/l2/perps/markets", auth=False)

    def collateral(self) -> Any:
        """The ``collateral`` of :meth:`markets`: the one asset every margin pool holds,
        USDC (:class:`~loaf.models.PerpsCollateral`): its ``symbol`` and ``decimals``,
        ``tickSize`` (its base units per tick: an allocate moves whole ticks) and
        ``extensionMultiplier`` (the reads' extended units per tick), the last two
        decimal strings. Neither changes once the asset is registered.

        Raises:
            LoafServerError: the reply has no usable collateral.
        """
        return _collateral_of(self.markets()).row

    def market(self, market: int) -> Any:
        """Perps market ``market``'s row of :meth:`markets`, or ``None`` when it is not listed."""
        index = _ranged(market, "market", 0, PERPS_LAST, "a perps market index")
        for row in _list_of(self.markets(), "markets"):
            if isinstance(row, Mapping) and row.get("marketIndex") == index:
                return row
        return None

    # -- Your pools and positions ------------------------------------------- #

    def positions(self) -> Any:
        """``GET /l2/perps/positions`` — your account's pools and their positions.

        Returns ``{l2AccountIndex, pools}`` (:class:`~loaf.models.PerpsAccount`):
        the pools that hold anything, ascending, each with its positions
        ascending by market. Values are the engine's, as of its last commit,
        each amount of 64 bits or more a decimal string:

        * a pool's ``collateral``, ``value`` (the collateral plus each
          position's signed notional less its funding owed) and a position's
          ``fundingOwed`` (positive pays, negative receives) and ``notional``
          (|base| x index x the market's ``qem``) are in the money book's
          extended units: ticks x the collateral's ``extensionMultiplier``
          (:meth:`collateral`), so USDC = extended / ``extensionMultiplier``
          x ``tickSize`` / 10^``decimals``;
        * a pool's ``requirement`` (its maintenance margin) is in those units
          x 65,536; ``liquidatable`` is ``value < 0`` or ``value x 65,536 <
          requirement``, and ``initialMarginOk`` (collateral may leave the
          pool) is ``value x 65,536 >= 2 x requirement``. The four are
          ``None`` together when the engine's summary of the pool is past its
          range;
        * a position's ``base`` is signed (positive long, negative short) in
          the market's base units, an order's signed quantity (tokens x 10);
          ``entryPrice`` is 10 x ``entryQuote`` / |base|, in the market's price
          units, as is ``liquidationPrice``: the index below which (a long) or
          above which (a short) the pool is liquidatable, every other term of
          the pool held, ``None`` when it has none; ``fundingIndex`` is the
          funding index it last settled at (32.32 fixed point);
        * ``adlRank``, where served: the position's place in the
          auto-deleveraging queue, 1 (deleveraged last) to 5 (first), ``None``
          while it is unranked.

        Raises:
            LoafNotFoundError: you have no trading account yet.
            LoafServiceUnavailableError: the exchange has no perps book yet, or
                could not read it; try again in a moment.
        """
        return self._client.get("/l2/perps/positions")

    def pool(self, pool: int = 0) -> Any:
        """Your margin pool ``pool`` (0, the cross pool, to 7) as :meth:`positions`
        serves it (:class:`~loaf.models.PerpsPool`), or ``None`` when it holds nothing."""
        number = _pool(pool)
        for row in _list_of(self.positions(), "pools"):
            if isinstance(row, Mapping) and row.get("pool") == number:
                return row
        return None

    def position(self, market: int, pool: int = 0) -> Any:
        """Your position in perps market ``market`` from margin pool ``pool``
        (:class:`~loaf.models.PerpsPosition`), or ``None`` when there is none."""
        index = _ranged(market, "market", 0, PERPS_LAST, "a perps market index")
        held = self.pool(pool)
        for row in _list_of(held, "positions") if held is not None else ():
            if isinstance(row, Mapping) and row.get("market") == index:
                return row
        return None

    def funding(self) -> list[Any]:
        """The funding of each of your open positions, from :meth:`positions`:
        ``[{pool, market, fundingIndex, fundingOwed}]``
        (:class:`~loaf.models.PerpsFunding`), ``fundingOwed`` in extended units
        (:meth:`positions`; positive pays, negative receives) and ``fundingIndex`` the
        funding index it last settled at. The pool's value counts what it owes,
        and the exchange moves it at the pool's next refresh."""
        return [
            LoafObject(
                pool=held.get("pool"),
                market=row.get("market"),
                fundingIndex=row.get("fundingIndex"),
                fundingOwed=row.get("fundingOwed"),
            )
            for held in _list_of(self.positions(), "pools")
            if isinstance(held, Mapping)
            for row in _list_of(held, "positions")
            if isinstance(row, Mapping)
        ]

    # -- Margin moves and exits (signed) ------------------------------------ #

    def allocate(
        self,
        amount: float | Decimal,
        *,
        from_side: int,
        to_side: int,
        ttl_ms: int | None = None,
    ) -> Any:
        """``POST /l2/allocations`` — move ``amount`` USDC from ``from_side``
        to ``to_side``, signed with your trading key.

        Each side is a margin pool, 0 (the cross pool) to 7, or
        :data:`~loaf.constants.PERPS_SPOT_SIDE` (255), your spot USDC balance;
        the two differ. :meth:`add_margin` and :meth:`return_margin` spell out
        the moves to and from your spot balance.

        Args:
            amount: USDC (a number; a ``Decimal`` for exact cents): a whole
                number of the collateral's ticks (:meth:`collateral`, see the
                module's Units), from one tick to 2^60 - 1 of them. Checked
                once the collateral is read; nothing is rounded.
            from_side: where the USDC leaves.
            to_side: where it goes.
            ttl_ms: how long the signed op stays valid, in whole milliseconds,
                from 60,000 to 86,400,000 (default 600,000, ten minutes). It
                rests nowhere: the exchange applies it at once or refuses it,
                and once its ``l2ExpiryMs`` (now + ``ttl_ms``) has passed it
                refuses one it has not applied.

        Returns:
            ``{journalSeq, duplicate}`` (:class:`~loaf.models.PerpsOpAccepted`):
            the exchange applied it at ``journalSeq`` of its journal
            (``duplicate``: a re-send of one applied before).

        Raises:
            LoafConfigError: no trading key or API key; your trading key is not
                the one in force; no trading account; trading not enabled; no
                perps market listed yet.
            LoafServerError: the perps markets' reply has no usable
                collateral, or chain id.
            LoafValidationError: refused locally (``status_code`` 0, nothing
                sent) or by the exchange (400).
            LoafAPIError: 422, the exchange refused it by its rules, with the
                reason: more than your spot USDC available (your resting orders
                hold some), a pool left below initial margin, a stale price,
                the nonce, and the like. Nothing was applied.
            LoafAuthError: your API key, or the op's signature or key.
            PerpsOutcomeUnknownError: it may or may not have been applied;
                :meth:`resubmit` it, never sign it afresh.
        """
        key = self._credentials()
        value = _to_decimal(amount, "amount")
        if value <= 0:
            raise _client_validation_error(f"amount must be positive (got {amount!r})")
        source = _side(from_side, "from_side")
        target = _side(to_side, "to_side")
        if source == target:
            raise _client_validation_error(
                f"from_side and to_side must differ (both {source}): an allocate moves USDC "
                "from one to the other"
            )
        ttl = _op_ttl(ttl_ms)
        ticks = _ticks(value, amount, self._collateral())

        def message(chain_id: int, account_index: int, nonce: int, expiry_ms: int) -> bytes:
            return k1.allocate_message(
                l2_chain_id=chain_id,
                l2_account_index=account_index,
                from_side=source,
                to_side=target,
                amount=ticks,
                l2_nonce=nonce,
                l2_expiry_ms=expiry_ms,
            )

        terms = {"fromSide": source, "toSide": target, "amount": str(ticks)}
        return self._submit("/l2/allocations", "allocate", key, terms, ttl, message)

    def add_margin(
        self, amount: float | Decimal, *, pool: int = 0, ttl_ms: int | None = None
    ) -> Any:
        """Move ``amount`` USDC from your spot balance into margin pool
        ``pool`` (0, the cross pool, to 7). See :meth:`allocate`."""
        return self.allocate(amount, from_side=PERPS_SPOT_SIDE, to_side=_pool(pool), ttl_ms=ttl_ms)

    def return_margin(
        self, amount: float | Decimal, *, pool: int = 0, ttl_ms: int | None = None
    ) -> Any:
        """Move ``amount`` USDC out of margin pool ``pool`` (0 to 7) back to
        your spot balance. The exchange refuses it (422) when it would leave the
        pool below initial margin (twice its maintenance requirement), while
        the price of an active market the pool holds is stale, or above what
        the perps markets' backing can pay out. See :meth:`allocate`."""
        return self.allocate(
            amount, from_side=_pool(pool), to_side=PERPS_SPOT_SIDE, ttl_ms=ttl_ms
        )

    def exit(self, market: int, *, pool: int = 0, ttl_ms: int | None = None) -> Any:
        """``POST /l2/exits`` — close your position in an EXPIRED perps market
        ``market`` (0 to 254) held in margin pool ``pool`` (0 to 7), signed
        with your trading key.

        An expired market takes no orders: an exit is how a position leaves
        it, at the market's final index. One exit closes all of it, or, for a
        very large one, as much as one exit can carry, and the exchange closes
        the rest itself. It refuses an exit in a market that has not expired,
        or of a pool with no position there (422). ``ttl_ms`` and the answer
        are as in :meth:`allocate`, and so are the errors, but for the
        collateral: an exit moves no amount, so a perps markets reply without a
        usable collateral never blocks it.
        """
        key = self._credentials()
        index = _ranged(market, "market", 0, PERPS_LAST, "a perps market index")
        number = _pool(pool)
        ttl = _op_ttl(ttl_ms)

        def message(chain_id: int, account_index: int, nonce: int, expiry_ms: int) -> bytes:
            return k1.exit_message(
                l2_chain_id=chain_id,
                l2_account_index=account_index,
                pool=number,
                market=index,
                l2_nonce=nonce,
                l2_expiry_ms=expiry_ms,
            )

        terms = {"pool": number, "market": index}
        return self._submit("/l2/exits", "exit", key, terms, ttl, message)

    def resubmit(self, signed_body: Mapping[str, Any]) -> Any:
        """Re-send a signed allocate or exit exactly as it was (the ``signed_body``
        of a :class:`~loaf.exceptions.PerpsOutcomeUnknownError`).

        A 202 settles it: ``duplicate: True``, applied before; otherwise
        applied now. Anything else raises ``PerpsOutcomeUnknownError`` again,
        with the reason in ``last_error``: before the op's ``l2ExpiryMs`` an
        earlier attempt may still be on its way. A 422 to a re-send made once
        its ``l2ExpiryMs`` has passed (allow a few minutes for clocks) settles
        it the other way: it was never applied, and never will be. Needs your
        API key, not your trading key.
        """
        body = signed_body if isinstance(signed_body, Mapping) else {}
        kind = (
            _ALLOCATE_KEYS if all(field in body for field in _ALLOCATE_KEYS)
            else _EXIT_KEYS if all(field in body for field in _EXIT_KEYS)
            else None
        )
        if kind is None or not (
            all(isinstance(body.get(field), str) for field in _SIGNED_OP_STRS)
            and all(_is_int(body.get(field)) for field in _SIGNED_OP_INTS)
        ):
            raise _client_validation_error(
                "resubmit needs a signed allocate or exit body (its fields and signature), "
                "e.g. PerpsOutcomeUnknownError.signed_body"
            )
        path = "/l2/allocations" if kind is _ALLOCATE_KEYS else "/l2/exits"
        return self._client._send_signed(path, dict(body), ambiguous=True, perps=True)

    # -- Signing & sending --------------------------------------------------- #

    def _credentials(self) -> TradingKey:
        """The trading key that signs, once both keys an op needs are set."""
        key = self._client._l2_key
        if key is None:
            raise LoafConfigError(
                f"Moving perps margin needs your trading key: set ${ENV_SECRET_KEY} (the web "
                "app's Show trading key gives it; see the README) or pass secret_key=..."
            )
        if not self._client.api_key:
            raise LoafConfigError(
                "Moving perps margin needs your API key too: the SDK reads your account's nonce "
                f"with it, and the exchange checks the account is yours. Pass api_key=... or set "
                f"${ENV_API_KEY}."
            )
        return key

    def _submit(
        self,
        path: str,
        what: str,
        key: TradingKey,
        terms: dict[str, Any],
        ttl: int,
        message: Callable[[int, int, int, int], bytes],
    ) -> Any:
        """Read what the op signs (the chain id, your account and its nonce), sign it at
        now + ``ttl`` and send it; one op at a time, so no two sign the same nonce."""
        with self._lock:
            chain_id = self._terms()[0]
            account_index, nonce, journaled = read_signing_account(self._client)
            if journaled != key.public_key:
                raise LoafConfigError(
                    "LOAF_SECRET_KEY is not the trading key in force on your account: it was "
                    "reset in the web app, or this key belongs to another vault. Set "
                    "LOAF_SECRET_KEY to the key in force (the web app's Show trading key) and "
                    "restart."
                )
            expiry_ms = time.time_ns() // 1_000_000 + ttl
            try:
                signed = message(chain_id, account_index, nonce, expiry_ms)
            except k1.K1Error as exc:
                # The engine's own text, e.g. an account index past an op's width; nothing was sent.
                raise _client_validation_error(str(exc)) from None
            s, e = key.sign(signed, what)
            body = {
                "l2ChainId": chain_id,
                "l2AccountIndex": account_index,
                **terms,
                "l2Nonce": nonce,
                "l2ExpiryMs": expiry_ms,
                "l2SigS": s,
                "l2SigE": e,
            }
            try:
                return self._client._send_signed(path, body, perps=True)
            except LoafAPIError as exc:
                if exc.status_code < 500 and exc.message.startswith("l2 chain id:"):
                    self._market_terms = None  # the deployment moved: read it again
                raise

    def _collateral(self) -> _Collateral:
        """The collateral an allocate's amount is counted in, from the perps markets' reply. Only
        an allocate needs it: an exit, which moves no amount, never waits on it."""
        terms = self._terms()
        try:
            return _collateral_of(terms[1])
        except LoafServerError:
            with self._terms_lock:
                if self._market_terms is terms:
                    self._market_terms = None  # read the reply again next time
            raise

    def _terms(self) -> tuple[int, Any]:
        """The L2 chain id every op signs, and the perps markets' reply it came from: read once."""
        with self._terms_lock:
            if self._market_terms is not None:
                return self._market_terms
            answer = self._client._request(
                "GET", "/l2/perps/markets", auth=False, before_order=True
            )
            rows = answer.get("markets") if isinstance(answer, Mapping) else None
            if not isinstance(rows, list):
                raise LoafServerError(
                    "GET /l2/perps/markets answered no markets list", status_code=200, body=answer
                )
            if not rows:
                raise LoafConfigError(
                    "The exchange lists no perps market yet: there is no pool to move margin to "
                    "and no position to exit."
                )
            first = rows[0]
            chain_id = first.get("l2ChainId") if isinstance(first, Mapping) else None
            if not _whole(chain_id, 1, _KB_MODULUS - 1):
                raise LoafServerError(
                    "GET /l2/perps/markets answered an unusable l2ChainId",
                    status_code=200,
                    body=answer,
                )
            self._market_terms = (chain_id, answer)
            return self._market_terms


def _collateral_of(answer: Any) -> _Collateral:
    """The perps markets' ``collateral``, checked as served (Shared-Types' perpsCollateralSchema),
    or :class:`LoafServerError`: an amount is never counted in an assumed tick."""
    row = answer.get("collateral") if isinstance(answer, Mapping) else None

    def unusable(field: str) -> LoafServerError:
        return LoafServerError(
            f"GET /l2/perps/markets answered no usable collateral ({field}): an allocate's amount "
            "is counted in its decimals and tickSize, which the SDK never assumes",
            status_code=200,
            body=answer,
        )

    if not isinstance(row, Mapping):
        raise unusable("collateral")
    if row.get("assetId") != PERPS_QUOTE_ASSET_ID or not _is_int(row.get("assetId")):
        raise unusable("collateral.assetId")
    symbol = row.get("symbol")
    if not isinstance(symbol, str) or not symbol:
        raise unusable("collateral.symbol")
    decimals = row.get("decimals")
    if not _whole(decimals, 0, 255):
        raise unusable("collateral.decimals")
    tick_size = _decimal_string(row.get("tickSize"), _TICK_SIZE_LIMIT - 1)
    if tick_size is None:
        raise unusable("collateral.tickSize")
    if _decimal_string(row.get("extensionMultiplier"), _EXTENSION_LIMIT - 1) is None:
        raise unusable("collateral.extensionMultiplier")
    with localcontext(_EXACT):
        tick = Decimal(tick_size).scaleb(-decimals)
        most = (Decimal(tick_size) * (_ALLOCATE_LIMIT - 1)).scaleb(-decimals)
    return _Collateral(row, symbol, decimals, tick_size, tick, most)


def _decimal_string(value: Any, high: int) -> int | None:
    """A decimal string from 1 to ``high`` (no sign, no leading zero), as its int."""
    if not isinstance(value, str) or len(value) > len(str(high)):
        return None
    if not _BASE_UNITS_RE.fullmatch(value) or not 1 <= int(value) <= high:
        return None
    return int(value)


def _ticks(value: Decimal, given: Any, collateral: _Collateral) -> int:
    """An allocate's ``amount`` (USDC) as the whole number of ticks it signs: amount x
    10^decimals / tickSize, exactly, or refused (never rounded)."""
    symbol = collateral.symbol
    if not collateral.tick <= value <= collateral.most:
        raise _client_validation_error(
            f"amount must be from {_plain(collateral.tick)} {symbol} (one tick) to "
            f"{_plain(collateral.most, ',')} {symbol} (2^60 - 1 ticks) (got {given!r})"
        )
    ticks = Fraction(value) * 10**collateral.decimals / collateral.tick_size
    if ticks.denominator != 1:
        raise _client_validation_error(
            f"amount must be a whole number of ticks of {_plain(collateral.tick)} {symbol} "
            f"(got {given!r})"
        )
    return int(ticks)


def _plain(value: Decimal, grouping: str = "") -> str:
    """``value`` without its trailing zeros or an exponent: 0.01, 11,529,215,046,068,469.75."""
    return format(value.normalize(_EXACT), grouping + "f")


def _ranged(value: Any, field: str, low: int, high: int, what: str) -> int:
    """``value`` checked: a whole number from ``low`` to ``high``, never rounded or read from
    text."""
    whole = isinstance(value, numbers.Integral) and not isinstance(value, bool)
    if not whole or not low <= int(value) <= high:
        raise _client_validation_error(
            f"{field} must be {what}, a whole number from {low:,} to {high:,} (got {value!r})"
        )
    return int(value)


def _pool(pool: Any) -> int:
    return _ranged(pool, "pool", 0, MAX_POOL, "a margin pool (0, the cross pool, or 1 to 7)")


def _side(side: Any, field: str) -> int:
    """An allocate's side: a pool, 0 to 7, or the spot balance, 255."""
    whole = isinstance(side, numbers.Integral) and not isinstance(side, bool)
    if not whole or not (0 <= int(side) <= MAX_POOL or int(side) == PERPS_SPOT_SIDE):
        raise _client_validation_error(
            f"{field} must be a margin pool, 0 (the cross pool) to {MAX_POOL}, or "
            f"{PERPS_SPOT_SIDE} (PERPS_SPOT_SIDE, your spot USDC balance) (got {side!r})"
        )
    return int(side)


def _op_ttl(ttl_ms: Any) -> int:
    """``ttl_ms=`` of an allocate or exit checked, or the default when omitted."""
    if ttl_ms is None:
        return DEFAULT_PERPS_OP_TTL_MS
    return _ranged(
        ttl_ms, "ttl_ms", MIN_ORDER_TTL_MS, MAX_PERPS_OP_TTL_MS, "a number of milliseconds"
    )


def _list_of(body: Any, key: str) -> list[Any]:
    """``body[key]`` when it is a list, else ``[]``."""
    rows = body.get(key) if isinstance(body, Mapping) else None
    return rows if isinstance(rows, list) else []

"""Perps: your margin pools and positions, margin moves and exits.

A perp trades a perpetual future settled in USDC: its ticker is its asset's
with ``-PERP`` appended, and its ``market.type`` is ``"PERP"``. Its orders are
placed by its ticker like any other, with ``pool=`` and ``reduce_only=``
(:class:`~loaf.resources.orders.OrdersResource`); this resource reads your
positions and moves your margin, naming a perp by its ``marketIndex`` (0 to
254), as the exchange's perps API does; a perp's terms and live state are
:meth:`~loaf.resources.market.MarketResource.get`'s::

    index = loaf.market.get("MUS-PERP").market.marketIndex
    loaf.perps.add_margin(5_000, pool=0)           # 5,000 USDC: spot balance -> pool 0
    loaf.orders.limit_buy("MUS-PERP", quantity=2, price=101.5, pool=0)
    loaf.perps.position(market=index, pool=0)

**Pools.** Your perps margin is USDC held in pools: pool 0, the cross pool,
and the isolated pools 1 to 7. Each holds at most 8 positions, one per market,
margined by that pool's collateral alone, and an order trades from the pool it
names (``pool=``, 0 by default). USDC moves between your spot balance and a
pool, or between two pools, by an allocate (:meth:`PerpsResource.add_margin`,
:meth:`PerpsResource.return_margin`, :meth:`PerpsResource.allocate`). Once a
market has expired, a position in it is closed by an exit
(:meth:`PerpsResource.exit`).

**Signing.** An allocate or an exit is signed on your machine with your
trading key over the exchange's message, as an order is, and is sent with a
login token your trading key signs (the exchange checks the account is yours).
Each spends your account's next nonce, one counter your withdrawals
(:meth:`~loaf.resources.funds.FundsResource.withdraw`) spend too: the SDK reads
it (``GET /l2/keys``) for every op, and sends one op at a time per client. An op
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
collateral your portfolio summary serves (:meth:`PerpsResource.collateral`:
ticks = amount x 10^``decimals`` / ``tickSize``), and refuses an amount that is
not a whole number of ticks, or a summary without a usable collateral (it never
assumes one; an exit, which moves no amount, never needs it). The reads are the
engine's units, not dollars: every amount of 64 bits or more is a decimal
string, and :meth:`PerpsResource.positions` lists their units.
"""

from __future__ import annotations

import numbers
import threading
from decimal import Context, Decimal, localcontext
from fractions import Fraction
from typing import TYPE_CHECKING, Any, Mapping, NamedTuple

from .. import k1
from .._l2 import _is_int, _whole, submit_signed_op
from ..constants import (
    DEFAULT_PERPS_OP_TTL_MS,
    ENV_TRADING_KEY,
    MAX_PERP_MARKET_INDEX,
    MAX_PERPS_OP_TTL_MS,
    MAX_POOL,
    MIN_ORDER_TTL_MS,
    PERPS_QUOTE_ASSET_ID,
    PERPS_SPOT_SIDE,
)
from ..exceptions import (
    LoafConfigError,
    LoafServerError,
    LoafServiceUnavailableError,
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
    """A summary's perps collateral, checked: ``row`` as served, ``tick`` one tick in
    the token's units, exact."""

    row: Mapping[str, Any]
    symbol: str
    decimals: int
    tick_size: int
    tick: Decimal

_SIGNED_OP_STRS = ("l2SigS", "l2SigE")
_SIGNED_OP_INTS = ("l2ChainId", "l2AccountIndex", "l2Nonce", "l2ExpiryMs")
_ALLOCATE_KEYS = ("fromSide", "toSide", "amount")
_EXIT_KEYS = ("pool", "market")


class PerpsResource(Resource):
    def __init__(self, client: LoafClient) -> None:
        super().__init__(client)
        #: The collateral an allocate is counted in, read once (under ``_collateral_lock``).
        self._collateral_lock = threading.Lock()
        self._kept_collateral: _Collateral | None = None

    # -- Your pools and positions ------------------------------------------- #

    def collateral(self) -> Any:
        """The one asset every margin pool holds, USDC
        (:class:`~loaf.models.PerpsCollateral`): the ``collateral`` of :meth:`positions`,
        read once per client: its ``symbol`` and ``decimals``, ``tickSize`` (its base
        units per tick: an allocate moves whole ticks) and ``extensionMultiplier`` (the
        reads' extended units per tick), the last two decimal strings. Neither changes
        once the asset is registered.

        Raises:
            LoafConfigError: no trading key.
            LoafServiceUnavailableError: the exchange could not read your perps just
                now: try again in a moment.
            LoafServerError: the answer has no usable collateral.
        """
        return self._collateral().row

    def positions(self) -> Any:
        """``GET /portfolio/summary``'s ``perps``: your account's pools and their positions.

        Returns ``{collateral, l2AccountIndex, pools}``
        (:class:`~loaf.models.PerpsAccount`): :meth:`collateral`, and the pools
        that hold anything, ascending, each with its positions ascending by
        market (a perp's ``marketIndex``). Values are the engine's, as of its
        last commit, at each market's mark
        (``market.get(ticker).market.live.markPrice``), each amount of 64 bits
        or more a decimal string:

        * a pool's ``collateral``, ``value`` (the collateral plus each
          position's signed notional less its funding owed) and a position's
          ``fundingOwed`` (positive pays, negative receives) and ``notional``
          (|base| x mark x the market's ``qem``) are in the money book's
          extended units: ticks x the collateral's ``extensionMultiplier``
          (:meth:`collateral`), so USDC = extended / ``extensionMultiplier``
          x ``tickSize`` / 10^``decimals``;
        * a pool's ``requirement`` (its maintenance margin, the sum of each
          position's ``risk`` x ``notional``) is in those units x 65,536;
          ``liquidatable`` is ``value < 0`` or ``value x 65,536 <
          requirement``. Collateral can leave the pool only at K1's initial
          margin, ``value x 65,536 >= 2 x requirement``, and while the pool
          also keeps a tenth of its positions' ``notional`` and any
          initial-margin tier's extra, otherwise the exchange answers 422
          ``TRANSFER_MARGIN_FLOOR``. The three are
          ``None`` together when the engine's summary of the pool is past its
          range;
        * a position's ``ticker``, ``qem``, ``risk``, ``maxLeverage`` and ``band`` are its
          market's, so valuing it needs no market read;
        * a position's ``base`` is signed (positive long, negative short) in
          the market's base units, an order's signed quantity (tokens x 10);
          ``entryPrice`` is 10 x ``entryQuote`` / |base|, in the market's price
          units, as is ``liquidationPrice``: the mark below which (a long) or
          above which (a short) the pool is liquidatable, every other term of
          the pool held, ``None`` when it has none; ``fundingIndex`` is the
          funding index it last settled at: it owes
          ``base x (F - fundingIndex)``, F the market's live ``fundingIndex``.

        Your open orders are ``portfolio.component().openOrders``, each with its
        ``pool`` and ``reduceOnly``. Each call is a whole portfolio read: to
        follow your positions, keep what
        :meth:`~loaf.ws.client.LoafWebSocketClient.on_perps_account` pushes
        (README, **Perps**).

        Raises:
            LoafConfigError: no trading key.
            LoafAuthError: the exchange refused the login token: your trading key
                is not the one in force on its account (reset, not active yet, or
                another deployment's), or this machine's clock is off, which the
                message names.
            LoafServiceUnavailableError: the exchange could not read your perps
                just now, which never means you hold none; try again in a moment.
        """
        return _perps_of(self._client.get("/portfolio/summary"))

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
        index = _ranged(market, "market", 0, MAX_PERP_MARKET_INDEX, "a perps market index")
        held = self.pool(pool)
        for row in _list_of(held, "positions") if held is not None else ():
            if isinstance(row, Mapping) and row.get("market") == index:
                return row
        return None

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
            LoafConfigError: no trading key.
            LoafServiceUnavailableError: the exchange could not read the
                collateral just now: nothing was sent.
            LoafServerError: your summary has no usable collateral.
            LoafValidationError: refused locally (``status_code`` 0, nothing
                sent) or by the exchange (400).
            LoafAPIError: 422, the exchange refused it by its rules, with the
                reason: more than your spot USDC available (your resting orders
                hold some), a pool left below initial margin, a stale price,
                the nonce, and the like. Nothing was applied.
            LoafAuthError: the exchange refused the login token or the op's
                signature: your trading key is not the one in force on its
                account (also a key of another deployment, refused at the nonce
                read).
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
        return submit_signed_op(self._client, key, "/l2/allocations", terms, ttl, message, "perps")

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
        pool below initial margin (twice its maintenance requirement), or below
        a tenth of its positions' notional or an initial-margin tier's extra
        (``TRANSFER_MARGIN_FLOOR``), while the price of an active market the
        pool holds is stale, or above what the perps markets' backing can pay
        out. See :meth:`allocate`."""
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
        collateral: an exit moves no amount, so it never reads it.
        """
        key = self._credentials()
        index = _ranged(market, "market", 0, MAX_PERP_MARKET_INDEX, "a perps market index")
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
        return submit_signed_op(self._client, key, "/l2/exits", terms, ttl, message, "perps")

    def resubmit(self, signed_body: Mapping[str, Any]) -> Any:
        """Re-send a signed allocate or exit exactly as it was (the ``signed_body``
        of a :class:`~loaf.exceptions.PerpsOutcomeUnknownError`).

        A 202 settles it: ``duplicate: True``, applied before; otherwise
        applied now. Anything else raises ``PerpsOutcomeUnknownError`` again,
        with the reason in ``last_error``: before the op's ``l2ExpiryMs`` an
        earlier attempt may still be on its way. A 422 to a re-send made once
        its ``l2ExpiryMs`` has passed (allow a few minutes for clocks) settles
        it the other way: it was never applied, and never will be. Needs your
        trading key, for the login token: it signs nothing new.
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
        return self._client._send_signed(path, dict(body), ambiguous=True, op="perps")

    # -- Signing & sending --------------------------------------------------- #

    def _credentials(self) -> TradingKey:
        """The trading key that signs the op and the login tokens that carry it."""
        key = self._client._l2_key
        if key is None:
            raise LoafConfigError(
                f"Moving perps margin needs your trading key: set ${ENV_TRADING_KEY} to the web "
                "app's Show trading key (README §2), or pass trading_key=..."
            )
        return key

    def _collateral(self) -> _Collateral:
        """The collateral an allocate's amount is counted in, from your summary: read once, and
        again only after a read that gave none. An exit, which moves no amount, never waits on
        it."""
        with self._collateral_lock:
            if self._kept_collateral is None:
                answer = self._client._request("GET", "/portfolio/summary", before_order=True)
                self._kept_collateral = _collateral_of(answer)
            return self._kept_collateral


def _perps_of(summary: Any) -> Mapping[str, Any]:
    """A summary's ``perps``, or the refusal its ``None`` stands for: never "no positions"."""
    perps = summary.get("perps") if isinstance(summary, Mapping) else None
    if not isinstance(perps, Mapping):
        raise LoafServiceUnavailableError(
            "GET /portfolio/summary could not read your perps just now, which does not mean you "
            "hold none; try again in a moment",
            status_code=503,
            body=summary,
        )
    return perps


def _collateral_of(answer: Any) -> _Collateral:
    """A summary's perps ``collateral``, checked as served,
    or :class:`LoafServerError`: an amount is never counted in an assumed tick."""
    row = _perps_of(answer).get("collateral")

    def unusable(field: str) -> LoafServerError:
        return LoafServerError(
            f"GET /portfolio/summary answered no usable collateral ({field}): an allocate's amount "
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
    return _Collateral(row, symbol, decimals, tick_size, tick)


def _decimal_string(value: Any, high: int) -> int | None:
    """A decimal string from 1 to ``high`` (no sign, no leading zero), as its int."""
    if not isinstance(value, str) or len(value) > len(str(high)):
        return None
    if not _BASE_UNITS_RE.fullmatch(value) or not 1 <= int(value) <= high:
        return None
    return int(value)


def _ticks(value: Decimal, given: Any, collateral: _Collateral, limit: int = _ALLOCATE_LIMIT) -> int:
    """An ``amount`` of USDC (an allocate's, or a withdrawal's) as the whole number of ticks it
    signs, below ``limit`` (a power of two): amount x 10^decimals / tickSize, exactly, or refused
    (never rounded)."""
    symbol = collateral.symbol
    with localcontext(_EXACT):
        most = (Decimal(collateral.tick_size) * (limit - 1)).scaleb(-collateral.decimals)
    if not collateral.tick <= value <= most:
        raise _client_validation_error(
            f"amount must be from {_plain(collateral.tick)} {symbol} (one tick) to "
            f"{_plain(most, ',')} {symbol} (2^{limit.bit_length() - 1} - 1 ticks) "
            f"(got {given!r})"
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

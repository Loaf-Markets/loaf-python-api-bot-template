"""L2 orders: every order the Loaf API books is signed by the account's L2 trading key.

The backend refuses an order that is not L2-signed (rule L0). This module signs as the Loaf web app
signs (loafx-frontend ``apps/trade/src/l2`` placeOrder.ts, orderBody.ts, orderIndexPool.ts):

* the property's L2 market (market index, base and quote asset) comes from its detail,
  ``GET /trade/{ticker}`` (``property.l2Market``), read again at most every 30 s;
* the account, its settle epoch and the key in force come from ``GET /l2/keys``, read again after
  ``key_state_max_age_s`` and after a refusal that names the epoch, the key or the signature;
* order indices come from blocks ``POST /l2/order-indices`` reserves to the account, each index
  signed once; the engine's refusal of an index (used, outside the account's ranges, or its range
  too old for the expiry) drops the block, so the next order reserves a fresh one;
* an order signs the price and quantity it is given (:class:`~loaf.resources.orders.OrdersResource`
  works out a MARKET order's worst price), and a BUY's take-profit and stop-loss legs take the
  next indices.

The engine keys an order by its account and order index, so the client re-sends a signed body
unchanged (``LoafClient._send_signed``): a re-send is a duplicate, whereas a re-signed order would
be a second order.
"""

from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from ..constants import ENV_L2_SECRET_KEY
from ..exceptions import LoafL2Error, _client_validation_error
from .pricing import scale_price, scale_quantity
from .signer import MAX_BATCH_ORDERS, L2Signer, batch_fields, default_signer, signed_fields

# Shared-Types src/l2-constants.ts: the values the engine and the backend use.
L2_BUFFER_MS = 86_400_000
L2_MAX_ORDER_LIFE_MS = 4_261_412_864
L2_ORDER_LIFE_MARGIN_MS = 16_777_216
#: The longest expiry less now an order signs: K1's longest order life less two windows, the
#: engine's margin and one for this machine's clock (the web app's L2_MAX_SIGNED_LIFE_MS).
L2_MAX_SIGNED_LIFE_MS = L2_MAX_ORDER_LIFE_MS - 2 * L2_ORDER_LIFE_MARGIN_MS
MIN_L2_ORDER_TTL_MS = 60_000
DEFAULT_L2_ORDER_TTL_MS = 7 * 86_400_000
#: The canonical ZkLoaf L2 chain id, "loaf" (Loaf-Backend src/config/l2Settle.ts DEFAULT_L2_CHAIN_ID).
DEFAULT_L2_CHAIN_ID = 1_819_238_758
#: The highest spot taker fee in the backend's tiers, 70 bps, per million (Loaf-Backend feeTiers.ts).
DEFAULT_L2_FEE_RATE_CAP = 7_000
#: Indices one reservation asks for: the web app's block. The engine lets an account hold at most
#: 65,536 reserved indices no order used in its live ranges, and a process that exits leaves the
#: rest of its block unused, so a larger block costs restarts: 64 allows about a thousand.
DEFAULT_ORDER_INDEX_BLOCK = 64
#: ``POST /l2/order-indices`` reserves 1 to 4,096 indices a call; the engine one call per account a second.
MAX_RESERVATION = 4_096
RESERVE_RETRY_S = 1.0
MARKET_REFRESH_S = 30.0
DEFAULT_KEY_STATE_MAX_AGE_S = 60.0
MAX_12 = 4_095
MAX_20 = 1_048_575
MAX_32 = 4_294_967_295
MAX_48 = 281_474_976_710_655
MAX_SAFE_INTEGER = 2**53 - 1
MAX_ASSET_ID = 62
MAX_ORDER_INDEX = 1_099_511_627_775

#: The engine's refusals this client acts on, byte for byte (Shared-Types L2_REASONS; engine l2.rs).
INDEX_USED = "l2 order_index: already used by the account; re-sign with the next index"
INDEX_NOT_RESERVED = (
    "l2 order_index: not inside a range reserved to the account; reserve one with ReserveOrderIndices"
)
INDEX_RANGE_TOO_OLD = (
    "l2 order_index: the range was reserved more than K1's longest order life before the expiry; reserve a new one"
)
RESERVE_TOO_SOON = "l2 order_index: the account's last reservation is less than 1 s old"
INDEX_REFUSALS = frozenset({INDEX_USED, INDEX_NOT_RESERVED, INDEX_RANGE_TOO_OLD})
#: A refusal that says the account's epoch or key moved: the next order reads ``GET /l2/keys`` again.
KEY_STATE_REFUSAL_PREFIXES = ("l2 epoch:", "l2 key:", "l2 signature:")

TICKER = re.compile(r"[A-Z0-9]{1,4}")
HEX40 = re.compile(r"0x[0-9a-fA-F]{40}")
HEX80 = re.compile(r"[0-9a-f]{80}")
PUBLIC_KEY = re.compile(r"0x[0-9a-f]{80}")


def _whole(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _env_int(env: Mapping[str, str], key: str, fallback: int, lo: int, hi: int) -> int:
    raw = (env.get(key) or "").strip()
    if raw == "":
        return fallback
    try:
        value: Optional[int] = int(raw, 10)
    except ValueError:
        value = None
    if value is None or value < lo or value > hi:
        raise LoafL2Error(f"{key} must be an integer from {lo} to {hi}; received {raw}")
    return value


def _ranged(name: str, value: int, lo: int, hi: int) -> int:
    if not _whole(value) or value < lo or value > hi:
        raise LoafL2Error(f"{name} must be an integer from {lo} to {hi}; received {value!r}")
    return value


@dataclass(frozen=True)
class L2Config:
    """What the API does not serve: the L2 chain, the fee consent, the order life and the index
    block (the web app's build env, config.ts)."""

    l2_chain_id: int = DEFAULT_L2_CHAIN_ID
    fee_rate_cap: int = DEFAULT_L2_FEE_RATE_CAP
    buffer_ms: int = L2_BUFFER_MS
    order_ttl_ms: int = DEFAULT_L2_ORDER_TTL_MS
    index_block: int = DEFAULT_ORDER_INDEX_BLOCK

    def __post_init__(self) -> None:
        _ranged("l2_chain_id", self.l2_chain_id, 1, MAX_32)
        _ranged("fee_rate_cap", self.fee_rate_cap, 0, MAX_20)
        # buffer_ms may only raise the engine's BUFFER: a lower value signs expiries the engine refuses.
        _ranged("buffer_ms", self.buffer_ms, L2_BUFFER_MS, L2_MAX_SIGNED_LIFE_MS - MIN_L2_ORDER_TTL_MS)
        _ranged("order_ttl_ms", self.order_ttl_ms, MIN_L2_ORDER_TTL_MS, L2_MAX_SIGNED_LIFE_MS - self.buffer_ms)
        _ranged("index_block", self.index_block, 1, MAX_RESERVATION)

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "L2Config":
        """``LOAF_L2_CHAIN_ID``, ``LOAF_L2_FEE_RATE_CAP`` (per million: must cover the account's
        maker and taker rate), ``LOAF_L2_BUFFER_MS``, ``LOAF_L2_ORDER_TTL_MS`` (how long an order
        rests once past the buffer) and ``LOAF_L2_ORDER_INDEX_BLOCK``; each unset one is the
        default."""
        env = os.environ if env is None else env
        buffer_ms = _env_int(env, "LOAF_L2_BUFFER_MS", L2_BUFFER_MS, L2_BUFFER_MS, L2_MAX_SIGNED_LIFE_MS - MIN_L2_ORDER_TTL_MS)
        return cls(
            l2_chain_id=_env_int(env, "LOAF_L2_CHAIN_ID", DEFAULT_L2_CHAIN_ID, 1, MAX_32),
            fee_rate_cap=_env_int(env, "LOAF_L2_FEE_RATE_CAP", DEFAULT_L2_FEE_RATE_CAP, 0, MAX_20),
            buffer_ms=buffer_ms,
            order_ttl_ms=_env_int(
                env, "LOAF_L2_ORDER_TTL_MS", DEFAULT_L2_ORDER_TTL_MS, MIN_L2_ORDER_TTL_MS, L2_MAX_SIGNED_LIFE_MS - buffer_ms
            ),
            index_block=_env_int(env, "LOAF_L2_ORDER_INDEX_BLOCK", DEFAULT_ORDER_INDEX_BLOCK, 1, MAX_RESERVATION),
        )


@dataclass(frozen=True)
class L2Market:
    """A property's book on the engine and its custody (Shared-Types ``L2PropertyMarket``)."""

    ticker: str
    chain_id: int
    proxy_address: str
    market_index: int
    base_asset_id: int
    quote_asset_id: int


def parse_l2_market(ticker: str, market: Any) -> Optional[L2Market]:
    """``property.l2Market`` of ``GET /trade/{ticker}``, checked as Shared-Types
    ``l2PropertyMarketSchema`` checks it; None when the property lists no market."""
    if market is None:
        return None
    where = f"GET /trade/{ticker}: property.l2Market"
    if not isinstance(market, Mapping):
        raise LoafL2Error(f"{where} must be an object")
    keys = {"chainId", "proxyAddress", "marketIndex", "baseAssetId", "quoteAssetId"}
    if set(market) != keys:
        raise LoafL2Error(f"{where} must have exactly the keys {sorted(keys)}; received {sorted(market)}")

    def ranged(key: str, lo: int, hi: int) -> int:
        v = market[key]
        if not _whole(v) or v < lo or v > hi:
            raise LoafL2Error(f"{where}.{key} must be an integer from {lo} to {hi}")
        return v

    proxy = market["proxyAddress"]
    if not isinstance(proxy, str) or not HEX40.fullmatch(proxy):
        raise LoafL2Error(f"{where}.proxyAddress must be 0x and 40 hex")
    parsed = L2Market(
        ticker=ticker,
        chain_id=ranged("chainId", 1, MAX_SAFE_INTEGER),
        proxy_address=proxy,
        market_index=ranged("marketIndex", 0, MAX_12),
        base_asset_id=ranged("baseAssetId", 1, MAX_ASSET_ID),
        quote_asset_id=ranged("quoteAssetId", 1, MAX_ASSET_ID),
    )
    if parsed.base_asset_id == parsed.quote_asset_id:
        raise LoafL2Error(f"{where}: the base and quote asset must differ")
    return parsed


@dataclass(frozen=True)
class L2KeyState:
    """``GET /l2/keys``: the account, the journal's settle epoch (orders sign it), the key in force
    at api key 0, whether the asked key is still pending, and the next withdrawal nonce."""

    account_index: int
    epoch: Optional[int]
    journaled_key: Optional[str]
    pending: bool
    withdrawal_nonce: Optional[int] = None


def parse_key_state(raw: Any) -> L2KeyState:
    if not isinstance(raw, Mapping):
        raise LoafL2Error("GET /l2/keys did not answer an object")
    account, epoch, key = raw.get("accountIndex"), raw.get("epoch"), raw.get("journaledKey")
    nonce = raw.get("withdrawalNonce")
    if not _whole(account) or account < 0 or account > MAX_48:
        raise LoafL2Error("GET /l2/keys: accountIndex must be an integer from 0 to 2^48 - 1")
    if epoch is not None and (not _whole(epoch) or epoch < 0 or epoch > MAX_32):
        raise LoafL2Error("GET /l2/keys: epoch must be null or an integer from 0 to 2^32 - 1")
    if key is not None and (not isinstance(key, str) or not PUBLIC_KEY.fullmatch(key)):
        raise LoafL2Error("GET /l2/keys: journaledKey must be null or 0x and 80 lowercase hex")
    if nonce is not None and (not _whole(nonce) or nonce < 0 or nonce > MAX_SAFE_INTEGER):
        raise LoafL2Error("GET /l2/keys: withdrawalNonce must be a non-negative integer")
    return L2KeyState(
        account_index=account, epoch=epoch, journaled_key=key, pending=raw.get("pending") is True, withdrawal_nonce=nonce
    )


@dataclass
class _Block:
    first: int
    count: int
    next: int
    reserved_at_ms: int

    def serves(self, span: int, expiry_ms: int) -> bool:
        """``span`` indices left, for an order that expires at ``expiry_ms``: a range serves an
        expiry up to its reservation plus K1's longest order life, less one window for the clocks."""
        latest = self.reserved_at_ms + L2_MAX_ORDER_LIFE_MS - L2_ORDER_LIFE_MARGIN_MS
        return self.next + span <= self.first + self.count and expiry_ms <= latest


class OrderIndexPool:
    """The block of order indices reserved to one (account, key), handed out in order, each once
    (orderIndexPool.ts). ``reserve(count)`` is ``POST /l2/order-indices``; a block that cannot serve
    an order, or is too old for its expiry, is replaced (its last indices stay unused)."""

    def __init__(
        self,
        reserve: Callable[[int], Mapping[str, Any]],
        account_index: int,
        block_size: int = DEFAULT_ORDER_INDEX_BLOCK,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._reserve = reserve
        self.account_index = account_index
        self._block_size = block_size
        self._sleep = sleep
        self._lock = threading.Lock()
        self._block: Optional[_Block] = None

    def _reserve_block(self) -> _Block:
        try:
            r = self._reserve(self._block_size)
        except Exception as e:  # the engine's rate refusal is waited out once
            if getattr(e, "message", None) != RESERVE_TOO_SOON:
                raise
            self._sleep(RESERVE_RETRY_S)
            r = self._reserve(self._block_size)
        if not isinstance(r, Mapping):
            raise LoafL2Error(f"POST /l2/order-indices did not answer an object: {r!r}")
        first, count, at, account = r.get("firstIndex"), r.get("count"), r.get("reservedAtMs"), r.get("accountIndex")
        usable = (
            all(_whole(v) for v in (first, count, at, account))
            and account == self.account_index
            and first >= 1
            and count >= 1
            and first + count - 1 <= MAX_ORDER_INDEX
        )
        if not usable:
            raise LoafL2Error(f"POST /l2/order-indices answered a range no order can be signed in: {dict(r)!r}")
        return _Block(first=first, count=count, next=first, reserved_at_ms=at)

    def take(self, span: int, expiry_ms: int) -> int:
        """The first of ``span`` consecutive indices (an order, then its legs)."""
        with self._lock:
            if self._block is None or not self._block.serves(span, expiry_ms):
                block = self._reserve_block()
                if not block.serves(span, expiry_ms):
                    raise LoafL2Error(
                        f"POST /l2/order-indices answered a range that cannot serve {span} "
                        f"index(es) expiring at {expiry_ms}: {block!r}"
                    )
                self._block = block
            index = self._block.next
            self._block.next += span
            return index

    def drop(self) -> None:
        """The engine refused an index of the block: the next order reserves a fresh one."""
        with self._lock:
            self._block = None


@dataclass(frozen=True)
class OrderIntent:
    """One order :meth:`~loaf.resources.orders.OrdersResource.create_many` signs with others under
    one root: a LIMIT at ``price``, or a MARKET at its worst price off ``reference_price`` (default:
    the mark)."""

    ticker: str
    side: str
    quantity: float
    type: str = "LIMIT"
    price: Optional[float] = None
    reference_price: Optional[float] = None


#: An attached leg as it is signed: its key (``tp`` / ``sl``), its trigger and the price it sells at.
Leg = Tuple[str, Decimal, Decimal]


class L2Trader:
    """Signs L2 orders for a :class:`~loaf.client.LoafClient` (its session and API key).

    ``secret_key`` is the account's trading key, 80 lowercase hex (``$LOAF_L2_SECRET_KEY``;
    :mod:`loaf.l2.keys` derives the key the web app derives for the wallet, and registers it).
    """

    def __init__(
        self,
        client: Any,
        secret_key: str,
        config: Optional[L2Config] = None,
        signer: Optional[L2Signer] = None,
        clock_ms: Callable[[], int] = lambda: int(time.time() * 1000),
        key_state_max_age_s: float = DEFAULT_KEY_STATE_MAX_AGE_S,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        try:
            if not isinstance(secret_key, str) or not HEX80.fullmatch(secret_key):
                raise LoafL2Error(f"{ENV_L2_SECRET_KEY} must be 80 lowercase hex (loaf.l2.keys derives it)")
            self._signer = signer if signer is not None else default_signer()
            self.public_key: str = self._signer.public_key(secret_key)
            self._sk = secret_key
        finally:
            # Crash reporters and debuggers record a traceback's frame locals: leave no key in them.
            secret_key = ""
        self._client = client
        self.config = config if config is not None else L2Config.from_env()
        self._clock_ms = clock_ms
        self._sleep = sleep
        self._monotonic = monotonic
        self._key_state_max_age_s = key_state_max_age_s
        self._lock = threading.Lock()
        self._state: Optional[L2KeyState] = None
        self._state_at = 0.0
        self._pools: Dict[int, OrderIndexPool] = {}
        self._markets: Dict[str, tuple] = {}

    # -- the market ------------------------------------------------------------------------
    def _detail(self, ticker: str) -> Mapping[str, Any]:
        if not isinstance(ticker, str) or not TICKER.fullmatch(ticker):
            raise _client_validation_error(f"ticker must be 1-4 of A-Z 0-9; received {ticker!r}")
        # Read before an order is signed: a 429 raises at once (a late order is stale).
        detail = self._client._request("GET", f"/trade/{ticker}", auth=False, before_order=True)
        if not isinstance(detail, Mapping) or not isinstance(detail.get("property"), Mapping):
            raise LoafL2Error(f"GET /trade/{ticker} did not answer a property")
        return detail

    def _market_of(self, ticker: str, detail: Mapping[str, Any]) -> L2Market:
        market = parse_l2_market(ticker, detail["property"].get("l2Market"))
        if market is None:
            raise LoafL2Error(f"no L2 market is listed for {ticker}: GET /trade/{ticker} property.l2Market is null")
        with self._lock:
            self._markets[ticker] = (market, self._monotonic())
        return market

    def market(self, ticker: str) -> L2Market:
        """The property's L2 market, read again at most every 30 s."""
        if not isinstance(ticker, str) or not TICKER.fullmatch(ticker):
            raise _client_validation_error(f"ticker must be 1-4 of A-Z 0-9; received {ticker!r}")
        with self._lock:
            held = self._markets.get(ticker)
        if held is not None and self._monotonic() - held[1] < MARKET_REFRESH_S:
            return held[0]
        return self._market_of(ticker, self._detail(ticker))

    def mark_price(self, ticker: str) -> Any:
        """The property's market reference now (``GET /trade/{ticker}``, its ``propertyList``
        entry), which a MARKET order's worst price is taken off; it also refreshes the market."""
        detail = self._detail(ticker)
        self._market_of(ticker, detail)
        for item in detail.get("propertyList") or []:
            if isinstance(item, Mapping) and item.get("ticker") == ticker:
                mark = item.get("marketPrice")
                if isinstance(mark, (int, float)) and not isinstance(mark, bool) and mark > 0:
                    return mark
                break
        raise _client_validation_error(
            f"No market reference price for {ticker!r} right now; pass reference_price= "
            "(e.g. the markprice WebSocket value) or place a LIMIT order"
        )

    # -- the account -----------------------------------------------------------------------
    def key_state(self, refresh: bool = False) -> L2KeyState:
        """The account and its epoch, with this client's key in force; read again after
        ``key_state_max_age_s`` or after a refusal that names the epoch, the key or the signature."""
        with self._lock:
            fresh = self._monotonic() - self._state_at < self._key_state_max_age_s
            if self._state is not None and fresh and not refresh:
                return self._state
        try:
            raw = self._client._request(
                "GET", "/l2/keys", params={"apiKeyIndex": 0, "publicKey": self.public_key}, before_order=True
            )
        except Exception as e:
            if getattr(e, "status_code", None) == 404:
                raise LoafL2Error(
                    "no L2 account for this user yet: deposit to open one, then enable trading (loaf.l2.keys)"
                ) from e
            raise
        state = parse_key_state(raw)
        if state.journaled_key is None or state.epoch is None:
            raise LoafL2Error(
                "trading is not enabled on the account: register the trading key (loaf.l2.keys), "
                "or wait for the registered one to take effect"
            )
        if state.journaled_key != self.public_key:
            raise LoafL2Error(
                f"the account's trading key is {state.journaled_key}, not this client's {self.public_key}: "
                f"{ENV_L2_SECRET_KEY} is not the key in force (rotated?)"
            )
        with self._lock:
            self._state, self._state_at = state, self._monotonic()
        return state

    def _forget_key_state(self) -> None:
        with self._lock:
            self._state = None

    def _pool(self, account_index: int) -> OrderIndexPool:
        with self._lock:
            pool = self._pools.get(account_index)
            if pool is None:
                pool = OrderIndexPool(self._reserve, account_index, self.config.index_block, self._sleep)
                self._pools[account_index] = pool
            return pool

    def _reserve(self, count: int) -> Mapping[str, Any]:
        return self._client.post("/l2/order-indices", json={"count": count})

    # -- signing ---------------------------------------------------------------------------
    def _expiry_ms(self) -> int:
        return self._clock_ms() + self.config.buffer_ms + self.config.order_ttl_ms

    def _terms(
        self, market: L2Market, state: L2KeyState, side: str, price: Decimal, quantity: Decimal, index: int, expiry_ms: int
    ) -> Dict[str, Any]:
        """The 12 signed terms. The backend re-scales the body's price and quantity with
        ``Math.round`` (:mod:`loaf.l2.pricing`), so they are scaled from the same floats."""
        assert state.epoch is not None
        return {
            "l2ChainId": self.config.l2_chain_id,
            "l2MarketIndex": market.market_index,
            "l2BaseAssetId": market.base_asset_id,
            "l2QuoteAssetId": market.quote_asset_id,
            "isAsk": side == "SELL",
            "price": scale_price(float(price)),
            "l2FeeRateCap": self.config.fee_rate_cap,
            "l2AccountIndex": state.account_index,
            "l2OrderIndex": index,
            "l2Epoch": state.epoch,
            "quantity": scale_quantity(float(quantity)),
            "l2ExpiryMs": expiry_ms,
        }

    @staticmethod
    def _body(
        market: L2Market, terms: Mapping[str, Any], side: str, price: Decimal, quantity: Decimal, order_type: str
    ) -> Dict[str, Any]:
        """orderBody.ts buildSignedL2OrderBody: the plain fields the backend re-scales (unscaled)
        and the L2 fields; the signature fields are added by the caller."""
        return {
            "ticker": market.ticker,
            "price": float(price),
            "quantity": float(quantity),
            "side": side,
            "type": order_type,
            "timeInForce": "GTC",
            "l2ChainId": terms["l2ChainId"],
            "l2AccountIndex": terms["l2AccountIndex"],
            "l2MarketIndex": terms["l2MarketIndex"],
            "l2OrderIndex": terms["l2OrderIndex"],
            "l2Epoch": terms["l2Epoch"],
            "l2FeeRateCap": terms["l2FeeRateCap"],
            "l2ExpiryMs": terms["l2ExpiryMs"],
            "l2BaseAssetId": terms["l2BaseAssetId"],
            "l2QuoteAssetId": terms["l2QuoteAssetId"],
        }

    def _leg(self, terms: Mapping[str, Any], trigger: Decimal, price: Decimal, index: int) -> Dict[str, Any]:
        """orderBody.ts buildSignedL2Leg: a SELL of the order's quantity at the leg's price, with
        the order's other signed terms, its own index and the order's expiry; signed alone."""
        leg_terms = {**terms, "l2OrderIndex": index, "price": scale_price(float(price)), "isAsk": True}
        sig = signed_fields(self._signer.sign_order(self._sk, leg_terms))
        return {
            "triggerPrice": float(trigger),
            "price": float(price),
            "l2OrderIndex": index,
            "l2ExpiryMs": leg_terms["l2ExpiryMs"],
            **sig,
        }

    def sign(
        self,
        ticker: str,
        side: str,
        order_type: str,
        price: Decimal,
        quantity: Decimal,
        *,
        trigger: Optional[Decimal] = None,
        legs: Sequence[Leg] = (),
    ) -> Dict[str, Any]:
        """The signed body of one order at the price and quantity given (placeOrder.ts
        placeL2Order), for ``POST /orders`` or, with a ``trigger``, ``POST /orders/conditional``.
        ``legs`` (``tp`` first) are signed alone with the next indices."""
        market = self.market(ticker)
        state = self.key_state()
        # One clock reading: the expiry the pool checks the block against is the one the order and its legs sign.
        expiry_ms = self._expiry_ms()
        index = self._pool(state.account_index).take(1 + len(legs), expiry_ms)
        terms = self._terms(market, state, side, price, quantity, index, expiry_ms)
        body = self._body(market, terms, side, price, quantity, order_type)
        body.update(signed_fields(self._signer.sign_order(self._sk, terms)))
        if trigger is not None:
            body["triggerPrice"] = float(trigger)
        for offset, (key, leg_trigger, leg_price) in enumerate(legs, start=1):
            body[key] = self._leg(terms, leg_trigger, leg_price, index + offset)
        return body

    def sign_many(self, orders: Sequence[Tuple[str, str, str, Decimal, Decimal]]) -> List[Dict[str, Any]]:
        """Signed bodies of 1 to 256 orders, each ``(ticker, side, type, price, quantity)``, under
        ONE signature (K1's batch signature: one root over the orders, each body carrying its place
        in the tree), in the order given. One order alone is signed alone."""
        if not 1 <= len(orders) <= MAX_BATCH_ORDERS:
            raise _client_validation_error(f"a batch signs 1 to {MAX_BATCH_ORDERS} orders; received {len(orders)}")
        state = self.key_state()
        pool = self._pool(state.account_index)
        expiry_ms = self._expiry_ms()
        terms_list, bodies = [], []
        for ticker, side, order_type, price, quantity in orders:
            market = self.market(ticker)
            terms = self._terms(market, state, side, price, quantity, pool.take(1, expiry_ms), expiry_ms)
            terms_list.append(terms)
            bodies.append(self._body(market, terms, side, price, quantity, order_type))
        if len(bodies) == 1:
            bodies[0].update(signed_fields(self._signer.sign_order(self._sk, terms_list[0])))
            return bodies
        for body, fields in zip(bodies, batch_fields(self._signer.sign_batch(self._sk, terms_list))):
            body.update(fields)
        return bodies

    # -- refusals --------------------------------------------------------------------------
    def after_refusal(self, error: Optional[str], account_index: Any) -> None:
        """What a refusal of a sent order changes: an index refusal drops the account's block; an
        epoch, key or signature refusal reads the account again before the next order."""
        if not isinstance(error, str):
            return
        if error in INDEX_REFUSALS and _whole(account_index):
            self._pool(account_index).drop()
        if error.startswith(KEY_STATE_REFUSAL_PREFIXES):
            self._forget_key_state()

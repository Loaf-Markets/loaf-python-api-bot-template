"""Shared fixtures for the offline tests: an in-memory exchange that checks the login
tokens it is sent, a trading client wired to it, independent signature checks (orders,
perps allocates and exits, login tokens), and a search of traceback frames for secrets.

Only public test keys appear here: never use them for real funds.
"""

from __future__ import annotations

import json
import math
import re
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

import loaf
import loaf._l2
from loaf import LoafClient, k1
from loaf.constants import DEFAULT_TIMEOUT

BASE = "http://test/api"
KAT = json.loads(Path(__file__).with_name("k1-sign.json").read_text())

# The trading key every test signs with: K1's first known-answer key, public — never use it for
# real funds.
L2_SECRET = KAT["keys"][0]["sk"]
L2_PUBLIC_KEY = "0x" + KAT["keys"][0]["pk"]
ACCOUNT_INDEX = 5
EPOCH = 2
L2_CHAIN_ID = 4242
# The one string the web app's Show trading key gives for it: the account's on that L2 chain.
TRADING_KEY = f"loaf_tk_{L2_CHAIN_ID}_{ACCOUNT_INDEX}_{L2_SECRET}"
# OPRA's market index and its market.l2, as the exchange serves them.
MARKET_INDEX = 2050
MARKET_L2 = {
    "chainId": 421614,
    "proxyAddress": "0x" + "11" * 20,
    "l2ChainId": L2_CHAIN_ID,
    "baseAssetId": 6,
    "quoteAssetId": 3,
}
# Perp 4 (OPRA-PERP)'s market.l2.
PERP_L2 = {
    "chainId": 421614,
    "proxyAddress": "0x" + "11" * 20,
    "l2ChainId": 4242,
    "quoteAssetId": 3,
    "status": "ACTIVE",
    "qem": "1000000",
    "takerFee": 500,
    "makerFee": 200,
    "risk": 3277,
    "maxLeverage": 9,
    "band": 500,
    "flags": 1,
    "minBase": 0,
    "minQuote": 0,
    "quoteLimit": 0,
}
# The perps collateral: USDC of 6 decimals, a tick of 10,000 base units (0.01 USDC), and 100
# extended units a tick.
PERPS_COLLATERAL = {
    "assetId": 3,
    "symbol": "USDC",
    "decimals": 6,
    "tickSize": "10000",
    "extensionMultiplier": "100",
}
# A perp's market.live: open, rated, risk-only, its mark off its index.
PERPS_LIVE = {
    "marketIndex": 4,
    "status": "ACTIVE",
    "markPrice": 2510,
    "indexPrice": 2500,
    "observedAtMs": 1_800_000_000_000,
    "openInterest": "18446744073709551615",
    "fundingIndex": "-4294967296",
    "nextTickAtMs": 1_800_003_600_000,
    "fundingPeriodMs": 3_600_000,
    "fundingRateCbp": -250,
    "heldPrice": {"price": 2600, "observedAtMs": 1_800_000_001_000},
    "halted": False,
    "riskOnly": True,
    "asOfMs": 1_800_000_001_250,
    "tradingState": "OPEN",
    "stateReason": None,
}
# GET /portfolio/summary's perps: pool 0 holds a long in perp 4, pool 2 a short in perp 9.
PERPS_ACCOUNT = {
    "collateral": PERPS_COLLATERAL,
    "l2AccountIndex": ACCOUNT_INDEX,
    "pools": [
        {
            "pool": 0,
            "collateral": "5000000000000",
            "value": "5100000000000",
            "requirement": "16384000000000000",
            "liquidatable": False,
            "positions": [
                {
                    "market": 4,
                    "ticker": "OPRA-PERP",
                    "qem": "1000000",
                    "risk": 3277,
                    "maxLeverage": 9,
                    "band": 500,
                    "base": "20",
                    "fundingIndex": "4294967296",
                    "entryQuote": "203000",
                    "entryPrice": "101500",
                    "fundingOwed": "-1200",
                    "notional": "2050000000000",
                    "liquidationPrice": "52000",
                }
            ],
        },
        {
            "pool": 2,
            "collateral": "300000000000",
            "value": None,
            "requirement": None,
            "liquidatable": None,
            "positions": [
                {
                    "market": 9,
                    "ticker": "MUS-PERP",
                    "qem": "1000000",
                    "risk": 3277,
                    "maxLeverage": 9,
                    "band": 500,
                    "base": "-5",
                    "fundingIndex": "-12",
                    "entryQuote": "50000",
                    "entryPrice": "100000",
                    "fundingOwed": "35",
                    "notional": "490000000000",
                    "liquidationPrice": None,
                }
            ],
        },
    ],
}
# A market's state refusals: the exchange's 403 body (its text and code), the error it raises, and
# its state.
MARKET_STATES = [
    ("Trading is halted on this market", "MARKET_HALTED", loaf.MarketHaltedError, "MARKET_HALTED"),
    (
        "This market is outside its trading hours",
        "MARKET_CLOSED",
        loaf.MarketStateError,
        "MARKET_CLOSED",
    ),
    (
        "This market is reduce-only: it takes only orders that reduce a position",
        "REDUCE_ONLY",
        loaf.MarketStateError,
        "REDUCE_ONLY",
    ),
    (
        "This market's price is stale: it takes only orders that reduce a position",
        "PRICE_STALE",
        loaf.MarketStateError,
        "PRICE_STALE",
    ),
    (
        "This market is reduce-only while its latest price waits for review: it takes only "
        "orders that reduce a position",
        "PRICE_HELD",
        loaf.PriceHeldError,
        "REDUCE_ONLY",
    ),
]
# GET /l2/keys for the account, its trading key in force.
KEYS = {
    "accountIndex": ACCOUNT_INDEX,
    "epoch": EPOCH,
    "journaledKey": L2_PUBLIC_KEY,
    "pending": False,
    "withdrawalNonce": 0,
}
# The exchange's 422 for a perps op not signed at the account's api key 0 nonce.
NONCE_REFUSAL = "l2 money: the nonce is not the api key's nonce (tx_nonce == api_key.nonce)"
# The backend's 400 for a withdrawal whose expiry is inside the 72-hour buffer (L2_REASONS).
EXPIRY_INSIDE_BUFFER = "l2 expiry: the expiry is inside the buffer"

# The backend's 401s to a request whose login token it does not take (Shared-Types'
# L2_LOGIN_REASONS, and the account's and the key's L2_BACKEND_REASONS).
NO_BEARER = "Missing or invalid Authorization header"
MALFORMED = "l2 login: the token is not a trading-key login token"
WRONG_CHAIN = "l2 login: the token is for another exchange's L2 chain"
EXPIRED = "l2 login: the token has expired"
TOO_LONG = "l2 login: the token expires more than 15 minutes ahead"
UNKNOWN_ACCOUNT = "l2 account: no trading account has this account index"
UNKNOWN_KEY = "l2 key: no trading key is enabled for the account at this key index"
KEY_DISABLED = "l2 key: this trading key has been disabled"
BAD_SIGNATURE = "l2 signature: the Schnorr signature does not verify"
# A login token: its chain, account and expiry, then its signature, s then e.
LOGIN_TOKEN_RE = re.compile(
    r"loaf_lt_([1-9][0-9]{0,9})_(0|[1-9][0-9]{0,14})_(0|[1-9][0-9]{0,14})_([0-9a-f]{160})"
)
# The longest life the backend takes, 15 minutes after its clock (Shared-Types'
# L2_LOGIN_TOKEN_MAX_LIFE_MS).
LOGIN_TOKEN_MAX_LIFE_MS = 900_000

_ENV_VARS = ("LOAF_TRADING_KEY", "LOAF_API_BASE_URL", "LOAF_WS_URL", "LOAF_MAX_SLIPPAGE_BPS")


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Nothing a developer shell sets (the trading key, max slippage, proxy) reaches a test.
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    # websockets would send the lifecycle tests' connections to 127.0.0.1 through a proxy set in
    # the environment or the OS settings: "*" bypasses both (no_proxy wins over NO_PROXY).
    monkeypatch.setenv("no_proxy", "*")


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    # The client's retries back off with real sleeps (honouring Retry-After); skip them. Only the
    # SDK modules' `time` is replaced, so time.sleep elsewhere stays real.
    monkeypatch.setattr("loaf.client.time", SimpleNamespace(sleep=lambda seconds: None))
    # One clock for loaf._l2 (an order's expiry, the order-number pool, login tokens, a perps
    # op's or a withdrawal's expiry) and resubmit's guard: tests freeze or advance it by replacing
    # time_ns.
    slept: list[float] = []
    clock = SimpleNamespace(sleep=slept.append, slept=slept, time_ns=time.time_ns)
    monkeypatch.setattr("loaf._l2.time", clock)
    monkeypatch.setattr("loaf.resources.orders.time", clock)
    return clock


@pytest.fixture
def l2_clock(_no_sleep: SimpleNamespace) -> SimpleNamespace:
    """The SDK's clock (orders, order numbers, login tokens): ``time_ns`` to replace, and
    ``slept``, the seconds each sleep asked for (every sleep returns at once)."""
    return _no_sleep


_TYPES_L2 = object()


def freeze(l2_clock: SimpleNamespace, at_ms: int | None = None) -> int:
    """Stop the SDK's clock at ``at_ms`` (default: now), in unix ms; returns it."""
    now_ms = time.time_ns() // 1_000_000 if at_ms is None else at_ms
    l2_clock.time_ns = lambda: now_ms * 1_000_000
    return now_ms


def market_detail(
    ticker: str = "OPRA",
    *,
    market_index: Any = MARKET_INDEX,
    l2: Any = _TYPES_L2,
    market_price: float = 160,
) -> dict:
    """A ``GET /trade/{ticker}`` body with the fields order placement reads, and a perp's
    ``live``.

    ``market_index=...`` or ``l2=...`` leaves that field out. ``l2`` defaults to its
    type's: ``PERP_L2`` for a perp (its ticker ends in -PERP), else ``MARKET_L2``.
    """
    perp = ticker.endswith("-PERP")
    if l2 is _TYPES_L2:
        l2 = PERP_L2 if perp else MARKET_L2
    market: dict[str, Any] = {
        "marketIndex": market_index,
        "ticker": ticker,
        "type": "PERP" if perp else "SPOT",
        "status": "LIVE",
        "isHalted": False,
        "tradingStartsAt": None,
        "l2": l2,
    }
    market = {k: v for k, v in market.items() if v is not ...}
    if perp:
        market["live"] = PERPS_LIVE
    listed = [{
        "ticker": ticker,
        "type": "PERP" if perp else "SPOT",
        "maxLeverage": PERP_L2["maxLeverage"] if perp else None,
        "marketPrice": market_price,
    }]
    if ticker != "MUS":  # another market ahead of it: the reference is found by its ticker
        listed.insert(
            0, {"ticker": "MUS", "type": "SPOT", "maxLeverage": None, "marketPrice": 50}
        )
    return {"market": market, "marketList": listed}


class FakeExchange:
    """The exchange's routes an order uses, in memory, for ``httpx.MockTransport``.

    * Every route but ``GET /trade…`` (public) and the placements (signed) takes a
      login token, ``Authorization: Bearer <token>``: one the exchange does not take
      (:func:`login_refusal`, the account's api key 0 in force ``key_in_force``, the
      clock ``now_ms``, ``None``: the SDK's own) is answered 401 with the backend's
      text, before anything queued. A signature is checked once per key, as the
      backend caches it.
    * ``GET /trade`` lists ``details``; ``GET /trade/{ticker}`` serves
      ``details[ticker]``, and an unknown ticker is a 404 ``Market not found``.
      Replace an entry to change what they serve.
    * ``GET /l2/keys`` serves ``keys``.
    * ``POST /l2/order-indices`` reserves the count asked for (201), from 4097
      up, for the account, at ``reserved_at_ms`` (``None``: the real clock's now).
    * ``POST /orders`` and ``/orders/conditional`` place the order: 200 with a
      fresh ``orderId`` (and one per attached leg).
    * ``GET /portfolio/summary`` serves no balances or holdings, and as its
      ``perps`` ``perps_account`` with ``perps_collateral`` as its collateral
      (``perps_account`` ``None``: ``perps`` null, as when the exchange could not
      read it).
    * ``GET /portfolio/component`` serves as its ``perpsLeverage`` the
      ``perps_leverage`` saved.
    * ``PUT /portfolio/perps-leverage`` saves its body's leverage in
      ``perps_leverage`` (by ticker) and answers the body.
    * ``POST /l2/allocations``, ``/l2/exits`` and ``/l2/withdrawals`` apply a signed
      op at the account's nonce (``keys["withdrawalNonce"]``, which moves up by one,
      one counter for the three): 202 with the next ``journalSeq``; a re-send of an
      applied op is its duplicate, and any other op off the nonce is refused (422). A
      withdrawal is first refused 400 when its expiry is inside the buffer (by
      ``now_ms``), as the backend does, and its 202 echoes its terms with
      ``status`` PENDING.

    ``queue(path, *replies)`` makes ``path`` (without ``/api``) answer with each
    reply in turn before any of that, once its login token is taken: an
    ``httpx.Response``, or an exception to raise. Every request is recorded in
    :attr:`requests`. Thread-safe.
    """

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.details: dict[str, dict] = {"OPRA": market_detail()}
        self.keys: dict[str, Any] = dict(KEYS)
        self.key_in_force: str | None = L2_PUBLIC_KEY
        self.now_ms: int | None = None
        self.reserved_at_ms: int | None = None
        self._verified: set[tuple[str, str]] = set()
        self._queues: dict[str, list[Any]] = {}
        self._lock = threading.Lock()
        self._next_index = 4097
        self._next_id = 0
        self.perps_collateral: dict[str, Any] | None = dict(PERPS_COLLATERAL)
        self.perps_account: dict[str, Any] | None = json.loads(json.dumps(PERPS_ACCOUNT))
        self.perps_leverage: dict[str, int] = {}
        # The ops applied, by nonce: (their body, their journal seq).
        self.applied: dict[int, tuple[bytes, int]] = {}
        self._next_seq = 1000

    def queue(self, path: str, *replies: Any) -> None:
        with self._lock:
            self._queues.setdefault("/api" + path, []).extend(replies)

    def handler(self, request: httpx.Request) -> httpx.Response:
        with self._lock:
            self.requests.append(request)
            reply = self._refusal(request)
            if reply is None:
                queue = self._queues.get(request.url.path)
                reply = queue.pop(0) if queue else self._serve(request)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def _refusal(self, request: httpx.Request) -> httpx.Response | None:
        """The 401 of a request whose login token the exchange does not take, if any."""
        route = request.method + " " + request.url.path[len("/api") :]
        if route.startswith("GET /trade") or route in ("POST /orders", "POST /orders/conditional"):
            return None
        bearer = request.headers.get("authorization", "")
        if not bearer.startswith("Bearer "):
            return httpx.Response(401, json={"error": NO_BEARER})
        now_ms = self.now_ms if self.now_ms is not None else loaf._l2.time.time_ns() // 1_000_000
        reason = login_refusal(
            bearer[len("Bearer ") :], self.key_in_force, now_ms, verified=self._verified
        )
        return None if reason is None else httpx.Response(401, json={"error": reason})

    def _serve(self, request: httpx.Request) -> httpx.Response:
        route = request.method + " " + request.url.path[len("/api") :]
        if route == "GET /trade":
            return httpx.Response(200, json=self._listing())
        if route.startswith("GET /trade/"):
            detail = self.details.get(route[len("GET /trade/") :])
            if detail is None:
                return httpx.Response(404, json={"error": "Market not found"})
            return httpx.Response(200, json=detail)
        if route == "GET /l2/keys":
            return httpx.Response(200, json=self.keys)
        if route == "POST /l2/order-indices":
            count = json.loads(request.content)["count"]
            first, self._next_index = self._next_index, self._next_index + count
            reserved_at = self.reserved_at_ms
            if reserved_at is None:
                reserved_at = time.time_ns() // 1_000_000
            return httpx.Response(
                201,
                json={
                    "accountIndex": ACCOUNT_INDEX,
                    "firstIndex": first,
                    "count": count,
                    "reservedAtMs": reserved_at,
                },
            )
        if route == "GET /portfolio/summary":
            perps = self.perps_account and {**self.perps_account, "collateral": self.perps_collateral}
            return httpx.Response(200, json={"balances": [], "holdings": [], "perps": perps})
        if route == "GET /portfolio/component":
            leverage = [{"ticker": t, "leverage": v} for t, v in sorted(self.perps_leverage.items())]
            return httpx.Response(200, json={"perpsLeverage": leverage})
        if route == "PUT /portfolio/perps-leverage":
            body = json.loads(request.content)
            self.perps_leverage[body["ticker"]] = body["leverage"]
            return httpx.Response(200, json=body)
        if route in ("POST /l2/allocations", "POST /l2/exits"):
            return self._apply(request.content)
        if route == "POST /l2/withdrawals":
            body = json.loads(request.content)
            now_ms = self.now_ms if self.now_ms is not None else loaf._l2.time.time_ns() // 1_000_000
            if body["l2ExpiryMs"] - loaf.L2_BUFFER_MS < now_ms:
                return httpx.Response(400, json={"error": EXPIRY_INSIDE_BUFFER})
            reply = self._apply(request.content)
            if reply.status_code != 202:
                return reply
            return httpx.Response(
                202,
                json={
                    **reply.json(),
                    "accountIndex": body["l2AccountIndex"],
                    "assetId": body["l2AssetId"],
                    "amount": int(body["amount"]),
                    "nonce": body["l2Nonce"],
                    "status": {"status": "PENDING"},
                },
            )
        if route == "POST /orders/conditional":
            self._next_id += 1
            return httpx.Response(200, json={"success": True, "orderId": self._next_id})
        if route == "POST /orders":
            body = json.loads(request.content)
            order_id = self._next_id + 1
            self._next_id = order_id + sum(leg in body for leg in ("tp", "sl"))
            legs = list(range(order_id + 1, self._next_id + 1))
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "orderId": order_id,
                    "status": "OPEN",
                    "quantityLeft": body["quantity"],
                    "duplicate": False,
                    "attachedLegIds": legs,
                },
            )
        return httpx.Response(404, json={"error": "Not found"})

    def _apply(self, content: bytes) -> httpx.Response:
        """A signed perps op: applied once, at the account's nonce (signatures are not
        checked here: verify_op does that)."""
        nonce = json.loads(content)["l2Nonce"]
        if nonce in self.applied:
            body, seq = self.applied[nonce]
            if body == content:
                return httpx.Response(202, json={"journalSeq": seq, "duplicate": True})
        if nonce != self.keys["withdrawalNonce"]:
            return httpx.Response(422, json={"error": NONCE_REFUSAL})
        self._next_seq += 1
        self.applied[nonce] = (content, self._next_seq)
        self.keys["withdrawalNonce"] = nonce + 1
        return httpx.Response(202, json={"journalSeq": self._next_seq, "duplicate": False})

    def _listing(self) -> dict:
        rows = []
        for ticker, detail in self.details.items():
            market = detail["market"]
            rows.append({
                "ticker": ticker,
                "status": market["status"],
                "isHalted": market["isHalted"],
                "tradingStartsAt": market.get("tradingStartsAt"),
                "hasL2Market": market.get("l2") is not None,
            })
        return {"markets": rows}

    def posts(self, path: str) -> list[bytes]:
        """The raw bodies POSTed to ``path`` (e.g. ``"/orders"``), in order."""
        return [
            r.content for r in self.requests if r.method == "POST" and r.url.path == "/api" + path
        ]

    def bodies(self, path: str) -> list[dict]:
        return [json.loads(content) for content in self.posts(path)]

    def gets(self, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == "GET" and r.url.path == "/api" + path]


def routes(requests: list[httpx.Request]) -> list[str]:
    """Each request as ``"METHOD /path"`` (without ``/api``)."""
    return [f"{r.method} {r.url.path[len('/api') :]}" for r in requests]


@pytest.fixture
def exchange() -> FakeExchange:
    return FakeExchange()


@pytest.fixture
def trading_client(exchange: FakeExchange):
    """``trading_client(trading_key=..., **LoafClient kwargs)`` -> a client that signs
    with ``trading_key`` (``TRADING_KEY`` by default) and talks to ``exchange``."""

    def make(*, trading_key: str | None = TRADING_KEY, **kwargs: Any) -> LoafClient:
        # The same timeout a LoafClient gives the httpx client it builds itself.
        http = httpx.Client(
            transport=httpx.MockTransport(exchange.handler), base_url=BASE, timeout=DEFAULT_TIMEOUT
        )
        return LoafClient(trading_key, base_url=BASE, http_client=http, **kwargs)

    return make


def js_round(value: float, scale: int) -> int:
    """The exchange's ``Math.round(value * scale)`` of a JSON number."""
    return math.floor(value * scale + 0.5)


def verify_order(body: dict, leg: str | None = None) -> None:
    """Check the signature of a posted ``body`` (or of its ``leg``, ``"tp"`` / ``"sl"``)
    under ``L2_PUBLIC_KEY``, or raise :class:`loaf.k1.K1Error`.

    The 12 signed terms are rebuilt from the JSON as the exchange reads it, not
    from the SDK's own values: price x 1000 and quantity x 10 rounded like
    ``Math.round``; a leg signs an order of the parent's quantity on the other
    side, at its own price, with its own order index and expiry; a conditional
    signs the order it books (its own side and price). A perps order signs its
    ``l2Pool`` and ``l2ReduceOnly`` as its base-asset field, and a perps leg the
    parent's ``l2Pool`` with the reduce-only bit (Shared-Types' signedLegSchema).
    """
    signed = body if leg is None else body[leg]
    if "l2Pool" in body:
        # The pool byte, built here and not by the SDK: the pool in bits 0-2, reduce-only bit 3.
        reduce_only = leg is not None or body["l2ReduceOnly"] is True
        base_field = body["l2Pool"] + (8 if reduce_only else 0)
    else:
        base_field = body["l2BaseAssetId"]
    message = k1.order_message(
        l2_chain_id=body["l2ChainId"],
        l2_market_index=body["l2MarketIndex"],
        l2_base_asset_id=base_field,
        l2_quote_asset_id=body["l2QuoteAssetId"],
        is_ask=(body["side"] == "SELL") != (leg is not None),
        price=js_round(signed["price"], 1000),
        l2_fee_rate_cap=body["l2FeeRateCap"],
        l2_account_index=body["l2AccountIndex"],
        l2_order_index=signed["l2OrderIndex"],
        l2_epoch=body["l2Epoch"],
        quantity=js_round(body["quantity"], 10),
        l2_expiry_ms=signed["l2ExpiryMs"],
    )
    k1.verify(
        bytes.fromhex(L2_PUBLIC_KEY[2:]),
        message,
        bytes.fromhex(signed["l2SigS"]),
        bytes.fromhex(signed["l2SigE"]),
    )


def verify_op(body: dict) -> None:
    """Check the signature of a posted perps allocate or exit, or withdrawal, ``body``
    under ``L2_PUBLIC_KEY``, or raise :class:`loaf.k1.K1Error`: its message rebuilt from
    the JSON, the amount from its decimal string."""
    if "l2Route" in body:
        assert isinstance(body["amount"], str)
        message = k1._withdraw_message(
            body["l2ChainId"],
            body["l2AccountIndex"],
            body["l2ApiKeyIndex"],
            body["l2AssetId"],
            body["l2Route"],
            int(body["amount"]),
            body["l2Nonce"],
            body["l2ExpiryMs"],
        )
    elif "fromSide" in body:
        assert isinstance(body["amount"], str)
        message = k1._allocate_message(
            body["l2ChainId"],
            body["l2AccountIndex"],
            body["fromSide"],
            body["toSide"],
            int(body["amount"]),
            body["l2Nonce"],
            body["l2ExpiryMs"],
        )
    else:
        message = k1._exit_message(
            body["l2ChainId"],
            body["l2AccountIndex"],
            body["pool"],
            body["market"],
            body["l2Nonce"],
            body["l2ExpiryMs"],
        )
    k1.verify(
        bytes.fromhex(L2_PUBLIC_KEY[2:]),
        message,
        bytes.fromhex(body["l2SigS"]),
        bytes.fromhex(body["l2SigE"]),
    )


def verify_login(token: str, public_key: str = L2_PUBLIC_KEY) -> dict[str, int]:
    """Check a login token's signature under ``public_key``, its message rebuilt from
    the token's own fields as the exchange rebuilds it, or raise
    :class:`loaf.k1.K1Error`. Returns the fields: ``l2ChainId``, ``l2AccountIndex`` and
    ``l2ExpiryMs``."""
    match = LOGIN_TOKEN_RE.fullmatch(token)
    assert match is not None, "not a login token"
    chain_id, account_index, expiry_ms = (int(field) for field in match.group(1, 2, 3))
    signature = match.group(4)
    message = k1.login_message(
        l2_chain_id=chain_id, l2_account_index=account_index, l2_expiry_ms=expiry_ms
    )
    k1.verify(
        bytes.fromhex(public_key[2:]),
        message,
        bytes.fromhex(signature[:80]),
        bytes.fromhex(signature[80:]),
    )
    return {"l2ChainId": chain_id, "l2AccountIndex": account_index, "l2ExpiryMs": expiry_ms}


def login_refusal(
    token: str,
    key_in_force: str | None,
    now_ms: int,
    *,
    verified: set[tuple[str, str]] | None = None,
) -> str | None:
    """Why the exchange refuses ``token``, as the backend checks it, cheapest first: its
    format, its chain (``L2_CHAIN_ID``), its life at ``now_ms``, its account (only
    ``ACCOUNT_INDEX`` exists), the account's api key 0 in force (``key_in_force``;
    ``None``: no key), then the signature. ``None`` when it takes the token. A token in
    ``verified`` under that key skips the signature, and one that verifies is added."""
    match = LOGIN_TOKEN_RE.fullmatch(token)
    if match is None:
        return MALFORMED
    chain_id, account_index, expiry_ms = (int(field) for field in match.group(1, 2, 3))
    if chain_id != L2_CHAIN_ID:
        return WRONG_CHAIN
    if expiry_ms <= now_ms:
        return EXPIRED
    if expiry_ms - now_ms > LOGIN_TOKEN_MAX_LIFE_MS:
        return TOO_LONG
    if account_index != ACCOUNT_INDEX:
        return UNKNOWN_ACCOUNT
    if key_in_force is None:
        return UNKNOWN_KEY
    if int(key_in_force, 16) == 0:
        return KEY_DISABLED
    if verified is None or (token, key_in_force) not in verified:
        try:
            verify_login(token, key_in_force)
        except k1.K1Error as exc:
            return str(exc)
        if verified is not None:
            verified.add((token, key_in_force))
    return None


def secret_locals(exc: BaseException, *secrets: str) -> list[str]:
    """``frame:name`` of each local of the SDK's frames in ``exc``'s traceback that holds
    one of ``secrets`` (lowercase: hex, a trading key or a login token), as text, as bytes
    (raw or hex) or, for a trading key's secret (80 hex), as the int k1 turns it into:
    directly, among a dict's, tuple's or list's items (``**kwargs``, ``*args``), as a regex
    match's string, or among an object's attributes. Crash reporters and debuggers record
    these locals."""
    scalars = {int.from_bytes(bytes.fromhex(s), "little") for s in secrets if len(s) == 80}
    found = []
    tb = exc.__traceback__
    while tb is not None:
        frame = tb.tb_frame
        if frame.f_globals.get("__name__", "").startswith("loaf"):
            for name, value in frame.f_locals.items():
                if isinstance(value, dict):
                    held = list(value.values())
                elif isinstance(value, (tuple, list)):
                    held = list(value)
                elif isinstance(value, re.Match):
                    held = [value.string]
                else:
                    held = [value]
                held += getattr(value, "__dict__", {}).values()
                for item in held:
                    if isinstance(item, (bytes, bytearray)):
                        item = f"{item.hex()} {item.decode('latin-1')}"
                    if (isinstance(item, int) and item in scalars) or (
                        isinstance(item, str) and any(s in item.lower() for s in secrets)
                    ):
                        found.append(f"{frame.f_code.co_name}:{name}")
                        break
        tb = tb.tb_next
    return found

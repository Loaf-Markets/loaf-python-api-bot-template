"""Shared fixtures for the offline tests: an in-memory exchange, a trading client
wired to it, independent signature checks (orders, perps allocates and exits), and
a search of traceback frames for secrets.

Only public test keys appear here: never use them for real funds.
"""

from __future__ import annotations

import json
import math
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

import loaf
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
# A property's l2Market as the exchange serves it: a spot market.
MARKET = {
    "kind": "spot",
    "chainId": 421614,
    "proxyAddress": "0x" + "11" * 20,
    "l2ChainId": 4242,
    "marketIndex": 2050,
    "baseAssetId": 6,
    "quoteAssetId": 3,
}
# A perps market, as a perps property's l2Market and GET /l2/perps/markets serve it.
PERPS_MARKET = {
    "kind": "perps",
    "chainId": 421614,
    "proxyAddress": "0x" + "11" * 20,
    "l2ChainId": 4242,
    "marketIndex": 4,
    "quoteAssetId": 3,
    "status": "ACTIVE",
    "sem": 1000,
    "qem": "1000000",
    "takerFee": 500,
    "makerFee": 200,
    "risk": 3277,
    "band": 500,
    "flags": 1,
    "oiCap": 1_000_000_000,
    "minBase": 0,
    "minQuote": 0,
    "quoteLimit": 0,
}
# GET /l2/perps/positions: pool 0 holds a long in market 4, pool 2 a short in market 9.
PERPS_ACCOUNT = {
    "l2AccountIndex": ACCOUNT_INDEX,
    "pools": [
        {
            "pool": 0,
            "collateral": "5000000000000",
            "value": "5100000000000",
            "requirement": "16384000000000000",
            "liquidatable": False,
            "initialMarginOk": True,
            "positions": [
                {
                    "market": 4,
                    "base": "20",
                    "fundingIndex": "4294967296",
                    "entryQuote": "203000",
                    "entryPrice": "101500",
                    "fundingOwed": "-1200",
                    "notional": "2050000000000",
                    "liquidationPrice": "52000",
                    "adlRank": 2,
                }
            ],
        },
        {
            "pool": 2,
            "collateral": "300000000000",
            "value": None,
            "requirement": None,
            "liquidatable": None,
            "initialMarginOk": None,
            "positions": [
                {
                    "market": 9,
                    "base": "-5",
                    "fundingIndex": "-12",
                    "entryQuote": "50000",
                    "entryPrice": "100000",
                    "fundingOwed": "35",
                    "notional": "490000000000",
                    "liquidationPrice": None,
                    "adlRank": None,
                }
            ],
        },
    ],
}
# A perps market's state refusals: the exchange's 403 text, the error it raises, and its state.
MARKET_STATES = [
    ("Trading is halted on this market", loaf.MarketHaltedError, "MARKET_HALTED"),
    ("This market is outside its trading hours", loaf.MarketStateError, "MARKET_CLOSED"),
    (
        "This market is reduce-only: it takes only orders that reduce a position",
        loaf.MarketStateError,
        "REDUCE_ONLY",
    ),
    (
        "This market's price is stale: it takes only orders that reduce a position",
        loaf.MarketStateError,
        "PRICE_STALE",
    ),
    (
        "This market is reduce-only while its latest price waits for review: it takes only "
        "orders that reduce a position",
        loaf.PriceHeldError,
        "REDUCE_ONLY",
    ),
]
# GET /l2/keys for an account whose trading key is in force.
KEYS = {
    "accountIndex": ACCOUNT_INDEX,
    "epoch": EPOCH,
    "journaledKey": L2_PUBLIC_KEY,
    "pending": False,
    "withdrawalNonce": 0,
}
# The 404 text of GET /l2/keys and POST /l2/order-indices for a user with no trading account.
NO_ACCOUNT = "l2 account: no L2 account for this user"
# The exchange's 422 for a perps op not signed at the account's api key 0 nonce.
NONCE_REFUSAL = "l2 money: the nonce is not the api key's nonce (tx_nonce == api_key.nonce)"

_ENV_VARS = (
    "LOAF_API_KEY",
    "LOAF_SECRET_KEY",
    "LOAF_WALLET_PRIVATE_KEY",
    "LOAF_API_BASE_URL",
    "LOAF_WS_URL",
    "LOAF_MAX_SLIPPAGE_BPS",
)
_NAMES = {"MUS": "Musgrave", "OPRA": "Opera"}


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Nothing a developer shell sets (credentials, max slippage, proxy, colours) reaches a test.
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    # websockets would send the lifecycle tests' connections to 127.0.0.1 through a proxy set in
    # the environment or the OS settings: "*" bypasses both (no_proxy wins over NO_PROXY).
    monkeypatch.setenv("no_proxy", "*")
    # Python 3.14's argparse colours its usage text, even into a pipe, under FORCE_COLOR or
    # PYTHON_COLORS=1: PYTHON_COLORS=0 wins over both, here and in the helper's subprocess test.
    monkeypatch.setenv("PYTHON_COLORS", "0")


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    # The client's retries back off with real sleeps (honouring Retry-After); skip them. Only the
    # SDK modules' `time` is replaced, so time.sleep elsewhere stays real.
    monkeypatch.setattr("loaf.client.time", SimpleNamespace(sleep=lambda seconds: None))
    # One clock for loaf._l2 (an order's expiry, the order-number pool) and resubmit's guard:
    # tests freeze or advance it by replacing time_ns.
    slept: list[float] = []
    clock = SimpleNamespace(sleep=slept.append, slept=slept, time_ns=time.time_ns)
    monkeypatch.setattr("loaf._l2.time", clock)
    monkeypatch.setattr("loaf.resources.orders.time", clock)
    monkeypatch.setattr("loaf.resources.perps.time", clock)  # an allocate's or exit's expiry
    return clock


@pytest.fixture
def l2_clock(_no_sleep: SimpleNamespace) -> SimpleNamespace:
    """The SDK's order clock: ``time_ns`` to replace, and ``slept``, the seconds each sleep
    asked for (every sleep returns at once)."""
    return _no_sleep


def property_detail(
    ticker: str = "OPRA", *, l2_market: Any = MARKET, market_price: float = 160
) -> dict:
    """A ``GET /trade/{ticker}`` body with the fields order placement reads.

    ``l2_market=...`` leaves ``l2Market`` out, as a server that predates the L2
    exchange does.
    """
    prop: dict[str, Any] = {
        "ticker": ticker,
        "tokenName": _NAMES.get(ticker, ticker),
        "status": "LIVE",
        "isHalted": False,
    }
    if l2_market is not ...:
        prop["l2Market"] = l2_market
    listed = [{"ticker": ticker, "tokenName": prop["tokenName"], "marketPrice": market_price}]
    if ticker != "MUS":  # another property ahead of it: the reference is found by its ticker
        listed.insert(0, {"ticker": "MUS", "tokenName": "Musgrave", "marketPrice": 50})
    return {"property": prop, "propertyList": listed}


class FakeExchange:
    """The exchange's routes an order uses, in memory, for ``httpx.MockTransport``.

    * ``GET /trade`` lists ``details``; ``GET /trade/{ticker}`` serves
      ``details[ticker]``, and an unknown ticker is a 404 ``Property not found``.
      Replace an entry to change what they serve.
    * ``GET /l2/keys`` serves ``keys``; ``None`` is the 404 of a user with no
      trading account.
    * ``POST /l2/order-indices`` reserves the count asked for (201), from 4097
      up, at ``reserved_at_ms`` (``None``: the real clock's now).
    * ``POST /orders`` and ``/orders/conditional`` place the order: 200 with a
      fresh ``orderId`` (and one per attached leg).
    * ``GET /l2/perps/markets`` lists ``perps_markets``; ``GET /l2/perps/positions``
      serves ``perps_account`` (404 without a trading account).
    * ``POST /l2/allocations`` and ``/l2/exits`` apply a signed op at the account's
      nonce (``keys["withdrawalNonce"]``, which moves up by one): 202 with the next
      ``journalSeq``; a re-send of an applied op is its duplicate, and any other op
      off the nonce is refused (422).

    ``queue(path, *replies)`` makes ``path`` (without ``/api``) answer with each
    reply in turn before any of that: an ``httpx.Response``, or an exception to
    raise. Every request is recorded in :attr:`requests`. Thread-safe.
    """

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.details: dict[str, dict] = {"OPRA": property_detail()}
        self.keys: dict[str, Any] | None = dict(KEYS)
        self.reserved_at_ms: int | None = None
        self._queues: dict[str, list[Any]] = {}
        self._lock = threading.Lock()
        self._next_index = 4097
        self._next_id = 0
        self.perps_markets: list[dict[str, Any]] = [dict(PERPS_MARKET)]
        self.perps_account: dict[str, Any] = json.loads(json.dumps(PERPS_ACCOUNT))
        # The ops applied, by nonce: (their body, their journal seq).
        self.applied: dict[int, tuple[bytes, int]] = {}
        self._next_seq = 1000

    def queue(self, path: str, *replies: Any) -> None:
        with self._lock:
            self._queues.setdefault("/api" + path, []).extend(replies)

    def handler(self, request: httpx.Request) -> httpx.Response:
        with self._lock:
            self.requests.append(request)
            queue = self._queues.get(request.url.path)
            reply = queue.pop(0) if queue else self._serve(request)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def _serve(self, request: httpx.Request) -> httpx.Response:
        route = request.method + " " + request.url.path[len("/api") :]
        if route == "GET /trade":
            return httpx.Response(200, json=self._listing())
        if route.startswith("GET /trade/"):
            detail = self.details.get(route[len("GET /trade/") :])
            if detail is None:
                return httpx.Response(404, json={"error": "Property not found"})
            return httpx.Response(200, json=detail)
        if route == "GET /l2/keys":
            if self.keys is None:
                return httpx.Response(404, json={"error": NO_ACCOUNT})
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
                    "accountIndex": (self.keys or {}).get("accountIndex", ACCOUNT_INDEX),
                    "firstIndex": first,
                    "count": count,
                    "reservedAtMs": reserved_at,
                },
            )
        if route == "GET /l2/perps/markets":
            return httpx.Response(200, json={"markets": self.perps_markets})
        if route == "GET /l2/perps/positions":
            if self.keys is None:
                return httpx.Response(404, json={"error": NO_ACCOUNT})
            return httpx.Response(200, json=self.perps_account)
        if route in ("POST /l2/allocations", "POST /l2/exits"):
            return self._apply(request.content)
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
        if self.keys is None or nonce != self.keys["withdrawalNonce"]:
            return httpx.Response(422, json={"error": NONCE_REFUSAL})
        self._next_seq += 1
        self.applied[nonce] = (content, self._next_seq)
        self.keys["withdrawalNonce"] = nonce + 1
        return httpx.Response(202, json={"journalSeq": self._next_seq, "duplicate": False})

    def _listing(self) -> dict:
        rows = []
        for ticker, detail in self.details.items():
            prop = detail["property"]
            row = {"ticker": ticker, "status": prop["status"]}
            if "l2Market" in prop:  # a server that predates the L2 exchange lists no hasL2Market
                row["hasL2Market"] = prop["l2Market"] is not None
            rows.append(row)
        return {"properties": rows}

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
    """``trading_client(api_key=..., secret_key=..., **LoafClient kwargs)`` -> a client
    that signs with ``secret_key`` and talks to ``exchange``."""

    def make(
        *, api_key: str | None = "testkey", secret_key: str | None = L2_SECRET, **kwargs: Any
    ) -> LoafClient:
        # The same timeout a LoafClient gives the httpx client it builds itself.
        http = httpx.Client(
            transport=httpx.MockTransport(exchange.handler), base_url=BASE, timeout=DEFAULT_TIMEOUT
        )
        return LoafClient(
            api_key=api_key, secret_key=secret_key, base_url=BASE, http_client=http, **kwargs
        )

    return make


def js_round(value: float, scale: int) -> int:
    """The exchange's ``Math.round(value * scale)`` of a JSON number."""
    return math.floor(value * scale + 0.5)


def verify_order(body: dict, leg: str | None = None) -> None:
    """Check the signature of a posted ``body`` (or of its ``leg``, ``"tp"`` / ``"sl"``)
    under ``L2_PUBLIC_KEY``, or raise :class:`loaf.k1.K1Error`.

    The 12 signed terms are rebuilt from the JSON as the exchange reads it, not
    from the SDK's own values: price x 1000 and quantity x 10 rounded like
    ``Math.round``; a leg signs a SELL of the parent's quantity at its own price,
    with its own order index and expiry; a conditional signs the order it books
    (its own side and price). A perps order signs its ``l2Pool`` and
    ``l2ReduceOnly`` as its base-asset field.
    """
    signed = body if leg is None else body[leg]
    if "l2Pool" in body:
        base_field = k1.perps_base_field(body["l2Pool"], body["l2ReduceOnly"])
    else:
        base_field = body["l2BaseAssetId"]
    message = k1.order_message(
        l2_chain_id=body["l2ChainId"],
        l2_market_index=body["l2MarketIndex"],
        l2_base_asset_id=base_field,
        l2_quote_asset_id=body["l2QuoteAssetId"],
        is_ask=leg is not None or body["side"] == "SELL",
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
    """Check the signature of a posted perps allocate or exit ``body`` under
    ``L2_PUBLIC_KEY``, or raise :class:`loaf.k1.K1Error`: its message rebuilt from the
    JSON, the allocate's amount from its decimal string."""
    if "fromSide" in body:
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


def secret_locals(exc: BaseException, *secrets: str) -> list[str]:
    """``frame:name`` of each local of the SDK's frames in ``exc``'s traceback that holds
    one of ``secrets`` (lowercase hex), as text, as bytes (raw or hex) or, for a trading key
    (80 hex), as the int k1 turns it into: directly, among a dict's values (``**kwargs``) or
    among an object's attributes. Crash reporters and debuggers record these locals."""
    scalars = {int.from_bytes(bytes.fromhex(s), "little") for s in secrets if len(s) == 80}
    found = []
    tb = exc.__traceback__
    while tb is not None:
        frame = tb.tb_frame
        if frame.f_globals.get("__name__", "").startswith("loaf"):
            for name, value in frame.f_locals.items():
                held = list(value.values()) if isinstance(value, dict) else [value]
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

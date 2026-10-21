"""Shared fixtures for the offline tests: an in-memory exchange, a trading client
wired to it, an independent signature check, and a search of traceback frames for
secrets.

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
# A property's l2Market as the exchange serves it.
MARKET = {
    "chainId": 421614,
    "proxyAddress": "0x" + "11" * 20,
    "l2ChainId": 4242,
    "marketIndex": 2050,
    "baseAssetId": 6,
    "quoteAssetId": 3,
}
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
    # One clock for loaf._l2 (an order's expiry, the order-number pool, the market-mode window)
    # and resubmit's guard: tests freeze or advance it by replacing time_ns / monotonic.
    slept: list[float] = []
    clock = SimpleNamespace(
        sleep=slept.append, slept=slept, time_ns=time.time_ns, monotonic=time.monotonic
    )
    monkeypatch.setattr("loaf._l2.time", clock)
    monkeypatch.setattr("loaf.resources.orders.time", clock)
    return clock


@pytest.fixture
def l2_clock(_no_sleep: SimpleNamespace) -> SimpleNamespace:
    """The SDK's order clock: ``time_ns`` and ``monotonic`` to replace, and ``slept``, the
    seconds each sleep asked for (every sleep returns at once)."""
    return _no_sleep


def property_detail(
    ticker: str = "OPRA",
    *,
    l2_market: Any = MARKET,
    market_price: float = 160,
    competition: bool = False,
    mode: bool = False,
) -> dict:
    """A ``GET /trade/{ticker}`` body with the fields order placement reads.

    ``competition`` is the property's ``isCompetition``, ``mode`` the answer's
    ``competitionModeActive``; ``l2_market=...`` leaves ``l2Market`` out, as a
    server that predates the L2 exchange does.
    """
    prop: dict[str, Any] = {
        "ticker": ticker,
        "tokenName": _NAMES.get(ticker, ticker),
        "status": "LIVE",
        "isHalted": False,
        "isCompetition": competition,
    }
    if l2_market is not ...:
        prop["l2Market"] = l2_market
    listed = [{"ticker": ticker, "tokenName": prop["tokenName"], "marketPrice": market_price}]
    if ticker != "MUS":  # another property ahead of it: the reference is found by its ticker
        listed.insert(0, {"ticker": "MUS", "tokenName": "Musgrave", "marketPrice": 50})
    return {"property": prop, "propertyList": listed, "competitionModeActive": mode}


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

    def _listing(self) -> dict:
        rows = []
        for ticker, detail in self.details.items():
            prop = detail["property"]
            row = {
                "ticker": ticker, "status": prop["status"], "isCompetition": prop["isCompetition"]
            }
            if "l2Market" in prop:  # a server that predates the L2 exchange lists no hasL2Market
                row["hasL2Market"] = prop["l2Market"] is not None
            rows.append(row)
        mode = any(detail.get("competitionModeActive") for detail in self.details.values())
        return {"properties": rows, "competitionModeActive": mode}

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
    (its own side and price).
    """
    signed = body if leg is None else body[leg]
    message = k1.order_message(
        l2_chain_id=body["l2ChainId"],
        l2_market_index=body["l2MarketIndex"],
        l2_base_asset_id=body["l2BaseAssetId"],
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

"""Shared fixtures for the offline tests: an in-memory exchange serving the routes an
L2 order touches, a client wired to it that signs with K1's KAT key, and a
signature check rebuilt from the backend's rules.

Only the KAT's public test key appears here: never use it for real funds.
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from loaf import LoafClient
from loaf.constants import DEFAULT_TIMEOUT
from loaf.l2.pricing import scale_price, scale_quantity
from loaf.l2.signer import KAT, L2Signer
from loaf.l2.trading import L2Config, L2Trader

BASE = "http://test/api"

_KAT_KEY = json.loads(KAT.read_text())["keys"][0]
#: K1's KAT key 0 (crates/l2-signer-ffi): a public test key, never use it for real funds.
SK0 = _KAT_KEY["sk"]
PK0 = "0x" + _KAT_KEY["pk"]

#: The trader's clock in these tests, read once so every order in a run signs the same expiry.
NOW = int(time.time() * 1000)
#: What an order signs at NOW: the engine's one-day buffer, then the default seven-day life.
EXPIRY = NOW + 86_400_000 + 7 * 86_400_000
ACCOUNT = 42
EPOCH = 3
#: The L2 chain id the test market serves and its orders sign: the one K1's KAT vectors and viem's
#: signatures use.
CHAIN_ID = 1_819_238_758
MARKET = {
    "chainId": 421614,
    "proxyAddress": "0x1111111111111111111111111111111111111111",
    "l2ChainId": CHAIN_ID,
    "marketIndex": 5,
    "baseAssetId": 7,
    "quoteAssetId": 1,
}

#: The keys of an order body, in the order the SDK sends them (orderBody.ts buildSignedL2OrderBody).
ORDER_KEYS = [
    "ticker", "price", "quantity", "side", "type", "timeInForce",
    "l2ChainId", "l2AccountIndex", "l2MarketIndex", "l2OrderIndex", "l2Epoch",
    "l2FeeRateCap", "l2ExpiryMs", "l2BaseAssetId", "l2QuoteAssetId", "l2SigS", "l2SigE",
]  # fmt: skip
#: A conditional order's: the same, then its trigger.
CONDITIONAL_KEYS = ORDER_KEYS + ["triggerPrice"]
#: An attached TP/SL leg's (orderBody.ts buildSignedL2Leg).
LEG_KEYS = ["triggerPrice", "price", "l2OrderIndex", "l2ExpiryMs", "l2SigS", "l2SigE"]

_ENV_VARS = (
    "LOAF_API_KEY",
    "LOAF_L2_SECRET_KEY",
    "LOAF_API_BASE_URL",
    "LOAF_WS_URL",
    "LOAF_MAX_SLIPPAGE_BPS",
    "LOAF_L2_FEE_RATE_CAP",
    "LOAF_L2_BUFFER_MS",
    "LOAF_L2_ORDER_TTL_MS",
    "LOAF_L2_ORDER_INDEX_BLOCK",
    "LOAF_L2_SIGNER_LIB",
    "LOAF_WALLET_PRIVATE_KEY",
    "LOAF_L1_RPC_URL",
    "LOAF_IPO_RPC_URL",
)


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # A developer shell's credentials, max slippage or L2 settings must not change what a test sees.
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    # The client's retries back off with real sleeps (honouring Retry-After); skip them.
    # Only loaf.client's `time` is replaced, so time.sleep elsewhere stays real.
    monkeypatch.setattr("loaf.client.time", SimpleNamespace(sleep=lambda seconds: None))


class ZeroAux(L2Signer):
    """The vendored signer with its random bytes fixed, so a signature can be rebuilt and compared."""

    @staticmethod
    def _aux(aux: bytes | None) -> bytes:
        return bytes(32) if aux is None else bytes(aux)


SIGNER = ZeroAux()


def detail(ticker: str = "OPRA", market: Any = MARKET, mark: Any = 160) -> dict:
    """A ``GET /trade/{ticker}`` body with the fields order placement reads: the
    property's L2 market and its market reference in ``propertyList``."""
    return {
        "property": {
            "ticker": ticker,
            "tokenName": "Opera",
            "status": "LIVE",
            "isHalted": False,
            "l2Market": market,
        },
        "propertyList": [
            {"ticker": "OTHR", "tokenName": "Other", "marketPrice": 9},
            {"ticker": ticker, "tokenName": "Opera", "marketPrice": mark},
        ],
    }


class FakeExchange:
    """The routes an L2 order touches, in memory, for ``httpx.MockTransport``,
    answering as Loaf-Backend at the K1 head does.

    * ``GET /trade/{ticker}`` serves ``details[ticker]``; an unknown ticker is a
      404 ``Property not found``. Replace an entry to change what it serves.
    * ``GET /l2/keys`` serves the account (``account``, ``epoch``, ``journaled``),
      or a 404 when ``keys_missing``.
    * ``POST /l2/order-indices`` reserves the next block (``next_first``, then
      1000 more each time), reserved a second before NOW.
    * ``POST /orders`` and ``/orders/conditional`` answer 200 with a fresh
      ``orderId``.

    Any route first pops the next reply queued for its path, if there is one: an
    ``httpx.Response``, an exception to raise, or a callable taking the request.
    Every request is recorded in :attr:`requests`.
    """

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.details: dict[str, dict] = {"OPRA": detail()}
        self.account = ACCOUNT
        self.epoch: int | None = EPOCH
        self.journaled: str | None = PK0
        self.keys_missing = False
        self.next_first = 1000
        self._queues: dict[str, list[Any]] = {}
        self._order_id = 0
        self._leg_id = 900

    def queue(self, path: str, *replies: Any) -> None:
        self._queues.setdefault("/api" + path, []).extend(replies)

    def _pop(self, request: httpx.Request) -> httpx.Response | None:
        queue = self._queues.get(request.url.path)
        if not queue:
            return None
        reply = queue.pop(0)
        if isinstance(reply, Exception):
            raise reply
        if callable(reply):
            return reply(request)
        return reply

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        queued = self._pop(request)
        if queued is not None:
            return queued
        method, path = request.method, request.url.path[len("/api") :]
        if method == "GET" and path.startswith("/trade/"):
            found = self.details.get(path[len("/trade/") :])
            if found is None:
                return httpx.Response(404, json={"error": "Property not found"})
            return httpx.Response(200, json=found)
        if method == "GET" and path == "/l2/keys":
            if self.keys_missing:
                return httpx.Response(404, json={"error": "l2 account: no L2 account for this user"})
            return httpx.Response(
                200,
                json={
                    "accountIndex": self.account,
                    "epoch": self.epoch,
                    "journaledKey": self.journaled,
                    "pending": False,
                    "withdrawalNonce": 0,
                },
            )
        if method == "POST" and path == "/l2/order-indices":
            first, self.next_first = self.next_first, self.next_first + 1000
            count = json.loads(request.content)["count"]
            return httpx.Response(
                201,
                json={
                    "accountIndex": self.account,
                    "firstIndex": first,
                    "count": count,
                    "reservedAtMs": NOW - 1000,
                },
            )
        if method == "POST" and path == "/orders/conditional":
            self._order_id += 1
            return httpx.Response(200, json={"success": True, "orderId": self._order_id})
        if method == "POST" and path == "/orders":
            body = json.loads(request.content)
            self._order_id += 1
            legs = []
            for leg in ("tp", "sl"):
                if leg in body:
                    self._leg_id += 1
                    legs.append(self._leg_id)
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "orderId": self._order_id,
                    "status": "OPEN",
                    "quantityLeft": body["quantity"],
                    "duplicate": False,
                    "attachedLegIds": legs,
                },
            )
        return httpx.Response(404, json={"error": f"no route {method} {path}"})

    def posts(self, path: str) -> list[bytes]:
        """The raw bodies POSTed to ``path`` (e.g. ``"/orders"``), in order."""
        return [
            r.content for r in self.requests if r.method == "POST" and r.url.path == "/api" + path
        ]

    def bodies(self, path: str) -> list[dict]:
        return [json.loads(content) for content in self.posts(path)]

    def gets(self, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == "GET" and r.url.path == "/api" + path]


class Clock:
    """A monotonic clock a test moves by hand (the trader's market and key-state ages)."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def exchange() -> FakeExchange:
    return FakeExchange()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def trader_sleeps() -> list:
    """The pauses the L2 trader took (a reservation refused as too soon is waited out once)."""
    return []


@pytest.fixture
def trading_client(exchange: FakeExchange, clock: Clock, trader_sleeps: list):
    """``trading_client(api_key=..., l2_key=..., l2_config=..., **LoafClient kwargs)`` -> a
    client talking to ``exchange`` whose orders ``l2_key`` signs, with :data:`SIGNER`, the
    clock reading NOW and ``clock`` as its monotonic clock."""

    def make(
        *,
        api_key: str | None = "testkey",
        l2_key: str | None = SK0,
        l2_config: L2Config | None = None,
        **kwargs: Any,
    ) -> LoafClient:
        # The same timeout a LoafClient gives the httpx client it builds itself.
        http = httpx.Client(
            transport=httpx.MockTransport(exchange.handler), base_url=BASE, timeout=DEFAULT_TIMEOUT
        )
        client = LoafClient(api_key=api_key, base_url=BASE, http_client=http, **kwargs)
        if l2_key is not None:
            client._l2 = L2Trader(
                client,
                l2_key,
                config=l2_config or L2Config(),
                signer=SIGNER,
                clock_ms=lambda: NOW,
                sleep=trader_sleeps.append,
                monotonic=clock,
            )
        return client

    return make


def signed_terms(body: dict) -> dict:
    """The 12 terms the backend re-derives from an order body (l2OrderFields.ts), as
    the order signs them: the price and quantity scaled with ``Math.round``."""
    return {
        "l2ChainId": body["l2ChainId"],
        "l2MarketIndex": body["l2MarketIndex"],
        "l2BaseAssetId": body["l2BaseAssetId"],
        "l2QuoteAssetId": body["l2QuoteAssetId"],
        "isAsk": body["side"] == "SELL",
        "price": scale_price(body["price"]),
        "l2FeeRateCap": body["l2FeeRateCap"],
        "l2AccountIndex": body["l2AccountIndex"],
        "l2OrderIndex": body["l2OrderIndex"],
        "l2Epoch": body["l2Epoch"],
        "quantity": scale_quantity(body["quantity"]),
        "l2ExpiryMs": body["l2ExpiryMs"],
    }


def leg_terms(body: dict, leg: str) -> dict:
    """What a TP/SL leg signs: a SELL of the order's quantity at the leg's price, with
    its own index and expiry and the order's other terms."""
    return {
        **signed_terms(body),
        "isAsk": True,
        "price": scale_price(body[leg]["price"]),
        "l2OrderIndex": body[leg]["l2OrderIndex"],
        "l2ExpiryMs": body[leg]["l2ExpiryMs"],
    }


def signature(terms: dict) -> tuple:
    """``(s, e)`` of SK0 signing ``terms`` alone, with the fixed random bytes."""
    sig = SIGNER.sign_order(SK0, terms)
    return sig["s"], sig["e"]


def assert_signed(body: dict) -> None:
    """``body`` and each of its legs carry SK0's signature of the terms rebuilt from them."""
    assert (body["l2SigS"], body["l2SigE"]) == signature(signed_terms(body))
    for leg in ("tp", "sl"):
        if leg in body:
            assert (body[leg]["l2SigS"], body[leg]["l2SigE"]) == signature(leg_terms(body, leg))

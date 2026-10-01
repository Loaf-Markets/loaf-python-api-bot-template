"""Shared fixtures for the offline tests: an in-memory exchange, a signing
client wired to it, and an independent signature check.

Only public test keys appear here: never use them for real funds.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils.crypto import keccak

from loaf import LoafClient
from loaf.constants import DEFAULT_TIMEOUT

# Hardhat account #0: public test key — never use for real funds.
HARDHAT_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
HARDHAT_ADDRESS = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"

CONTRACT = "0x1111111111111111111111111111111111111111"
CONTRACT_B = "0x2222222222222222222222222222222222222222"
BASE = "http://test/api"

# The reference order vector: HARDHAT_KEY signs BUY 1.5 @ 160 over CONTRACT.
VECTOR_NONCE = "0192a3b4c5d600112233445566778899"
VECTOR_SIGNATURE = (
    "0x7bc9e57296d5263219ed48f5fa5f0d04409ccf982345daca98b0bdef5516b6a6"
    "724a2c4ce21bbd8886b90fcb360298ec0f240907e6a0b78cffc7ade0410066391c"
)

_ENV_VARS = (
    "LOAF_API_KEY",
    "LOAF_AGENT_PRIVATE_KEY",
    "LOAF_API_BASE_URL",
    "LOAF_WS_URL",
    "LOAF_MAX_SLIPPAGE_BPS",
)


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # A developer shell's credentials (or max slippage) must not change what a test sees.
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    # The client's retries back off with real sleeps (honouring Retry-After); skip them.
    # Only loaf.client's `time` is replaced, so time.sleep elsewhere stays real.
    monkeypatch.setattr("loaf.client.time", SimpleNamespace(sleep=lambda seconds: None))


def opera_detail(contract: str | None = CONTRACT, market_price: float = 160) -> dict:
    """A ``GET /trade/opera`` body with just the fields order placement reads."""
    return {
        "property": {
            "tokenName": "opera",
            "contractAddress": contract,
            "status": "LIVE",
            "isHalted": False,
        },
        "propertyList": [
            {"tokenName": "opera", "marketPrice": market_price},
            {"tokenName": "musgrave", "marketPrice": 50},
        ],
    }


class FakeExchange:
    """The order routes of the exchange, in memory, for ``httpx.MockTransport``.

    * ``GET /trade/{token}`` pops the next reply queued for it, else serves
      ``details[token]``; an unknown token is a 404 ``Property not found``.
      Replace an entry to change what it serves.
    * ``POST /orders`` and ``/orders/conditional`` pop the next queued reply
      (an ``httpx.Response``, or an exception to raise); an empty queue answers
      200 with a fresh ``orderId``.

    Every request is recorded in :attr:`requests`.
    """

    _ORDER_ROUTES = ("/api/orders", "/api/orders/conditional")

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.details: dict[str, dict] = {"opera": opera_detail()}
        self._queues: dict[str, list[Any]] = {path: [] for path in self._ORDER_ROUTES}
        self._order_id = 0

    def queue(self, path: str, *replies: Any) -> None:
        self._queues.setdefault("/api" + path, []).extend(replies)

    def _pop(self, path: str) -> httpx.Response | None:
        queue = self._queues.get(path)
        if not queue:
            return None
        reply = queue.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.method == "GET" and path.startswith("/api/trade/"):
            queued = self._pop(path)
            if queued is not None:
                return queued
            detail = self.details.get(path[len("/api/trade/") :])
            if detail is None:
                return httpx.Response(404, json={"error": "Property not found"})
            return httpx.Response(200, json=detail)
        if request.method == "POST" and path in self._ORDER_ROUTES:
            queued = self._pop(path)
            if queued is not None:
                return queued
            self._order_id += 1
            if path.endswith("/conditional"):
                return httpx.Response(200, json={"success": True, "orderId": self._order_id})
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "orderId": self._order_id,
                    "status": "OPEN",
                    "quantityLeft": json.loads(request.content)["quantity"],
                    "duplicate": False,
                },
            )
        return httpx.Response(404, json={"error": "Not found"})

    def posts(self, path: str) -> list[bytes]:
        """The raw bodies POSTed to ``path`` (e.g. ``"/orders"``), in order."""
        return [
            r.content for r in self.requests if r.method == "POST" and r.url.path == "/api" + path
        ]

    def bodies(self, path: str) -> list[dict]:
        return [json.loads(content) for content in self.posts(path)]

    def gets(self, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == "GET" and r.url.path == "/api" + path]


@pytest.fixture
def exchange() -> FakeExchange:
    return FakeExchange()


@pytest.fixture
def trading_client(exchange: FakeExchange):
    """``trading_client(api_key=..., agent_key=..., **LoafClient kwargs)`` -> a client
    that signs with ``agent_key`` and talks to ``exchange``."""

    def make(
        *, api_key: str | None = "testkey", agent_key: str | None = HARDHAT_KEY, **kwargs: Any
    ) -> LoafClient:
        # The same timeout a LoafClient gives the httpx client it builds itself.
        http = httpx.Client(
            transport=httpx.MockTransport(exchange.handler), base_url=BASE, timeout=DEFAULT_TIMEOUT
        )
        return LoafClient(
            api_key=api_key, agent_private_key=agent_key, base_url=BASE, http_client=http, **kwargs
        )

    return make


def recover(terms: dict, signature: str, contract: str = CONTRACT) -> str:
    """The address that signed ``terms`` (``{side, price, quantity, nonce}``) over ``contract``.

    Rebuilt straight from the exchange's verification rules with eth-account, not
    with :mod:`loaf.signing`, so a match proves the two agree: the JSON numbers
    are scaled like the backend's ``Math.round(price * 1000)`` /
    ``Math.round(quantity * 10)`` (on-grid values are never at a .5 tie).
    """
    price_scaled = round(terms["price"] * 1000)
    quantity_scaled = round(terms["quantity"] * 10)
    typed_data = {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
            ],
            "Order": [
                {"name": "nonceHash", "type": "bytes32"},
                {"name": "propertyToken", "type": "address"},
                {"name": "tokenAmount", "type": "uint96"},
                {"name": "paymentAmount", "type": "uint96"},
                {"name": "deadline", "type": "uint64"},
                {"name": "isBuying", "type": "bool"},
            ],
        },
        "primaryType": "Order",
        "domain": {"name": "LoafSettlement", "version": "1", "chainId": 421614},
        "message": {
            "nonceHash": keccak(text=terms["nonce"]),
            "propertyToken": contract.lower(),
            "tokenAmount": quantity_scaled * 10**17,
            "paymentAmount": price_scaled * quantity_scaled * 10**6 // 10**4,
            "deadline": 0,
            "isBuying": terms["side"] == "BUY",
        },
    }
    return Account.recover_message(encode_typed_data(full_message=typed_data), signature=signature)

"""L2 signing, offline: the vendored signer against K1's KAT, the integers an order signs, what the
L2 trader keeps between orders (index blocks, key state) against the in-memory exchange of
``conftest``, the batch signature, and the wallet flows against viem's signatures. Run with: pytest
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional

import httpx
import pytest

import loaf
from loaf import LoafClient, OrderOutcomeUnknownError
from loaf.exceptions import L2SignerError, LoafL2Error
from loaf.l2.pricing import js_round, scale_price, scale_quantity
from loaf.l2.signer import HERE, KAT, PLATFORMS, PROVENANCE, L2Signer, vendored_library
from loaf.l2.trading import (
    DEFAULT_L2_CHAIN_ID,
    INDEX_REFUSALS,
    INDEX_USED,
    RESERVE_TOO_SOON,
    L2Config,
    OrderIntent,
)

from conftest import (
    ACCOUNT,
    BASE,
    MARKET,
    NOW,
    ORDER_KEYS,
    PK0,
    SIGNER,
    SK0,
    assert_signed,
    detail,
    signed_terms,
)

KAT_DATA = json.loads(KAT.read_text())
#: The engine commit whose crates/l2-signer-ffi/release.sh built the vendored libraries.
ENGINE_COMMIT = "cdcd0feef52b055f326333b6f66b1c0f96d25bbd"
#: kat_native.py's digest of every deterministic answer, equal on every platform the builds were run on.
KAT_DIGEST = "6885b67dd2cb5df2483721dca374f79562f86bd4fbbe4e9f203a7a1be0d92bb9"


def wire(v: Dict[str, Any]) -> Dict[str, Any]:
    return {k: x for k, x in v.items() if k not in ("m", "kind", "code")}


def error_reply(status: int, message: str) -> httpx.Response:
    return httpx.Response(status, json={"error": message})


# --------------------------------------------------------------------------- #
# The vendored signer
# --------------------------------------------------------------------------- #


def test_vendored_libraries_are_the_pinned_build():
    prov = json.loads(PROVENANCE.read_text())
    assert prov["source"] == "loaf-matching-engine" and prov["commit"] == ENGINE_COMMIT
    assert prov["path"] == "crates/l2-signer-ffi"
    for name, sha in prov["files"].items():
        assert hashlib.sha256((HERE / name).read_bytes()).hexdigest() == sha, name
    # Every platform the SDK maps has its library pinned.
    for system, machine in PLATFORMS:
        assert vendored_library(system, machine).relative_to(HERE).as_posix() in prov["files"]
    assert prov["files"]["kat/k1-sign.json"] == hashlib.sha256(KAT.read_bytes()).hexdigest()


def test_a_library_that_is_not_the_pinned_one_is_refused(tmp_path, monkeypatch):
    lib = vendored_library()
    other = tmp_path / lib.name
    other.write_bytes(lib.read_bytes() + b"\0")
    monkeypatch.setattr("loaf.l2.signer.vendored_library", lambda *a: other)
    monkeypatch.setattr("loaf.l2.signer.HERE", tmp_path)
    monkeypatch.setattr("loaf.l2.signer._pinned_sha256", lambda name: "0" * 64)
    with pytest.raises(L2SignerError, match="is not the library PROVENANCE.json pins"):
        L2Signer()


def test_no_library_for_an_unknown_platform():
    with pytest.raises(L2SignerError, match="LOAF_L2_SIGNER_LIB"):
        vendored_library("plan9", "mips")


def test_kat_through_the_sdk_signer():
    s = L2Signer()
    assert all(
        s.key_from_seed(bytes.fromhex(k["seed"])) == k["sk"] and s.public_key(k["sk"]) == "0x" + k["pk"]
        for k in KAT_DATA["keys"]
    )
    assert s.order_message(wire(KAT_DATA["orders"][0])) == KAT_DATA["orders"][0]["m"]
    assert s.withdraw_message(wire(KAT_DATA["withdraws"][0])) == KAT_DATA["withdraws"][0]["m"]
    o = wire(KAT_DATA["orders"][0])
    sig = s.sign_order(SK0, o, aux=bytes(32))
    assert s.sign_order(SK0, o, aux=bytes(32)) == sig and s.sign_order(SK0, o, aux=bytes([1]) * 32) != sig
    assert s.sign_order(SK0, o) != s.sign_order(SK0, o)  # fresh random bytes each call
    batch_orders = lambda n: [dict(o, l2OrderIndex=i + 1) for i in range(n)]
    batches = []
    for b in KAT_DATA["batches"]:
        got = s.sign_batch(SK0, batch_orders(b["count"]), aux=bytes(32))
        batches.append(got)
        assert got["count"] == b["count"] and got["treeRoot"] == b["root"] and got["message"] == b["m"]
        assert got["places"][b["position"]]["siblings"] == b["path"]
        assert [p["position"] for p in got["places"]] == list(range(b["count"]))
    w = s.sign_withdraw(SK0, wire(KAT_DATA["withdraws"][0]), aux=bytes(32))
    digest = hashlib.sha256(json.dumps({"order": sig, "withdraw": w, "batches": batches}, sort_keys=True).encode()).hexdigest()
    assert digest == KAT_DIGEST


def test_signer_refusals_by_text():
    s = L2Signer()
    o = wire(KAT_DATA["orders"][0])
    mixed = [dict(o, l2OrderIndex=1), dict(o, l2OrderIndex=2, l2AccountIndex=o["l2AccountIndex"] + 1)]
    cases = [
        ("l2 signer: a batch signs 1 to 256 orders", lambda: s.sign_batch(SK0, [])),
        ("l2 signer: a batch's orders share one chain id, account and epoch", lambda: s.sign_batch(SK0, mixed)),
        ("l2 signer: the random bytes must be 32 bytes", lambda: s.sign_order(SK0, o, aux=bytes(31))),
        ("l2 signer: the secret key must be 80 lowercase hex", lambda: s.sign_order(SK0.upper(), o)),
        ("l2 signer: order json: unknown field `l2ApiKeyIndex`", lambda: s.sign_order(SK0, dict(o, l2ApiKeyIndex=0))),
        ("l2 signer: the seed must be 32 bytes", lambda: s.key_from_seed(bytes(31))),
    ]
    for text, call in cases:
        with pytest.raises(L2SignerError) as e:
            call()
        assert str(e.value).startswith(text)
    # A signer refusal is an L2 error and a ValueError.
    assert issubclass(L2SignerError, LoafL2Error) and issubclass(L2SignerError, ValueError)


# --------------------------------------------------------------------------- #
# The integers an order signs (Loaf-Backend formatters.ts)
# --------------------------------------------------------------------------- #


def test_js_round_and_the_signed_integers():
    assert [js_round(x) for x in (2.5, -2.5, 2.4999, -2.5001, 0.5)] == [3, -2, 2, -3, 1]
    assert scale_price(141.45) == 141450 and scale_price(1.005) == 1005 and scale_price(0.01) == 10
    assert scale_price(2.01) == 2010  # 2.01 * 1000 is 2009.9999999999998 in floating point
    assert scale_quantity(0.1) == 1 and scale_quantity(47.3) == 473 and scale_quantity(10) == 100


# --------------------------------------------------------------------------- #
# What the trader keeps between orders: index blocks, the key state
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("refusal", sorted(INDEX_REFUSALS))
def test_an_index_refusal_drops_the_block(exchange, trading_client, refusal):
    exchange.queue("/orders", error_reply(400, refusal))
    client = trading_client()
    with pytest.raises(loaf.LoafValidationError):
        client.orders.limit_buy("OPRA", 1, 100)
    client.orders.limit_buy("OPRA", 1, 100)
    first, second = exchange.bodies("/orders")
    assert (first["l2OrderIndex"], second["l2OrderIndex"]) == (1000, 2000)
    assert len(exchange.bodies("/l2/order-indices")) == 2


def test_an_index_refusal_after_an_unanswered_attempt_drops_the_block_too(exchange, trading_client):
    exchange.queue("/orders", httpx.ReadTimeout("timed out"), error_reply(400, INDEX_USED))
    client = trading_client()
    with pytest.raises(OrderOutcomeUnknownError):
        client.orders.limit_buy("OPRA", 1, 100)
    client.orders.limit_buy("OPRA", 1, 100)
    assert [b["l2OrderIndex"] for b in exchange.bodies("/orders")] == [1000, 1000, 2000]


def test_other_refusals_keep_the_block_and_the_key_state(exchange, trading_client):
    exchange.queue("/orders", error_reply(400, "Insufficient available balance for this order"))
    client = trading_client()
    with pytest.raises(loaf.LoafValidationError):
        client.orders.limit_buy("OPRA", 1, 100)
    client.orders.limit_buy("OPRA", 1, 100)
    assert [b["l2OrderIndex"] for b in exchange.bodies("/orders")] == [1000, 1001]
    assert len(exchange.bodies("/l2/order-indices")) == len(exchange.gets("/l2/keys")) == 1


def test_the_reservation_rate_refusal_is_waited_out_once(exchange, trading_client, trader_sleeps):
    exchange.queue("/l2/order-indices", error_reply(422, RESERVE_TOO_SOON))
    trading_client().orders.limit_buy("OPRA", 1, 100)
    assert trader_sleeps == [1.0] and len(exchange.bodies("/l2/order-indices")) == 2


def test_another_reservation_refusal_is_raised(exchange, trading_client, trader_sleeps):
    exchange.queue("/l2/order-indices", error_reply(422, "l2 order_index: some other refusal"))
    with pytest.raises(loaf.LoafBusinessRuleError):
        trading_client().orders.limit_buy("OPRA", 1, 100)
    assert trader_sleeps == [] and exchange.posts("/orders") == []


def test_an_unusable_reservation_is_refused(exchange, trading_client):
    client = trading_client()
    other_account = {"accountIndex": ACCOUNT - 1, "firstIndex": 1, "count": 64, "reservedAtMs": NOW}
    exchange.queue("/l2/order-indices", httpx.Response(201, json=other_account))
    with pytest.raises(LoafL2Error, match="no order can be signed in"):
        client.orders.limit_buy("OPRA", 1, 100)
    # Reserved longer ago than K1's longest order life before the order's expiry.
    too_old = {**other_account, "accountIndex": ACCOUNT, "reservedAtMs": NOW - 4_300_000_000}
    exchange.queue("/l2/order-indices", httpx.Response(201, json=too_old))
    with pytest.raises(LoafL2Error, match="cannot serve"):
        client.orders.limit_buy("OPRA", 1, 100)
    assert exchange.posts("/orders") == []


@pytest.mark.parametrize(
    "refusal",
    [
        lambda: error_reply(400, "l2 epoch: not the account's current settle epoch"),
        lambda: error_reply(401, "l2 signature: the Schnorr signature does not verify"),
    ],
)
def test_key_state_read_again_after_a_key_refusal(exchange, trading_client, clock, refusal):
    exchange.queue("/orders", refusal())
    client = trading_client()
    with pytest.raises(loaf.LoafAPIError):
        client.orders.limit_buy("OPRA", 1, 100)
    exchange.epoch = 4
    client.orders.limit_buy("OPRA", 1, 100)
    client.orders.limit_buy("OPRA", 1, 100)
    assert len(exchange.gets("/l2/keys")) == 2
    assert [b["l2Epoch"] for b in exchange.bodies("/orders")] == [3, 4, 4]
    clock.t += 60  # and every 60 s
    client.orders.limit_buy("OPRA", 1, 100)
    assert len(exchange.gets("/l2/keys")) == 3


@pytest.mark.parametrize(
    "state, text",
    [
        ({"journaled": "0x" + "ab" * 40}, "not this client's"),
        ({"epoch": None}, "not enabled"),
        ({"journaled": None}, "not enabled"),
        ({"keys_missing": True}, "no L2 account"),
        ({"market": None}, "no L2 market is listed"),
        ({"market": {**MARKET, "extra": 1}}, "exactly the keys"),
        ({"market": {**MARKET, "quoteAssetId": 7}}, "must differ"),
    ],
)
def test_setups_no_order_can_be_signed_under(exchange, trading_client, state, text):
    state = dict(state)
    if "market" in state:
        exchange.details["OPRA"] = detail(market=state.pop("market"))
    for k, v in state.items():
        setattr(exchange, k, v)
    with pytest.raises(LoafL2Error, match=text):
        trading_client().orders.limit_buy("OPRA", 1, 100)
    assert exchange.posts("/orders") == []


# --------------------------------------------------------------------------- #
# The batch signature
# --------------------------------------------------------------------------- #


def test_create_many_signs_one_root_and_posts_each(exchange, trading_client):
    exchange.queue(
        "/orders",
        httpx.Response(200, json={"success": True, "orderId": 1}),
        error_reply(400, "Order value must be at least 10 USDL"),
        httpx.ReadTimeout("timed out"),
    )
    client = trading_client(max_retries=0)
    results = client.orders.create_many(
        [
            OrderIntent("OPRA", "BUY", 1, price=100),
            OrderIntent("OPRA", "SELL", 2, price=110),
            OrderIntent("OPRA", "BUY", 1, type="MARKET", reference_price=141.45),
        ]
    )
    bodies = exchange.bodies("/orders")
    # Each order answers on its own; a refusal or an unknown outcome does not stop the others.
    assert results[0].orderId == 1 and isinstance(results[1], loaf.LoafValidationError)
    assert isinstance(results[2], OrderOutcomeUnknownError) and results[2].signed_body == bodies[2]
    assert [b["l2OrderIndex"] for b in bodies] == [1000, 1001, 1002]
    assert [b["l2BatchPosition"] for b in bodies] == [0, 1, 2]
    assert {(b["l2BatchCount"], b["l2BatchTreeRoot"], b["l2SigS"], b["l2SigE"]) for b in bodies} == {
        (3, bodies[0]["l2BatchTreeRoot"], bodies[0]["l2SigS"], bodies[0]["l2SigE"])
    }
    assert bodies[2]["price"] == 144.27  # 141.45 + 2% (the client's max slippage), down to the cent
    batch = SIGNER.sign_batch(SK0, [signed_terms(b) for b in bodies])
    assert bodies[0]["l2BatchTreeRoot"] == batch["treeRoot"]
    assert (bodies[0]["l2SigS"], bodies[0]["l2SigE"]) == (batch["s"], batch["e"])
    assert [b["l2BatchSiblings"] for b in bodies] == [p["siblings"] for p in batch["places"]]

    # One order alone is signed alone: no batch fields. max_slippage_bps prices its MARKET orders.
    client.orders.create_many(
        [OrderIntent("OPRA", "BUY", 1, type="MARKET", reference_price=100)], max_slippage_bps=0
    )
    lone = exchange.bodies("/orders")[-1]
    assert list(lone) == ORDER_KEYS and lone["price"] == 100.0
    assert_signed(lone)
    for refused in ([OrderIntent("OPRA", "BUY", 1, type="STOP_MARKET")], [], [("OPRA", "BUY", 1)]):
        with pytest.raises(loaf.LoafValidationError):
            client.orders.create_many(refused)


# --------------------------------------------------------------------------- #
# L2 settings
# --------------------------------------------------------------------------- #


def test_config_from_env(exchange, trading_client, monkeypatch):
    cfg = L2Config.from_env({})
    defaults = (cfg.l2_chain_id, cfg.fee_rate_cap, cfg.order_ttl_ms, cfg.index_block)
    assert defaults == (1_819_238_758, 7000, 7 * 86_400_000, 64)
    assert L2Config.from_env({"LOAF_L2_CHAIN_ID": "304", "LOAF_L2_ORDER_INDEX_BLOCK": "4096"}).l2_chain_id == 304
    for env, text in [
        ({"LOAF_L2_BUFFER_MS": "1000"}, "LOAF_L2_BUFFER_MS"),
        ({"LOAF_L2_ORDER_INDEX_BLOCK": "4097"}, "LOAF_L2_ORDER_INDEX_BLOCK"),
        ({"LOAF_L2_ORDER_TTL_MS": "59999"}, "LOAF_L2_ORDER_TTL_MS"),
        ({"LOAF_L2_CHAIN_ID": "x"}, "LOAF_L2_CHAIN_ID"),
        ({"LOAF_L2_FEE_RATE_CAP": "1048576"}, "LOAF_L2_FEE_RATE_CAP"),
    ]:
        with pytest.raises(LoafL2Error, match=text):
            L2Config.from_env(env)
    with pytest.raises(LoafL2Error, match="order_ttl_ms"):
        L2Config(order_ttl_ms=4_261_412_864)
    config = L2Config(l2_chain_id=304, fee_rate_cap=5000, order_ttl_ms=60_000)
    trading_client(l2_config=config).orders.limit_buy("OPRA", 1, 100)
    (body,) = exchange.bodies("/orders")
    signed = (body["l2ChainId"], body["l2FeeRateCap"], body["l2ExpiryMs"])
    assert signed == (304, 5000, NOW + 86_400_000 + 60_000)
    assert_signed(body)
    # A client given no l2_config reads the environment when it makes its trader.
    monkeypatch.setenv("LOAF_L2_CHAIN_ID", "304")
    assert LoafClient(base_url=BASE, l2_secret_key=SK0, l2_signer=SIGNER).l2.config.l2_chain_id == 304


# --------------------------------------------------------------------------- #
# The wallet flows (the l2keys extra), against viem's signatures
# --------------------------------------------------------------------------- #

# viem 2.36 (the web app's dependency) signing the same typed data with the same test wallet:
# privateKeyToAccount(keccak256(stringToBytes(TEST_WALLET_PHRASE))).signTypedData(...), RFC 6979.
TEST_WALLET_PHRASE = "loaf-sdk test wallet, never funded"
VIEM = {
    "wallet": "0xAd383110972cCf0Da622FC898AAC03e963FCf948",
    "l2Key": "0x5fe633d8f463f38ab56ca446e536e20bb158b45bbe83ae9284b391d9eb6bd0947081c353a115c37a8f0182a955f41280c81ec96832deca84a10008d0940262261c",
    "changePubKey": "0x68fdfc06efaa86fd25d52383b41fc5b8f41c27ee4cf8370d4b06e50948f484f1354d4b2294f83062aa9c39cf5143c423ffb7ef2ddc97f285c87f638d032656bc1c",
    "purchase": "0x3eb876767b84370dd5c56894d85b2c7984899ab5bf0f58ee4f9cbe539c47ea0c2edfa7e525bb03e0ef4b82bdf67e340e1a645863ba1ca1969639fa6724a979441b",
    "permit": "0xce989d8b7fa23e82a834b3e416fb37f351e1b01ca154c5515348eea10ce411fc4f2eb7d25f99fa320ef55252d0a505e8d78d243d9a554bd446bf9610774e7b7a1c",
    "permitDomainSeparator": "0x17b7fc9acea47d28a93410b0b01400d1c3609f6b2e07a4a137b71c8829fa8188",
}
PROXY = "0x1111111111111111111111111111111111111111"
SALE = "0x2222222222222222222222222222222222222222"
USDC = "0x3333333333333333333333333333333333333333"


def wallet_key() -> str:
    pytest.importorskip("eth_account")
    from loaf.evm import keccak

    return "0x" + keccak(text=TEST_WALLET_PHRASE).hex()


def test_key_typed_data_signs_as_viem():
    key = wallet_key()
    from loaf.evm import Account, sign_typed
    from loaf.l2.keys import Custody, change_pub_key_typed_data, l2_key_typed_data

    assert Account.from_key(key).address == VIEM["wallet"]
    custody = Custody(421614, PROXY)
    assert sign_typed(key, l2_key_typed_data(VIEM["wallet"], custody, DEFAULT_L2_CHAIN_ID, 7, 2)) == VIEM["l2Key"]
    typed = change_pub_key_typed_data(VIEM["wallet"], custody, 7, PK0, 2, 1_800_000_000)
    assert sign_typed(key, typed) == VIEM["changePubKey"]
    with pytest.raises(LoafL2Error, match="key nonce"):
        l2_key_typed_data(VIEM["wallet"], custody, DEFAULT_L2_CHAIN_ID, 7, 2**53)


class FakeCustody:
    def __init__(self, nonce: int, account: int = 7, chain: int = 421614) -> None:
        self.nonce, self.account, self.chain = nonce, account, chain

    def chain_id(self) -> int:
        return self.chain

    def key_nonce(self, proxy: str, owner: str) -> int:
        return self.nonce

    def account_index(self, proxy: str, owner: str) -> int:
        return self.account


class KeysAPI:
    def __init__(self, journaled: Optional[str]) -> None:
        self.journaled = journaled
        self.posted: List[Dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path[len("/api"):]
        if path == "/trade/OPRA":
            return httpx.Response(200, json={"property": {"l2Market": {**MARKET, "proxyAddress": PROXY}}})
        if path == "/l2/keys" and request.method == "GET":
            return httpx.Response(200, json={"accountIndex": 7, "epoch": 1, "journaledKey": self.journaled, "pending": False})
        if path == "/l2/keys" and request.method == "POST":
            body = json.loads(request.content)
            self.posted.append(body)
            self.journaled = body["pubKey"]
            return httpx.Response(200, json={"status": "ALREADY_QUEUED", "txHash": "0x" + "ab" * 32, "accountIndex": 7,
                                             "apiKeyIndex": 0, "publicKey": body["pubKey"], "epoch": 1, "pending": True})
        return httpx.Response(404, json={"error": "no route"})


def test_rotate_derives_the_web_apps_key_and_registers_it():
    key = wallet_key()
    from loaf.evm import Account, keccak
    from loaf.l2.keys import L2Keys, change_pub_key_typed_data
    from eth_account.messages import encode_typed_data

    api = KeysAPI(journaled="0x" + "cd" * 40)
    client = LoafClient(api_key="k", base_url=BASE, http_client=httpx.Client(transport=httpx.MockTransport(api), base_url=BASE))
    keys = L2Keys(client, FakeCustody(nonce=2), key, DEFAULT_L2_CHAIN_ID, signer=SIGNER, now_s=lambda: 1_799_999_400.0,
                  sleep=lambda s: None, log=lambda line: None)
    got = keys.rotate("OPRA")
    # The key the web app derives from the same signature (viem's), at the proxy's nonce.
    assert got.key_nonce == 2 and got.account_index == 7
    assert got.secret_key == SIGNER.key_from_seed(keccak(hexstr=VIEM["l2Key"]))
    (body,) = api.posted
    assert body == {**body, "owner": VIEM["wallet"], "accountIndex": 7, "apiKeyIndex": 0, "pubKey": got.public_key, "deadline": 1_800_000_000}
    typed = change_pub_key_typed_data(VIEM["wallet"], got.custody, 7, got.public_key, 2, 1_800_000_000)
    assert Account.recover_message(encode_typed_data(full_message=typed), signature=body["signature"]) == VIEM["wallet"]
    # derive: the key in force is the one at nonce n - 1 = 2 once the proxy's nonce is 3.
    keys2 = L2Keys(client, FakeCustody(nonce=3), key, DEFAULT_L2_CHAIN_ID, signer=SIGNER, log=lambda line: None)
    derived, in_force = keys2.derive("OPRA")
    assert derived.secret_key == got.secret_key and in_force is True


def test_enable_refuses_a_key_in_force_that_differs_and_a_stale_nonce():
    key = wallet_key()
    from loaf.l2.keys import L2Keys

    api = KeysAPI(journaled="0x" + "cd" * 40)
    client = LoafClient(api_key="k", base_url=BASE, http_client=httpx.Client(transport=httpx.MockTransport(api), base_url=BASE))
    keys = L2Keys(client, FakeCustody(nonce=2), key, DEFAULT_L2_CHAIN_ID, signer=SIGNER, log=lambda line: None)
    with pytest.raises(LoafL2Error, match="rotate"):
        keys.enable("OPRA")
    with pytest.raises(LoafL2Error, match="serves chain 1"):
        L2Keys(client, FakeCustody(nonce=2, chain=1), key, DEFAULT_L2_CHAIN_ID, signer=SIGNER).custody("OPRA")
    with pytest.raises(LoafL2Error, match="deposit to open one"):
        L2Keys(client, FakeCustody(nonce=0, account=2**48 - 1), key, DEFAULT_L2_CHAIN_ID, signer=SIGNER).derive("OPRA")
    assert api.posted == []


def test_a_custody_ticker_is_matched_whole():
    # A ^...$ re.match let "OPRA\n" through, into the GET /trade/{ticker} path (PR #3, PY-03).
    key = wallet_key()
    from loaf.l2.keys import L2Keys

    api = KeysAPI(journaled=None)
    paths: List[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/api/trade":
            listed = [{"ticker": "OPRA\n", "hasL2Market": True}, {"ticker": "OPRA", "hasL2Market": True}]
            return httpx.Response(200, json={"properties": listed})
        return api(request)

    client = LoafClient(api_key="k", base_url=BASE, http_client=httpx.Client(transport=httpx.MockTransport(handler), base_url=BASE))
    keys = L2Keys(client, FakeCustody(nonce=2), key, DEFAULT_L2_CHAIN_ID, signer=SIGNER, log=lambda line: None)
    with pytest.raises(LoafL2Error, match="ticker must be 1-4"):
        keys.custody("OPRA\n")
    assert paths == []
    assert keys.custody() == keys.custody("OPRA")  # the listed "OPRA\n" is skipped, never read
    assert paths == ["/api/trade", "/api/trade/OPRA", "/api/trade/OPRA"]


def test_custody_reader_encodes_the_proxy_views():
    pytest.importorskip("eth_account")
    from loaf.evm import ChainReader, selector
    from loaf.l2.keys import CustodyReader

    seen: List[Dict[str, Any]] = []

    def rpc(request: httpx.Request) -> httpx.Response:
        call = json.loads(request.content)
        seen.append(call)
        if call["method"] == "eth_chainId":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": call["id"], "result": "0x66eee"})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": call["id"], "result": "0x" + "00" * 31 + "05"})

    reader = CustodyReader(ChainReader("http://rpc", http=httpx.Client(transport=httpx.MockTransport(rpc))))
    assert reader.chain_id() == 421614
    assert reader.key_nonce(PROXY, VIEM["wallet"]) == 5
    data = seen[-1]["params"][0]["data"]
    assert data == "0x" + selector("changePubKeyNonce(address)").hex() + "00" * 12 + VIEM["wallet"][2:].lower()
    assert seen[-1]["params"][1] == "latest"
    assert reader.account_index(PROXY, VIEM["wallet"]) == 5


class FakeSaleChain:
    def __init__(self, separator: str = VIEM["permitDomainSeparator"]) -> None:
        self.separator = separator

    def chain_id(self) -> int:
        return 421614

    def chain_must_be(self, chain_id: int, what: str) -> None:
        assert chain_id == 421614

    def read(self, to, signature, args, arg_types, out_types):
        answers = {
            (SALE, "paymentToken()"): (USDC,),
            (USDC, "DOMAIN_SEPARATOR()"): (bytes.fromhex(self.separator[2:]),),
            (USDC, "name()"): ("USD Coin",),
            (USDC, "version()"): ("2",),
            (USDC, "nonces(address)"): (5,),
        }
        if (to, signature) not in answers:
            raise LoafL2Error(f"{signature} reverted")  # no eip712Domain(): the name()/version() path
        return answers[(to, signature)]


def test_ipo_purchase_signs_as_viem():
    key = wallet_key()
    from loaf.evm import domain_separator
    from loaf.ipo import IpoSaleTerms, purchase_caps, sign_purchase

    terms = IpoSaleTerms(ipo_id=9, sale=SALE, chain_id=421614, unit_price=1.5, fee_bps=100)
    assert purchase_caps(2, terms) == (3_000_000, 30_000)
    body = sign_purchase(key, FakeSaleChain(), terms, 2, now_s=lambda: 1_800_000_000.0, nonce=lambda: "0123456789abcdef0123456789abcdef")
    assert body == {
        "ipoId": 9, "quantity": 2, "maxBasePayment": 3_000_000, "maxFeeAmount": 30_000,
        "nonce": "0123456789abcdef0123456789abcdef", "deadline": 1_800_000_600, "signature": VIEM["purchase"],
        "permitDeadline": 1_800_000_600, "permitSignature": VIEM["permit"],
    }
    domain = {"name": "USD Coin", "version": "2", "chainId": 421614, "verifyingContract": USDC}
    assert "0x" + domain_separator(domain).hex() == VIEM["permitDomainSeparator"]
    with pytest.raises(LoafL2Error, match="does not match its DOMAIN_SEPARATOR"):
        sign_purchase(key, FakeSaleChain(separator="0x" + "00" * 32), terms, 2)
    with pytest.raises(LoafL2Error, match="whole number"):
        sign_purchase(key, FakeSaleChain(), terms, 0)


def test_ipo_caps_never_below_the_backend_charge():
    pytest.importorskip("eth_account")
    from loaf.ipo import IpoSaleTerms, parse_units, purchase_caps

    assert parse_units(1.5, 6) == 1_500_000 and parse_units(0.1234565, 6) == 123_457 and parse_units(167.49, 6) == 167_490_000
    terms = IpoSaleTerms(ipo_id=1, sale=SALE, chain_id=1, unit_price=167.49, fee_bps=125)
    base, fee = purchase_caps(3, terms)
    assert base == 502_470_000
    # The fee is ceiled at the backend's 3 decimals: 1.25% of 502.47 USDC = 6.280875, charged 6.281.
    assert fee == 6_281_000

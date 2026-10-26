"""``python -m loaf.secret_key`` offline: the web app's L2 key derivation against the
signatures viem makes for it, and the command end to end against an in-memory
exchange, every refusal included.

The wallet is Hardhat's (and Anvil's) public test account #0, the one the
vectors were made with: never use it for real funds.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any, NamedTuple

import httpx
import pytest

import loaf
from loaf import LoafClient, k1, secret_key
from loaf.secret_key import L2_KEY_WARNING, l2_key_typed_data, main

from conftest import BASE, FakeExchange, market_detail, routes, secret_locals

# The helper's `wallet` extra: without it there is nothing to test here.
Account = pytest.importorskip("eth_account").Account
encode_typed_data = pytest.importorskip("eth_account.messages").encode_typed_data
keccak = pytest.importorskip("eth_utils.crypto").keccak


class Vector(NamedTuple):
    """An L2Key signature by WALLET for the vault at PROXY, and the trading key it seeds."""

    chain_id: int
    l2_chain_id: int
    account_index: int
    key_nonce: int
    signature: str  # r || s || v, as the wallet returns it
    seed: str  # keccak256(signature)
    secret: str  # k1.key_from_seed(seed): what LOAF_SECRET_KEY holds
    public_key: str  # k1.public_key(secret): journaledKey while it is in force


WALLET_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
WALLET = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
PROXY = "0xedf6877B00000000000000000000000000000001"
# secp256k1's group order: every private key is below it.
SECP256K1_ORDER = 0xFFFFFFFFFFFFFFFF_FFFFFFFFFFFFFFFE_BAAEDCE6AF48A03B_BFD25E8CD0364141
# The web app's typed data signed by viem, and the keys loaf.k1 derives from those signatures.
VECTORS = [
    Vector(
        chain_id=421614,
        l2_chain_id=304,
        account_index=7,
        key_nonce=0,
        signature="184ed5ae6ae095f3ed74b7e5eda9158ec7dcf0b9f0074d319855fc0703ef5b99"
        "2dea551529e51a5665900b5ef669e10a1c57093aba3b0a0fd1bccae3eb149331"
        "1b",
        seed="34bc6a1c89c815fc111926c1fa400c43220a2c8fac4277ebb0c4131da8428ae9",
        secret="018af9b2cf054f7d1c3f1892fd4d3e8c01f56eb2c0a01eeb29024e6bdfa5e2a32f05792cce720d48",
        public_key="93fea21049464487d7e6c8902c85ccf41bc77bff"
        "c640a30076877f7f16252cc91587ac047535133c",
    ),
    Vector(
        chain_id=421614,
        l2_chain_id=304,
        account_index=7,
        key_nonce=3,
        signature="906106f46fdde97088405f9122df5e60b099f0da4a5903f1d92946055ced3f49"
        "1db4c7c1e8c6b0afc38eaeb587b8b5600b09c77d210969a794a2b6624ab4a0b2"
        "1b",
        seed="c3cc25a4cc665f89354a2fc2fae21fb3a438a70b97474c0ef9b124078772d227",
        secret="bb843ce009adf4fde928aecb50dfc88ed72c2b718c639510176e429abc993e91d9c8da3a99842e20",
        public_key="28d6af1ed2dcddf1bdac2b72e4aabf5a016413d6"
        "3bb13508352b3b944adf6ed6b46899e926c3d8a9",
    ),
    # Each field near the top of its range; the key nonce is the web app's largest, 2^53 - 1.
    Vector(
        chain_id=42161,
        l2_chain_id=4294967295,
        account_index=281474976710654,
        key_nonce=9007199254740991,
        signature="b9132c02f5b20481deaec37c8356b5f7a5835d528acb2206cad097c36894f263"
        "6ea917e98dbc682e8bdf61167a1f037d08226e939307ff83e8788da7d24e6174"
        "1c",
        seed="128b5e898799b5f64fff8f1caeb5df8f2154c34b0956da5e51a99058f54e89ab",
        secret="1ef640bd6e47df6bc7ffcff59eb55fcdb042b55939b79862fa5e67f7642d2802343322096d6dc104",
        public_key="a0515f41856892e44970c63117515ea23c0755e1"
        "c04d95ae52bcfbea6a52d632c133b5947c493319",
    ),
]
NONCE_0, NONCE_3, _ = VECTORS

# MUS's market.l2 in the tests of main(): the vault the first two vectors sign for.
VAULT = {
    "chainId": 421614,
    "proxyAddress": PROXY,
    "l2ChainId": 304,
    "baseAssetId": 2,
    "quoteAssetId": 3,
}
API_KEY = "ab" * 32
# What main() prints when the key in force is the wallet's at key nonce 3.
PRINTED = f"LOAF_SECRET_KEY={NONCE_3.secret}\n"


def account(journaled: str | None) -> dict[str, Any]:
    """``GET /l2/keys`` for account 7, whose key in force is ``journaled``."""
    return {"accountIndex": 7, "epoch": 4, "journaledKey": journaled, "pending": False}


def write_dotenv(monkeypatch: pytest.MonkeyPatch, **values: str) -> None:
    """Write ``values`` to ``./.env``. load_dotenv sets ``os.environ`` directly, so each
    name not set yet is first recorded as unset: monkeypatch removes it after the test."""
    for name in values:
        if name not in os.environ:
            monkeypatch.setenv(name, "")
            monkeypatch.delenv(name)
    Path(".env").write_text("".join(f"{name}={value}\n" for name, value in values.items()))


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    """``main(argv)``'s exit status, stdout and stderr. Whatever happened, neither stream
    shows the wallet key, a signature or a seed, and the wallet key has left the
    environment."""
    status = main(list(argv))
    out, err = capsys.readouterr()
    shown = (out + err).lower()
    assert WALLET_KEY[2:] not in shown
    assert not any(v.signature in shown or v.seed in shown for v in VECTORS)
    assert "LOAF_WALLET_PRIVATE_KEY" not in os.environ
    return status, out, err


def refused(capsys: pytest.CaptureFixture[str], *argv: str) -> str:
    """``main(argv)``'s refusal: exit status 1, nothing on stdout and one line on stderr,
    which is returned."""
    status, out, err = run(capsys, *argv)
    assert (status, out) == (1, "")
    assert err.count("\n") == 1 and err.endswith("\n")
    return err


@pytest.fixture(autouse=True)
def _in_tmp_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # main() loads the .env python-dotenv finds from the working directory up: this empty one,
    # never the repo's.
    (tmp_path / ".env").write_text("")
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def clients(monkeypatch: pytest.MonkeyPatch, exchange: FakeExchange) -> list[LoafClient]:
    """The clients main() builds: each a real ``LoafClient()``, reading the environment,
    whose requests go to ``exchange``."""
    built: list[LoafClient] = []

    def client() -> LoafClient:
        http = httpx.Client(transport=httpx.MockTransport(exchange.handler), base_url=BASE)
        built.append(LoafClient(http_client=http))
        return built[-1]

    monkeypatch.setattr("loaf.secret_key.LoafClient", client)
    return built


@pytest.fixture
def helper(
    monkeypatch: pytest.MonkeyPatch, exchange: FakeExchange, clients: list[LoafClient]
) -> FakeExchange:
    """``exchange``, ready for main(): it lists MUS, on the vectors' vault, and account 7,
    whose key in force is the wallet's at key nonce 3; the wallet key and an API key are
    set in the environment."""
    exchange.details = {"MUS": market_detail("MUS", market_index=2049, l2=VAULT)}
    exchange.keys = account("0x" + NONCE_3.public_key)
    monkeypatch.setenv("LOAF_WALLET_PRIVATE_KEY", WALLET_KEY)
    monkeypatch.setenv("LOAF_API_KEY", API_KEY)
    return exchange


@pytest.fixture
def candidates(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """The key nonces main() signs a candidate key for, in order."""
    nonces: list[int] = []

    def typed_data(wallet: str, **fields: Any) -> dict[str, Any]:
        nonces.append(fields["key_nonce"])
        return l2_key_typed_data(wallet, **fields)

    monkeypatch.setattr("loaf.secret_key.l2_key_typed_data", typed_data)
    return nonces


# --------------------------------------------------------------------------- #
# The derivation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("vector", VECTORS, ids=lambda v: f"key-nonce-{v.key_nonce}")
def test_the_web_apps_vectors(vector: Vector) -> None:
    signer = Account.from_key(WALLET_KEY)
    assert signer.address == WALLET
    signatures = set()
    # Addresses in any case sign the same.
    for address, proxy in ((WALLET, PROXY), (WALLET.lower(), PROXY.lower())):
        typed = l2_key_typed_data(
            address,
            chain_id=vector.chain_id,
            proxy_address=proxy,
            l2_chain_id=vector.l2_chain_id,
            account_index=vector.account_index,
            key_nonce=vector.key_nonce,
        )
        signed = signer.sign_message(encode_typed_data(full_message=typed))
        signatures.add(bytes(signed.signature).hex())
    assert signatures == {vector.signature}
    seed = keccak(bytes.fromhex(vector.signature))
    assert seed.hex() == vector.seed
    secret = k1.key_from_seed(seed)
    assert secret.hex() == vector.secret
    assert k1.public_key(secret).hex() == vector.public_key


def test_the_warning_is_the_web_apps() -> None:
    assert L2_KEY_WARNING == (
        "This signature is your Loaf L2 trading key. Anyone who holds it can trade for this "
        "account. Sign it only in the Loaf app."
    )


# --------------------------------------------------------------------------- #
# main()
# --------------------------------------------------------------------------- #


def test_prints_the_key_in_force(helper, capsys, candidates):
    status, out, err = run(capsys)
    assert (status, out) == (0, PRINTED)
    assert err == (
        f"Trading key in force for wallet {WALLET}, account 7 on vault {PROXY} (L2 chain 304), "
        f"key nonce 3: public key 0x{NONCE_3.public_key}. Put the line above in your .env as "
        "LOAF_SECRET_KEY and restart your bot. It signs orders for this account on this vault "
        "only; keep it as secret as the wallet key.\n"
    )
    assert candidates == [0, 1, 2, 3]  # up to the key in force
    # The listing, the one live L2 market's detail, then the account: no publicKey asked for.
    assert routes(helper.requests) == ["GET /trade", "GET /trade/MUS", "GET /l2/keys"]
    keys = helper.gets("/l2/keys")[0]
    assert dict(keys.url.params) == {"apiKeyIndex": "0"}
    assert keys.headers["Authorization"] == f"Bearer {API_KEY}"
    assert not any("Authorization" in r.headers for r in helper.requests[:2])


def test_ticker_names_the_vault(helper, capsys):
    assert run(capsys, "--ticker", "MUS")[:2] == (0, PRINTED)
    assert routes(helper.requests) == ["GET /trade/MUS", "GET /l2/keys"]
    assert "Authorization" not in helper.requests[0].headers


def test_key_in_force_at_key_nonce_0(helper, capsys, candidates):
    helper.keys = account("0x" + NONCE_0.public_key)
    status, out, err = run(capsys)
    assert (status, out) == (0, f"LOAF_SECRET_KEY={NONCE_0.secret}\n")
    assert "key nonce 0: public key 0x" + NONCE_0.public_key in err
    assert candidates == [0]


def test_only_live_l2_markets_count(helper, capsys):
    # Listed ahead of MUS: a delisted market and one without an L2 market. OPRA, the next live
    # L2 market, is never read.
    delisted = market_detail("DLST")
    delisted["market"]["status"] = "DELISTED"
    helper.details = {
        "DLST": delisted,
        "NOL2": market_detail("NOL2", l2=None),
        "MUS": helper.details["MUS"],
        "OPRA": market_detail("OPRA", market_index=2050, l2=VAULT),
    }
    assert run(capsys)[:2] == (0, PRINTED)
    assert routes(helper.requests) == ["GET /trade", "GET /trade/MUS", "GET /l2/keys"]


def test_the_trading_key_being_replaced_is_ignored(helper, capsys, monkeypatch):
    # LoafClient() would refuse this one; the helper is how it gets replaced.
    monkeypatch.setenv("LOAF_SECRET_KEY", "not-a-trading-key")
    assert run(capsys)[:2] == (0, PRINTED)
    assert "LOAF_SECRET_KEY" not in os.environ


@pytest.mark.parametrize("api_key_set", [False, True])
def test_dotenv_is_loaded(helper, capsys, monkeypatch, clients, api_key_set):
    pytest.importorskip("dotenv")
    if not api_key_set:
        monkeypatch.delenv("LOAF_API_KEY")
    write_dotenv(
        monkeypatch,
        LOAF_API_KEY="cd" * 32,
        LOAF_API_BASE_URL="http://dotenv.test/api",
        LOAF_SECRET_KEY="stale",
    )
    assert run(capsys)[:2] == (0, PRINTED)
    api_key = API_KEY if api_key_set else "cd" * 32  # the environment wins over the file
    assert (clients[0].base_url, clients[0].api_key) == ("http://dotenv.test/api", api_key)
    assert helper.gets("/l2/keys")[0].headers["Authorization"] == f"Bearer {api_key}"


def test_no_dotenv_found(helper, capsys, monkeypatch):
    # python-dotenv installed, and no .env from the working directory up: nothing to load.
    pytest.importorskip("dotenv")
    monkeypatch.setattr("dotenv.find_dotenv", lambda usecwd: "")  # its answer when none is found
    assert run(capsys)[:2] == (0, PRINTED)


@pytest.mark.parametrize("given", [True, False], ids=["wallet key given", "wallet key not given"])
def test_dotenv_holding_the_wallet_key(helper, capsys, monkeypatch, clients, given):
    pytest.importorskip("dotenv")
    if not given:  # refused by the name alone, before the missing key is
        monkeypatch.delenv("LOAF_WALLET_PRIVATE_KEY")
    write_dotenv(
        monkeypatch, LOAF_WALLET_PRIVATE_KEY=WALLET_KEY, LOAF_API_BASE_URL="http://dotenv.test/api"
    )
    assert refused(capsys) == (
        f"LOAF_WALLET_PRIVATE_KEY is set in {Path.cwd() / '.env'}: delete it from that file. Give "
        "the wallet key to this command only (README §2), so it never sits next to your bot.\n"
    )
    assert "LOAF_API_BASE_URL" not in os.environ  # nothing loaded from it
    assert clients == []


@pytest.mark.parametrize(
    "wallet_key",
    [WALLET_KEY, WALLET_KEY[2:], "0X" + WALLET_KEY[2:].upper()],
    ids=["wallet key 0x", "wallet key bare", "wallet key 0X upper"],
)
@pytest.mark.parametrize(
    "name, value",
    [
        ("PRIVATE_KEY", WALLET_KEY[2:]),  # a wallet tool's .env line
        ("PRIVATE_KEY", f'"{WALLET_KEY}"'),
        ("LOAF_SECRET_KEY", WALLET_KEY[2:].upper()),
        ("LOAF_API_KEY", WALLET_KEY[2:]),  # refused here, before the API key's own check
    ],
    ids=["PRIVATE_KEY bare", "PRIVATE_KEY 0x quoted", "LOAF_SECRET_KEY upper", "LOAF_API_KEY"],
)
def test_dotenv_holding_the_wallet_key_under_another_name(
    helper, capsys, monkeypatch, clients, wallet_key, name, value
):
    pytest.importorskip("dotenv")
    monkeypatch.setenv("LOAF_WALLET_PRIVATE_KEY", wallet_key)
    write_dotenv(monkeypatch, **{name: value}, LOAF_API_BASE_URL="http://dotenv.test/api")
    assert refused(capsys) == (  # run() checks that the key is not echoed
        f"{Path.cwd() / '.env'} holds your wallet key: delete it from that file (a commented-out "
        "line still holds it). Give the wallet key to this command only (README §2), so it never "
        "sits next to your bot.\n"
    )
    assert "LOAF_API_BASE_URL" not in os.environ  # nothing loaded from it
    assert clients == []


@pytest.mark.parametrize(
    "line",
    [
        f"# LOAF_WALLET_PRIVATE_KEY={WALLET_KEY}",  # commented out, not deleted
        f"#PRIVATE_KEY={WALLET_KEY[2:]}",
        f"LOAF_API_KEY={API_KEY}  # wallet {WALLET_KEY.upper()}",
        f"PRIVATE KEY={WALLET_KEY}",  # a line python-dotenv cannot parse
        WALLET_KEY[2:],  # alone on a line
    ],
    ids=["commented out", "PRIVATE_KEY commented out", "inline comment", "unparsed", "alone"],
)
def test_dotenv_holding_the_wallet_key_outside_any_value(helper, capsys, line):
    # python-dotenv reads no value that holds it, so the bot would load none, but the file it
    # loads still holds it.
    pytest.importorskip("dotenv")
    Path(".env").write_text(f"{line}\n")
    assert refused(capsys).startswith(f"{Path.cwd() / '.env'} holds your wallet key: ")


def test_help(capsys):
    with pytest.raises(SystemExit) as info:
        main(["--help"])
    assert info.value.code == 0
    shown = " ".join(capsys.readouterr().out.split())  # argparse wraps to the terminal's width
    assert shown.startswith("usage: python -m loaf.secret_key [-h] [--ticker TICKER] Print the trading")


@pytest.mark.parametrize(
    "argv",
    [
        ["--ticker"],
        ["--ticker", "mus"],
        ["--ticker", "MUSICHALLS1"],  # 11 characters
        ["--ticker", "MUS-perp"],
        # The wallet key where it does not belong, which argparse's own messages would repeat.
        ["--wallet-key", WALLET_KEY],
        [WALLET_KEY[2:]],
        ["--ticker", WALLET_KEY],
        [f"--help={WALLET_KEY}"],
    ],
)
def test_usage_errors(helper, capsys, clients, argv):
    with pytest.raises(SystemExit) as info:
        main(argv)
    assert info.value.code == 2
    out, err = capsys.readouterr()
    assert out == ""
    assert WALLET_KEY[2:] not in err.lower()
    assert " ".join(err.split()) == (  # argparse wraps the usage line to the terminal's width
        "usage: python -m loaf.secret_key [-h] [--ticker TICKER] python -m loaf.secret_key: error: the "
        "only argument is --ticker, a market's ticker (1-10 uppercase letters or digits, -PERP "
        "appended for a perp, e.g. MUS). The wallet key is read from LOAF_WALLET_PRIVATE_KEY, "
        "never from an argument (README §2)."
    )
    assert clients == []


def test_the_command(tmp_path):
    # python -m loaf.secret_key itself, in a process of its own: its exit statuses, and no stdout.
    # Without python-dotenv, which README §2's install (the `wallet` extra alone) leaves out: the
    # dotenv module first on the path cannot be imported.
    (tmp_path / "dotenv.py").write_text("raise ModuleNotFoundError\n")
    path = [str(tmp_path), str(Path(loaf.__file__).parents[1])]
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(path))
    for argv, status, err in (
        (["--wallet-key", "x"], 2, "usage: python -m loaf.secret_key"),
        ([], 1, "Set LOAF_WALLET_PRIVATE_KEY to your account wallet's private key"),
    ):
        done = subprocess.run(
            [sys.executable, "-m", "loaf.secret_key", *argv], capture_output=True, text=True, env=env
        )
        assert (done.returncode, done.stdout) == (status, "")
        assert done.stderr.startswith(err)


# --------------------------------------------------------------------------- #
# Refusals
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("value", [None, "", " \n"])
def test_no_wallet_key(helper, capsys, monkeypatch, clients, value):
    if value is None:
        monkeypatch.delenv("LOAF_WALLET_PRIVATE_KEY")
    else:
        monkeypatch.setenv("LOAF_WALLET_PRIVATE_KEY", value)
    assert refused(capsys) == (
        "Set LOAF_WALLET_PRIVATE_KEY to your account wallet's private key for this one command "
        "(README §2): the helper signs with it on this machine to derive your trading key.\n"
    )
    assert (clients, helper.requests) == ([], [])


@pytest.mark.parametrize(
    "value",
    [
        WALLET_KEY[:-1],
        WALLET_KEY + "0",
        "0x" + WALLET_KEY,
        WALLET_KEY.replace("a", "g"),
        "0x" + "00" * 32,
        f"0x{SECP256K1_ORDER:064x}",
    ],
    ids=["63 hex", "65 hex", "0x twice", "not hex", "zero", "the curve order"],
)
def test_malformed_wallet_key(helper, capsys, monkeypatch, clients, value):
    monkeypatch.setenv("LOAF_WALLET_PRIVATE_KEY", value)
    assert refused(capsys) == (  # the value is never echoed
        "LOAF_WALLET_PRIVATE_KEY is not a private key: expected 64 hex characters, optionally "
        "0x-prefixed.\n"
    )
    assert (clients, helper.requests) == ([], [])


def test_wallet_key_in_any_case(helper, capsys, monkeypatch):
    monkeypatch.setenv("LOAF_WALLET_PRIVATE_KEY", " 0X" + WALLET_KEY[2:].upper() + "\n")
    assert run(capsys)[:2] == (0, PRINTED)


def test_needs_eth_account(helper, capsys, monkeypatch, clients):
    monkeypatch.setitem(sys.modules, "eth_account", None)  # main() imports it when it runs
    assert refused(capsys) == (
        'python -m loaf.secret_key needs eth-account: pip install -e ".[wallet]"\n'
    )
    assert clients == []


def test_no_api_key(helper, capsys, monkeypatch):
    monkeypatch.delenv("LOAF_API_KEY")
    assert refused(capsys) == (
        "Set LOAF_API_KEY (your Loaf API key) so the helper can read your trading account.\n"
    )
    assert helper.requests == []


@pytest.mark.parametrize(
    "wallet_key",
    [WALLET_KEY, WALLET_KEY[2:], "0X" + WALLET_KEY[2:].upper()],
    ids=["wallet key 0x", "wallet key bare", "wallet key 0X upper"],
)
@pytest.mark.parametrize(
    "api_key",
    [
        WALLET_KEY[2:],
        WALLET_KEY[2:].upper(),
        "PRIVATE_KEY=" + WALLET_KEY[2:],  # a wallet tool's .env line, pasted whole
        f'"{WALLET_KEY[2:]}"',  # quotes kept
    ],
    ids=["API key lower", "API key upper", "API key a pasted line", "API key quoted"],
)
def test_api_key_holding_the_wallet_key(helper, capsys, monkeypatch, wallet_key, api_key):
    monkeypatch.setenv("LOAF_WALLET_PRIVATE_KEY", wallet_key)
    monkeypatch.setenv("LOAF_API_KEY", api_key)
    assert refused(capsys) == (  # run() checks that neither is echoed
        "LOAF_API_KEY holds your wallet's private key. The API key is a different secret "
        "(API -> Generate API Token in the web app); remove the wallet key from LOAF_API_KEY "
        "wherever you set it — the SDK sends LOAF_API_KEY to the server with every request.\n"
    )
    assert helper.requests == []


def test_api_key_shaped_like_a_wallet_key(helper, capsys, monkeypatch):
    monkeypatch.setenv("LOAF_API_KEY", WALLET_KEY)
    assert "LOAF_API_KEY holds what looks like a wallet private key" in refused(capsys)
    assert helper.requests == []


@pytest.mark.parametrize("server", ["before markets", "no Loaf API"])
def test_server_without_the_exchange(helper, capsys, server):
    if server == "before markets":  # it listed properties
        listing = {"properties": [{"ticker": "MUS", "status": "LIVE", "hasL2Market": True}]}
        helper.queue("/trade", httpx.Response(200, json=listing))
    else:
        helper.queue("/trade", httpx.Response(200, text="<!doctype html><title>Loaf</title>"))
    assert refused(capsys) == (
        "GET /trade lists no markets with hasL2Market: this server does not run the exchange this "
        "SDK trades on (check LOAF_API_BASE_URL).\n"
    )


def test_ticker_on_a_server_without_the_exchange(helper, capsys):
    helper.details["MUS"] = {"property": {"ticker": "MUS", "status": "LIVE", "isHalted": False}}
    assert refused(capsys, "--ticker", "MUS") == (
        "GET /trade/MUS answered no market: this server does not run the exchange this SDK "
        "trades on (check LOAF_API_BASE_URL).\n"
    )


@pytest.mark.parametrize("why", ["no l2", "not live"])
def test_no_live_l2_market(helper, capsys, why):
    if why == "no l2":
        helper.details = {"MUS": market_detail("MUS", market_index=2049, l2=None)}
    else:
        helper.details["MUS"]["market"]["status"] = "PENDING"
    assert refused(capsys) == (
        "No live market has an L2 market right now, so there is no vault to derive a key for "
        "(pass --ticker to name one).\n"
    )
    assert helper.gets("/l2/keys") == []


def test_ticker_without_an_l2_market(helper, capsys):
    helper.details["MUS"] = market_detail("MUS", market_index=2049, l2=None)
    assert "MUS has no L2 market right now" in refused(capsys, "--ticker", "MUS")


def test_unknown_ticker(helper, capsys):
    assert refused(capsys, "--ticker", "ZZZ") == "HTTP 404: Market not found\n"


@pytest.mark.parametrize("argv, ticker", [([], "MUS"), (["--ticker", "OPRA"], "OPRA")])
def test_no_trading_account(helper, capsys, argv, ticker):
    # The refusal names the market the account was read for, by default the first live one.
    helper.details["OPRA"] = market_detail("OPRA", market_index=2050, l2=VAULT)
    helper.keys = None  # 404 l2 account: no L2 account for this user
    assert refused(capsys, *argv) == (
        f"You have no trading account on {ticker}'s market yet: deposit in the Loaf web app "
        "first.\n"
    )


@pytest.mark.parametrize("journaled", [None, "0x" + "0" * 80], ids=["none", "disabled"])
def test_trading_not_enabled(helper, capsys, candidates, journaled):
    helper.keys = account(journaled)
    assert refused(capsys) == (
        "Trading is not enabled on your account on MUS's market (no trading key is in force): "
        "open MUS on the Loaf web app's Trade page, enable trading there and wait for it to take "
        "effect, then set LOAF_SECRET_KEY to the key in force: the web app's Show trading key "
        "(README §2).\n"
    )
    assert candidates == []  # nothing to search for


def test_no_derived_key_is_in_force(helper, capsys, monkeypatch, candidates):
    monkeypatch.setattr("loaf.secret_key.KEY_NONCE_LIMIT", 4)
    helper.keys = account("0x" + VECTORS[2].public_key)  # another account's key
    assert refused(capsys) == (
        "None of the keys this wallet derives for key nonces 0-3 is the trading key in force: is "
        "LOAF_WALLET_PRIVATE_KEY this account's wallet? A key enabled outside the Loaf web app "
        "cannot be derived here.\n"
    )
    assert candidates == [0, 1, 2, 3]


def test_unexpected_failure_is_one_line(helper, capsys, monkeypatch):
    def fail(wallet: str, **fields: Any) -> dict[str, Any]:
        raise RuntimeError("boom")

    monkeypatch.setattr("loaf.secret_key.l2_key_typed_data", fail)
    assert refused(capsys) == "Deriving your trading key failed (RuntimeError: boom).\n"


def test_interrupted(helper, capsys, monkeypatch):
    def interrupt(wallet: str, **fields: Any) -> dict[str, Any]:
        raise KeyboardInterrupt

    monkeypatch.setattr("loaf.secret_key.l2_key_typed_data", interrupt)
    assert refused(capsys) == "Interrupted.\n"


@pytest.mark.parametrize("stage", [".env", "wallet key", "API key", "account", "search"])
def test_no_frame_keeps_a_key(helper, monkeypatch, stage):
    # Crash reporters and debuggers record every traceback frame's locals.
    if stage == ".env":
        pytest.importorskip("dotenv")
        write_dotenv(monkeypatch, PRIVATE_KEY=WALLET_KEY)
    elif stage == "wallet key":
        monkeypatch.setenv("LOAF_WALLET_PRIVATE_KEY", WALLET_KEY + "0")  # nearly the key, refused
    elif stage == "API key":
        monkeypatch.setenv("LOAF_API_KEY", WALLET_KEY[2:])
    elif stage == "account":
        helper.keys = None
    else:
        monkeypatch.setattr("loaf.secret_key.KEY_NONCE_LIMIT", 4)  # its last candidate is NONCE_3's
        helper.keys = account("0x" + VECTORS[2].public_key)
    with pytest.raises(loaf.LoafConfigError) as info:
        secret_key._print_trading_key(None)
    # The wallet key and the keys it derives: as text, or held by the account that signs with
    # it (its key bytes) or by a client (its API key).
    assert secret_locals(info.value, WALLET_KEY[2:], NONCE_0.secret, NONCE_3.secret) == []

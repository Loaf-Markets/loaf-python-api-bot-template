"""Laptop checks of e2e.py's own parts: no stack, no network. E2E_SDK names an SDK tree to import when the venv
has none installed; E2E_ZKLOAF_ABI, ZkLoaf's ABI (Smart-Contracts abis/ZkLoaf.abi.json), checks the calls."""

from __future__ import annotations

import dataclasses
import fcntl
import json
import os
import re
import signal
import stat
import subprocess
import sys
import threading
import time
import types
import urllib.parse
from decimal import Decimal
from fractions import Fraction

import pytest

if os.environ.get("E2E_SDK"):
    sys.path.insert(0, os.environ["E2E_SDK"])

import e2e  # noqa: E402
from eth_abi import decode as abi_decode  # noqa: E402
from eth_abi import encode as abi_encode  # noqa: E402
from eth_account import Account  # noqa: E402
from eth_account.messages import encode_typed_data  # noqa: E402
from eth_utils import keccak  # noqa: E402
from loaf import k1  # noqa: E402
from loaf.exceptions import LoafValidationError  # noqa: E402

# K1's withdraw known answers (the engine's crates/k1-sign/kat/sign.json, "withdraws").
WITHDRAW_KATS = [
    {"l2ChainId": 1819238758, "l2AccountIndex": 5, "l2ApiKeyIndex": 0, "l2AssetId": 2, "l2Route": 1,
     "amount": "123456789012", "l2Nonce": 3, "l2ExpiryMs": 1700000000,
     "m": "51a51f0f69990732029b424bc44aab36ed347dd0b3192a1f1c7f0ba50b1f722d"},
    {"l2ChainId": 2130706432, "l2AccountIndex": 281474976710655, "l2ApiKeyIndex": 255, "l2AssetId": 65535,
     "l2Route": 255, "amount": "1152921504606846975", "l2Nonce": 281474976710655, "l2ExpiryMs": 281474976710655,
     "m": "2044dd43f0085825b9889bd800d1683ed471559ec570220f638e4ce366566a13"},
    {"l2ChainId": 0, "l2AccountIndex": 0, "l2ApiKeyIndex": 0, "l2AssetId": 0, "l2Route": 0, "amount": "0",
     "l2Nonce": 0, "l2ExpiryMs": 0, "m": "7748f6661fc98f34abc32b8fa173a1291bc936fb7feaf113199f253877d7242a"},
]

# A throwaway key for signing checks (the secp256k1 generator's scalar 1): no account anyone holds funds on.
TEST_KEY = "0x" + "00" * 31 + "01"


@pytest.mark.parametrize("v", WITHDRAW_KATS, ids=["typical", "max", "zero"])
def test_the_withdraw_message_is_k1s(v):
    m = e2e.withdraw_message(v["l2ChainId"], v["l2AccountIndex"], v["l2AssetId"], v["l2Route"], int(v["amount"]),
                             v["l2Nonce"], v["l2ExpiryMs"], v["l2ApiKeyIndex"])
    assert m.hex() == v["m"]


def test_a_signed_withdrawal_verifies_under_its_key():
    sk = k1.key_from_seed(keccak(b"e2e test seed"))
    m = e2e.withdraw_message(31337, 7, e2e.USDC_ASSET, e2e.ROUTE_SPOT, 1_000, 2, 1_900_000_000_000)
    s, e = k1.sign(sk, m, bytes(32))
    assert len(s.hex()) == len(e.hex()) == 80
    k1.verify(k1.public_key(sk), m, s, e)
    with pytest.raises(k1.K1Error):
        k1.verify(k1.public_key(sk), e2e.withdraw_message(31337, 7, 3, 1, 1_001, 2, 1_900_000_000_000), s, e)


def test_change_pub_key_hashes_as_zkloaf_governance_does():
    owner, proxy, pub = "0x" + "ab" * 20, "0x" + "11" * 20, bytes(range(40))
    signable = encode_typed_data(full_message=e2e.change_pub_key_typed_data(owner, 31337, proxy, 5, pub, 0, 1_700_000_000))
    domain = keccak(abi_encode(
        ["bytes32", "bytes32", "bytes32", "uint256", "address"],
        [keccak(text="EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"),
         keccak(text="ZkLoaf"), keccak(text="1"), 31337, proxy]))
    typehash = keccak(text="ChangePubKey(address owner,uint48 accountIndex,uint8 apiKeyIndex,bytes pubKey,uint256 nonce,uint256 deadline)")
    struct = keccak(abi_encode(["bytes32", "address", "uint48", "uint8", "bytes32", "uint256", "uint256"],
                               [typehash, owner, 5, 0, keccak(pub), 0, 1_700_000_000]))
    assert (signable.header, signable.body) == (domain, struct)
    signed = Account.from_key(TEST_KEY).sign_message(signable)
    assert Account.recover_message(signable, signature=signed.signature) == Account.from_key(TEST_KEY).address


def test_index_for_brings_the_pool_to_the_fraction():
    p0, n0, r0 = 1_500_000, 1_500_000_000, 46_875_000  # maintenance 1/32 of the notional
    v0 = r0 * 22 // 10  # initial margin (2 r0) plus 10 %
    pool = {"pool": 2, "value": str(v0), "requirement": str(r0 * 65_536),
            "positions": [{"market": 1, "base": "10", "notional": str(n0)}]}
    def ratio(p):  # the pool's value over its requirement at index p
        return Fraction(v0 * p0 - n0 * (p0 - p), r0 * p)

    for num, den in ((2, 5), (17, 20), (-1, 5)):
        p = e2e.index_for(pool, 1, p0, num, den)
        assert p < p0 and ratio(p) <= Fraction(num, den) < ratio(p + 1)
    assert v0 * p0 - n0 * (p0 - e2e.index_for(pool, 1, p0, -1, 5)) < 0  # the value below 0
    with pytest.raises(e2e.Failed):
        e2e.index_for({**pool, "positions": []}, 1, p0, 2, 5)
    with pytest.raises(e2e.Failed):
        e2e.index_for({**pool, "value": str(r0 // 2)}, 1, p0, 2, 5)


def test_provision_sql_carries_digests_and_quotes_labels():
    sql = e2e.provision_sql([("e2e-A-o'brien", "0x" + "ab" * 20, "INVESTOR", "d" * 64)], 1_700_000_000)
    assert "'e2e-A-o''brien'" in sql and "'" + "d" * 64 + "'" in sql
    assert "1700086400" in sql and sql.startswith("BEGIN;") and sql.rstrip().endswith("COMMIT;")
    assert sql.index("pg_get_serial_sequence") < sql.index("INSERT INTO users")
    assert "EXCEPTION WHEN insufficient_privilege" in sql


def test_the_operator_and_deleverage_sql():
    assert e2e.market_flags_sql(3, True) == (
        "UPDATE markets SET is_halted = false, is_reduce_only = true WHERE market_index = 3;\n"
        "SELECT 'E2E', market_index, is_halted, is_reduce_only FROM markets WHERE market_index = 3;\n")
    assert "is_reduce_only = false" in e2e.market_flags_sql(3, False)
    sql = e2e.deleverage_sql([7, 12])
    assert "kind = 65 AND journal_seq IN (7, 12)" in sql and "counter_account, counter_pool" in sql


class FakeChain(e2e.Chain):
    """Answers eth_call by selector."""

    def __init__(self, answers):
        self.answers, self._id, self.chain_id = answers, 0, 31337

    def rpc(self, method, params):
        assert method == "eth_call"
        return "0x" + self.answers[params[0]["data"][:10]].hex()


def selector(sig):
    return "0x" + keccak(text=sig)[:4].hex()


def test_the_chain_reads_decode():
    token = "0x" + "22" * 20
    chain = FakeChain({
        selector("assetConfig(uint16)"): abi_encode(["(address,uint8,uint56,uint128,uint64,uint64)"],
                                                    [(token, 1, 10**6, 10**4, 0, 100)]),
        selector("decimals()"): abi_encode(["uint8"], [6]),
        selector("batchPointers()"): abi_encode(["uint64", "uint64", "uint64"], [9, 8, 7]),
    })
    assert chain.asset("0x" + "11" * 20, 3) == {"id": 3, "token": token, "ext": 10**6, "tick": 10**4, "cap": 0,
                                                "min": 100, "decimals": 6}
    assert chain.batch_pointers("0x" + "11" * 20) == (9, 8, 7)


def test_calldata_encodes_the_deposit():
    data = e2e.calldata("deposit(address,uint16,uint8,uint256)", "0x" + "ab" * 20, 3, 1, 10**9)
    assert data[:10] == selector("deposit(address,uint16,uint8,uint256)")
    assert abi_decode(["address", "uint16", "uint8", "uint256"], bytes.fromhex(data[10:])) == ("0x" + "ab" * 20, 3, 1, 10**9)


@pytest.mark.skipif(not os.environ.get("E2E_ZKLOAF_ABI"), reason="E2E_ZKLOAF_ABI names no ABI")
def test_every_zkloaf_call_is_in_its_abi():
    with open(os.environ["E2E_ZKLOAF_ABI"]) as f:
        abi = json.load(f)

    def sig(fn):
        return fn["name"] + "(" + ",".join(i["type"] for i in fn["inputs"]) + ")"

    def outs(fn):
        return [o["type"] if o["type"] != "tuple" else "(" + ",".join(c["type"] for c in o["components"]) + ")"
                for o in fn["outputs"]]

    have = {sig(fn): outs(fn) for fn in abi if fn.get("type") == "function"}
    used = {
        "assetConfig(uint16)": ["(address,uint8,uint56,uint128,uint64,uint64)"],
        "getAccountIndexFromAddress(address)": ["uint48"],
        "changePubKeyNonce(address)": ["uint256"],
        "getPendingBalance(address,uint16)": ["uint128"],
        "batchPointers()": ["uint64", "uint64", "uint64"],
        "deposit(address,uint16,uint8,uint256)": [],
        "withdrawPendingBalance(address,uint16,uint128)": [],
    }
    for s, out in used.items():
        assert have.get(s) == out, s


def test_the_spot_side_and_buffer_are_the_sdks():
    assert e2e.PERPS_SPOT_SIDE == 255 and e2e.BUFFER_MS == 72 * 3_600_000


@pytest.fixture
def env(monkeypatch):
    for k in list(os.environ):
        if k.startswith("E2E_") and k not in ("E2E_SDK", "E2E_ZKLOAF_ABI"):
            monkeypatch.delenv(k)
    monkeypatch.setenv("E2E_API", "http://127.0.0.1:1/api")
    monkeypatch.setenv("E2E_RPC", "http://127.0.0.1:2")
    monkeypatch.setenv("E2E_PSQL", "psql")
    monkeypatch.setenv("E2E_MNEMONIC", "test " * 11 + "junk")
    return monkeypatch


def step(kind):
    def fn(run):
        if kind == "fail":
            raise e2e.Failed("no such thing")
        if kind == "incomplete":
            raise e2e.Incomplete("no hook")
        if kind == "error":
            raise ValueError("boom")
        if kind == "late":
            run.incomplete.append("a leg left")
    return fn


def test_the_step_log_and_verdict(env, capsys):
    fns = {name: step("ok") for name in e2e.STEPS}
    fns.update(spot=step("fail"), perps=step("incomplete"), funding=step("error"), halt=step("late"))
    assert e2e.main(fns) == 1
    out = capsys.readouterr().out
    rcs = {line.split()[-2]: int(line.split()[-1]) for line in out.splitlines() if " ==== exit " in line}
    assert rcs == {"setup": 0, "spot": 1, "perps": 3, "mixed": 0, "funding": 2, "liquidation": 0, "halt": 3, "exit": 0,
                   "proof": 0}
    assert "==== VERDICT FAIL setup=0 spot=1 perps=3 mixed=0 funding=2 liquidation=0 halt=3 exit=0 proof=0" in out
    assert out.rstrip().endswith("==== DONE")


def test_a_setup_that_fails_skips_the_rest(env, capsys):
    fns = {name: step("ok") for name in e2e.STEPS}
    fns["setup"] = step("fail")
    assert e2e.main(fns) == 1
    out = capsys.readouterr().out
    assert " ==== exit setup 1" in out and "==== step spot" not in out and "==== VERDICT FAIL setup=1" in out


def test_e2e_steps_picks_steps_and_setup_always_runs(env, capsys):
    env.setenv("E2E_STEPS", "perps,proof")
    assert e2e.main({name: step("ok") for name in e2e.STEPS}) == 0
    out = capsys.readouterr().out
    assert "==== VERDICT PASS setup=0 perps=0 proof=0" in out


def test_config_refuses_unknown_steps_and_missing_markets(env):
    env.setenv("E2E_STEPS", "perps,teleport")
    with pytest.raises(SystemExit):
        e2e.Config.read()
    env.delenv("E2E_STEPS")
    for var, value in (("E2E_PRICE_CONTROL", "/c"), ("E2E_OPERATOR", "privy")):
        env.setenv(var, value)
        with pytest.raises(SystemExit):
            e2e.Config.read()
        env.delenv(var)
    env.delenv("E2E_MNEMONIC")
    with pytest.raises(SystemExit, match="E2E_WALLET_SEED_FILE or E2E_MNEMONIC"):
        e2e.Config.read()
    env.setenv("E2E_WALLET_SEED_FILE", "/s")
    env.delenv("E2E_PSQL")
    with pytest.raises(SystemExit, match="E2E_PSQL or E2E_PSQL_DSN_FILE"):
        e2e.Config.read()
    env.setenv("E2E_PSQL_DSN_FILE", "/lb.dsn")
    env.setenv("E2E_JOURNAL_DSN_FILE", "/journal.dsn")
    cfg = e2e.Config.read()
    assert (cfg.pg, cfg.journal) == (e2e.Pg(("psql",), "/lb.dsn"), e2e.Pg(("psql",), "/journal.dsn"))
    assert cfg.markets == {"trade": 0, "liq1": 6, "liq0": 2, "halt": 3, "carry_pos": 8, "carry_neg": 9}
    env.delenv("E2E_JOURNAL_DSN_FILE")
    cfg = e2e.Config.read()
    assert cfg.journal == cfg.pg == e2e.Pg(("psql",), "/lb.dsn")
    with pytest.raises(SystemExit):
        e2e.parse_markets("trade=0,liq1=1")


def test_wait_for_retries_reads_and_times_out():
    calls = []

    def probe():
        calls.append(1)
        if len(calls) == 1:
            raise e2e.ChainError("not yet")
        return len(calls) >= 3 and "done"

    assert e2e.wait_for("x", probe, 5, 0.01) == "done"
    with pytest.raises(e2e.Failed, match="no thing within"):
        e2e.wait_for("thing", lambda: None, 0.05, 0.01)


def test_a_numeric_wait_ends_on_0_as_on_any_number():
    """Every numeric wait (a pool's collateral, a deposit's total, L1's pending balance) ends once its number reaches
    the goal, 0 included; a number short of the goal keeps it waiting to its deadline."""
    reads = iter([5, 3] + [0] * 1_000)
    assert e2e.wait_for("x", e2e.value_once(lambda: next(reads), lambda v: v < 1), 1, 0.01) == (0,)
    with pytest.raises(e2e.Failed, match="no y within"):
        e2e.wait_for("y", e2e.value_once(lambda: 7, lambda v: v == 0), 0.05, 0.01)


def test_run_units(env):
    cfg = e2e.Config.read()
    run = e2e.Run(cfg, collateral={"decimals": 6, "tickSize": "10000", "extensionMultiplier": "1000000"})
    assert run.dollars(1_500_123) == Decimal("1500.123") and run.to_index(Decimal("1500.12")) == 1_500_120
    assert run.units(Decimal("1.5")) == 15 and run.tokens(15) == Decimal("1.5")
    assert run.cents(Decimal("1500.123"), True) == Decimal("1500.13") and run.cents(Decimal("1500.129"), False) == Decimal("1500.12")
    assert run.ext_per_dollar() == 100 * 10**6


class FakeBook:
    """One account's spot USDC (base units) and pools (extended units), moved by allocates as the exchange
    moves them: a tick is 10,000 base units ($0.01) and ``ext`` extended units."""

    def __init__(self, spot, ext=1_000_000):
        self.spot, self.pools, self.seq, self.held, self.ext = spot, {}, 0, set(), ext
        self.perps, self.portfolio = self, self

    def allocate(self, amount, *, from_side, to_side):
        ticks = amount * 100
        assert ticks == int(ticks) and ticks > 0, amount
        for side, sign in ((from_side, -1), (to_side, 1)):
            if side == e2e.PERPS_SPOT_SIDE:
                self.spot += sign * int(ticks) * 10_000
            else:
                self.pools[side] = self.pools.get(side, 0) + sign * int(ticks) * self.ext
        self.seq += 1
        return {"journalSeq": self.seq}

    def positions(self):
        return {"pools": [{"pool": p, "collateral": str(c), "positions": [{"market": 0}] if p in self.held else []}
                          for p, c in sorted(self.pools.items())]}

    def component(self):
        return {"balances": [{"assetId": e2e.USDC_ASSET, "total": str(self.spot), "available": str(self.spot)}]}


def test_fund_allocate_and_sweep_move_exact_ticks(env):
    cfg = dataclasses.replace(e2e.Config.read(), wait_s=1)
    run = e2e.Run(cfg, collateral={"decimals": 6, "tickSize": "10000", "extensionMultiplier": "1000000"})
    book = FakeBook(5_000 * 10**6)
    user = e2e.User("A", wallet=None, client=book)
    run.fund(user, 1, Decimal(2_000))
    assert (book.spot, book.pools) == (3_000 * 10**6, {1: 200_000 * 10**6})
    run.fund(user, 1, Decimal(1_500))
    assert book.seq == 1
    run.fund(user, 1, Decimal("2000.005"))
    assert book.seq == 2 and book.pools[1] == 200_100 * 10**6
    book.held.add(1)
    run.allocate(user, Decimal(100), 1, e2e.PERPS_SPOT_SIDE)
    run.sweep(user, 1)
    assert book.seq == 3 and book.pools[1] == 190_100 * 10**6
    book.held.clear()
    book.pools[1] += 999_999  # less than a tick stays
    run.sweep(user, 1)
    assert book.pools[1] == 999_999 and book.spot == 5_000 * 10**6
    with pytest.raises(e2e.Failed):  # an allocate the exchange books wrong fails its check
        book.allocate = lambda amount, *, from_side, to_side: (book.pools.__setitem__(2, 1), {"journalSeq": 9})[1]
        run.allocate(user, Decimal(1), e2e.PERPS_SPOT_SIDE, 2)


def test_a_sweep_that_empties_the_pool_passes(env):
    """With an extension multiplier of 1 (the stack's USDC), a sweep moves the pool's whole collateral and the pool
    reads 0: that 0 is the allocate's answer, not a wait that runs out (run-1's exit step)."""
    cfg = dataclasses.replace(e2e.Config.read(), wait_s=1)
    run = e2e.Run(cfg, collateral={"decimals": 6, "tickSize": "10000", "extensionMultiplier": "1"})
    book = FakeBook(5_000 * 10**6, ext=1)
    user = e2e.User("A", wallet=None, client=book)
    run.fund(user, 0, Decimal(300))
    assert (book.spot, book.pools) == (4_700 * 10**6, {0: 30_000})
    run.sweep(user, 0)
    assert (book.spot, book.pools, book.seq) == (5_000 * 10**6, {0: 0}, 2)


class SpotStop(Exception):
    """Ends the spot step at its first order, which the fake exchange records."""


class FakeSpotClient:
    """A user's client as the spot step reads it up to its first order: the market list, the balances, the order."""

    def __init__(self, sent):
        self.market = self.portfolio = self.orders = self
        self.sent = sent

    def list(self):
        return {"markets": [{"ticker": "EBS"}]}

    def component(self):
        return {"balances": [{"assetId": e2e.USDC_ASSET, "total": str(10**12), "available": str(10**12)}]}

    def limit_buy(self, ticker, quantity, price, **_):
        self.sent.append((Decimal(str(quantity)), Decimal(str(price))))
        raise SpotStop


@pytest.mark.parametrize("ref, size", [("10", 3), ("25", 2), ("1500", 1), ("0.5", 52)])
def test_the_spot_legs_orders_clear_the_backends_least_order_value(env, monkeypatch, ref, size):
    """The backend refuses an order worth under 10 USDL (run-1: a 1-token bid at $9.80). The leg's least order is
    its first, the bid, sized to 2.5 times that value in whole tokens."""
    sent = []
    run = e2e.Run(dataclasses.replace(e2e.Config.read(), spot_price=Decimal(ref)))
    run.spot, run.spot_l2 = "EBS", {"quoteAssetId": e2e.USDC_ASSET, "baseAssetId": 2}
    run.chain = types.SimpleNamespace(asset=lambda proxy, asset: {"decimals": 18})
    run.users = {n: e2e.User(n, wallet=None, client=FakeSpotClient(sent)) for n in e2e.USERS}
    monkeypatch.setattr(e2e.Run, "cancel_all", lambda self: None)
    with pytest.raises(SpotStop):
        e2e.step_spot(run)
    [(quantity, bid)] = sent
    assert bid == run.cents(Decimal(ref) * Decimal("0.98"), False)
    assert quantity == size and quantity * bid >= 25 > (quantity - 1) * bid


class FakeVenue:
    """A spot book and a perps book: a GTC order rests, an IOC fills against one resting at its price (the whole of
    it) or is refused NOT_FILLED; spot moves tokens and cents, perps moves base and nothing else. Books and
    balances are the venue's; each user's client only names the user."""

    def __init__(self, spot="EBS", perps="PERP", ext=1_000_000):
        self.spot, self.perps_t, self.ext = spot, perps, ext
        self.rest = {}  # (ticker, buy) -> [(price, qty, user)]
        self.tokens, self.cents, self.base = {}, {}, {}  # user -> base units; user -> cents; (user, pool) -> base
        self.pools = {}  # (user, pool) -> collateral
        self.fills, self.seq, self.lock = {spot: 0, perps: 0}, 0, threading.Lock()

    def client(self, name):
        return FakeVenueClient(self, name)

    def order(self, user, ticker, buy, qty, price, pool, tif):
        qty, price = Decimal(str(qty)), Decimal(str(price))
        with self.lock:
            self.seq += 1
            if tif != "IOC":
                self.rest.setdefault((ticker, buy), []).append((price, qty, user))
                return {"orderId": self.seq, "status": "OPEN", "quantityLeft": str(qty)}
            book = self.rest.get((ticker, not buy), [])
            hit = next((o for o in book if o[1] == qty and (o[0] <= price if buy else o[0] >= price)), None)
            if hit is None:
                raise LoafValidationError(e2e.NOT_FILLED, status_code=422)
            book.remove(hit)
            maker = hit[2]
            if ticker == self.spot:
                for u, sign in ((user, 1), (maker, -1)):
                    self.tokens[u] = self.tokens.get(u, 0) + sign * (1 if buy else -1) * int(qty * 10**6)
                    cost = int(qty * price * 100)
                    self.cents[u] = self.cents.get(u, 0) - sign * (1 if buy else -1) * (cost + (cost // 200 if u == user else 0))
            else:
                for u, sign in ((user, 1), (maker, -1)):
                    self.base[(u, pool)] = self.base.get((u, pool), 0) + sign * (1 if buy else -1) * int(qty * 10)
            self.fills[ticker] += 1
            return {"orderId": self.seq, "status": "FILLED", "quantityLeft": "0"}


class FakeVenueClient:
    def __init__(self, venue, name):
        self.v, self.name = venue, name
        self.orders = self.perps = self.portfolio = self.market = self

    def limit_buy(self, t, q, p, *, pool=None, time_in_force="GTC", reduce_only=False):
        return self.v.order(self.name, t, True, q, p, pool, time_in_force)

    def limit_sell(self, t, q, p, *, pool=None, time_in_force="GTC", reduce_only=False):
        return self.v.order(self.name, t, False, q, p, pool, time_in_force)

    def cancel_all(self):
        with self.v.lock:
            for book in self.v.rest.values():
                book[:] = [o for o in book if o[2] != self.name]

    def allocate(self, amount, *, from_side, to_side):
        pool, sign = (from_side, -1) if to_side == e2e.PERPS_SPOT_SIDE else (to_side, 1)
        self.v.pools[(self.name, pool)] = self.v.pools.get((self.name, pool), 0) + sign * int(amount * 100) * self.v.ext
        self.v.cents[self.name] = self.v.cents.get(self.name, 0) - sign * int(amount * 100)
        return {"journalSeq": 1}

    def positions(self):
        pools = {p for (u, p) in list(self.v.pools) + list(self.v.base) if u == self.name}
        return {"pools": [{"pool": p, "collateral": str(self.v.pools.get((self.name, p), 0)),
                           "positions": [{"market": 0, "base": str(b)}] if (b := self.v.base.get((self.name, p), 0))
                           else []} for p in sorted(pools)]}

    def component(self):
        return {"balances": [{"assetId": e2e.USDC_ASSET, "total": str(self.v.cents.get(self.name, 0) * 10_000),
                              "available": "0"},
                             {"assetId": 2, "total": str(self.v.tokens.get(self.name, 0)), "available": "0"}]}

    def list(self):
        return {"markets": [{"ticker": self.v.spot, "marketPrice": "10"}]}


def mixed_run(venue, secs="0.4"):
    cfg = dataclasses.replace(e2e.Config.read(), wait_s=2, mixed_s=float(secs))
    run = e2e.Run(cfg, collateral={"decimals": 6, "tickSize": "10000", "extensionMultiplier": "1000000"})
    run.spot, run.spot_l2 = venue.spot, {"quoteAssetId": e2e.USDC_ASSET, "baseAssetId": 2}
    run.chain = types.SimpleNamespace(asset=lambda proxy, asset: {"decimals": 6})
    run.anon = types.SimpleNamespace(perps=types.SimpleNamespace(
        market_live=lambda m: {"tradingState": "OPEN", "indexPrice": "1500000"}))
    run.perps, run.tickers = {0: {"minBase": "1"}}, {0: venue.perps_t}
    run.users = {n: e2e.User(n, wallet=None, client=venue.client(n)) for n in e2e.USERS}
    return run


def test_the_mixed_step_fills_both_books_at_once_and_reconciles(env):
    venue = FakeVenue()
    run = mixed_run(venue)
    e2e.step_mixed(run)
    assert venue.fills["EBS"] > 0 and venue.fills["PERP"] > 0
    assert not any(venue.rest.values()) and not any(venue.base.values())  # books empty, every position flat
    # C bought and sold the same tokens: it paid the spread plus the taker's 0.5 % fee, within the step's 1 % bound
    assert venue.tokens["C"] == 0 and venue.tokens["MM"] == 0 and venue.cents["C"] < 0 < venue.cents["MM"]
    [note] = run.notes
    assert note.startswith("mixed ") and "spot " in note and "perps " in note
    assert "mixed" in e2e.STEPS and e2e.STEP_FNS["mixed"] is e2e.step_mixed
    assert e2e.STEPS.index("mixed") == e2e.STEPS.index("perps") + 1


def test_the_mixed_step_fails_on_a_stranger_fill_or_a_dead_side(env, monkeypatch):
    venue = FakeVenue()
    run = mixed_run(venue)
    # a fee above the 1 % bound: the spot reconcile fails
    taker_fee = venue.order

    def pricey(user, ticker, buy, qty, price, pool, tif):
        r = taker_fee(user, ticker, buy, qty, price, pool, tif)
        if ticker == venue.spot and tif == "IOC":
            venue.cents[user] -= int(Decimal(str(qty)) * Decimal(str(price)) * 100) // 20
        return r
    venue.order = pricey
    with pytest.raises(e2e.Failed, match="plus fees"):
        e2e.step_mixed(run)
    # the perps side raising anything else: the step fails naming the side
    venue = FakeVenue()
    run = mixed_run(venue)
    plain = venue.order

    def refusing(user, ticker, buy, qty, price, pool, tif):
        if ticker == venue.perps_t and tif == "IOC":
            raise LoafValidationError("Order rejected: the market is REDUCE_ONLY", status_code=422)
        return plain(user, ticker, buy, qty, price, pool, tif)
    venue.order = refusing
    with pytest.raises(e2e.Failed, match="the perps side stopped: LoafValidationError"):
        e2e.step_mixed(run)
    # no spot market: INCOMPLETE, as the spot step
    run = mixed_run(FakeVenue())
    run.spot = None
    with pytest.raises(e2e.Incomplete):
        e2e.step_mixed(run)


def test_the_proof_step_pages_the_history_as_the_backend_allows(env):
    """The backend refuses a history page over 100 rows (400 "pageSize: Too big"), so a step that asks more retries
    that refusal to its deadline (run-1's proof step). Within it, a run with no trade has nothing to wait for and
    the step goes on to its batch check."""
    pages = []

    class History:
        def iter_trades(self, *, page_size=100):
            pages.append(page_size)
            if page_size > 100:
                raise LoafValidationError("Validation failed", status_code=400,
                                          details=["pageSize: Too big: expected number to be <=100"])
            return iter(())

    run = e2e.Run(dataclasses.replace(e2e.Config.read(), proof_wait_s=0.05))
    run.chain = types.SimpleNamespace()
    run.users = {n: e2e.User(n, wallet=None, client=types.SimpleNamespace(history=History())) for n in e2e.USERS}
    with pytest.raises(e2e.Failed, match="every trade of the run is IN_BATCH and final"):
        e2e.step_proof(run)
    assert len(pages) == len(e2e.USERS) and max(pages) <= 100


SECRET = "s3cr3t-p@ss/word"


@pytest.mark.parametrize("text", [
    "postgres://lb%5Fuser:" + "s3cr3t-p%40ss%2Fword" + "@127.0.0.1:45433/loaf?sslmode=disable\n",
    "host=127.0.0.1 port=45433 user=lb_user password='" + SECRET + "' dbname=loaf sslmode=disable",
])
def test_a_dsn_file_becomes_pg_variables(tmp_path, text):
    path = tmp_path / "x.dsn"
    path.write_text(text)
    assert e2e.dsn_env(str(path)) == {"PGHOST": "127.0.0.1", "PGPORT": "45433", "PGUSER": "lb_user",
                                      "PGPASSWORD": SECRET, "PGDATABASE": "loaf", "PGSSLMODE": "disable"}


def test_a_bad_dsn_fails_naming_the_file_only(tmp_path):
    for secret, text in (("s3cr3tpass", "postgres://u:s3cr3tpass@h:notaport/db"),
                         (SECRET, "host=h user=u password='" + SECRET + "'"), (SECRET, "x=" + SECRET + " y")):
        path = tmp_path / "bad.dsn"
        path.write_text(text)
        with pytest.raises(e2e.Failed) as got:
            e2e.dsn_env(str(path))
        assert secret not in str(got.value) and str(path) in str(got.value)
        assert got.value.__cause__ is None and (got.value.__context__ is None or got.value.__suppress_context__)


def test_psql_takes_the_dsn_by_environment_never_argv(tmp_path, monkeypatch):
    path = tmp_path / "lb.dsn"
    path.write_text("postgres://u:" + urllib.parse.quote(SECRET, safe="") + "@h:5432/loaf")
    seen = {}

    def fake_run(argv, input, capture_output, text, timeout, env):
        seen.update(argv=argv, env=env, input=input)
        return subprocess.CompletedProcess(argv, 0, "E2E|a|1\nnoise\nE2E|b|2\n", "")

    monkeypatch.setattr(e2e.subprocess, "run", fake_run)
    assert e2e.Pg(("psql",), str(path))("SELECT 1") == [["a", "1"], ["b", "2"]]
    assert SECRET not in " ".join(seen["argv"]) and seen["env"]["PGPASSWORD"] == SECRET
    assert seen["argv"][0] == "psql" and "ON_ERROR_STOP=1" in seen["argv"]
    e2e.Pg(("docker", "exec", "-i", "pg", "psql"))("SELECT 1")
    assert seen["argv"][:5] == ["docker", "exec", "-i", "pg", "psql"] and seen["env"] is None


def test_seed_wallets_are_stable_and_distinct(env, tmp_path):
    seed = tmp_path / "wallet.seed"
    seed.write_text("ab" * 32 + "\n")
    env.setenv("E2E_WALLET_SEED_FILE", str(seed))
    cfg = e2e.Config.read()
    wallets = [e2e.wallet_of(cfg, i, n).address for i, n in enumerate(e2e.USERS)]
    assert len(set(wallets)) == 4 and wallets == [e2e.wallet_of(cfg, i, n).address for i, n in enumerate(e2e.USERS)]
    assert wallets[0] == Account.from_key(keccak(bytes.fromhex("ab" * 32) + b"/A")).address
    seed.write_text("cd" * 32)
    assert e2e.wallet_of(cfg, 0, "A").address != wallets[0]
    seed.write_text("ab" * 8)
    with pytest.raises(e2e.Failed, match="16 bytes"):
        e2e.wallet_of(cfg, 0, "A")


class FakePump:
    """The kit's side of the control file: applies each new control (a thread), or answers it with an error. With
    ``knob`` set ("done", "pending" or "error"), the status carries the kit's knob record for the newest control
    that named the backstop; setting ``dirty`` rewrites the status."""

    def __init__(self, tmp_path, applied=0, error=None):
        self.control, self.status = tmp_path / "price-control.json", tmp_path / "price-status.json"
        self.status.write_text(json.dumps({"applied_seq": applied, "error": None}))
        self.error, self.seen, self.stop, self.below = error, [], False, None
        self.knob, self.knob_seq, self.dirty = None, 0, False
        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.thread.start()

    def loop(self):
        while not self.stop:
            try:
                body = json.loads(self.control.read_text())
            except (OSError, ValueError):
                body = None
            if body and (not self.seen or body["seq"] > self.seen[-1]["seq"]):
                self.seen.append(body)
                if "backstop" in body:
                    self.knob_seq = body["seq"]
                self.dirty = True
            if self.dirty and self.seen:
                self.dirty = False
                self.write(self.seen[-1])
            time.sleep(0.01)

    def write(self, body):
        backstop = {"free_ticks": body.get("backstop", {}).get("free_ticks", 0)}
        if self.knob is not None:
            backstop.update(applied_seq=self.knob_seq, tries=1)
            if self.knob == "done":
                backstop["done"] = {"from": 7, "to": backstop["free_ticks"], "moved_ticks": 7, "journal_seq": 99}
            elif self.knob == "error":
                backstop["error"] = "K1 refused the allocate: market 6 is PRICE_STALE"
            else:
                backstop.update(pending=True, last_error="the mark moved")
        if self.below is not None:
            backstop["below"] = self.below
        tmp = self.status.with_suffix(".tmp")
        tmp.write_text(json.dumps({"applied_seq": body["seq"], "error": self.error, "backstop": backstop}))
        os.replace(tmp, self.status)


def test_price_controls_grow_their_seq_and_wait_for_the_answer(tmp_path):
    pump = FakePump(tmp_path, applied=41)
    prices = e2e.Prices(str(pump.control), str(pump.status), 5)
    try:
        got = prices.send({3: {"mode": "walk", "index": 1_500_000}})
        assert got["applied_seq"] == 42 and pump.seen[-1] == {"seq": 42, "markets": {"3": {"mode": "walk", "index": 1_500_000}}}
        prices.send({3: {"mode": "quiet"}}, backstop={"free_ticks": 0})
        assert pump.seen[-1] == {"seq": 43, "markets": {"3": {"mode": "quiet"}}, "backstop": {"free_ticks": 0}}
        pump.status.write_text(json.dumps({"applied_seq": 50}))  # a status ahead of the file
        assert prices.next_seq() == 51
        assert [p.name for p in tmp_path.iterdir() if ".tmp" in p.name and "e2e" in p.name] == []
    finally:
        pump.stop = True


def test_a_control_the_pump_answers_with_an_error_fails(tmp_path):
    pump = FakePump(tmp_path, error="market 3: no such market")
    try:
        with pytest.raises(e2e.Failed, match="no such market"):
            e2e.Prices(str(pump.control), str(pump.status), 5).send({3: {"mode": "walk", "index": 1}})
    finally:
        pump.stop = True
    silent = tmp_path / "silent"
    silent.mkdir()
    with pytest.raises(e2e.Failed, match="answer to control 1"):
        e2e.Prices(str(silent / "c.json"), str(silent / "s.json"), 0.3).send({3: {"mode": "quiet"}})
    (silent / "s.json").write_text(json.dumps({"applied_seq": 1, "file_error": "unknown field `markts`"}))
    with pytest.raises(e2e.Failed, match=r"answer to control 2.*could not read the control: .*markts"):
        e2e.Prices(str(silent / "c.json"), str(silent / "s.json"), 0.3).send({3: {"mode": "quiet"}})


def test_the_backstop_waits_for_the_status_to_read_it(env, tmp_path):
    pump = FakePump(tmp_path)
    run = e2e.Run(dataclasses.replace(e2e.Config.read(), wait_s=5), usdc={"decimals": 6, "tick": 10_000})
    run.pump = e2e.Prices(str(pump.control), str(pump.status), 5)
    try:
        assert run.backstop_ticks() == 5_000_000  # $50,000 at $0.01 a tick
        run.backstop(run.backstop_ticks())
        run.backstop(0)
        assert [p.get("backstop") for p in pump.seen] == [{"free_ticks": 5_000_000}, {"free_ticks": 0}]
    finally:
        pump.stop = True
    run.pump = None
    with pytest.raises(e2e.Incomplete):
        run.walk(3, 1)


def test_the_backstop_waits_for_its_knob_done_and_fails_on_its_error(env, tmp_path):
    """The kit's knob record decides (me-p5-bench's contract): pending waits, done ends the wait, error fails."""
    pump = FakePump(tmp_path)
    pump.knob = "pending"
    run = e2e.Run(dataclasses.replace(e2e.Config.read(), wait_s=5), usdc={"decimals": 6, "tick": 10_000})
    run.pump = e2e.Prices(str(pump.control), str(pump.status), 5)
    flipped = threading.Event()

    def flip():
        pump.knob, pump.dirty = "done", True
        flipped.set()

    try:
        threading.Timer(1.5, flip).start()
        run.backstop(5_000_000)
        assert flipped.is_set() and pump.knob_seq == pump.seen[-1]["seq"]
        pump.knob = "error"
        with pytest.raises(e2e.Failed, match=r"knob \d+ \(free collateral to 0 ticks\) failed: .K1 refused"):
            run.backstop(0)
    finally:
        pump.stop = True


def test_the_liquidation_step_refuses_a_stack_whose_backstop_holds_a_position_on_its_market(env, monkeypatch):
    """A stack an earlier run used: the backstop's long on the deleverage market fails the step before anything."""
    run = e2e.Run(e2e.Config.read())
    seen = []

    def journal(sql):
        seen.append(sql)
        return [["6", "0", "12"]]

    run.cfg = dataclasses.replace(run.cfg, journal=journal)
    run.users = {"C": object(), "MM": object()}
    monkeypatch.setattr(e2e.Run, "cancel_all", lambda self: None)
    monkeypatch.setattr(e2e.Run, "fund", lambda self, user, pool, dollars: None)
    monkeypatch.setattr(e2e.Run, "backstop", lambda self, ticks: pytest.fail("the backstop moved"))
    refused = r"the backstop holds no position on market 6 .*fresh stack.*\['6', '0', '12'\]"
    with pytest.raises(e2e.Failed, match=refused):
        e2e.step_liquidation(run)
    assert "account_index = 1 AND bal <> 0 AND market IN (6)" in seen[0]



class FakeStack:
    """The engine and the backend for a step's restore. A market's index is the pump's newest walk of it (else
    ``start``, else 1,500,000); its state is ``states``' (the admin halt and the operator's controls set it), else
    PRICE_STALE while the pump's newest mode for it is quiet, else OPEN. Patches Run.index, Run.live, Run.admin and
    Pg.__call__ (the operator's flags)."""

    def __init__(self, monkeypatch, pump, states=None, start=None):
        self.pump, self.states, self.start, self.flags = pump, dict(states or {}), dict(start or {}), {}
        stack = self
        monkeypatch.setattr(e2e.Run, "index", lambda run, m: stack.index(m))
        monkeypatch.setattr(e2e.Run, "live",
                            lambda run, m: {"indexPrice": stack.index(m), "tradingState": stack.state(m)})
        monkeypatch.setattr(e2e.Run, "admin", lambda run, path, body: stack.admin(path, body))
        monkeypatch.setattr(e2e.Pg, "__call__", lambda pg, sql: stack.pg(sql))

    def mode(self, m):
        for body in reversed(self.pump.seen):
            got = body.get("markets", {}).get(str(m))
            if got:
                return got
        return None

    def index(self, m):
        for body in reversed(self.pump.seen):
            got = body.get("markets", {}).get(str(m))
            if got and got["mode"] == "walk":
                return got["index"]
        return self.start.get(m, 1_500_000)

    def state(self, m):
        if m in self.states:
            return self.states[m]
        return "PRICE_STALE" if (self.mode(m) or {}).get("mode") == "quiet" else "OPEN"

    def admin(self, path, body):
        m = body["marketIndex"]
        if path == "/admin/markets/halt":
            self.states[m] = "HALTED"
            answer = {"engineLoaded": True}
        else:  # the engine's load of a LIVE market adopts its row's flags
            self.states[m] = "REDUCE_ONLY" if self.flags.get(m) else "OPEN"
            answer = {"oldStatus": "LIVE"}
        return types.SimpleNamespace(status_code=200, json=lambda: answer, text=json.dumps(answer))

    def pg(self, sql):
        m = int(re.search(r"WHERE market_index = (\d+)", sql).group(1))
        self.flags[m] = "is_reduce_only = true" in sql
        return [[str(m), "f", "t" if self.flags[m] else "f"]]


def walks_of(pump):
    return [(int(m), v["index"]) for b in pump.seen for m, v in b["markets"].items() if v["mode"] == "walk"]


def knobs_of(pump):
    return [b["backstop"]["free_ticks"] for b in pump.seen if "backstop" in b]


@pytest.mark.parametrize("home", ["1500000", None])
def test_a_failing_step_puts_back_what_it_moved(env, tmp_path, monkeypatch, capsys, home):
    """Run-2's liquidation and halt steps failed at a check and left their markets walked, halted and the knob up:
    the step's restore (run_step's finally) reopens what it controlled, walks home what it moved (E2E_HOME_INDEX,
    else the index before its first move) and puts the knob at 0."""
    pump = FakePump(tmp_path)
    stack = FakeStack(monkeypatch, pump, start={6: 1_234_000, 2: 1_455_000, 8: 1_510_000})
    env.setenv("E2E_STEPS", "liquidation")
    env.setenv("E2E_WAIT_S", "5")
    env.setenv("E2E_OPERATOR", "sql")
    if home:
        env.setenv("E2E_HOME_INDEX", home)

    def moves(run):
        run.pump = e2e.Prices(str(pump.control), str(pump.status), 5)
        run.walk(6, 1_386_086)
        run.backstop(5_000_000)
        run.walk(2, 1_432_808)
        assert run.halt(3).status_code == 200
        run.operator(0, "REDUCE_ONLY")
        run.silence(8)
        assert {m: stack.state(m) for m in (0, 3, 8)} == {0: "REDUCE_ONLY", 3: "HALTED", 8: "PRICE_STALE"}
        raise e2e.Failed("no the tape's take 299153 within 180 s; last None")

    try:
        assert e2e.main({"setup": step("ok"), "liquidation": moves}) == 1
    finally:
        pump.stop = True
    out = capsys.readouterr().out
    homes = {m: int(home) if home else stack.start[m] for m in (2, 6, 8)}
    assert " ==== exit liquidation 1" in out and "not restored" not in out
    assert (f"restore: market 0 OPEN; market 3 OPEN; market 8 OPEN; market 2's index home at {homes[2]}; "
            f"market 6's index home at {homes[6]}; market 8's index home at {homes[8]}; "
            "the backstop's knob at 0") in out
    assert walks_of(pump) == [(6, 1_386_086), (2, 1_432_808), (0, 1_500_000), (3, 1_500_000), (8, 1_510_000),
                              (2, homes[2]), (6, homes[6]), (8, homes[8])]
    assert knobs_of(pump) == [5_000_000, 0]
    assert {m: stack.state(m) for m in (0, 2, 3, 6, 8)} == dict.fromkeys((0, 2, 3, 6, 8), "OPEN")
    assert stack.flags == {0: False, 3: False}


@pytest.mark.parametrize("kind, rc", [("ok", 2), ("fail", 1)])
def test_a_step_whose_restore_fails_says_what_it_left(env, tmp_path, monkeypatch, capsys, kind, rc):
    """No operator to reopen the halted market (E2E_OPERATOR unset): the stack is left as it is, the log names it,
    and a step that passed is an error; one that failed keeps its verdict."""
    pump = FakePump(tmp_path)
    FakeStack(monkeypatch, pump)
    env.setenv("E2E_STEPS", "halt")
    env.setenv("E2E_WAIT_S", "5")

    def halts(run):
        run.pump = e2e.Prices(str(pump.control), str(pump.status), 5)
        run.halt(3)
        step(kind)(run)

    try:
        assert e2e.main({"setup": step("ok"), "halt": halts}) == 1
    finally:
        pump.stop = True
    out = capsys.readouterr().out
    assert f" ==== exit halt {rc}" in out
    assert "ERROR halt: not restored: market 3 OPEN: Incomplete: OPEN on market 3 needs an operator session" in out


def test_the_one_shot_restore_runs_alone_and_puts_every_role_market_back(env, tmp_path, monkeypatch, capsys):
    """E2E_STEPS=restore: no setup; every role market reopened if it is not OPEN and walked home, the knob at 0."""
    pump = FakePump(tmp_path)
    stack = FakeStack(monkeypatch, pump, states={3: "HALTED"}, start={2: 1_432_808, 3: 1_480_000})
    monkeypatch.setattr(e2e, "LoafClient", lambda **kw: object())
    for var, value in (("E2E_STEPS", "restore"), ("E2E_WAIT_S", "5"), ("E2E_OPERATOR", "sql"),
                       ("E2E_HOME_INDEX", "1500000"), ("E2E_PRICE_CONTROL", str(pump.control)),
                       ("E2E_PRICE_STATUS", str(pump.status))):
        env.setenv(var, value)
    fns = dict(e2e.STEP_FNS, setup=lambda run: pytest.fail("setup ran"))
    try:
        assert e2e.main(fns) == 0
    finally:
        pump.stop = True
    out = capsys.readouterr().out
    assert "==== step setup" not in out and "==== VERDICT PASS restore=0" in out
    assert walks_of(pump) == [(3, 1_480_000)] + [(m, 1_500_000) for m in (0, 2, 3, 6, 8, 9)]
    assert knobs_of(pump) == [0] and stack.flags == {3: False}
    assert all(f"ok market {m} is OPEN at its home index 1500000" in out for m in (0, 2, 3, 6, 8, 9))


def test_the_one_shot_restore_runs_alone_and_needs_its_home(env, capsys):
    env.setenv("E2E_STEPS", "restore,halt")
    with pytest.raises(SystemExit, match="restore runs alone"):
        e2e.Config.read()
    env.setenv("E2E_STEPS", "restore")
    assert e2e.main() == 1
    out = capsys.readouterr().out
    assert "INCOMPLETE restore: the restore needs the kit's price control" in out and " ==== exit restore 3" in out


def test_a_stop_runs_the_steps_restore_on_the_way_out(env, tmp_path, monkeypatch, capsys):
    """SIGTERM (a stopped run, as run-1 was) ends the run as SystemExit: the running step's restore still runs."""
    pump = FakePump(tmp_path)
    FakeStack(monkeypatch, pump)
    for var, value in (("E2E_STEPS", "liquidation,halt"), ("E2E_WAIT_S", "5"), ("E2E_HOME_INDEX", "1500000")):
        env.setenv(var, value)
    before = signal.getsignal(signal.SIGTERM)

    def stopped(run):
        assert signal.getsignal(signal.SIGTERM) is e2e._stopped
        run.pump = e2e.Prices(str(pump.control), str(pump.status), 5)
        run.walk(2, 1_432_808)
        e2e._stopped(signal.SIGTERM, None)  # what the handler does on a SIGTERM here

    try:
        assert e2e.main({"setup": step("ok"), "liquidation": stopped, "halt": step("ok")}) == 143
    finally:
        pump.stop = True
    out = capsys.readouterr().out
    assert signal.getsignal(signal.SIGTERM) is before
    assert "STOPPED (exit 143)" in out and "==== step halt" not in out and "==== VERDICT FAIL setup=0" in out
    assert walks_of(pump) == [(2, 1_432_808), (2, 1_500_000)]


M = 65_536  # a pool reads its requirement x 65,536


def test_backstop_targets_pick_what_the_backstop_would_act_on():
    pools = [
        {"account": 9, "pool": 2, "value": 10, "requirement": 100 * M, "markets": [6]},  # the leg's victim
        {"account": 40, "pool": 1, "value": 90, "requirement": 100 * M, "markets": [4]},  # 9/10: the book's tier
        {"account": 41, "pool": 1, "value": 66, "requirement": 100 * M, "markets": [4]},  # under 2/3: mode 1
        {"account": 46, "pool": 1, "value": 67, "requirement": 100 * M, "markets": [4]},  # 2/3 and over: the book's
        {"account": 42, "pool": 3, "value": 90, "requirement": 100 * M, "markets": [5]},  # on an expired market
        {"account": 43, "pool": 0, "value": -1, "requirement": 0, "markets": []},  # flat in debt: its write-off
        {"account": 44, "pool": 0, "value": 0, "requirement": 0},  # flat, no debt
        {"account": 45, "pool": 1, "value": 100, "requirement": 100 * M, "markets": [5]},  # at maintenance
    ]
    got = e2e.backstop_targets(pools, {5}, (9, 2))
    assert [(p["account"], p["pool"]) for p in got] == [(41, 1), (42, 3), (43, 0)]


def test_metric_value_reads_an_unlabelled_sample():
    page = (f"# HELP {e2e.LIQ_PENDING} pools\n# TYPE {e2e.LIQ_PENDING} gauge\n"
            f'{e2e.LIQ_PENDING}_total{{reason="book"}} 7\n{e2e.LIQ_PENDING} 3\n')
    assert e2e.metric_value(page, e2e.LIQ_PENDING) == 3
    assert e2e.metric_value("other 1\n", e2e.LIQ_PENDING) is None


@pytest.fixture
def guarded(env, tmp_path):
    """A run on a fake pump, market 5 expired; the leg's victim is account 9's pool 2."""
    pump = FakePump(tmp_path)
    run = e2e.Run(dataclasses.replace(e2e.Config.read(), wait_s=5), usdc={"decimals": 6, "tick": 10_000})
    run.pump = e2e.Prices(str(pump.control), str(pump.status), 5)
    run.perps = {4: {"status": "ACTIVE"}, 5: {"status": "EXPIRED"}}
    yield run, pump
    pump.stop = True


def test_the_backstop_guard_fails_naming_another_pool_that_waits_for_it(guarded):
    run, pump = guarded
    pump.below = [{"account": 9, "pool": 2, "value": 10, "requirement": 100 * M, "markets": [6]},
                  {"account": 77, "pool": 3, "value": 50, "requirement": 100 * M, "markets": [4]}]
    with pytest.raises(e2e.Failed, match=r"waits for the backstop: account 77 pool 3 \(value 50, requirement 6553600"):
        run.backstop_clear((9, 2))
    assert pump.seen[-1] == {"seq": pump.seen[-1]["seq"], "markets": {}}  # a no-op control: a status written after


def test_the_backstop_guard_passes_the_victim_and_the_books_tier(guarded):
    run, pump = guarded
    pump.below = [{"account": 9, "pool": 2, "value": 10, "requirement": 100 * M, "markets": [6]},
                  {"account": 40, "pool": 1, "value": 90, "requirement": 100 * M, "markets": [4]}]
    assert run.backstop_clear((9, 2)) is True
    pump.below = []
    assert run.backstop_clear((9, 2)) is True and run.incomplete == []


def test_the_backstop_guard_without_a_list_reads_the_debts_and_the_engine_gauge(guarded, monkeypatch):
    run, _ = guarded
    debts: list[list[str]] = []
    run.cfg = dataclasses.replace(run.cfg, journal=lambda sql: debts if "l2_perps_pools" in sql else [])
    assert run.backstop_clear((9, 2)) is False and "nothing tells" in run.incomplete[0]
    page = types.SimpleNamespace(text=f"{e2e.LIQ_PENDING} 0\n")
    monkeypatch.setattr(e2e.httpx, "get", lambda url, timeout: page)
    run.cfg = dataclasses.replace(run.cfg, engine_metrics="http://127.0.0.1:3/metrics")
    assert run.backstop_clear((9, 2)) is True
    page.text = f"{e2e.LIQ_PENDING} 2\n"
    with pytest.raises(e2e.Failed, match="the engine holds 2 pools below maintenance"):
        run.backstop_clear((9, 2))
    page.text = f"{e2e.LIQ_PENDING} 0\n"
    debts[:] = [["9", "2", "-5"], ["5", "1", "-1200"]]
    with pytest.raises(e2e.Failed, match=r"write-off: account 5 pool 1 \(collateral -1200\)$"):
        run.backstop_clear((9, 2))


P5_ENV = """# the phase's settings
P5_LB_URL=http://127.0.0.1:48005
P5_LB_WS='ws://127.0.0.1:48005/ws'
P5_RPC="http://127.0.0.1:48545"
P5_FUNDER=0x9965507d1a55bcc2695c58ba16fb37d819b0a4dc
P5_ENGINE_HEAD=366d0143
NOT_P5=1
P5_EVIL=$(touch {canary})
"""


@pytest.fixture
def p5kit(tmp_path):
    """A copy of p5-fullstack.sh beside a run.sh that prints its E2E_* environment, a kit directory, flock and
    pgrep stand-ins on PATH, this Python as its python3, and a driver lock of the test's own."""
    here = os.path.dirname(os.path.abspath(e2e.__file__))
    drv, kit, bin_ = tmp_path / "drv", tmp_path / "kit", tmp_path / "bin"
    for d in (drv, kit / "run", bin_):
        d.mkdir(parents=True)
    script = drv / "p5-fullstack.sh"
    script.write_text(open(os.path.join(here, "p5-fullstack.sh")).read())
    (drv / "run.sh").write_text("#!/usr/bin/env bash\nenv | grep '^E2E_' | sort\necho \"args $*\"\n")
    (bin_ / "flock").write_text("#!/usr/bin/env bash\nshift 2\nexec \"$@\"\n")
    (bin_ / "pgrep").write_text("#!/usr/bin/env bash\nexit 1\n")
    (bin_ / "psql").write_text("#!/usr/bin/env bash\nexit 0\n")
    (bin_ / "systemd-run").write_text("#!/usr/bin/env bash\nwhile [ \"$1\" != -- ]; do shift; done; shift\nexec \"$@\"\n")
    (bin_ / "python3").write_text(f"#!/bin/sh\nexec '{sys.executable}' \"$@\"\n")
    stand_ins = ("flock", "pgrep", "psql", "systemd-run", "python3")
    for f in (script, drv / "run.sh", *(bin_ / name for name in stand_ins)):
        f.chmod(0o755)
    (kit / "fullstack.ok").write_text("")
    (kit / "round.log").write_text("x\n==== DONE\n\n")
    (kit / "run" / "fullstack.env").write_text(P5_ENV.format(canary=tmp_path / "canary"))
    env = {k: v for k, v in os.environ.items() if not k.startswith(("E2E_", "P5_"))}
    env.update(PATH=f"{bin_}:/usr/bin:/bin", P5_KIT=str(kit), P5_HEAVY_LOCK=str(tmp_path / "lock"),
               P5_DRIVER_LOCK=str(tmp_path / "driver.lock"))
    return script, kit, env, tmp_path


def test_p5_fullstack_maps_the_phase_and_evaluates_nothing(p5kit):
    script, kit, env, tmp = p5kit
    done = subprocess.run([str(script), "/sdk", "/log"], capture_output=True, text=True, env=env, timeout=60)
    assert done.returncode == 0, done.stderr
    got = dict(line.split("=", 1) for line in done.stdout.splitlines() if line.startswith("E2E_"))
    run = kit / "run"
    assert got == {
        "E2E_API": "http://127.0.0.1:48005/api", "E2E_WS": "ws://127.0.0.1:48005/ws", "E2E_RPC": "http://127.0.0.1:48545",
        "E2E_FUNDER": "0x9965507d1a55bcc2695c58ba16fb37d819b0a4dc",
        "E2E_PRICE_CONTROL": f"{run}/price-control.json", "E2E_PRICE_STATUS": f"{run}/price-status.json",
        "E2E_SETTLESUB_METRICS": "http://127.0.0.1:49476/metrics", "E2E_ADMIN_API_KEY_FILE": f"{run}/lb-admin.key",
        "E2E_ENGINE_METRICS": "http://127.0.0.1:49091/metrics",
        "E2E_OPERATOR": "sql", "E2E_HOME_INDEX": "1500000", "E2E_TAG": "p5fs",
        "E2E_PSQL_DSN_FILE": f"{run}/lb.dsn", "E2E_JOURNAL_DSN_FILE": f"{run}/journal.dsn",
        "E2E_WALLET_SEED_FILE": str(script.parent / "state" / "wallet.seed"),
    }
    assert "args /sdk /log" in done.stdout and "engine 366d0143" in done.stdout
    assert not (tmp / "canary").exists()
    seed = script.parent / "state" / "wallet.seed"
    assert stat.S_IMODE(seed.stat().st_mode) == 0o600 and len(bytes.fromhex(seed.read_text())) == 32
    first = seed.read_text()
    (kit / "run" / "psql").write_text("#!/usr/bin/env bash\n")
    (kit / "run" / "psql").chmod(0o755)
    done = subprocess.run([str(script), "/sdk", "/log"], capture_output=True, text=True, env=env, timeout=60)
    assert f"E2E_PSQL={kit}/run/psql" in done.stdout and "E2E_PSQL_DSN_FILE" not in done.stdout
    assert seed.read_text() == first


@pytest.mark.parametrize("breakage, why", [
    ("no-ok", "fullstack phase is not up"), ("not-done", "does not end ==== DONE"), ("round", "round.sh is running"),
])
def test_p5_fullstack_refuses_a_kit_that_is_not_ready(p5kit, breakage, why):
    script, kit, env, _ = p5kit
    if breakage == "no-ok":
        (kit / "fullstack.ok").unlink()
    elif breakage == "not-done":
        (kit / "round.log").write_text("==== DONE\nstep 3\n")
    else:
        pgrep = env["PATH"].split(":")[0] + "/pgrep"
        with open(pgrep, "w") as f:
            f.write("#!/usr/bin/env bash\nexit 0\n")
    done = subprocess.run([str(script), "/sdk", "/log"], capture_output=True, text=True, env=env, timeout=60)
    assert done.returncode == 2 and why in done.stderr and "args" not in done.stdout


def test_p5_fullstack_holds_the_driver_lock_for_its_run_and_refuses_a_second_driver(p5kit):
    """While one driver runs, a second one (started here from inside the first's run.sh) is refused at once, naming
    the holder; the lock is free once the first run ends."""
    script, _, env, _ = p5kit
    (script.parent / "run.sh").write_text(
        "#!/usr/bin/env bash\n"
        "if [ -n \"${SECOND:-}\" ]; then echo 'second driver ran'; exit 0; fi\n"
        "echo \"first driver runs: $(head -n 1 \"$P5_DRIVER_LOCK\")\"\n"
        "SECOND=1 \"$(dirname \"$0\")/p5-fullstack.sh\" /sdk /log2 2>&1 \\\n"
        "    && echo 'second rc 0' || echo \"second rc $?\"\n")
    done = subprocess.run([str(script), "/sdk", "/log"], capture_output=True, text=True, env=env, timeout=60)
    assert done.returncode == 0, done.stderr
    lock = env["P5_DRIVER_LOCK"]
    first = re.search(r"first driver runs: (pid \d+ p5-fullstack\.sh \(sdk e2e\) since \S+Z)$", done.stdout, re.M)
    assert first, done.stdout
    assert f"another driver holds {lock} by: {first[1]}; one driver at a time" in done.stdout, done.stdout
    assert "second rc 2" in done.stdout and "second driver ran" not in done.stdout
    with open(lock) as f:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)


@pytest.mark.parametrize("alive", [True, False])
def test_p5_fullstack_is_refused_at_once_while_another_driver_holds_the_lock(p5kit, alive):
    """Another lane's driver holding the lock (flock(2) on its own open, as `flock -x` takes it) refuses this one
    before anything runs; the holder's line is named only while its pid lives."""
    script, _, env, _ = p5kit
    lock = env["P5_DRIVER_LOCK"]
    pid = os.getpid()
    if not alive:
        gone = subprocess.Popen(["true"])
        gone.wait()
        pid = gone.pid
    with open(lock, "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        held.write(f"pid {pid} browser pass since 2026-10-27T17:30:00Z\n")
        held.flush()
        done = subprocess.run([str(script), "/sdk", "/log"], capture_output=True, text=True, env=env, timeout=60)
    assert done.returncode == 2 and "args" not in done.stdout and "E2E_" not in done.stdout
    by = f" by: pid {pid} browser pass since 2026-10-27T17:30:00Z" if alive else ""
    assert f"another driver holds {lock}{by}; one driver at a time on the stack" in done.stderr, done.stderr

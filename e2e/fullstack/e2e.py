#!/usr/bin/env python3
"""The full-stack perps e2e driver: the Python SDK is the only trading client, against the backend's REST,
WebSocket and admin routes, with the devnet's L1 for the money that moves on chain.

Steps, in order (README.md has what each asserts):

  setup        users A, B, C and the market maker MM (rows in the backend's database, an API key each), gas,
               L1 deposits credited on L2, each user's trading key derived from its wallet and registered; the
               role markets walked to E2E_HOME_INDEX
  spot         a limit order rests and is cancelled, post-only rests and refuses to take, IOC, FOK, a crossing
               fill between two users
  perps        allocates into pool 0 and isolated pools; long and short by limit and market; post-only, IOC, FOK,
               reduce-only; TP/SL legs armed by the first fill, the take-profit triggered; the position, pool and
               market reads
  funding      ticks of both signs (the two carry markets), funding owed by sign, the pool's value, the close
  liquidation  price walks: with the backstop's free collateral at 0, a deleverage (C's long closed against the
               best-ranked counter, whose own close is under the same journalSeq); with it funded, the backstop's
               take (mode 1); a take into the maker's bid (mode 0) with the maker's liquidation_fill and the tape's
               take (tradeId == perpsSeq == the close's journalSeq)
  halt         the admin halt refuses orders and the API key cannot resume it; the operator's reduce-only takes
               only reducing orders and its reopen opens again; an unreported price turns the market PRICE_STALE,
               which takes only reducing orders until the reports resume
  exit         pool 0 closed and allocated out to spot, an L2 withdrawal, its L1 claim paid
  proof        the run's trades in final batches, every batch through the run's last verified and executed on
               L1, settlesub's verified batch equal to L1's, no new settled refusal

Whatever its verdict, each step then puts back what it moved on the stack (run_step's finally, Run.restore): the
markets whose state it controlled OPEN again, the markets it walked or silenced back home, the backstop's knob at 0
if it raised it; a stop (SIGTERM) runs it too. `E2E_STEPS=restore` alone does only that for every role market and
the knob, with no setup: the one-shot for a stack a run left behind.

Each step logs `==== step <name>` and `==== exit <name> <rc>`: 0 passed, 1 an assertion failed, 2 an error (so
is a passing step whose restore failed), 3 incomplete (a leg the run's configuration cannot drive; the rest of the
step passed). A setup that does not pass skips every later step. The last lines are
`==== VERDICT PASS|FAIL <step>=<rc> ...` and `==== DONE`; the exit status is 0 only when every step that ran passed.

Configuration is the environment only (README.md: E2E_*). Nothing secret is written or printed: the API keys are
minted for the run and kept in memory, the wallets derive from a seed file or a mnemonic, the trading keys from
the wallets' signatures, and the admin key and the database DSNs are read from their files when a request needs
them, never put in an argv.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shlex
import signal
import subprocess
import sys
import time
import traceback
import urllib.parse
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any, Callable, Mapping, Sequence

import httpx
from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode
from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils import keccak, to_checksum_address
from loaf import k1
from loaf.client import LoafClient
from loaf.constants import PERPS_SPOT_SIDE
from loaf.exceptions import (
    LoafAPIError,
    LoafConnectionError,
    LoafNotFoundError,
    LoafValidationError,
    MarketHaltedError,
    MarketStateError,
    PostOnlyWouldTakeError,
)
from loaf.secret_key import l2_key_typed_data

STEPS = ("setup", "spot", "perps", "funding", "liquidation", "halt", "exit", "proof")
#: The one-shot restore (E2E_STEPS=restore, alone): step_restore.
RESTORE = "restore"
#: E2E_KEEP_BACKSTOP=1: a step's restore leaves the backstop's knob where the step put it, so a position the backstop
#: took in the step (liquidation's mode 1) stays on the stack for the desk leg that unwinds it (the P5 kit's phase
#: desk); the one-shot restore puts the knob back later. Every other part of the restore runs.
KEEP_BACKSTOP = (os.environ.get("E2E_KEEP_BACKSTOP") or "").strip() == "1"

#: The run's users, in HD order from E2E_HD_BASE.
USERS = ("A", "B", "C", "MM")
#: Each user's L2 USDC the setup tops up to (dollars); every run deposits at least MIN_DEPOSIT.
USDC_TARGET = {"A": Decimal(50_000), "B": Decimal(50_000), "C": Decimal(50_000), "MM": Decimal(200_000)}
MIN_DEPOSIT = Decimal(100)
#: The perps collateral, and a deposit's or withdrawal's route (TxTypes.RouteType: 0 perps, 1 spot).
USDC_ASSET = 3
ROUTE_SPOT = 1
#: ZkLoaf's NIL_ACCOUNT_INDEX: an address that never deposited.
NIL_ACCOUNT = 2**48 - 1
#: The engine's BUFFER (l2-settle-schema L2_BUFFER_MS, 72 h): a signed withdrawal's expiry must clear it.
BUFFER_MS = 259_200_000
#: The backend's texts for the engine's order-term refusals (Loaf-Backend ordersService.ts).
NOT_FILLED = "Order rejected: no resting order could fill any of it"
FOK_NOT_FILLED = "Fill-or-kill order rejected: it could not be filled in full"
#: The backend's least order value, quote dollars (Loaf-Backend orderPlacementGates.ts MIN_ORDER_VALUE_USDL); the
#: spot leg sizes its least order, its bid, to SPOT_VALUE_MARGIN times it.
SPOT_MIN_VALUE = Decimal(10)
SPOT_VALUE_MARGIN = Decimal("2.5")
#: The backend's largest history page (Shared-Types historyPageQuerySchema, MAX_PAGE_SIZE): a larger one is a 400.
HISTORY_PAGE_MAX = 100
#: The admin API key's resume refusal (Loaf-Backend adminPerpsController.ts RESUME_OPERATOR_REQUIRED).
RESUME_NEEDS_OPERATOR = "A resume (ACTIVE) needs an operator session (Privy): the admin API key halts only"
# The pools (0 the cross pool, 1-7 isolated, one position each): A trades pool 0, and isolated 1 (limits), 2 (a
# reduce-only order that must open nothing), 3 (TP/SL), 5 (the halt market), 6 and 7 (the carry markets); B and
# MM trade pool 0; C's victims hold 1 (the deleverage), 2 (mode 1) and 4 (mode 0).


# --------------------------------------------------------------------------------------------- #
# Configuration, log, checks
# --------------------------------------------------------------------------------------------- #


def _env(name: str, default: str | None = None) -> str:
    value = (os.environ.get(name) or "").strip()
    if value:
        return value
    if default is None:
        raise SystemExit(f"{name} is required (README.md)")
    return default


def parse_markets(text: str) -> dict[str, int]:
    out = {}
    for part in text.split(","):
        role, _, index = part.partition("=")
        out[role.strip()] = int(index)
    missing = {"trade", "liq1", "liq0", "halt", "carry_pos", "carry_neg"} - set(out)
    if missing:
        raise SystemExit(f"E2E_MARKETS names no {sorted(missing)}")
    return out


class Failed(Exception):
    """An assertion of the run failed."""


class Incomplete(Exception):
    """A leg the run's configuration cannot drive."""


class ChainError(Exception):
    """The devnet's RPC answered an error."""


#: A libpq DSN's keywords the driver passes on, as psql's environment variables.
_PG_ENV = {"host": "PGHOST", "port": "PGPORT", "user": "PGUSER", "password": "PGPASSWORD", "dbname": "PGDATABASE",
           "sslmode": "PGSSLMODE"}


def dsn_env(path: str) -> dict[str, str]:
    """The PG* variables of the DSN in file ``path`` (a postgres:// URL or libpq key=value words). What the file
    holds is never echoed: a DSN psql cannot take fails naming the file only."""
    with open(path) as f:
        text = f.read().strip()
    try:
        if text.startswith(("postgres://", "postgresql://")):
            u = urllib.parse.urlsplit(text)
            words: dict[str, str | None] = {
                "host": u.hostname, "port": str(u.port) if u.port else None,
                "user": urllib.parse.unquote(u.username) if u.username else None,
                "password": urllib.parse.unquote(u.password) if u.password else None,
                "dbname": urllib.parse.unquote(u.path[1:]) or None,
            }
            words.update((k, v[0]) for k, v in urllib.parse.parse_qs(u.query).items())
        else:
            words = dict(w.split("=", 1) for w in shlex.split(text))
    except ValueError:
        raise Failed(f"{path} holds no DSN psql takes") from None
    env = {_PG_ENV[k]: v for k, v in words.items() if k in _PG_ENV and v}
    if "PGDATABASE" not in env:
        raise Failed(f"{path} names no database")
    return env


@dataclass(frozen=True)
class Pg:
    """psql on one database: ``argv`` (E2E_PSQL), with the PG* variables of the DSN in ``dsn_file``
    (E2E_PSQL_DSN_FILE) when there is one, so no DSN reaches an argv or the log."""

    argv: tuple[str, ...]
    dsn_file: str | None = None

    @classmethod
    def of(cls, argv_var: str, dsn_var: str) -> Pg | None:
        argv = tuple(shlex.split(os.environ.get(argv_var) or ""))
        dsn = os.environ.get(dsn_var) or None
        if not argv and not dsn:
            return None
        return cls(argv or ("psql",), dsn)

    def __call__(self, sql: str) -> list[list[str]]:
        """Runs ``sql``; returns the rows it prints whose first column is ``E2E``."""
        env = {**os.environ, **dsn_env(self.dsn_file)} if self.dsn_file else None
        argv = [*self.argv, "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1"]
        done = subprocess.run(argv, input=sql, capture_output=True, text=True, timeout=180, env=env)
        if done.returncode != 0:
            raise Failed(f"psql exit {done.returncode}: {done.stderr.strip()[-1500:]}")
        return [line.split("|")[1:] for line in done.stdout.splitlines() if line.startswith("E2E|")]


@dataclass(frozen=True)
class Config:
    api: str
    ws: str | None
    rpc: str
    seed_file: str | None
    hd_base: int
    tag: str
    funder: str | None
    pg: Pg
    journal: Pg
    admin_key_file: str | None
    price_control: str | None
    price_status: str | None
    operator: str
    settlesub_metrics: str | None
    engine_metrics: str | None
    markets: dict[str, int]
    home_index: int | None
    spot: str | None
    spot_price: Decimal
    price_decimals: int
    size_decimals: int
    notional: Decimal
    backstop_dollars: Decimal
    wait_s: float
    final_wait_s: float
    proof_wait_s: float
    steps: tuple[str, ...]

    @classmethod
    def read(cls) -> Config:
        steps = tuple(s for s in _env("E2E_STEPS", ",".join(STEPS)).split(",") if s)
        if RESTORE in steps and steps != (RESTORE,):
            raise SystemExit(f"E2E_STEPS: {RESTORE} runs alone, not with {[s for s in steps if s != RESTORE]}")
        unknown = [s for s in steps if s not in STEPS and s != RESTORE]
        if unknown:
            raise SystemExit(f"E2E_STEPS: unknown steps {unknown}; the steps are {STEPS}, or {RESTORE} alone")
        seed_file = os.environ.get("E2E_WALLET_SEED_FILE") or None
        if not seed_file and not (os.environ.get("E2E_MNEMONIC") or "").strip():
            raise SystemExit("E2E_WALLET_SEED_FILE or E2E_MNEMONIC is required (README.md)")
        pg = Pg.of("E2E_PSQL", "E2E_PSQL_DSN_FILE")
        if pg is None:
            raise SystemExit("E2E_PSQL or E2E_PSQL_DSN_FILE is required (README.md)")
        control, status = os.environ.get("E2E_PRICE_CONTROL") or None, os.environ.get("E2E_PRICE_STATUS") or None
        if bool(control) != bool(status):
            raise SystemExit("E2E_PRICE_CONTROL and E2E_PRICE_STATUS go together (README.md)")
        operator = _env("E2E_OPERATOR", "none")
        if operator not in ("none", "sql"):
            raise SystemExit(f"E2E_OPERATOR is none or sql, not {operator!r}")
        home = os.environ.get("E2E_HOME_INDEX") or None
        return cls(
            api=_env("E2E_API").rstrip("/"),
            ws=os.environ.get("E2E_WS") or None,
            rpc=_env("E2E_RPC"),
            seed_file=seed_file,
            hd_base=int(_env("E2E_HD_BASE", "100")),
            tag=_env("E2E_TAG", "e2e"),
            funder=os.environ.get("E2E_FUNDER") or None,
            pg=pg,
            journal=Pg.of("E2E_JOURNAL_PSQL", "E2E_JOURNAL_DSN_FILE") or pg,
            admin_key_file=os.environ.get("E2E_ADMIN_API_KEY_FILE") or None,
            price_control=control,
            price_status=status,
            operator=operator,
            settlesub_metrics=os.environ.get("E2E_SETTLESUB_METRICS") or None,
            engine_metrics=os.environ.get("E2E_ENGINE_METRICS") or None,
            markets=parse_markets(_env("E2E_MARKETS", "trade=0,liq1=6,liq0=2,halt=3,carry_pos=8,carry_neg=9")),
            home_index=int(home) if home else None,
            spot=os.environ.get("E2E_SPOT") or None,
            spot_price=Decimal(_env("E2E_SPOT_PRICE", "10")),
            price_decimals=int(_env("E2E_PRICE_DECIMALS", "3")),
            size_decimals=int(_env("E2E_SIZE_DECIMALS", "1")),
            notional=Decimal(_env("E2E_NOTIONAL", "1500")),
            backstop_dollars=Decimal(_env("E2E_BACKSTOP_DOLLARS", "50000")),
            wait_s=float(_env("E2E_WAIT_S", "180")),
            final_wait_s=float(_env("E2E_FINAL_WAIT_S", "2400")),
            proof_wait_s=float(_env("E2E_PROOF_WAIT_S", "5400")),
            steps=steps,
        )


def log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%SZ', time.gmtime())} {msg}", flush=True)


def check(cond: Any, what: str) -> None:
    if not cond:
        raise Failed(what)
    log(f"  ok {what}")


def short(value: Any, n: int = 400) -> str:
    text = repr(value)
    return text if len(text) <= n else text[: n - 3] + "..."


def wait_for(what: str, probe: Callable[[], Any], timeout: float, every: float = 2.0) -> Any:
    """Polls ``probe`` until it answers something truthy, which is returned, for at most ``timeout`` seconds;
    a read that fails is retried. A timeout fails naming ``what`` and the last answer."""
    deadline = time.monotonic() + timeout
    last: Any = None
    while True:
        try:
            value = last = probe()
        except (LoafAPIError, LoafConnectionError, httpx.HTTPError, ChainError) as e:
            value, last = None, f"{type(e).__name__}: {e}"
        if value:
            return value
        if time.monotonic() >= deadline:
            raise Failed(f"no {what} within {timeout:.0f} s; last {short(last)}")
        time.sleep(every)


def value_once(read: Callable[[], int], done: Callable[[int], bool]) -> Callable[[], tuple[int] | None]:
    """A numeric wait's probe: what ``read`` answers, in a tuple, once ``done`` holds for it, so 0 ends the wait as
    any other number does (a pool swept empty reads 0); the wait returns the tuple."""

    def probe() -> tuple[int] | None:
        value = read()
        return (value,) if done(value) else None

    return probe


def refused(call: Callable[[], Any], kind: type[BaseException]) -> Any:
    """Runs ``call``, which must raise ``kind``; returns the error. Any other outcome fails."""
    try:
        answer = call()
    except kind as e:
        return e
    except LoafAPIError as e:
        raise Failed(f"expected {kind.__name__}, got {type(e).__name__} {e.status_code}: {e}") from e
    raise Failed(f"expected {kind.__name__}, the call succeeded: {short(answer)}")


# --------------------------------------------------------------------------------------------- #
# L1: JSON-RPC over httpx, ABI with eth-abi
# --------------------------------------------------------------------------------------------- #


def abi_types(sig: str) -> list[str]:
    inner = sig[sig.index("(") + 1 : sig.rindex(")")]
    return [t for t in inner.split(",") if t]


def calldata(sig: str, *args: Any) -> str:
    return "0x" + (keccak(text=sig)[:4] + abi_encode(abi_types(sig), list(args))).hex()


class Chain:
    def __init__(self, url: str) -> None:
        self.url = url
        self.http = httpx.Client(timeout=30.0)
        self._id = 0
        self.chain_id = int(self.rpc("eth_chainId", []), 16)

    def rpc(self, method: str, params: list[Any]) -> Any:
        self._id += 1
        r = self.http.post(self.url, json={"jsonrpc": "2.0", "id": self._id, "method": method, "params": params})
        r.raise_for_status()
        body = r.json()
        if body.get("error"):
            raise ChainError(f"{method}: {body['error']}")
        return body.get("result")

    def call(self, to: str, sig: str, out: Sequence[str], *args: Any) -> tuple[Any, ...]:
        result = self.rpc("eth_call", [{"to": to, "data": calldata(sig, *args)}, "latest"])
        return tuple(abi_decode(list(out), bytes.fromhex(result[2:])))

    def receipt(self, tx: str, what: str) -> Mapping[str, Any]:
        got = wait_for(f"the receipt of {what} ({tx})", lambda: self.rpc("eth_getTransactionReceipt", [tx]), 120, 0.5)
        if int(got["status"], 16) != 1:
            raise Failed(f"{what} reverted on L1 (tx {tx})")
        return got

    def send_unlocked(self, frm: str, to: str, sig: str, *args: Any) -> Mapping[str, Any]:
        """``frm``, one of the node's own accounts, sends ``sig(args)`` to ``to``: the node signs."""
        tx = self.rpc("eth_sendTransaction", [{"from": frm, "to": to, "data": calldata(sig, *args)}])
        return self.receipt(tx, f"{sig} from {frm}")

    def send(self, account: Any, to: str, sig: str, *args: Any) -> Mapping[str, Any]:
        """``account`` signs and sends ``sig(args)`` to ``to``: a legacy transaction at the node's gas price."""
        data = calldata(sig, *args)
        gas = int(self.rpc("eth_estimateGas", [{"from": account.address, "to": to, "data": data}]), 16)
        signed = account.sign_transaction(
            {
                "to": to_checksum_address(to),
                "data": data,
                "value": 0,
                "gas": gas * 13 // 10 + 20_000,
                "gasPrice": int(self.rpc("eth_gasPrice", []), 16),
                "nonce": int(self.rpc("eth_getTransactionCount", [account.address, "pending"]), 16),
                "chainId": self.chain_id,
            }
        )
        sent = self.rpc("eth_sendRawTransaction", ["0x" + bytes(signed.raw_transaction).hex()])
        return self.receipt(sent, f"{sig} from {account.address}")

    def accounts(self) -> list[str]:
        return [a.lower() for a in self.rpc("eth_accounts", [])]

    def eth(self, address: str) -> int:
        return int(self.rpc("eth_getBalance", [address, "latest"]), 16)

    def set_gas(self, address: str, wei: int) -> None:
        self.rpc("anvil_setBalance", [address, hex(wei)])

    # ZkLoaf (abis/ZkLoaf.abi.json) and its tokens
    def asset(self, proxy: str, asset: int) -> dict[str, Any]:
        out = self.call(proxy, "assetConfig(uint16)", ["(address,uint8,uint56,uint128,uint64,uint64)"], asset)[0]
        token, _, ext, tick, cap, least = out
        decimals = int(self.call(token, "decimals()", ["uint8"])[0])
        return {"id": asset, "token": token.lower(), "ext": int(ext), "tick": int(tick), "cap": int(cap),
                "min": int(least), "decimals": decimals}

    def balance_of(self, token: str, owner: str) -> int:
        return int(self.call(token, "balanceOf(address)", ["uint256"], owner)[0])

    def allowance(self, token: str, owner: str, spender: str) -> int:
        return int(self.call(token, "allowance(address,address)", ["uint256"], owner, spender)[0])

    def account_index(self, proxy: str, owner: str) -> int:
        return int(self.call(proxy, "getAccountIndexFromAddress(address)", ["uint48"], owner)[0])

    def key_nonce(self, proxy: str, owner: str) -> int:
        return int(self.call(proxy, "changePubKeyNonce(address)", ["uint256"], owner)[0])

    def pending_balance(self, proxy: str, owner: str, asset: int) -> int:
        return int(self.call(proxy, "getPendingBalance(address,uint16)", ["uint128"], owner, asset)[0])

    def batch_pointers(self, proxy: str) -> tuple[int, int, int]:
        """(committed, verified, executed)."""
        c, v, e = self.call(proxy, "batchPointers()", ["uint64", "uint64", "uint64"])
        return int(c), int(v), int(e)


# --------------------------------------------------------------------------------------------- #
# The backend's database through psql: the run's users and API keys, a market's control flags; the journal's
# closes and settled's refusals
# --------------------------------------------------------------------------------------------- #


def _lit(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


#: Moves each id sequence of the tables the provisioning inserts into past the table's largest id: a kit that
#: inserted rows with explicit ids left the sequence behind them. Without the right to, it notes and goes on.
_SEQUENCES_SQL = """DO $$
DECLARE t text; s text; top bigint;
BEGIN
  FOREACH t IN ARRAY ARRAY['users', 'referral_codes', 'api_keys'] LOOP
    s := pg_get_serial_sequence(t, 'id');
    CONTINUE WHEN s IS NULL;
    EXECUTE format('SELECT GREATEST(COALESCE(max(id), 0), (SELECT last_value FROM %s)) FROM %I', s, t) INTO top;
    PERFORM setval(s, GREATEST(top, 1));
  END LOOP;
EXCEPTION WHEN insufficient_privilege THEN
  RAISE NOTICE 'e2e: the id sequences stay as they are: %', SQLERRM;
END $$;"""


def provision_sql(rows: Sequence[tuple[str, str, str, str]], now: int) -> str:
    """Upserts each (label, wallet, user type, API key hash) as Loaf-Backend authenticates a key: the user, its
    personal referral code (the key lookup INNER JOINs it) and an API key valid for a day (revoked_at is the
    expiry). Prints ``E2E|label|user id`` for each."""
    values = ",\n".join(
        f"  ({_lit(label)}, {_lit(wallet)}, {_lit(kind)}::user_type_enum, {_lit(digest)}, "
        f"{_lit('E2E' + secrets.token_hex(4).upper())})"
        for label, wallet, kind, digest in rows
    )
    return f"""BEGIN;
{_SEQUENCES_SQL}
CREATE TEMP TABLE e2e_users (label text, wallet text, kind user_type_enum, digest text, ref text) ON COMMIT DROP;
INSERT INTO e2e_users VALUES
{values};
INSERT INTO users (wallet_address, display_name, user_type, kyc_status, wholesale_status, created_at)
SELECT e.wallet, e.label, e.kind, 'VERIFIED', 'VERIFIED', {now} FROM e2e_users e
ON CONFLICT (wallet_address) DO NOTHING;
UPDATE users u SET deleted_at = NULL FROM e2e_users e WHERE u.wallet_address = e.wallet AND u.deleted_at IS NOT NULL;
INSERT INTO referral_codes (owner_user_id, is_personal, max_uses, code, created_at)
SELECT u.id, true, -1, e.ref, {now} FROM e2e_users e JOIN users u ON u.wallet_address = e.wallet
 WHERE NOT EXISTS (SELECT 1 FROM referral_codes rc WHERE rc.owner_user_id = u.id AND rc.is_personal);
INSERT INTO api_keys (user_id, token_hash, description, created_at, revoked_at)
SELECT u.id, e.digest, 'e2e', {now}, {now + 86_400} FROM e2e_users e JOIN users u ON u.wallet_address = e.wallet;
SELECT 'E2E', e.label, u.id FROM e2e_users e JOIN users u ON u.wallet_address = e.wallet ORDER BY e.label;
COMMIT;
"""


def market_flags_sql(market: int, reduce_only: bool) -> str:
    """An operator's control as the backend's perps control route writes it (adminPerpsService writeMarketControl):
    the market row's flags, not halted and reduce-only or not; prints the row back."""
    m = int(market)
    return (f"UPDATE markets SET is_halted = false, is_reduce_only = {'true' if reduce_only else 'false'} "
            f"WHERE market_index = {m};\n"
            f"SELECT 'E2E', market_index, is_halted, is_reduce_only FROM markets WHERE market_index = {m};\n")


def deleverage_sql(seqs: Sequence[int]) -> str:
    """The journal's two closes of each deleverage (row 65) in ``seqs``: the deleveraged pool's (adl false), which
    names its counter, and the counter's own (adl true)."""
    return (f"SELECT 'E2E', journal_seq, adl, account_index, pool, counter_account, counter_pool FROM l2_perps_closes "
            f"WHERE kind = 65 AND journal_seq IN ({', '.join(str(int(s)) for s in seqs)}) ORDER BY journal_seq, adl;\n")


#: The backstop's account (k1-rules cells.rs BACKSTOP): its pool 0 takes the mode-1 liquidations.
BACKSTOP_ACCOUNT = 1
#: How long a backstop knob may take: the kit retries an allocate K1 refused each tick for 120 s (P5_KNOB_RETRY_MS).
KNOB_WAIT_S = 180.0


def backstop_positions_sql(markets: Sequence[int]) -> str:
    """The backstop's open positions on ``markets``, from the read model."""
    return (f"SELECT 'E2E', market, pool, bal FROM l2_perps_positions WHERE account_index = {BACKSTOP_ACCOUNT} "
            f"AND bal <> 0 AND market IN ({', '.join(str(int(m)) for m in markets)}) ORDER BY market, pool;\n")


#: The flat pools in debt (the read model's pools with no position and collateral below 0): the backstop writes each
#: off (row 70) once it covers it.
DEBTS_SQL = ("SELECT 'E2E', account_index, pool, coll FROM l2_perps_pools WHERE npos = 0 AND coll < 0 "
             "ORDER BY account_index, pool;\n")

REFUSALS_SQL = "SELECT 'E2E', to_regclass('l2_settle_refusals') IS NOT NULL;"
REFUSALS_NEWEST_SQL = """SELECT 'E2E', count(*) FROM l2_settle_refusals;
SELECT 'E2E', journal_id, kind, k1_class, party, block_number, left(detail, 300)
  FROM l2_settle_refusals ORDER BY created_at DESC, journal_id DESC LIMIT 5;"""


def refusals(cfg: Config) -> tuple[int, list[list[str]]] | None:
    """settled's refusals in the journal (l2_settle_refusals): their count and the newest five; None when the
    database has no such table."""
    if cfg.journal(REFUSALS_SQL) != [["t"]]:
        return None
    rows = cfg.journal(REFUSALS_NEWEST_SQL)
    return int(rows[0][0]), rows[1:]


# --------------------------------------------------------------------------------------------- #
# The kit's price pump, steered by its control file
# --------------------------------------------------------------------------------------------- #


class Prices:
    """The kit's price pump (the P5 kit's fullstack phase): each control is written whole to a temporary file
    beside ``control`` and renamed onto it, its ``seq`` above every earlier one, and names only what it changes:
    a market's mode (``{"mode": "walk", "index": i}``, a walk inside the move bound, or ``{"mode": "quiet"}``, no
    reports) and the backstop's free collateral (``{"free_ticks": n}``). The pump answers in ``status``
    (``applied_seq``, ``error``, ``markets`` and ``backstop``); a control it cannot read, with ``file_error`` and
    ``applied_seq`` unchanged."""

    def __init__(self, control: str, status: str, wait_s: float) -> None:
        self.control, self.status_file, self.wait_s = control, status, wait_s
        self.last_seq = 0

    def status(self) -> dict[str, Any]:
        try:
            with open(self.status_file) as f:
                got = json.load(f)
        except (OSError, ValueError):
            return {}
        return got if isinstance(got, dict) else {}

    def next_seq(self) -> int:
        try:
            with open(self.control) as f:
                written = int(json.load(f).get("seq") or 0)
        except (OSError, ValueError, AttributeError, TypeError):
            written = 0
        return max(written, int(self.status().get("applied_seq") or 0)) + 1

    def send(self, markets: Mapping[int, Mapping[str, Any]] | None = None,
             backstop: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Writes one control; returns the pump's status once it has applied it. A status that answers it with
        an error fails."""
        seq = self.next_seq()
        body: dict[str, Any] = {"seq": seq, "markets": {str(m): dict(v) for m, v in (markets or {}).items()}}
        if backstop is not None:
            body["backstop"] = dict(backstop)
        tmp = f"{self.control}.e2e-{os.getpid()}.tmp"
        with open(tmp, "w") as f:
            json.dump(body, f, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.control)
        self.last_seq = seq
        log(f"  price control {json.dumps(body, sort_keys=True)}")
        last: dict[str, Any] = {}

        def applied() -> dict[str, Any] | None:
            got = self.status()
            last.clear()
            last.update(got)
            if int(got.get("applied_seq") or 0) < seq:
                return None
            if got.get("error"):
                raise Failed(f"the pump answered control {seq} with an error: {short(got['error'])}")
            return got

        try:
            return wait_for(f"the pump's answer to control {seq}", applied, self.wait_s, 0.5)
        except Failed as e:
            if last.get("file_error") and int(last.get("applied_seq") or 0) < seq:
                raise Failed(f"{e}; the pump could not read the control: {short(last['file_error'])}") from None
            raise


# --------------------------------------------------------------------------------------------- #
# What the driver signs for the backend's routes the SDK does not serve (key change, withdrawal)
# --------------------------------------------------------------------------------------------- #


def withdraw_message(chain_id: int, account: int, asset: int, route: int, amount: int, nonce: int, expiry: int,
                     api_key: int = 0) -> bytes:
    """K1's L2 withdraw message (k1-sign msg.rs WithdrawFields): the chain id; tag 13 with the api key above it;
    the account (low and middle 24 bits); the asset; the route; the amount (low 24, middle 24, the bits above 48);
    the nonce and the expiry (low and middle 24 bits each); three zeros. Hashed by the SDK's sponge, as its
    allocate and exit messages are."""
    m24 = (1 << 24) - 1
    return k1._digest(
        (
            chain_id, 13 + (api_key << 8), account & m24, account >> 24 & m24, asset, route,
            amount & m24, amount >> 24 & m24, amount >> 48,
            nonce & m24, nonce >> 24 & m24, expiry & m24, expiry >> 24 & m24, 0, 0, 0,
        )
    )


def change_pub_key_typed_data(owner: str, chain_id: int, proxy: str, account: int, pub_key: bytes, nonce: int,
                              deadline: int) -> dict[str, Any]:
    """ZkLoafGovernance's ChangePubKey (EIP-712, domain ZkLoaf/1 on the proxy), as POST /l2/keys relays it."""
    return {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "ChangePubKey": [
                {"name": "owner", "type": "address"},
                {"name": "accountIndex", "type": "uint48"},
                {"name": "apiKeyIndex", "type": "uint8"},
                {"name": "pubKey", "type": "bytes"},
                {"name": "nonce", "type": "uint256"},
                {"name": "deadline", "type": "uint256"},
            ],
        },
        "primaryType": "ChangePubKey",
        "domain": {"name": "ZkLoaf", "version": "1", "chainId": chain_id, "verifyingContract": to_checksum_address(proxy)},
        "message": {"owner": to_checksum_address(owner), "accountIndex": account, "apiKeyIndex": 0, "pubKey": pub_key,
                    "nonce": nonce, "deadline": deadline},
    }


def index_for(pool: Mapping[str, Any], market: int, p0: int, num: int, den: int) -> int:
    """The index that brings a pool holding one long on ``market`` (notional n0 at index p0, the pool's value v0
    and maintenance requirement r0 read at p0) to ``num/den`` of its requirement (the P5 kit's flows.rs
    index_for; a negative ``num`` takes the value below 0): both move with the index, v(p) = v0 - n0 (1 - p/p0)
    and r(p) = r0 p/p0. The pool reads its requirement x 65,536."""
    pos = [p for p in pool["positions"] if int(p["market"]) == market]
    if len(pos) != 1 or int(pos[0]["base"]) <= 0:
        raise Failed(f"pool {pool.get('pool')} holds no single long on {market}: {short(pool)}")
    n0, v0, r0 = int(pos[0]["notional"]), int(pool["value"]), int(pool["requirement"]) // 65_536
    if not 0 < r0 < v0:
        raise Failed(f"pool {pool.get('pool')}: value {v0} against requirement {r0} is not a healthy pool")
    return p0 * den * (n0 - v0) // (den * n0 - num * r0)


#: The engine's gauge of the pools its liquidation step found below maintenance and has not found healthy since.
LIQ_PENDING = "matching_engine_perps_liq_pending"


def metric_value(text: str, name: str) -> float | None:
    """The value of the unlabelled sample ``name`` in a Prometheus text page; None when the page has none."""
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == name:
            return float(parts[1])
    return None


def backstop_targets(below: Sequence[Mapping[str, Any]], expired: set[int],
                     victim: tuple[int, int]) -> list[Mapping[str, Any]]:
    """The pools of ``below`` (the pump's status ``backstop.below``: each pool under its maintenance requirement or
    in debt, as the engine values it) that the backstop acts on once its free collateral allows, ``victim``
    (account, pool) aside: a flat pool in debt (its write-off, row 70), or one with positions under 2/3 of its
    requirement or on an expired market (row 64 mode 1). The requirement reads x 65,536."""
    out = []
    for p in below:
        if (int(p["account"]), int(p["pool"])) == victim:
            continue
        value, req, markets = int(p["value"]), int(p["requirement"]), {int(m) for m in p.get("markets") or ()}
        if not markets:
            waits = value < 0
        else:
            waits = value * 65_536 < req and (3 * value * 65_536 < 2 * req or bool(markets & expired))
        if waits:
            out.append(p)
    return out


def pool_named(p: Mapping[str, Any]) -> str:
    return (f"account {p['account']} pool {p['pool']} (value {p['value']}, requirement {p['requirement']}, "
            f"markets {list(p.get('markets') or [])})")


# --------------------------------------------------------------------------------------------- #
# Users and their reads
# --------------------------------------------------------------------------------------------- #


def wallet_of(cfg: Config, i: int, name: str) -> Any:
    """User ``name``'s wallet: keccak(seed || "/" || name) for the seed in E2E_WALLET_SEED_FILE (hex), else the HD
    account E2E_HD_BASE + ``i`` of E2E_MNEMONIC. Either way the same users each run."""
    if cfg.seed_file:
        with open(cfg.seed_file) as f:
            seed = bytes.fromhex(f.read().strip())
        if len(seed) < 16:
            raise Failed(f"{cfg.seed_file} holds less than 16 bytes of seed")
        return Account.from_key(keccak(seed + b"/" + name.encode()))
    Account.enable_unaudited_hdwallet_features()
    return Account.from_mnemonic(_env("E2E_MNEMONIC"), account_path=f"m/44'/60'/0'/0/{cfg.hd_base + i}")


@dataclass
class User:
    name: str
    wallet: Any = field(repr=False)
    api_key: str = field(default="", repr=False)
    trading_key: str = field(default="", repr=False)
    client: LoafClient | None = field(default=None, repr=False)
    l2_account: int = 0
    user_id: int = 0
    trade_floor: int = 0

    @property
    def address(self) -> str:
        return str(self.wallet.address).lower()

    @property
    def c(self) -> LoafClient:
        assert self.client is not None
        return self.client


def keys_state(user: User, pub_key: str | None = None) -> Mapping[str, Any] | None:
    params: dict[str, Any] = {"apiKeyIndex": 0}
    if pub_key:
        params["publicKey"] = pub_key
    try:
        return user.c.get("/l2/keys", params=params)
    except LoafNotFoundError:
        return None


def balance(user: User, asset: int) -> tuple[int, int]:
    """``(total, available)`` of ``asset`` in the user's L2 book, token base units; (0, 0) without a row."""
    for row in user.c.portfolio.component()["balances"]:
        if int(row["assetId"]) == asset:
            return int(row["total"]), int(row["available"])
    return 0, 0


def pool_of(user: User, pool: int) -> Mapping[str, Any] | None:
    for p in user.c.perps.positions()["pools"]:
        if int(p["pool"]) == pool:
            return p
    return None


def position_of(user: User, market: int, pool: int) -> Mapping[str, Any] | None:
    for pos in (pool_of(user, pool) or {}).get("positions", []):
        if int(pos["market"]) == market:
            return pos
    return None


def base_of(user: User, market: int, pool: int) -> int:
    pos = position_of(user, market, pool)
    return 0 if pos is None else int(pos["base"])


def collateral_of(user: User, pool: int) -> int:
    return int((pool_of(user, pool) or {}).get("collateral") or 0)


def placed(answer: Mapping[str, Any], what: str) -> Mapping[str, Any]:
    check(answer.get("success") is not False and int(answer.get("orderId") or 0) > 0,
          f"{what}: order {answer.get('orderId')} {answer.get('status')}, {answer.get('quantityLeft')} left")
    return answer


def left(answer: Mapping[str, Any]) -> Decimal:
    return Decimal(str(answer.get("quantityLeft") or 0))


def active_ids(user: User) -> set[int]:
    return {int(o["orderId"]) for o in user.c.history.active_orders().get("activeOrders", [])}


def legs_of(user: User, parent: int) -> list[Mapping[str, Any]]:
    return [o for o in user.c.portfolio.component()["openOrders"] if int(o.get("parentOrderId") or 0) == parent]


def newest_trade(user: User) -> int:
    rows = user.c.history.trades(page_size=1)["trades"]
    return int(rows[0]["tradeId"]) if rows else 0


@dataclass
class Touched:
    """What a step moved on the stack, for its restore (Run.restore): each market it walked or silenced with the
    index it goes home to (E2E_HOME_INDEX, else the index before the step's first move), the markets whose state it
    controlled (a halt, the operator's controls, a silence), and whether it raised the backstop's knob."""

    homes: dict[int, int] = field(default_factory=dict)
    states: set[int] = field(default_factory=set)
    knob: bool = False


@dataclass
class Run:
    cfg: Config
    anon: LoafClient | None = None
    chain: Chain | None = None
    pump: Prices | None = None
    proxy: str = ""
    l1_chain: int = 0
    l2_chain: int = 0
    funder: str = ""
    usdc: dict[str, Any] = field(default_factory=dict)
    users: dict[str, User] = field(default_factory=dict)
    perps: dict[int, Mapping[str, Any]] = field(default_factory=dict)
    tickers: dict[int, str] = field(default_factory=dict)
    collateral: Mapping[str, Any] = field(default_factory=dict)
    spot: str | None = None
    spot_l2: Mapping[str, Any] = field(default_factory=dict)
    spot_failed: str | None = None
    start_pointers: tuple[int, int, int] = (0, 0, 0)
    refusals_before: int | None = None
    ws_fills: list[Any] = field(default_factory=list)
    sockets: list[Any] = field(default_factory=list)
    last_batch: int = 0
    notes: list[str] = field(default_factory=list)
    incomplete: list[str] = field(default_factory=list)
    touched: Touched = field(default_factory=Touched)

    @property
    def mm(self) -> User:
        return self.users["MM"]

    def mkt(self, role: str) -> int:
        return self.cfg.markets[role]

    def ticker(self, market: int) -> str:
        return self.tickers[market]

    # An index is in E2E_PRICE_DECIMALS units, an order's price in dollars (cents); a size in tokens
    # (E2E_SIZE_DECIMALS), a position's base in base units; collateral in extended units.
    def dollars(self, index: int) -> Decimal:
        return Decimal(index) / (Decimal(10) ** self.cfg.price_decimals)

    def to_index(self, dollars: Decimal) -> int:
        return int(dollars * (Decimal(10) ** self.cfg.price_decimals))

    def units(self, tokens: Decimal) -> int:
        return int(tokens * (Decimal(10) ** self.cfg.size_decimals))

    def tokens(self, units: int) -> Decimal:
        return Decimal(units) / (Decimal(10) ** self.cfg.size_decimals)

    @staticmethod
    def cents(price: Decimal, up: bool) -> Decimal:
        return price.quantize(Decimal("0.01"), rounding=ROUND_CEILING if up else ROUND_FLOOR)

    def ext_per_dollar(self) -> Decimal:
        c = self.collateral
        return Decimal(10) ** int(c["decimals"]) / int(c["tickSize"]) * int(c["extensionMultiplier"])

    def live(self, market: int) -> Mapping[str, Any]:
        assert self.anon is not None
        row = self.anon.perps.market_live(market)
        if row is None:
            raise Failed(f"market {market} has no live row")
        return row

    def index(self, market: int) -> int:
        return int(self.live(market)["indexPrice"])

    def qty(self, market: int) -> Decimal:
        """A position's size on ``market``: tokens worth about E2E_NOTIONAL dollars, at least its minimum base."""
        step = Decimal(1) / (Decimal(10) ** self.cfg.size_decimals)
        q = (self.cfg.notional / self.dollars(self.index(market))).quantize(step, rounding=ROUND_CEILING)
        return max(q, self.tokens(int(self.perps[market].get("minBase") or 1)), step)

    def until(self, what: str, probe: Callable[[], Any], timeout: float | None = None, every: float = 1.0) -> Any:
        got = wait_for(what, probe, self.cfg.wait_s if timeout is None else timeout, every)
        log(f"  ok {what}")
        return got

    def wait_base(self, user: User, market: int, pool: int, base: int) -> None:
        self.until(f"{user.name}'s base {base} on market {market} in pool {pool}",
                   lambda: base_of(user, market, pool) == base)

    def state_is(self, market: int, state: str, timeout: float | None = None) -> None:
        self.until(f"market {market} {state}", lambda: self.live(market).get("tradingState") == state, timeout)

    # The kit's pump: prices and the backstop
    def prices(self) -> Prices:
        if self.pump is None:
            raise Incomplete("a price move needs the kit's price control (E2E_PRICE_CONTROL, E2E_PRICE_STATUS)")
        return self.pump

    def touch(self, market: int) -> None:
        """Records ``market`` as moved by the step, with its home (Touched), before its first move."""
        if market not in self.touched.homes:
            self.touched.homes[market] = self.cfg.home_index or self.index(market)

    def walk(self, market: int, index: int) -> None:
        """The pump walks ``market`` to ``index`` inside the move bound (every report accepted); waits until the
        engine's index is within 1 bp of it."""
        prices = self.prices()
        self.touch(market)
        prices.send({market: {"mode": "walk", "index": index}})
        self.until(f"market {market}'s index at {index}", lambda: abs(self.index(market) - index) * 10_000 <= index)

    def silence(self, market: int) -> None:
        """The pump stops reporting ``market``: once its stale window passes, the market is PRICE_STALE."""
        prices = self.prices()
        self.touch(market)
        self.touched.states.add(market)
        prices.send({market: {"mode": "quiet"}})

    def backstop_clear(self, victim: tuple[int, int]) -> bool:
        """Before a leg raises the backstop's free collateral: no pool but the leg's ``victim`` (account, pool)
        waits for the backstop, which would act on it first. Read from the status that answers a no-op control
        (so written after the check began): its ``backstop.below``, classified here (backstop_targets); without
        that list, from the read model's debts and the engine's pending gauge (E2E_ENGINE_METRICS), where any
        pending pool fails, as none can be ruled out. False, the leg INCOMPLETE, when neither can tell."""
        status = self.prices().send()
        below = (status.get("backstop") or {}).get("below")
        pending = None
        if self.cfg.engine_metrics:
            pending = metric_value(httpx.get(self.cfg.engine_metrics, timeout=30).text, LIQ_PENDING)
        log(f"  the backstop's guard: the status lists {'no' if below is None else len(below)} pools under "
            f"maintenance or in debt; the engine holds {pending} pending ({LIQ_PENDING})")
        who = f"the leg's victim (account {victim[0]} pool {victim[1]})"
        if below is not None:
            expired = {m for m, row in self.perps.items() if row.get("status") == "EXPIRED"}
            others = backstop_targets(below, expired, victim)
            check(not others, f"no pool but {who} waits for the backstop: "
                              + ("; ".join(pool_named(p) for p in others) or "none"))
            return True
        debts = [r for r in self.cfg.journal(DEBTS_SQL) if (int(r[0]), int(r[1])) != victim]
        check(not debts, f"no flat pool but {who} waits for the backstop's write-off: "
                         + ("; ".join(f"account {a} pool {p} (collateral {c})" for a, p, c in debts) or "none"))
        if pending is None:
            self.incomplete.append(f"no backstop.below in the pump's status and no engine gauge (E2E_ENGINE_METRICS): "
                                   f"nothing tells whether a pool but {who} waits for the backstop, so the leg did "
                                   f"not raise it")
            return False
        check(pending == 0, f"the engine holds {int(pending)} pools below maintenance ({LIQ_PENDING}) and the "
                            f"pump's status names none (no backstop.below), so none is ruled out")
        return True

    def backstop_ticks(self) -> int:
        return int(self.cfg.backstop_dollars * 10 ** int(self.usdc["decimals"])) // int(self.usdc["tick"])

    def backstop(self, ticks: int) -> None:
        """The kit moves the backstop's free collateral (account 1's pool 0) to ``ticks`` USDC ticks. Waits until
        the status's knob record answers this control (``backstop.applied_seq``) with ``done``; its ``error``
        fails, and ``pending`` (an allocate K1 refused, retried each tick for P5_KNOB_RETRY_MS, then an error)
        waits. ``free_ticks`` is the live free margin, which a mark or funding move shifts, so it decides only for
        a status with no knob record."""
        prices = self.prices()
        self.touched.knob = self.touched.knob or ticks != 0
        prices.send(backstop={"free_ticks": ticks})
        seq = prices.last_seq

        def there() -> Any:
            got = prices.status().get("backstop") or {}
            if "applied_seq" not in got:
                if "free_ticks" not in got:
                    return got or "applied"
                free = int(got["free_ticks"])
                return got if (free >= ticks if ticks else free == 0) else None
            if int(got["applied_seq"]) < seq:
                return None
            if got.get("error"):
                raise Failed(f"the backstop's knob {seq} (free collateral to {ticks} ticks) failed: "
                             f"{short(got['error'])}")
            return got if "done" in got else None

        got = self.until(f"the backstop's knob {seq}: free collateral to {ticks} ticks", there,
                         max(self.cfg.wait_s, KNOB_WAIT_S))
        log(f"  backstop: {short(got)}")

    # The admin routes and the operator's controls
    def admin(self, path: str, body: Mapping[str, Any]) -> httpx.Response:
        if self.cfg.admin_key_file:
            with open(self.cfg.admin_key_file) as f:
                key = f.read().strip()
        else:
            key = (os.environ.get("E2E_ADMIN_API_KEY") or "").strip()
        if not key:
            raise Incomplete("the admin routes need E2E_ADMIN_API_KEY_FILE or E2E_ADMIN_API_KEY")
        with httpx.Client(timeout=60.0) as http:
            return http.post(f"{self.cfg.api}{path}", json=body, headers={"Authorization": f"Bearer {key}"})

    def halt(self, market: int) -> httpx.Response:
        """The admin API key's halt of ``market`` (POST /admin/markets/halt HALTED), which the step's restore lifts."""
        self.touched.states.add(market)
        return self.admin("/admin/markets/halt", {"marketIndex": market, "status": "HALTED"})

    def operator(self, market: int, control: str) -> None:
        """The operator's market control, OPEN or REDUCE_ONLY. The backend takes it from an operator's Privy session
        only (POST /admin/perps/markets/:m/control). With E2E_OPERATOR=sql, for a stack without Privy, the driver
        does what that route does: the market row's flags (market_flags_sql), then the engine's load of the market,
        which adopts them, and the backend's market reload, through the admin API key's POST /admin/markets/status
        LIVE on the LIVE market."""
        if self.cfg.operator != "sql":
            raise Incomplete(f"{control} on market {market} needs an operator session (E2E_OPERATOR=sql emulates one)")
        self.touched.states.add(market)
        reduce_only = control == "REDUCE_ONLY"
        rows = self.cfg.pg(market_flags_sql(market, reduce_only))
        check(rows == [[str(market), "f", "t" if reduce_only else "f"]],
              f"market {market}'s row: not halted, reduce-only {reduce_only} ({rows})")
        r = self.admin("/admin/markets/status", {"marketIndex": market, "newStatus": "LIVE"})
        check(r.status_code == 200 and r.json().get("oldStatus") == "LIVE",
              f"the engine loaded market {market} (POST /admin/markets/status LIVE): {r.status_code} {short(r.text, 300)}")

    def reopen(self, market: int) -> None:
        """Leaves ``market`` OPEN: one an earlier run left unreported, halted or reduce-only is reported again and
        reopened by the operator's control."""
        live = self.live(market)
        if live.get("tradingState") == "OPEN":
            return
        log(f"  market {market} is {live.get('tradingState')} ({live.get('stateReason')}): reopening it")
        self.walk(market, int(live["indexPrice"]) or (self.cfg.home_index or 0))
        state = self.until(f"market {market} reported", lambda: (lambda s: s if s and s != "PRICE_STALE" else None)(
            self.live(market).get("tradingState")))
        if state != "OPEN":
            self.operator(market, "OPEN")
        self.state_is(market, "OPEN")

    def restore(self, touched: Touched) -> list[str]:
        """Puts back what a step moved (``touched``), whatever the step's verdict: the run's orders cancelled, each
        market whose state it controlled OPEN again (reopen), each market it walked or silenced back home, and the
        backstop's knob at 0 if it raised it. Each part is tried whatever the others did; returns the ones that
        failed. The restore's own moves are not recorded."""
        parts: list[tuple[str, Callable[[], None]]] = []
        if self.users and (touched.homes or touched.states or touched.knob):
            parts.append(("the run's orders cancelled", self.cancel_all))
        for m in sorted(touched.states):
            parts.append((f"market {m} OPEN", lambda m=m: self.reopen(m)))
        for m, home in sorted(touched.homes.items()):
            parts.append((f"market {m}'s index home at {home}", lambda m=m, home=home: self.walk(m, home)))
        if touched.knob and KEEP_BACKSTOP:
            log("  restore: the backstop's knob kept (E2E_KEEP_BACKSTOP=1): its position stays for the desk leg")
        elif touched.knob:
            parts.append(("the backstop's knob at 0", lambda: self.backstop(0)))
        if not parts:
            return []
        log("  restore: " + "; ".join(what for what, _ in parts))
        saved, self.touched = self.touched, Touched()
        failed: list[str] = []
        try:
            for what, fn in parts:
                try:
                    fn()
                except Exception as e:  # noqa: BLE001 each part is tried whatever the others did
                    failed.append(f"{what}: {type(e).__name__}: {e}")
                    log(f"  restore failed: {failed[-1]}")
        finally:
            self.touched = saved
        return failed

    # Orders and allocations
    def cancel_all(self) -> None:
        for u in self.users.values():
            u.c.orders.cancel_all()

    def book(self, market: int) -> Mapping[str, Any] | None:
        assert self.anon is not None
        return self.anon.market.get(self.ticker(market)).get("orderBook")

    def flat(self, user: User, market: int, pool: int) -> None:
        """Closes ``user``'s position on ``market`` in ``pool``: a reduce-only IOC against the maker's quote."""
        base = base_of(user, market, pool)
        if base == 0:
            return
        t = self.ticker(market)
        px = self.dollars(self.index(market))
        q = float(self.tokens(abs(base)))
        if base > 0:
            self.mm.c.orders.limit_buy(t, q, float(self.cents(px * Decimal("0.995"), False)), pool=0)
            user.c.orders.limit_sell(t, q, float(self.cents(px * Decimal("0.99"), False)), pool=pool,
                                     reduce_only=True, time_in_force="IOC")
        else:
            self.mm.c.orders.limit_sell(t, q, float(self.cents(px * Decimal("1.005"), True)), pool=0)
            user.c.orders.limit_buy(t, q, float(self.cents(px * Decimal("1.01"), True)), pool=pool,
                                    reduce_only=True, time_in_force="IOC")
        self.wait_base(user, market, pool, 0)
        self.mm.c.orders.cancel_all()

    def allocate(self, user: User, dollars: Decimal, frm: int, to: int) -> None:
        """``user`` moves ``dollars`` USDC (whole ticks) from ``frm`` to ``to``: the spot balance moves by exactly
        that, and so does the pool's collateral when the pool holds no position (funding settles with it)."""
        c = self.collateral
        tick, ext, dec = int(c["tickSize"]), int(c["extensionMultiplier"]), int(c["decimals"])
        ticks = int(dollars * (Decimal(10) ** dec)) // tick
        pool = frm if to == PERPS_SPOT_SIDE else to
        sign = 1 if to == pool else -1
        held = pool_of(user, pool)
        flat = not (held or {}).get("positions")
        before = int((held or {}).get("collateral") or 0)
        spot0, _ = balance(user, USDC_ASSET)
        r = user.c.perps.allocate(Decimal(ticks * tick) / (Decimal(10) ** dec), from_side=frm, to_side=to)
        check(int(r["journalSeq"]) > 0, f"{user.name} allocates {ticks} ticks {frm} -> {to} (journal {r['journalSeq']})")
        after = wait_for(f"{user.name}'s pool {pool} collateral moved",
                         value_once(lambda: collateral_of(user, pool), lambda x: x != before), self.cfg.wait_s)[0]
        if flat:
            check(after - before == sign * ticks * ext, f"pool {pool}'s collateral {before} -> {after}")
        else:
            check((after - before) * sign > 0, f"pool {pool}'s collateral {before} -> {after} (it holds positions)")
        self.until(f"{user.name}'s spot USDC {spot0} -> {spot0 - sign * ticks * tick}",
                   lambda: balance(user, USDC_ASSET)[0] == spot0 - sign * ticks * tick)

    def fund(self, user: User, pool: int, dollars: Decimal) -> None:
        """Tops ``user``'s ``pool`` up to ``dollars`` of collateral."""
        short_by = dollars * self.ext_per_dollar() - collateral_of(user, pool)
        if short_by > 0:
            self.allocate(user, (short_by / self.ext_per_dollar()).quantize(Decimal(1), rounding=ROUND_CEILING),
                          PERPS_SPOT_SIDE, pool)

    def sweep(self, user: User, pool: int) -> None:
        """Moves a flat pool's whole ticks back to spot."""
        held = pool_of(user, pool)
        if not held or held.get("positions"):
            return
        c = self.collateral
        ticks = int(held["collateral"]) // int(c["extensionMultiplier"])
        if ticks > 0:
            self.allocate(user, Decimal(ticks * int(c["tickSize"])) / (Decimal(10) ** int(c["decimals"])), pool,
                          PERPS_SPOT_SIDE)


# --------------------------------------------------------------------------------------------- #
# 1. setup
# --------------------------------------------------------------------------------------------- #


def step_setup(run: Run) -> None:
    cfg = run.cfg
    if cfg.operator == "sql":
        run.notes.append("E2E_OPERATOR=sql: the operator's REDUCE_ONLY and OPEN are emulated (the market row's flags, "
                         "then POST /admin/markets/status LIVE with the admin API key); the backend's operator route "
                         "POST /admin/perps/markets/:m/control and its Privy auth are not exercised")
    anon = run.anon = LoafClient(base_url=cfg.api, ws_url=cfg.ws, max_retries=2)
    rows = anon.market.list()["markets"]
    run.tickers = {int(r["marketIndex"]): r["ticker"] for r in rows if r["type"] == "PERP"}
    listed = anon.perps.markets()
    run.perps = {int(m["marketIndex"]): m for m in listed["markets"]}
    run.collateral = listed.get("collateral") or {}
    check(int(run.collateral.get("assetId") or 0) == USDC_ASSET, f"the perps collateral is asset {USDC_ASSET}")
    for role, index in sorted(cfg.markets.items()):
        m = run.perps.get(index)
        check(m is not None and m["status"] == "ACTIVE" and index in run.tickers,
              f"the {role} market {index} ({run.tickers.get(index)}) is listed ACTIVE")
    spots = [r for r in rows if r["type"] == "SPOT" and r["status"] == "LIVE" and not r["isHalted"] and r.get("hasL2Market")]
    run.spot = cfg.spot or (spots[0]["ticker"] if spots else None)
    perp = run.perps[cfg.markets["trade"]]
    run.proxy = str(perp["proxyAddress"]).lower()
    run.l1_chain, run.l2_chain = int(perp["chainId"]), int(perp["l2ChainId"])
    if run.spot:
        run.spot_l2 = anon.market.get(run.spot)["market"]["l2"]
        check(str(run.spot_l2["proxyAddress"]).lower() == run.proxy and int(run.spot_l2["l2ChainId"]) == run.l2_chain,
              f"spot {run.spot} and the perps share the vault {run.proxy}, L2 chain {run.l2_chain}")
    else:
        run.notes.append("no LIVE spot market with an L2 is listed")

    chain = run.chain = Chain(cfg.rpc)
    check(chain.chain_id == run.l1_chain, f"the RPC serves the vault's chain {run.l1_chain}")
    check(len(chain.rpc("eth_getCode", [run.proxy, "latest"]) or "0x") > 2, f"ZkLoaf is deployed at {run.proxy}")
    run.usdc = chain.asset(run.proxy, USDC_ASSET)
    check(run.usdc["tick"] == int(run.collateral["tickSize"]) and run.usdc["ext"] == int(run.collateral["extensionMultiplier"]),
          f"L1's USDC config (tick {run.usdc['tick']}, ext {run.usdc['ext']}) is the backend's collateral")
    run.start_pointers = chain.batch_pointers(run.proxy)
    log(f"  L1 batches at the start: committed {run.start_pointers[0]}, verified {run.start_pointers[1]}, "
        f"executed {run.start_pointers[2]}")
    run.funder = (cfg.funder or chain.accounts()[5]).lower()

    # The pump: every role market walked home (E2E_HOME_INDEX), so a run starts where the last one did whatever it
    # left; a market an earlier run left unreported is reported again.
    if cfg.price_control and cfg.price_status:
        pump = run.pump = Prices(cfg.price_control, cfg.price_status, cfg.wait_s)
        log(f"  the pump's status: applied control {pump.status().get('applied_seq')}")
        if cfg.home_index:
            home = cfg.home_index
            roles = sorted(set(cfg.markets.values()))
            pump.send({m: {"mode": "walk", "index": home} for m in roles})
            for m in roles:
                run.until(f"market {m}'s index home at {home}", lambda m=m: abs(run.index(m) - home) * 10_000 <= home)
    else:
        run.notes.append("no price control (E2E_PRICE_CONTROL, E2E_PRICE_STATUS): every price leg is INCOMPLETE")

    for i, name in enumerate(USERS):
        wallet = wallet_of(cfg, i, name)
        run.users[name] = User(name, wallet)
        if chain.eth(wallet.address) < 10**19:
            chain.set_gas(wallet.address, 100 * 10**18)

    # The users and their API keys: the wallets' users, a fresh key each run.
    tokens = {name: secrets.token_hex(32) for name in USERS}
    wanted = [
        (f"{cfg.tag}-{name}-{run.users[name].address[2:10]}", run.users[name].address,
         "MARKET_MAKER" if name == "MM" else "INVESTOR", hashlib.sha256(tokens[name].encode()).hexdigest())
        for name in USERS
    ]
    got = {r[0]: r for r in cfg.pg(provision_sql(wanted, int(time.time())))}
    for (label, *_), name in zip(wanted, USERS, strict=True):
        u = run.users[name]
        check(label in got, f"user {name} provisioned with an API key")
        u.user_id, u.api_key = int(got[label][1]), tokens.pop(name)
        u.client = LoafClient(api_key=u.api_key, base_url=cfg.api, ws_url=cfg.ws, max_retries=2)
        u.c.portfolio.component()
    check(True, "every API key authenticates: " + ", ".join(f"{u.name}=user {u.user_id}" for u in run.users.values()))

    # L1 deposits on the spot route, each credited on L2: USDC to everyone; the spot market's quote (when it is
    # not USDC) to A and C and its base to B and MM. A failed one fails the spot step, not the perps ones.
    for name in USERS:
        deposit(run, run.users[name], run.usdc, USDC_TARGET[name])
    if run.spot:
        quote = chain.asset(run.proxy, int(run.spot_l2["quoteAssetId"]))
        base = chain.asset(run.proxy, int(run.spot_l2["baseAssetId"]))
        legs = [(name, quote, Decimal(50_000)) for name in ("A", "C") if quote["id"] != USDC_ASSET]
        try:
            for name, conf, target in legs + [(name, base, Decimal(1_000)) for name in ("B", "MM")]:
                deposit(run, run.users[name], conf, target)
        except (Failed, ChainError) as e:
            run.spot_failed = f"the spot market {run.spot}'s deposits failed in setup: {e}"
            log(f"  {run.spot_failed}")

    # Trading keys: derived as the web app derives them, registered through POST /l2/keys.
    for u in run.users.values():
        register_key(run, u)
        u.client = LoafClient(api_key=u.api_key, base_url=cfg.api, ws_url=cfg.ws, secret_key=u.trading_key,
                              max_retries=2)
        u.trade_floor = newest_trade(u)

    # MM's private channel: its liquidation fills.
    ws = run.mm.c.websocket()
    ws.on_liquidation_fill(run.ws_fills.append)
    ws.subscribe_portfolio()
    ws.start()
    run.sockets.append(ws)
    check(ws.wait_until_connected(30), "MM's WebSocket is connected on its portfolio channel")

    got_refusals = refusals(cfg)
    run.refusals_before = None if got_refusals is None else got_refusals[0]
    log(f"  settled refusals before the run: {run.refusals_before}")


def deposit(run: Run, user: User, conf: Mapping[str, Any], target: Decimal) -> None:
    """``user`` tops its L2 balance of the asset up to ``target`` tokens (at least MIN_DEPOSIT) from its own
    wallet on the spot route (the funder mints them first); its L2 book is credited with exactly that."""
    chain = run.chain
    assert chain is not None
    asset, token, tick, scale = int(conf["id"]), str(conf["token"]), int(conf["tick"]), 10 ** int(conf["decimals"])
    total0, _ = balance(user, asset)
    units = max(int(target * scale) - total0, int(MIN_DEPOSIT * scale))
    if conf["cap"]:
        units = min(units, int(conf["cap"]) * tick)
    units -= units % tick
    check(units // tick >= int(conf["min"]), f"{user.name} deposits {units // tick} ticks of asset {asset}")
    if chain.balance_of(token, user.address) < units:
        chain.send_unlocked(run.funder, token, "mint(address,uint256)", user.address, units)
    if chain.allowance(token, user.address, run.proxy) < units:
        chain.send(user.wallet, token, "approve(address,uint256)", run.proxy, 2**256 - 1)
    chain.send(user.wallet, run.proxy, "deposit(address,uint16,uint8,uint256)", user.address, asset, ROUTE_SPOT, units)
    index = chain.account_index(run.proxy, user.address)
    check(index != NIL_ACCOUNT, f"{user.name} holds L2 account {index} on L1")
    user.l2_account = index
    total = wait_for(f"{user.name}'s deposit of asset {asset} credited",
                     value_once(lambda: balance(user, asset)[0], lambda t: t >= total0 + units), 2 * run.cfg.wait_s)[0]
    check(total == total0 + units, f"{user.name}'s L2 asset {asset}: {total0} + {units} = {total}")


def register_key(run: Run, user: User) -> None:
    """The key in force, derived from the wallet as the web app derives it (key nonce n - 1 for the vault's
    nonce n). With none yet (n == 0) the key of nonce 0 is signed as ChangePubKey and sent through POST /l2/keys,
    then awaited in the journal."""
    chain = run.chain
    assert chain is not None
    state = keys_state(user)
    check(state is not None and int(state["accountIndex"]) == user.l2_account,
          f"{user.name}'s backend account is its L1 account {user.l2_account}")
    n = chain.key_nonce(run.proxy, user.address)
    typed = l2_key_typed_data(user.wallet.address, chain_id=run.l1_chain, proxy_address=run.proxy,
                              l2_chain_id=run.l2_chain, account_index=user.l2_account, key_nonce=max(n - 1, 0))
    sk = k1.key_from_seed(keccak(user.wallet.sign_message(encode_typed_data(full_message=typed)).signature))
    pk = "0x" + k1.public_key(sk).hex()
    if n == 0:
        deadline = int(time.time()) + 600
        signed = user.wallet.sign_message(encode_typed_data(full_message=change_pub_key_typed_data(
            user.address, run.l1_chain, run.proxy, user.l2_account, bytes.fromhex(pk[2:]), 0, deadline)))
        answer = user.c.post("/l2/keys", json={
            "owner": user.address, "accountIndex": user.l2_account, "apiKeyIndex": 0, "pubKey": pk,
            "deadline": deadline, "signature": "0x" + bytes(signed.signature).hex()})
        check(answer.get("status") in ("SENT", "ALREADY_QUEUED"),
              f"{user.name}'s key change {answer.get('status')} (tx {answer.get('txHash')})")
    run.until(f"{user.name}'s key journaled", lambda: (lambda s: bool(s) and s.get("journaledKey") == pk
                                                       and s.get("pending") is False)(keys_state(user, pk)), 600, 3.0)
    check(chain.key_nonce(run.proxy, user.address) == max(n, 1), f"{user.name}'s key is the vault's (key nonce {max(n - 1, 0)})")
    user.trading_key = sk.hex()


# --------------------------------------------------------------------------------------------- #
# 2. spot
# --------------------------------------------------------------------------------------------- #


def spot_size(bid: Decimal) -> Decimal:
    """The spot leg's size, whole tokens: its least order, the bid, is worth SPOT_VALUE_MARGIN times the backend's
    least order value, and every other order of the leg is that size or twice it at a higher price."""
    return max(Decimal(1), (SPOT_MIN_VALUE * SPOT_VALUE_MARGIN / bid).to_integral_value(rounding=ROUND_CEILING))


def step_spot(run: Run) -> None:
    if run.spot_failed:
        raise Failed(run.spot_failed)
    if not run.spot:
        raise Incomplete("no spot market to trade: no LIVE spot market with an L2 is listed")
    chain = run.chain
    assert chain is not None
    t = run.spot
    a, b, c, mm = (run.users[n] for n in USERS)
    quote, base = int(run.spot_l2["quoteAssetId"]), int(run.spot_l2["baseAssetId"])
    qscale, bscale = 10 ** chain.asset(run.proxy, quote)["decimals"], 10 ** chain.asset(run.proxy, base)["decimals"]
    row = next((r for r in a.c.market.list()["markets"] if r["ticker"] == t), {})
    ref = Decimal(str(row.get("marketPrice") or 0)) or run.cfg.spot_price
    bid, ask = run.cents(ref * Decimal("0.98"), False), run.cents(ref * Decimal("1.02"), True)
    q = spot_size(bid)
    log(f"  spot {t}: reference ${ref}, bid ${bid}, ask ${ask}, size {q}")
    run.cancel_all()

    # a limit order rests, and its cancel releases exactly its hold
    _, avail0 = balance(a, quote)
    r = placed(a.c.orders.limit_buy(t, float(q), float(bid)), "A's limit bid")
    check(left(r) == q, "A's bid rests whole")
    run.until("A's bid among its active orders", lambda: int(r["orderId"]) in active_ids(a))
    run.until("the bid holds A's cash", lambda: balance(a, quote)[1] < avail0)
    a.c.orders.cancel(int(r["orderId"]))
    run.until("the cancel released exactly the bid's hold", lambda: balance(a, quote)[1] == avail0)
    run.until("the bid off A's active orders", lambda: int(r["orderId"]) not in active_ids(a))

    # post-only rests, and is refused where it would take
    r = placed(mm.c.orders.limit_sell(t, float(q), float(ask), time_in_force="POST_ONLY"), "MM's post-only ask")
    check(left(r) == q, "the post-only ask rests whole")
    refused(lambda: a.c.orders.limit_buy(t, float(q), float(ask), time_in_force="POST_ONLY"), PostOnlyWouldTakeError)
    check(True, "A's post-only bid at the ask is refused POST_ONLY_WOULD_TAKE")

    # an IOC with nothing to cross and a FOK for more than rests are refused whole
    e = refused(lambda: a.c.orders.limit_buy(t, float(q), float(bid), time_in_force="IOC"), LoafValidationError)
    check(NOT_FILLED in str(e), f"an IOC under the ask: {e}")
    e = refused(lambda: a.c.orders.limit_buy(t, float(2 * q), float(ask), time_in_force="FOK"), LoafValidationError)
    check(FOK_NOT_FILLED in str(e), f"a FOK for twice the ask: {e}")
    # an IOC for twice the ask takes it and cancels the rest; a FOK that can fill whole does
    base0, _ = balance(a, base)
    r = placed(a.c.orders.limit_buy(t, float(2 * q), float(ask), time_in_force="IOC"), "A's IOC for twice the ask")
    check(left(r) == q, f"the IOC filled the ask's {q} and left {left(r)}")
    run.until(f"A's {q} bought by the IOC", lambda: balance(a, base)[0] - base0 == int(q * bscale))
    check(int(r["orderId"]) not in active_ids(a), "nothing of the IOC rests")
    placed(mm.c.orders.limit_sell(t, float(q), float(ask)), "MM's ask")
    r = placed(a.c.orders.limit_buy(t, float(q), float(ask), time_in_force="FOK"), "A's FOK for the ask")
    check(left(r) == 0, "the FOK filled whole")

    # a crossing fill between two users: C buys B's ask
    base_c0, quote_c0 = balance(c, base)[0], balance(c, quote)[0]
    base_b0 = balance(b, base)[0]
    rb = placed(b.c.orders.limit_sell(t, float(q), float(ask)), "B's ask")
    rc = placed(c.c.orders.limit_buy(t, float(q), float(ask)), "C's crossing bid")
    check(left(rc) == 0, "C's bid filled whole")
    run.until(f"{q} tokens moved from B to C", lambda: balance(c, base)[0] - base_c0 == int(q * bscale)
              and base_b0 - balance(b, base)[0] == int(q * bscale))
    paid, notional = quote_c0 - balance(c, quote)[0], int(ask * q * qscale)
    check(notional <= paid <= notional * 101 // 100, f"C paid the notional {notional} plus its fee: {paid}")
    check(int(rb["orderId"]) not in active_ids(b), "B's ask is off the book")
    trades = c.c.history.trades(page_size=20)["trades"]
    check(any(int(x.get("orderId") or 0) == int(rc["orderId"]) for x in trades), "the fill is in C's trade history")


# --------------------------------------------------------------------------------------------- #
# 3. perps
# --------------------------------------------------------------------------------------------- #


def step_perps(run: Run) -> None:
    m = run.mkt("trade")
    t = run.ticker(m)
    a, b, mm = run.users["A"], run.users["B"], run.mm
    run.cancel_all()
    run.fund(mm, 0, Decimal(100_000))
    run.fund(b, 0, Decimal(10_000))
    for pool in (0, 1, 2, 3):
        run.fund(a, pool, Decimal(2_000))
    for pool in (0, 1, 2, 3):
        run.flat(a, m, pool)
    run.flat(b, m, 0)
    # allocates both ways: into the cross pool and an isolated one, and out of the isolated one
    run.allocate(a, Decimal(1_000), PERPS_SPOT_SIDE, 0)
    run.allocate(a, Decimal(1_000), PERPS_SPOT_SIDE, 1)
    run.allocate(a, Decimal(500), 1, PERPS_SPOT_SIDE)

    live = run.live(m)
    check(live.get("tradingState") == "OPEN", f"market {m} is OPEN: {live.get('tradingState')} {live.get('stateReason')}")
    idx = int(live["indexPrice"])
    px = run.dollars(idx)
    q = run.qty(m)
    units = run.units(q)
    lo, hi = run.cents(px * Decimal("0.999"), False), run.cents(px * Decimal("1.001"), True)
    log(f"  market {m} ({t}): index {idx} = ${px}, size {q}, bid ${lo}, ask ${hi}")

    # long by market (A takes MM's ask), short by market (B hits MM's bid)
    placed(mm.c.orders.limit_sell(t, float(q), float(hi), pool=0), "MM's ask")
    placed(mm.c.orders.limit_buy(t, float(q), float(lo), pool=0), "MM's bid")
    r = a.c.orders.market_buy(t, float(q), reference_price=float(px), max_slippage_bps=50, pool=0)
    check(left(r) == 0, f"A's market buy filled: order {r.get('orderId')}")
    run.wait_base(a, m, 0, units)
    r = b.c.orders.market_sell(t, float(q), reference_price=float(px), max_slippage_bps=50, pool=0)
    check(left(r) == 0, f"B's market sell filled: order {r.get('orderId')}")
    run.wait_base(b, m, 0, -units)
    pos = position_of(a, m, 0)
    assert pos is not None
    check(abs(int(pos["entryPrice"]) - run.to_index(hi)) <= 1, f"A's entry {pos['entryPrice']} is the ask ${hi}")

    # long and short by limit, in isolated pool 1
    placed(mm.c.orders.limit_sell(t, float(q), float(hi), pool=0), "MM's ask")
    r = placed(a.c.orders.limit_buy(t, float(q), float(hi), pool=1), "A's crossing limit buy from pool 1")
    check(left(r) == 0, "A's limit buy filled whole")
    run.wait_base(a, m, 1, units)
    placed(mm.c.orders.limit_buy(t, float(q), float(lo), pool=0), "MM's bid")
    r = placed(a.c.orders.limit_sell(t, float(q), float(lo), pool=1), "A's crossing limit sell from pool 1")
    check(left(r) == 0, "A's limit sell filled whole")
    run.wait_base(a, m, 1, 0)

    # post-only rests below the ask and is refused at it; an IOC with nothing to cross; a FOK for more than rests
    r = placed(a.c.orders.limit_buy(t, float(q), float(lo), pool=0, time_in_force="POST_ONLY"), "A's post-only bid")
    check(left(r) == q, "A's post-only bid rests whole")
    a.c.orders.cancel(int(r["orderId"]))
    placed(mm.c.orders.limit_sell(t, float(q), float(hi), pool=0), "MM's ask")
    refused(lambda: a.c.orders.limit_buy(t, float(q), float(hi), pool=0, time_in_force="POST_ONLY"),
            PostOnlyWouldTakeError)
    check(True, "A's post-only bid at the ask is refused POST_ONLY_WOULD_TAKE")
    e = refused(lambda: b.c.orders.limit_sell(t, float(q), float(hi + 1), pool=0, time_in_force="IOC"),
                LoafValidationError)
    check(NOT_FILLED in str(e), f"B's IOC above every bid: {e}")
    e = refused(lambda: a.c.orders.limit_buy(t, float(2 * q), float(hi), pool=0, time_in_force="FOK"),
                LoafValidationError)
    check(FOK_NOT_FILLED in str(e), f"A's FOK for twice the ask: {e}")
    check(base_of(a, m, 0) == units, "the refused FOK filled nothing")

    # reduce-only: a sell of half A's long fills; a reduce-only buy in a flat pool opens nothing
    placed(mm.c.orders.limit_buy(t, float(q), float(lo), pool=0), "MM's bid")
    half = (q / 2).quantize(Decimal(1) / (Decimal(10) ** run.cfg.size_decimals), rounding=ROUND_FLOOR)
    placed(a.c.orders.limit_sell(t, float(half), float(lo), pool=0, reduce_only=True), "A's reduce-only sell of half")
    run.wait_base(a, m, 0, units - run.units(half))
    try:
        r = a.c.orders.limit_buy(t, float(q), float(hi), pool=2, reduce_only=True)
        log(f"  A's reduce-only buy in flat pool 2 was taken: {short(r)}")
    except LoafAPIError as e:
        log(f"  A's reduce-only buy in flat pool 2 is refused {e.status_code}: {e}")
    time.sleep(3)
    check(base_of(a, m, 2) == 0, "A's reduce-only buy in flat pool 2 opened nothing (MM's ask rests at its price)")
    run.cancel_all()

    # TP/SL legs: armed by the parent's fill; the take-profit fires on the index's rise and closes the long
    placed(mm.c.orders.limit_sell(t, float(q), float(hi), pool=0), "MM's ask")
    tp, sl = run.cents(px * Decimal("1.02"), True), run.cents(px * Decimal("0.97"), False)
    r = placed(a.c.orders.limit_buy(t, float(q), float(hi), pool=3, tp_price=float(tp), sl_price=float(sl),
                                    leg_max_slippage_bps=100), f"A's buy from pool 3 with TP ${tp} and SL ${sl}")
    parent = int(r["orderId"])
    check(len(r.get("attachedLegIds") or []) == 2, f"two legs attached: {r.get('attachedLegIds')}")
    run.wait_base(a, m, 3, units)
    rows = run.until("both legs ARMED by the parent's fill", lambda: (lambda x: x if len(x) == 2 and all(
        o.get("status") == "ARMED" for o in x) else None)(legs_of(a, parent)))
    log(f"  legs: {[(o.get('id'), o.get('type'), o.get('triggerPrice'), o.get('status')) for o in rows]}")
    placed(mm.c.orders.limit_buy(t, float(q), float(run.cents(tp * Decimal("0.995"), False)), pool=0),
           "MM's bid for the take-profit")
    run.walk(m, run.to_index(tp * Decimal("1.004")))
    run.wait_base(a, m, 3, 0)
    rows = legs_of(a, parent)
    log(f"  legs after the trigger: {[(o.get('id'), o.get('type'), o.get('status')) for o in rows]}")
    for o in rows:
        if o.get("status") in ("PENDING", "ARMED"):
            a.c.orders.cancel_conditional(int(o["id"]))
    run.cancel_all()
    run.walk(m, idx)

    # the reads: A's long and B's short, their pools, the market's live state
    for user, sign in ((a, 1), (b, -1)):
        pos, pool = position_of(user, m, 0), pool_of(user, 0)
        assert pos is not None and pool is not None
        for k in ("entryPrice", "entryQuote", "notional", "fundingOwed", "liquidationPrice", "adlRank"):
            check(k in pos, f"{user.name}'s position reads {k} = {pos.get(k)}")
        upnl = (run.index(m) - int(pos["entryPrice"])) * int(pos["base"])
        log(f"  {user.name}'s pool 0: collateral {pool['collateral']}, value {pool['value']}, requirement "
            f"{pool['requirement']}; uPnL ${run.dollars(upnl) / 10 ** run.cfg.size_decimals:.2f}")
        liq = pos.get("liquidationPrice")
        if liq is not None:
            check((int(liq) - int(pos["entryPrice"])) * sign < 0,
                  f"{user.name}'s liquidation price {liq} is on the losing side of its entry {pos['entryPrice']}")
        check(pool["initialMarginOk"] is True and pool["liquidatable"] is False, f"{user.name}'s pool 0 is healthy")
    live = run.live(m)
    for k in ("indexPrice", "openInterest", "fundingIndex", "nextTickAtMs", "fundingPeriodMs", "tradingState"):
        check(live.get(k) is not None, f"market {m}'s live state reads {k} = {live.get(k)}")
    check(int(live["openInterest"]) >= units, "the open interest counts the run's positions")


# --------------------------------------------------------------------------------------------- #
# 4. funding
# --------------------------------------------------------------------------------------------- #


def step_funding(run: Run) -> None:
    a, mm = run.users["A"], run.mm
    run.cancel_all()
    run.fund(mm, 0, Decimal(100_000))
    legs = (("carry_pos", 1, 6), ("carry_neg", -1, 7))
    since: dict[int, int] = {}
    for role, _, pool in legs:
        m = run.mkt(role)
        t = run.ticker(m)
        run.fund(a, pool, Decimal(1_000))
        run.flat(a, m, pool)
        px = run.cents(run.dollars(run.index(m)), True)  # at the index: the fill adds no premium sample
        q = run.qty(m)
        placed(mm.c.orders.limit_sell(t, float(q), float(px), pool=0), f"MM's ask at market {m}'s index")
        placed(a.c.orders.limit_buy(t, float(q), float(px), pool=pool), f"A's long on carry market {m}")
        run.wait_base(a, m, pool, run.units(q))
        ticks = mm.c.perps.funding_ticks(m, limit=1)["ticks"]
        since[m] = int(ticks[0]["journalSeq"]) if ticks else 0

    period_s = int(run.live(run.mkt("carry_pos"))["fundingPeriodMs"]) / 1000
    for role, sign, pool in legs:
        m = run.mkt(role)
        t = run.ticker(m)

        def moved(m: int = m) -> list[Mapping[str, Any]] | None:
            got = [x for x in mm.c.perps.funding_ticks(m, limit=20)["ticks"] if int(x["journalSeq"]) > since[m] and x["moved"]]
            return got if len(got) >= 2 else None

        got = run.until(f"two funding ticks moving market {m}", moved,
                        min(3 * period_s + run.cfg.wait_s, run.cfg.final_wait_s), 5.0)
        rates = [int(x["rateCbp"]) for x in got]
        check(all(r * sign > 0 for r in rates), f"market {m}'s ticks carry the sign {sign:+d}: rates {rates}")
        pos = position_of(a, m, pool)
        assert pos is not None
        owed = int(pos["fundingOwed"])
        check(owed * sign > 0, f"A's long {'pays' if sign > 0 else 'receives'} on market {m}: funding owed {owed}")
        held = position_of(mm, m, 0)
        if held is not None and int(held["base"]) < 0:
            check(int(held["fundingOwed"]) * sign < 0, f"MM's short on market {m} the other way: {held['fundingOwed']}")

    # The closes, once every leg's owed is read: a close fills against MM's pool 0, whose first touch after a tick
    # settles the whole pool's funding (K1's rule), the other carry market's short with it.
    for role, sign, pool in legs:
        m = run.mkt(role)
        t = run.ticker(m)

        # the close, away from a tick: the pool keeps its value (funding owed counted) less the close's fee
        live = run.live(m)
        wait_ms = int(live["nextTickAtMs"]) - int(time.time() * 1000)
        if wait_ms < 8_000:
            time.sleep(max(wait_ms, 0) / 1000 + 2)
        p = pool_of(a, pool)
        pos = position_of(a, m, pool)
        assert p is not None and pos is not None
        value, owed, notional = int(p["value"]), int(pos["fundingOwed"]), int(pos["notional"])
        check(value == int(p["collateral"]) + notional - owed,
              f"pool {pool}'s value {value} = collateral {p['collateral']} + notional {notional} - owed {owed}")
        px = run.cents(run.dollars(run.index(m)), False)
        q = run.tokens(int(pos["base"]))
        placed(mm.c.orders.limit_buy(t, float(q), float(px), pool=0), f"MM's bid at market {m}'s index")
        placed(a.c.orders.limit_sell(t, float(q), float(px), pool=pool, reduce_only=True, time_in_force="IOC"),
               f"A's close on market {m}")
        run.wait_base(a, m, pool, 0)
        after = collateral_of(a, pool)
        fee = notional * int(run.perps[m]["takerFee"]) // 1_000_000 + 1
        slack = int(q * Decimal("0.01") * run.ext_per_dollar()) + int(run.collateral["extensionMultiplier"])
        check(value - fee - slack <= after <= value + slack,
              f"the close realised the funding: pool {pool}'s collateral {after} = value {value} less the fee "
              f"(<= {fee}), within {slack}")


# --------------------------------------------------------------------------------------------- #
# 5. liquidation
# --------------------------------------------------------------------------------------------- #


def open_victim(run: Run, market: int, pool: int) -> tuple[int, Mapping[str, Any]]:
    """C opens a long on ``market`` from isolated ``pool`` holding its initial margin plus 10 %, against the
    maker's ask at the index, with no bid left on the book; returns the index and C's pool."""
    c, mm = run.users["C"], run.mm
    t = run.ticker(market)
    run.flat(c, market, pool)
    run.sweep(c, pool)
    idx = run.index(market)
    px = run.cents(run.dollars(idx), True)
    q = run.qty(market)
    im = Decimal(2 * int(run.perps[market]["risk"])) / 65_536 * q * px
    run.allocate(c, (im * Decimal("1.10") + 2).quantize(Decimal(1), rounding=ROUND_CEILING), PERPS_SPOT_SIDE, pool)
    placed(mm.c.orders.limit_sell(t, float(q), float(px), pool=0), "MM's ask at the index")
    placed(c.c.orders.limit_buy(t, float(q), float(px), pool=pool), f"C's long on market {market}")
    run.wait_base(c, market, pool, run.units(q))
    run.cancel_all()
    run.until(f"market {market}'s book with no bid", lambda: (lambda b: b is not None and not b.get("bids"))(run.book(market)))
    p = pool_of(c, pool)
    assert p is not None
    return run.index(market), p


def closes_of(user: User, market: int, pool: int, after: int) -> list[Mapping[str, Any]]:
    """``user``'s closes on ``market`` in ``pool`` past journal ``after``, newest first."""
    return [row for row in user.c.perps.closes(limit=50)["closes"]
            if int(row["market"]) == market and int(row["pool"]) == pool and int(row["journalSeq"]) > after]


def close_of(user: User, market: int, pool: int, after: int) -> Mapping[str, Any] | None:
    got = closes_of(user, market, pool, after)
    return got[0] if got else None


def last_close(user: User) -> int:
    rows = user.c.perps.closes(limit=1)["closes"]
    return int(rows[0]["journalSeq"]) if rows else 0


def tape_take(run: Run, market: int, seq: int) -> Mapping[str, Any] | None:
    """The market's tape print of the liquidation take ``seq`` (isLiquidation, tradeId == seq), if any."""
    after = 0
    while True:
        page = run.mm.c.market.trades(run.ticker(market), after_trade_id=after, limit=1000)["trades"]
        for row in page:
            if row.get("isLiquidation") and int(row["tradeId"]) == seq:
                return row
        if len(page) < 1000:
            return None
        after = max(int(row["tradeId"]) for row in page)


def counters_of(run: Run, rows: Sequence[Mapping[str, Any]], market: int) -> None:
    """Each deleverage of C's names its counter in the journal, whose own close (adl) is under the same journalSeq;
    when the counter is MM, MM's closes read it too."""
    c, mm = run.users["C"], run.mm
    seqs = sorted({int(r["journalSeq"]) for r in rows})
    pairs: dict[int, dict[bool, tuple[int, int, int, int]]] = {}
    for seq, adl, account, pool, counter, counter_pool in run.cfg.journal(deleverage_sql(seqs)):
        pairs.setdefault(int(seq), {})[adl == "t"] = (int(account), int(pool), int(counter), int(counter_pool))
    for seq in seqs:
        mine, theirs = pairs.get(seq, {}).get(False), pairs.get(seq, {}).get(True)
        check(mine is not None and mine[0] == c.l2_account and theirs is not None and theirs[:2] == mine[2:],
              f"deleverage {seq}: C's close names its counter, whose own close is under it: {pairs.get(seq)}")
        assert theirs is not None
        if theirs[0] != mm.l2_account:
            log(f"  deleverage {seq}'s counter is account {theirs[0]} pool {theirs[1]}, not MM")
            continue
        row = run.until(f"MM's own close of deleverage {seq}", lambda seq=seq: next(
            (r for r in mm.c.perps.closes(limit=50)["closes"] if int(r["journalSeq"]) == seq and r["adl"]), None))
        check(row["kind"] == "DELEVERAGE" and int(row["market"]) == market and int(row["pool"]) == theirs[1],
              f"MM's short was deleverage {seq}'s counter: {short(row)}")


def step_liquidation(run: Run) -> None:
    c, mm = run.users["C"], run.mm
    run.cancel_all()
    run.fund(mm, 0, Decimal(100_000))
    m1 = run.mkt("liq1")

    # one run per stack: a long the backstop took in an earlier run's mode 1 would ride this deleverage walk with its
    # free collateral at 0, which takes its pool 0 under initial margin
    held = run.cfg.journal(backstop_positions_sql([m1]))
    check(not held, f"the backstop holds no position on market {m1} (one from an earlier run needs a fresh stack): "
                    f"{held}")

    # the deleverage: the backstop's free collateral at 0 refuses C's long (a refused 64 mode 1), and once the
    # walk takes C's pool below 0 (K1 deleverages no pool above it) the 65 closes it against the best-ranked counter
    run.backstop(0)
    after = last_close(c)
    p0, pool = open_victim(run, m1, 1)
    run.walk(m1, index_for(pool, m1, p0, -1, 5))
    rows = run.until("C's long closed by deleverage", lambda: (lambda got: got if got and got[0]["baseAfter"] == "0"
                                                               else None)(closes_of(c, m1, 1, after)))
    check(all(r["kind"] == "DELEVERAGE" and not r["adl"] for r in rows),
          f"only deleverages closed C's long, no backstop take: {short(rows)}")
    check(position_of(c, m1, 1) is None, "C's pool 1 holds no position")
    counters_of(run, rows, m1)
    run.walk(m1, p0)

    # mode 1: with the backstop funded and no bid on the book, the index walks C's pool to 2/5 of its maintenance
    # and the backstop takes it; it is funded only once no other pool waits for it (backstop_clear)
    after = last_close(c)
    p0, pool = open_victim(run, m1, 2)
    if run.backstop_clear((c.l2_account, 2)):
        run.backstop(run.backstop_ticks())
        run.walk(m1, index_for(pool, m1, p0, 2, 5))
        row = run.until("C's close on the mode 1 market", lambda: close_of(c, m1, 2, after))
        seq1 = int(row["journalSeq"])
        check(row["kind"] == "LIQUIDATION" and row["baseAfter"] == "0" and not row["adl"],
              f"the backstop took C's whole long: {short(row)}")
        check(position_of(c, m1, 2) is None, "C's pool 2 holds no position")
        check(not [f for f in run.ws_fills if int(f["fill"]["perpsSeq"]) == seq1],
              "no order of MM's filled the backstop's take")
        run.walk(m1, p0)

    # mode 0 (the P5 kit's mode0_leg): the index walks C's pool to 17/20 of its maintenance, the maker bids just
    # under the index, and a batch's liquidation step takes C's long into that bid (a far ask runs a batch)
    m0 = run.mkt("liq0")
    t0 = run.ticker(m0)
    after = last_close(c)
    p0, pool = open_victim(run, m0, 4)
    target = index_for(pool, m0, p0, 17, 20)
    run.walk(m0, target)
    check(close_of(c, m0, 4, after) is None, "no liquidation with no bid on the book above 2/3 of maintenance")
    q = run.tokens(base_of(c, m0, 4))
    fills0 = len(run.ws_fills)
    bid = placed(mm.c.orders.limit_buy(t0, float(q), float(run.cents(run.dollars(target) * Decimal("0.999"), False)),
                                       pool=0), "MM's bid just under the index")
    far = float(run.cents(run.dollars(target) * Decimal("1.05"), True))
    tick = float(run.tokens(1))

    def taken() -> Mapping[str, Any] | None:
        got = close_of(c, m0, 4, after)
        if got is None:
            mm.c.orders.limit_sell(t0, tick, far, pool=0)
        return got

    row = run.until("C's close on the mode 0 market", taken, every=2.0)
    seq = int(row["journalSeq"])
    check(row["kind"] == "LIQUIDATION" and row["baseAfter"] == "0" and not row["adl"],
          f"the liquidation took C's whole long into the book: {short(row)}")
    check(position_of(c, m0, 4) is None, "C's pool 4 holds no position")
    fill = run.until("MM's liquidation fill in its portfolio", lambda: next(
        (f for f in mm.c.portfolio.component().get("liquidationFills", []) if int(f["perpsSeq"]) == seq), None))
    check(int(fill["orderId"]) == int(bid["orderId"]) and fill.get("role") == "MAKER" and Decimal(str(fill.get("fee") or 0)) == 0,
          f"MM's bid {bid['orderId']} filled by take {seq} as MAKER, no fee: {short(fill)}")
    check("tradeId" not in fill and "txHash" not in fill, "the fill carries no tradeId and no txHash")
    frame = run.until("MM's liquidation_fill frame", lambda: next(
        (f for f in run.ws_fills[fills0:] if int(f["fill"]["perpsSeq"]) == seq), None))
    check(int(frame["fill"]["orderId"]) == int(bid["orderId"]), "the frame names MM's bid")
    take = run.until(f"the tape's take {seq}", lambda: tape_take(run, m0, seq))
    check(int(take["tradeId"]) == seq and "txHash" not in take,
          f"the tape prints the take as tradeId {take['tradeId']} == perpsSeq == the close's journalSeq")


# --------------------------------------------------------------------------------------------- #
# 6. halt, reduce-only, a stale price
# --------------------------------------------------------------------------------------------- #


def step_halt(run: Run) -> None:
    a, b, mm = run.users["A"], run.users["B"], run.mm
    m = run.mkt("halt")
    t = run.ticker(m)
    run.cancel_all()
    run.reopen(m)
    run.fund(a, 5, Decimal(1_000))
    run.fund(b, 0, Decimal(10_000))
    run.flat(a, m, 5)
    run.flat(b, m, 0)
    idx = run.index(m)
    px = run.dollars(idx)
    q = run.qty(m)
    units = run.units(q)
    step = Decimal(1) / (Decimal(10) ** run.cfg.size_decimals)
    half, quarter = (q / 2).quantize(step, rounding=ROUND_FLOOR), (q / 4).quantize(step, rounding=ROUND_FLOOR)
    check(quarter > 0, f"a quarter of the size {q} is an order ({quarter})")
    lo, hi = run.cents(px * Decimal("0.999"), False), run.cents(px * Decimal("1.001"), True)
    placed(b.c.orders.limit_sell(t, float(q), float(hi), pool=0), "B's ask")
    placed(a.c.orders.limit_buy(t, float(q), float(hi), pool=5), "A's long on the halt market")
    run.wait_base(a, m, 5, units)
    run.wait_base(b, m, 0, -units)
    resting = placed(mm.c.orders.limit_sell(t, float(q), float(run.cents(px * Decimal("1.05"), True)), pool=0),
                     "MM's far ask, resting through the halt")

    # the admin API key halts: no order is taken, a cancel is; its resume is refused
    r = run.halt(m)
    check(r.status_code == 200 and r.json().get("engineLoaded") is True,
          f"the admin API key halts market {m}: {r.status_code} {short(r.text, 200)}")
    run.state_is(m, "HALTED")
    check(mm.c.market.get(t)["market"]["isHalted"] is True, f"GET /trade/{t} reads isHalted")
    e = refused(lambda: a.c.orders.limit_buy(t, float(q), float(lo), pool=5), MarketHaltedError)
    check(e.state == "MARKET_HALTED", f"an opening order is refused while halted: {e.status_code} {e}")
    e = refused(lambda: a.c.orders.limit_sell(t, float(q), float(lo), pool=5, reduce_only=True), MarketHaltedError)
    check(e.state == "MARKET_HALTED", f"a reducing one too: {e}")
    mm.c.orders.cancel(int(resting["orderId"]))
    run.until("the cancel taken on the halted market", lambda: int(resting["orderId"]) not in active_ids(mm))
    r = run.admin("/admin/markets/halt", {"marketIndex": m, "status": "ACTIVE"})
    check(r.status_code == 403 and RESUME_NEEDS_OPERATOR in r.text, f"the admin API key cannot resume: {r.status_code}")

    # the operator's reduce-only takes only orders that reduce; its reopen takes an opening order again
    run.operator(m, "REDUCE_ONLY")
    run.state_is(m, "REDUCE_ONLY")
    check(mm.c.market.get(t)["market"]["isHalted"] is False, f"GET /trade/{t} reads the halt lifted")
    e = refused(lambda: a.c.orders.limit_buy(t, float(q), float(lo), pool=5), MarketStateError)
    check(e.state == "REDUCE_ONLY", f"an order adding to A's long is refused REDUCE_ONLY: {e}")
    placed(b.c.orders.limit_buy(t, float(half), float(lo), pool=0, reduce_only=True), "B's reduce-only bid")
    placed(a.c.orders.limit_sell(t, float(half), float(lo), pool=5, reduce_only=True), "A's reduce-only sell")
    run.wait_base(a, m, 5, units - run.units(half))
    run.wait_base(b, m, 0, -units + run.units(half))
    run.operator(m, "OPEN")
    run.state_is(m, "OPEN")
    r = placed(a.c.orders.limit_buy(t, float(q), float(lo), pool=5, time_in_force="POST_ONLY"),
               "an order adding to A's long after the reopen")
    a.c.orders.cancel(int(r["orderId"]))

    # a stale price: the pump stops reporting the market; past its stale window it takes only orders that reduce,
    # until the reports resume
    run.silence(m)
    run.state_is(m, "PRICE_STALE")
    e = refused(lambda: a.c.orders.limit_buy(t, float(q), float(lo), pool=5), MarketStateError)
    check(e.state == "PRICE_STALE", f"an order adding to A's long is refused PRICE_STALE: {e}")
    placed(b.c.orders.limit_buy(t, float(quarter), float(lo), pool=0, reduce_only=True), "B's reduce-only bid")
    placed(a.c.orders.limit_sell(t, float(quarter), float(lo), pool=5, reduce_only=True), "A's reduce-only sell")
    run.wait_base(a, m, 5, units - run.units(half) - run.units(quarter))
    run.wait_base(b, m, 0, -units + run.units(half) + run.units(quarter))
    run.walk(m, idx)
    run.state_is(m, "OPEN")


# --------------------------------------------------------------------------------------------- #
# 7. exit: allocate out, withdraw on L2, claim on L1
# --------------------------------------------------------------------------------------------- #


def step_exit(run: Run) -> None:
    chain = run.chain
    assert chain is not None
    a = run.users["A"]
    run.cancel_all()
    for pos in (pool_of(a, 0) or {}).get("positions", []):
        run.flat(a, int(pos["market"]), 0)
    check(not (pool_of(a, 0) or {}).get("positions"), "A's pool 0 holds no position")
    run.sweep(a, 0)
    check(collateral_of(a, 0) < int(run.collateral["extensionMultiplier"]), "pool 0's whole ticks are back in spot")

    tick, scale = int(run.usdc["tick"]), 10 ** int(run.usdc["decimals"])
    _, avail = balance(a, USDC_ASSET)
    w = min(1_000 * scale // tick, avail // tick)
    check(w > 0, f"A withdraws {w} ticks")
    state = keys_state(a)
    assert state is not None
    nonce = int(state["withdrawalNonce"])
    expiry = int(time.time() * 1000) + BUFFER_MS + 600_000
    s, e = k1.sign(bytes.fromhex(a.trading_key),
                   withdraw_message(run.l2_chain, a.l2_account, USDC_ASSET, ROUTE_SPOT, w, nonce, expiry), os.urandom(32))
    total0, _ = balance(a, USDC_ASSET)
    pend0 = chain.pending_balance(run.proxy, a.address, USDC_ASSET)
    r = a.c.post("/l2/withdrawals", json={
        "l2AccountIndex": a.l2_account, "l2ApiKeyIndex": 0, "l2AssetId": USDC_ASSET, "l2Route": ROUTE_SPOT,
        "amount": str(w), "l2Nonce": nonce, "l2ExpiryMs": expiry, "l2ChainId": run.l2_chain,
        "l2SigS": s.hex(), "l2SigE": e.hex()})
    seq = int(r.get("journalSeq") or 0)
    check(seq > 0 and r.get("duplicate") is False, f"A's L2 withdrawal of {w} ticks journaled at {seq}")
    run.until(f"A's L2 USDC debited {w * tick}", lambda: total0 - balance(a, USDC_ASSET)[0] == w * tick)
    pend = run.until(f"the withdrawal executed on L1 (A's pending balance {pend0} + {w})",
                     value_once(lambda: chain.pending_balance(run.proxy, a.address, USDC_ASSET), lambda x: x >= pend0 + w),
                     run.cfg.final_wait_s, 10.0)[0]
    check(pend == pend0 + w, f"A's L1 pending balance is {pend}")
    row = run.until("the withdrawal's transfer row CONFIRMED", lambda: next(
        (x for x in a.c.portfolio.component().get("transfers", []) if int(x.get("journalSeq") or 0) == seq
         and x.get("status") == "CONFIRMED"), None), run.cfg.wait_s, 5.0)
    batch = row.get("batch") or {}
    log(f"  transfer row: {short(row)}")
    run.last_batch = max(run.last_batch, int(batch.get("batchNumber") or 0))
    wallet0 = chain.balance_of(run.usdc["token"], a.address)
    chain.send(a.wallet, run.proxy, "withdrawPendingBalance(address,uint16,uint128)", a.address, USDC_ASSET, pend)
    paid = chain.balance_of(run.usdc["token"], a.address) - wallet0
    check(paid == pend * tick and chain.pending_balance(run.proxy, a.address, USDC_ASSET) == 0,
          f"the L1 claim paid A's wallet {paid} and left nothing pending")


# --------------------------------------------------------------------------------------------- #
# 8. proof
# --------------------------------------------------------------------------------------------- #


def step_proof(run: Run) -> None:
    chain = run.chain
    assert chain is not None
    waiting: list[Any] = []

    def all_final() -> bool:
        waiting.clear()
        for u in run.users.values():
            for row in u.c.history.iter_trades(page_size=HISTORY_PAGE_MAX):
                if int(row["tradeId"]) <= u.trade_floor:
                    break
                proof = row.get("proof") or {}
                if proof.get("status") == "IN_BATCH" and proof.get("final") is True:
                    run.last_batch = max(run.last_batch, int(proof["batchNumber"]))
                else:
                    waiting.append((u.name, row["tradeId"], proof.get("status")))
        return not waiting

    try:
        wait_for("every trade of the run in a final batch", all_final, run.cfg.proof_wait_s, 15.0)
    except Failed as e:
        raise Failed(f"{e}; {len(waiting)} not final, the first: {waiting[:10]}") from e
    check(run.last_batch > run.start_pointers[0],
          f"every trade of the run is IN_BATCH and final; the run's batches are {run.start_pointers[0] + 1}..{run.last_batch}")
    c, v, e = run.until(f"L1 verified and executed through batch {run.last_batch}", lambda: (
        lambda p: p if p[1] >= run.last_batch and p[2] >= run.last_batch else None)(chain.batch_pointers(run.proxy)),
        run.cfg.proof_wait_s, 15.0)
    log(f"  L1 batches: committed {c}, verified {v}, executed {e} (at the start {run.start_pointers})")
    if run.cfg.settlesub_metrics:
        url = run.cfg.settlesub_metrics

        def settlesub() -> int:
            lines = [x for x in httpx.get(url, timeout=30).text.splitlines() if x.startswith("settlesub_last_verified_batch")]
            return int(float(lines[0].split()[-1])) if lines else -1

        run.until("settlesub's last verified batch equal to L1's", lambda: settlesub() == chain.batch_pointers(run.proxy)[1])
    else:
        run.incomplete.append("settlesub's verified batch: no E2E_SETTLESUB_METRICS")
    if run.refusals_before is None:
        run.incomplete.append("settled's refusals: the journal has no l2_settle_refusals")
        return
    now = refusals(run.cfg)
    assert now is not None
    if now[0] != run.refusals_before:
        for row in now[1]:
            log(f"  refusal {row}")
    check(now[0] == run.refusals_before, f"no settled refusal in the run: {run.refusals_before} -> {now[0]}")


# --------------------------------------------------------------------------------------------- #
# The one-shot restore
# --------------------------------------------------------------------------------------------- #


def step_restore(run: Run) -> None:
    """E2E_STEPS=restore, with no setup and no users: every role market OPEN and walked home to E2E_HOME_INDEX, and
    the backstop's knob at 0 (Run.restore), as a run's steps leave the stack; for one a run left otherwise. The
    backstop's positions stay (the kit's backstop_flatten closes them) and are logged."""
    cfg = run.cfg
    if not (cfg.price_control and cfg.price_status):
        raise Incomplete("the restore needs the kit's price control (E2E_PRICE_CONTROL, E2E_PRICE_STATUS)")
    if not cfg.home_index:
        raise Incomplete("the restore walks the role markets home to E2E_HOME_INDEX, which is unset")
    run.anon = LoafClient(base_url=cfg.api, ws_url=cfg.ws, max_retries=2)
    pump = run.pump = Prices(cfg.price_control, cfg.price_status, cfg.wait_s)
    home, roles = cfg.home_index, sorted(set(cfg.markets.values()))
    failed = run.restore(Touched(homes={m: home for m in roles}, states=set(roles), knob=True))
    check(not failed, "every part of the restore: " + ("; ".join(failed) or "done"))
    for m in roles:
        live = run.live(m)
        check(live.get("tradingState") == "OPEN" and abs(int(live["indexPrice"]) - home) * 10_000 <= home,
              f"market {m} is OPEN at its home index {home}: {live.get('tradingState')} {live.get('indexPrice')}")
    backstop = pump.status().get("backstop") or {}
    log(f"  the backstop: free {backstop.get('free_ticks')} ticks, positions {backstop.get('positions')}")


# --------------------------------------------------------------------------------------------- #
# The run
# --------------------------------------------------------------------------------------------- #

STEP_FNS: dict[str, Callable[[Run], None]] = {
    "setup": step_setup, "spot": step_spot, "perps": step_perps, "funding": step_funding,
    "liquidation": step_liquidation, "halt": step_halt, "exit": step_exit, "proof": step_proof,
    RESTORE: step_restore,
}


def run_step(run: Run, name: str, fn: Callable[[Run], None]) -> int:
    log(f"==== step {name}")
    run.incomplete, run.touched = [], Touched()
    rc = 0
    try:
        fn(run)
    except Incomplete as e:
        run.incomplete.append(str(e))
    except Failed as e:
        rc = 1
        log(f"FAIL {name}: {e}")
    except Exception as e:  # noqa: BLE001 a step's error is its verdict
        rc = 2
        log(f"ERROR {name}: {type(e).__name__}: {e}")
        for line in traceback.format_exc().splitlines()[-12:]:
            log(f"  | {line}")
    finally:
        touched, run.touched = run.touched, Touched()
        unrestored = run.restore(touched)
    for what in unrestored:
        log(f"ERROR {name}: not restored: {what}")
    if unrestored and rc in (0, 3):
        rc = 2
    if rc == 0 and run.incomplete:
        rc = 3
        for reason in run.incomplete:
            log(f"INCOMPLETE {name}: {reason}")
    log(f"==== exit {name} {rc}")
    return rc


def _stopped(signum: int, _frame: Any) -> None:
    """SIGTERM ends the run as SystemExit, so the running step's restore (run_step's finally) runs on the way out."""
    raise SystemExit(128 + signum)


def main(fns: Mapping[str, Callable[[Run], None]] = STEP_FNS) -> int:
    run = Run(Config.read())
    rcs: dict[str, int] = {}
    stopped: int | None = None
    names = run.cfg.steps if run.cfg.steps == (RESTORE,) else [n for n in STEPS if n == "setup" or n in run.cfg.steps]
    previous = signal.signal(signal.SIGTERM, _stopped)
    try:
        for name in names:
            rcs[name] = run_step(run, name, fns[name])
            if name == "setup" and rcs[name] != 0:
                log("setup did not pass: every later step is skipped")
                break
    except SystemExit as e:
        stopped = e.code if isinstance(e.code, int) else 1
        log(f"STOPPED (exit {stopped})")
    finally:
        signal.signal(signal.SIGTERM, previous)
        for ws in run.sockets:
            ws.stop()
    for note in run.notes:
        log(f"NOTE {note}")
    verdict = "PASS" if stopped is None and all(rc == 0 for rc in rcs.values()) else "FAIL"
    log("==== VERDICT " + verdict + " " + " ".join(f"{k}={v}" for k, v in rcs.items()))
    log("==== DONE")
    if stopped is not None:
        return stopped
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())

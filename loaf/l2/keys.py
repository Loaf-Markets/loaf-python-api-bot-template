"""The account's L2 trading key (api key 0, a K1 ECgFp5 Schnorr key): the key the Loaf web app
derives for the wallet, and its registration (loafx-frontend ``apps/trade/src/l2/key.ts``, ported).

The seed is keccak256 of the wallet's EIP-712 signature over L2Key(wallet, zkLoaf, l2ChainId,
accountIndex, apiKeyIndex, keyNonce, warning), domain ``Loaf L2 Key`` version 2 on the custody
chain, with no verifyingContract; the key nonce is the ZkLoaf change-pubkey nonce the key is queued
with. The derivation is deterministic, so this module, the web app and any device derive the same
key for a wallet, vault and nonce, and the signature is never kept. Registration is the wallet's
EIP-712 ChangePubKey over the proxy's nonce, posted to ``POST /l2/keys`` (the backend sends it and
pays the gas); the key is in force once the engine journal holds it.

Needs the ``l2keys`` extra (``pip install "loaf-bot[l2keys]"``, for eth-account) and an RPC of the
custody chain. As a command::

    python -m loaf.l2.keys derive    # the key in force; exit 1 unless the journal holds it
    python -m loaf.l2.keys enable    # registers the key in force and waits for it
    python -m loaf.l2.keys rotate    # a new key at the proxy's nonce, registered

Each prints ``LOAF_L2_SECRET_KEY=<80 hex>`` on stdout and what it did on stderr. Env: the wallet's
private key in ``LOAF_WALLET_PRIVATE_KEY`` (``--wallet-key-env`` names another variable; never a
command-line argument), ``LOAF_L1_RPC_URL``, and the client's ``LOAF_API_KEY`` (the wallet's user)
and ``LOAF_API_BASE_URL``. The custody chain and the ZkLoaf proxy come from the API
(``GET /trade/{ticker}``, ``property.l2Market``); ``--ticker`` picks the vault of that property
(a competition round's market is on the round's own vault, with its own account and key).
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Mapping, Optional, Sequence

from ..constants import ENV_L2_SECRET_KEY
from ..evm import Account, ChainReader, keccak, sign_typed, to_checksum_address, typed_data
from ..exceptions import LoafError, LoafL2Error
from .signer import L2Signer, default_signer
from .trading import MAX_SAFE_INTEGER, TICKER, L2KeyState, parse_key_state, parse_l2_market

# key.ts L2_KEY_DOMAIN, L2_KEY_WARNING, L2_KEY_TYPES and CHANGE_PUB_KEY_TYPES, byte for byte.
L2_KEY_DOMAIN = {"name": "Loaf L2 Key", "version": "2"}
L2_KEY_WARNING = (
    "This signature is your Loaf L2 trading key. Anyone who holds it can trade for this account. "
    "Sign it only in the Loaf app."
)
L2_KEY_TYPES = [
    {"name": "wallet", "type": "address"},
    {"name": "zkLoaf", "type": "address"},
    {"name": "l2ChainId", "type": "uint64"},
    {"name": "accountIndex", "type": "uint48"},
    {"name": "apiKeyIndex", "type": "uint8"},
    {"name": "keyNonce", "type": "uint64"},
    {"name": "warning", "type": "string"},
]
# ZkLoafGovernance.sol CHANGE_PUB_KEY_TYPEHASH (Loaf-Backend l2KeyService.ts).
CHANGE_PUB_KEY_TYPES = [
    {"name": "owner", "type": "address"},
    {"name": "accountIndex", "type": "uint48"},
    {"name": "apiKeyIndex", "type": "uint8"},
    {"name": "pubKey", "type": "bytes"},
    {"name": "nonce", "type": "uint256"},
    {"name": "deadline", "type": "uint256"},
]
CHANGE_PUB_KEY_TTL_S = 600
#: The settle layout admits api key 0 only.
API_KEY_INDEX = 0
#: ZkLoaf.sol NIL_ACCOUNT_INDEX: an address that never deposited.
NIL_ACCOUNT_INDEX = 2**48 - 1
JOURNAL_POLL_S = 5.0
JOURNAL_WAIT_S = 600.0
RELAY_POLL_S = 3.0
RELAY_WAIT_S = 180.0
ENV_WALLET_KEY = "LOAF_WALLET_PRIVATE_KEY"
ENV_L1_RPC_URL = "LOAF_L1_RPC_URL"

KEY_ERRORS = {
    "badNonce": "l2 key: the key nonce must be an integer from 0 to 2^53 - 1",
    "staleNonce": "l2 key: the proxy's key nonce moved past this key: rotate (python -m loaf.l2.keys rotate)",
    "noAccount": "l2 key: no L2 account for this wallet yet: deposit to open one, then enable trading",
    "otherAccount": "l2 key: this wallet's account on chain is not the one Loaf has on record",
    "differs": "l2 key: the account's key in force is another key (rotated elsewhere?): rotate (python -m loaf.l2.keys rotate)",
    "pending": "l2 key: the key change is not applied on L2 yet: run derive again in a moment",
    "relayFailed": "l2 key: the key change did not land on chain; nothing changed, run the command again",
}


def in_force_key_nonce(n: int) -> int:
    """The key nonce of the key in force: every L1 key change of the owner consumes the proxy's
    nonce, so the last change used n - 1; before any change (n = 0) the first key is derived at
    0. A rotation derives at n."""
    return 0 if n == 0 else n - 1


@dataclass(frozen=True)
class Custody:
    """The ZkLoaf proxy, the chain it is on and its own L2 chain id (a property's ``l2Market``)."""

    l1_chain_id: int
    proxy: str
    l2_chain_id: int


def l2_key_typed_data(wallet: str, custody: Custody, account_index: int, key_nonce: int) -> Dict[str, Any]:
    """The typed data the wallet signs to derive the key (key.ts l2KeyTypedData)."""
    if not isinstance(key_nonce, int) or isinstance(key_nonce, bool) or not 0 <= key_nonce <= MAX_SAFE_INTEGER:
        raise LoafL2Error(KEY_ERRORS["badNonce"])
    return typed_data(
        {**L2_KEY_DOMAIN, "chainId": custody.l1_chain_id},
        "L2Key",
        L2_KEY_TYPES,
        {
            "wallet": to_checksum_address(wallet),
            "zkLoaf": to_checksum_address(custody.proxy),
            "l2ChainId": custody.l2_chain_id,
            "accountIndex": account_index,
            "apiKeyIndex": API_KEY_INDEX,
            "keyNonce": key_nonce,
            "warning": L2_KEY_WARNING,
        },
    )


def change_pub_key_typed_data(owner: str, custody: Custody, account_index: int, pub_key: str, nonce: int, deadline: int) -> Dict[str, Any]:
    """The ChangePubKey the proxy verifies (key.ts registerSessionKey)."""
    return typed_data(
        {"name": "ZkLoaf", "version": "1", "chainId": custody.l1_chain_id, "verifyingContract": to_checksum_address(custody.proxy)},
        "ChangePubKey",
        CHANGE_PUB_KEY_TYPES,
        {
            "owner": to_checksum_address(owner),
            "accountIndex": account_index,
            "apiKeyIndex": API_KEY_INDEX,
            "pubKey": bytes.fromhex(pub_key[2:]),
            "nonce": nonce,
            "deadline": deadline,
        },
    )


def relay_verdict(calls: Sequence[Mapping[str, Any]], public_key: str) -> str:
    """relay.ts relayVerdict over this key's change in the batch: FAILED when any failed, LANDED
    when all landed, else PENDING (also while the batch lists none)."""
    mine = [c for c in calls if c.get("kind") == "key" and c.get("publicKey") == public_key]
    if not mine:
        return "PENDING"
    if any(c.get("status") == "FAILED" for c in mine):
        return "FAILED"
    return "LANDED" if all(c.get("status") == "LANDED" for c in mine) else "PENDING"


class CustodyReader:
    """The custody chain's reads: ZkLoaf.sol's views ``changePubKeyNonce(address)`` and
    ``getAccountIndexFromAddress(address)``, over :class:`~loaf.evm.ChainReader`."""

    def __init__(self, chain: ChainReader) -> None:
        self._chain = chain

    @classmethod
    def from_url(cls, rpc_url: str) -> "CustodyReader":
        return cls(ChainReader(rpc_url, env_name=ENV_L1_RPC_URL))

    def chain_id(self) -> int:
        return self._chain.chain_id()

    def key_nonce(self, proxy: str, owner: str) -> int:
        return int(self._chain.read(proxy, "changePubKeyNonce(address)", [owner], ["address"], ["uint256"])[0])

    def account_index(self, proxy: str, owner: str) -> int:
        return int(self._chain.read(proxy, "getAccountIndexFromAddress(address)", [owner], ["address"], ["uint48"])[0])


@dataclass(frozen=True)
class TradingKey:
    wallet: str
    custody: Custody
    account_index: int
    key_nonce: int
    secret_key: str
    public_key: str


@dataclass(frozen=True)
class RegisterResult:
    """``backend``: ``POST /l2/keys``' answer, or None when nothing was sent; ``sent``: False when
    the journal holds the key or its change is on chain already."""

    backend: Optional[Mapping[str, Any]]
    sent: bool


class L2Keys:
    """Derives and registers the wallet's key through a :class:`~loaf.client.LoafClient` (whose API
    key is the wallet's user) and a :class:`CustodyReader` of the custody chain."""

    def __init__(
        self,
        client: Any,
        reader: Any,
        private_key: str,
        signer: Optional[L2Signer] = None,
        now_s: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        log: Callable[[str], None] = lambda line: print(line, file=sys.stderr),
    ) -> None:
        self._client = client
        self._reader = reader
        self._private_key = private_key
        self.wallet: str = Account.from_key(private_key).address
        self._signer = signer if signer is not None else default_signer()
        self._now_s = now_s
        self._sleep = sleep
        self._monotonic = monotonic
        self._log = log

    # -- reads -----------------------------------------------------------------------------
    def _custody_of(self, ticker: str) -> Optional[Custody]:
        detail = self._client.get(f"/trade/{ticker}", auth=False)
        prop = detail.get("property") if isinstance(detail, Mapping) else None
        if not isinstance(prop, Mapping):
            raise LoafL2Error(f"GET /trade/{ticker} did not answer a property")
        market = parse_l2_market(ticker, prop.get("l2Market"))
        return None if market is None else Custody(market.chain_id, to_checksum_address(market.proxy_address), market.l2_chain_id)

    def custody(self, ticker: Optional[str] = None) -> Custody:
        """The vault of ``ticker``'s market; with no ticker, the one vault every listed L2 market
        names (refused when none is listed or two differ). It must be on the chain the RPC serves."""
        if ticker is not None:
            if not TICKER.fullmatch(ticker):
                raise LoafL2Error(f"ticker must be 1-4 of A-Z 0-9; received {ticker!r}")
            found = {self._custody_of(ticker)} - {None}
            if not found:
                raise LoafL2Error(f"no L2 market is listed for {ticker}")
        else:
            listed = self._client.get("/trade", auth=False)
            items = listed.get("properties") if isinstance(listed, Mapping) else None
            tickers = [p.get("ticker") for p in items or [] if isinstance(p, Mapping) and p.get("hasL2Market") is True]
            found = {self._custody_of(t) for t in tickers if isinstance(t, str) and TICKER.fullmatch(t)} - {None}
            if not found:
                raise LoafL2Error("GET /trade lists no L2 market: this API serves no L2 custody")
            if len(found) > 1:
                raise LoafL2Error(f"GET /trade lists more than one L2 vault: pass a ticker; {sorted(found, key=repr)!r}")
        custody = found.pop()
        served = self._reader.chain_id()
        if served != custody.l1_chain_id:
            raise LoafL2Error(f"{ENV_L1_RPC_URL} serves chain {served}; the custody is on chain {custody.l1_chain_id}")
        return custody

    def key_state(self, public_key: Optional[str] = None) -> Optional[L2KeyState]:
        """``GET /l2/keys`` (with ``public_key``: whether that key is still pending); None when the
        user has no L2 account yet (404)."""
        params: Dict[str, Any] = {"apiKeyIndex": API_KEY_INDEX}
        if public_key is not None:
            params["publicKey"] = public_key
        try:
            raw = self._client.get("/l2/keys", params=params)
        except LoafError as e:
            if getattr(e, "status_code", None) == 404:
                return None
            raise
        return parse_key_state(raw)

    def account_index(self, custody: Custody, state: Optional[L2KeyState]) -> int:
        """The index the proxy assigned at the first deposit, the one Loaf has on record
        (key.ts registrationAccountIndex)."""
        index = self._reader.account_index(custody.proxy, self.wallet)
        if index == NIL_ACCOUNT_INDEX:
            raise LoafL2Error(KEY_ERRORS["noAccount"])
        if not 0 <= index < NIL_ACCOUNT_INDEX:
            raise LoafL2Error(f"l2 key: the proxy answered account index {index}")
        if state is not None and state.account_index != index:
            raise LoafL2Error(KEY_ERRORS["otherAccount"])
        return index

    # -- the key ---------------------------------------------------------------------------
    def derive_at(self, custody: Custody, account_index: int, key_nonce: int) -> TradingKey:
        """The key for this wallet, vault, account and nonce (key.ts deriveSessionKey)."""
        data = l2_key_typed_data(self.wallet, custody, account_index, key_nonce)
        seed = keccak(hexstr=sign_typed(self._private_key, data))
        secret = self._signer.key_from_seed(seed)
        return TradingKey(
            wallet=self.wallet,
            custody=custody,
            account_index=account_index,
            key_nonce=key_nonce,
            secret_key=secret,
            public_key=self._signer.public_key(secret),
        )

    def register(self, key: TradingKey, backend_key: Optional[str]) -> RegisterResult:
        """key.ts registerSessionKey: by the proxy's nonce n, nothing is sent when the journal holds
        the key or n = keyNonce + 1 (its change is on chain); n = keyNonce signs ChangePubKey and
        posts it; any other n is refused before anything is signed. A 202 SENT is followed by the
        relay's verdict on the key change."""
        if key.wallet != self.wallet:
            raise LoafL2Error("l2 key: the key belongs to another wallet")
        if backend_key is not None and backend_key.lower() == key.public_key:
            return RegisterResult(None, False)
        custody = key.custody
        nonce = self._reader.key_nonce(custody.proxy, self.wallet)
        if nonce == key.key_nonce + 1:
            return RegisterResult(None, False)
        if nonce != key.key_nonce:
            raise LoafL2Error(KEY_ERRORS["staleNonce"])
        deadline = int(self._now_s()) + CHANGE_PUB_KEY_TTL_S
        typed = change_pub_key_typed_data(self.wallet, custody, key.account_index, key.public_key, nonce, deadline)
        body = {
            "owner": self.wallet,
            "accountIndex": key.account_index,
            "apiKeyIndex": API_KEY_INDEX,
            "pubKey": key.public_key,
            "deadline": deadline,
            "signature": sign_typed(self._private_key, typed),
        }
        backend = self._client.post("/l2/keys", json=body)
        self._log(f"l2 key: POST /l2/keys {backend.get('status')} tx {backend.get('txHash')}")
        if backend.get("status") == "SENT":
            if self.await_relayed(str(backend.get("txHash")), key.public_key) == "FAILED":
                raise LoafL2Error(KEY_ERRORS["relayFailed"])
        return RegisterResult(backend, True)

    def await_relayed(self, tx_hash: str, public_key: str) -> str:
        """relay.ts awaitRelayed: ``GET /relay/{tx}`` until this key's change is LANDED or FAILED,
        or RELAY_WAIT_S passed (PENDING). A 404, a 5xx or no answer is PENDING; another 4xx is the
        caller's own and is raised."""
        deadline = self._monotonic() + RELAY_WAIT_S
        while True:
            try:
                status: Optional[Mapping[str, Any]] = self._client.get(f"/relay/{tx_hash}")
            except LoafError as e:
                code = getattr(e, "status_code", None)
                if isinstance(code, int) and 400 <= code < 500 and code != 404:
                    raise
                status = None
            verdict = "PENDING" if status is None else relay_verdict(status.get("calls") or [], public_key)
            if verdict != "PENDING" or self._monotonic() >= deadline:
                return verdict
            self._sleep(RELAY_POLL_S)

    def wait_journaled(self, public_key: str) -> L2KeyState:
        """key.ts waitForJournaledKey: ``GET /l2/keys`` every JOURNAL_POLL_S until the journal holds
        the key, refused by name at JOURNAL_WAIT_S."""
        until = self._monotonic() + JOURNAL_WAIT_S
        while True:
            state = self.key_state(public_key)
            if state is None:
                raise LoafL2Error(KEY_ERRORS["noAccount"])
            if not state.pending and state.journaled_key == public_key:
                return state
            if self._monotonic() + JOURNAL_POLL_S > until:
                raise LoafL2Error(KEY_ERRORS["pending"])
            self._sleep(JOURNAL_POLL_S)

    # -- the commands ----------------------------------------------------------------------
    def derive(self, ticker: Optional[str] = None) -> tuple:
        """The key in force (key.ts unlockSessionKey) and whether the journal holds it."""
        custody = self.custody(ticker)
        state = self.key_state()
        index = self.account_index(custody, state)
        key = self.derive_at(custody, index, in_force_key_nonce(self._reader.key_nonce(custody.proxy, self.wallet)))
        journaled = None if state is None else state.journaled_key
        return key, journaled is not None and journaled == key.public_key

    def enable(self, ticker: Optional[str] = None) -> TradingKey:
        """Enable trading: registers the key in force and waits for the journal to hold it. A key
        in force that differs from this wallet's after a change is refused: rotate."""
        custody = self.custody(ticker)
        state = self.key_state()
        index = self.account_index(custody, state)
        n = self._reader.key_nonce(custody.proxy, self.wallet)
        key = self.derive_at(custody, index, in_force_key_nonce(n))
        journaled = None if state is None else state.journaled_key
        if journaled is not None and journaled != key.public_key and n > 0:
            raise LoafL2Error(KEY_ERRORS["differs"])
        self.register(key, journaled)
        self.wait_journaled(key.public_key)
        return key

    def rotate(self, ticker: Optional[str] = None) -> TradingKey:
        """Reset trading key: a new key at the proxy's nonce, registered; the old key's orders
        then fail the epoch and the signature checks."""
        custody = self.custody(ticker)
        state = self.key_state()
        index = self.account_index(custody, state)
        key = self.derive_at(custody, index, self._reader.key_nonce(custody.proxy, self.wallet))
        self.register(key, None if state is None else state.journaled_key)
        self.wait_journaled(key.public_key)
        return key


def main(
    argv: Optional[Sequence[str]] = None,
    env: Optional[Mapping[str, str]] = None,
    client: Any = None,
    reader: Any = None,
    **kwargs: Any,
) -> int:
    parser = argparse.ArgumentParser(prog="python -m loaf.l2.keys", description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("command", choices=("derive", "enable", "rotate"))
    parser.add_argument("--wallet-key-env", default=ENV_WALLET_KEY, help="the environment variable holding the wallet's private key")
    parser.add_argument("--ticker", default=None, help="a property on the vault to key (default: the one vault every L2 market names)")
    args = parser.parse_args(argv)
    env = os.environ if env is None else env
    try:
        private_key = (env.get(args.wallet_key_env) or "").strip()
        if not private_key:
            raise LoafL2Error(f"{args.wallet_key_env} is required: the wallet's private key")
        if reader is None:
            rpc_url = (env.get(ENV_L1_RPC_URL) or "").strip()
            if not rpc_url:
                raise LoafL2Error(f"{ENV_L1_RPC_URL} is required: an RPC of the custody chain")
            reader = CustodyReader.from_url(rpc_url)
        if client is None:
            from ..client import LoafClient

            client = LoafClient()
        keys = L2Keys(client, reader, private_key, **kwargs)
        if args.command == "derive":
            key, in_force = keys.derive(args.ticker)
        else:
            key, in_force = (keys.enable(args.ticker) if args.command == "enable" else keys.rotate(args.ticker)), True
    except LoafError as e:
        print(str(e), file=sys.stderr)
        return 1
    print(
        f"l2 key: wallet {key.wallet}, vault {key.custody.proxy} on chain {key.custody.l1_chain_id}, "
        f"account {key.account_index}, key nonce {key.key_nonce}, public key {key.public_key}: "
        f"{'in force' if in_force else 'NOT in force (enable or rotate)'}",
        file=sys.stderr,
    )
    print(f"{ENV_L2_SECRET_KEY}={key.secret_key}")
    return 0 if in_force else 1


if __name__ == "__main__":
    sys.exit(main())

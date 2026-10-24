"""Print the trading key in force on your Loaf account, for LOAF_SECRET_KEY:
derived on this machine from your account wallet's private key, as the Loaf web
app derives it.

The web app's Show trading key gives the same key without it. Run it on a
trusted machine, once trading is enabled on the market, with the ``wallet``
extra installed (``pip install -e ".[wallet]"``). Give it the wallet key for
this one command only (README §2)::

    read -rs W && LOAF_WALLET_PRIVATE_KEY="$W" python -m loaf.secret_key; unset W

On success stdout carries one line, ``LOAF_SECRET_KEY=<80 hex>``, for your
bot's ``.env``, and stderr says which key it is. Otherwise stderr says why and
stdout stays empty. Exit status: 0 printed, 1 refused, 2 a usage error.

* ``$LOAF_WALLET_PRIVATE_KEY`` (64 hex, optionally ``0x``-prefixed) is read once
  and removed from this process's environment at once. The wallet signs the web
  app's ``L2Key`` typed data here, once per candidate key, and each key is
  derived from its signature: only the trading key in force is ever printed.
* ``$LOAF_API_KEY`` reads your trading account (``GET /l2/keys``) on
  ``$LOAF_API_BASE_URL``. With python-dotenv installed it also loads the
  ``.env`` found from the working directory up (run it from your bot's
  directory to use your bot's), and refuses one that holds the wallet key.
  ``$LOAF_SECRET_KEY`` is ignored.
* A trading key serves one vault: ``--ticker``'s market's, else the first live
  L2 market's. A Reset trading key in the web app replaces it: run this again
  once the new key is active, and restart your bot with the new key.
* The web app derives one key per key change your wallet made on the vault. The
  one in force is found by comparing each candidate with your account's key in
  ``GET /l2/keys``, so no chain RPC is needed.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Any, Mapping, NoReturn, Sequence

from . import k1
from ._l2 import _TICKER_RE, Market, market_of, read_account
from .client import LoafClient
from .constants import ENV_API_KEY, ENV_BASE_URL, ENV_SECRET_KEY, ENV_WALLET_PRIVATE_KEY
from .exceptions import LoafConfigError, LoafError

#: The warning the web app's ``L2Key`` typed data carries, byte for byte: the wallet signs it,
#: so any other text derives another key.
L2_KEY_WARNING = (
    "This signature is your Loaf L2 trading key. Anyone who holds it can trade for this account. "
    "Sign it only in the Loaf app."
)

#: The key nonces tried, from 0: the web app derives one key per key change the wallet made on
#: the vault, so an account with this many changes or more is refused.
KEY_NONCE_LIMIT = 1_024

_WALLET_KEY_RE = re.compile(r"(0[xX])?[0-9a-fA-F]{64}")
# secp256k1's group order: a wallet's private key is a number from 1 to this, exclusive.
_SECP256K1_ORDER = 0xFFFFFFFFFFFFFFFF_FFFFFFFFFFFFFFFE_BAAEDCE6AF48A03B_BFD25E8CD0364141


def l2_key_typed_data(
    wallet: str,
    *,
    chain_id: int,
    proxy_address: str,
    l2_chain_id: int,
    account_index: int,
    key_nonce: int,
) -> dict[str, Any]:
    """The EIP-712 typed data whose signature by ``wallet`` seeds the trading key, byte for
    byte the web app's: domain ``Loaf L2 Key`` version 2 on ``chain_id`` (the vault's chain,
    ``l2Market.chainId``) with no verifying contract, and an ``L2Key`` message binding the
    vault (``proxy_address``, ``l2_chain_id``), the account, its api key 0, ``key_nonce`` (one
    per key change the wallet made on the vault) and :data:`L2_KEY_WARNING`.

    The seed is keccak256 of the 65-byte signature; addresses in any case sign the same.
    """
    return {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
            ],
            "L2Key": [
                {"name": "wallet", "type": "address"},
                {"name": "zkLoaf", "type": "address"},
                {"name": "l2ChainId", "type": "uint64"},
                {"name": "accountIndex", "type": "uint48"},
                {"name": "apiKeyIndex", "type": "uint8"},
                {"name": "keyNonce", "type": "uint64"},
                {"name": "warning", "type": "string"},
            ],
        },
        "primaryType": "L2Key",
        "domain": {"name": "Loaf L2 Key", "version": "2", "chainId": chain_id},
        "message": {
            "wallet": wallet,
            "zkLoaf": proxy_address,
            "l2ChainId": l2_chain_id,
            "accountIndex": account_index,
            "apiKeyIndex": 0,
            "keyNonce": key_nonce,
            "warning": L2_KEY_WARNING,
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    """``python -m loaf.secret_key`` with ``argv`` (default: the command line).

    Returns 0 once the trading key is printed, or 1 after saying on stderr why
    not. A usage error exits with 2 (argparse).
    """
    parser = _Parser(prog="python -m loaf.secret_key", description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument(
        "--ticker",
        type=_ticker,
        help="a property on the vault to derive the key for, e.g. MUS (default: the first live "
        "L2 market's)",
    )
    ticker = parser.parse_args(argv).ticker
    # Whatever stops it, stderr gets one line and never a traceback, which would carry the frames
    # that held the keys (to a crash reporter, say).
    try:
        _print_trading_key(ticker)
        return 0
    except LoafError as exc:
        message = str(exc)
    except Exception as exc:  # e.g. an unreadable .env
        message = f"Deriving your trading key failed ({type(exc).__name__}: {exc})."
    except KeyboardInterrupt:
        message = "Interrupted."
    print(message, file=sys.stderr)
    return 1


def _print_trading_key(ticker: str | None) -> None:
    """Print the key in force on ``ticker``'s vault (or the first live L2 market's).

    Raises:
        LoafError: why it cannot be derived or read (stderr gets its text).
    """
    try:
        # Read once and removed from this process's environment at once: it lives here no longer
        # than it must, and LoafClient() refuses to start while it is set.
        raw = (os.environ.pop(ENV_WALLET_PRIVATE_KEY, None) or "").strip()
        _load_dotenv(raw[-64:].lower() if _WALLET_KEY_RE.fullmatch(raw) else None)
        if not raw:
            raise LoafConfigError(
                f"Set {ENV_WALLET_PRIVATE_KEY} to your account wallet's private key for this one "
                "command (README §2): the helper signs with it on this machine to derive your "
                "trading key."
            )
        # Checked here, before eth-account sees it: not every version refuses a zero key.
        if not _WALLET_KEY_RE.fullmatch(raw) or not 0 < int(raw, 16) < _SECP256K1_ORDER:
            raise LoafConfigError(
                f"{ENV_WALLET_PRIVATE_KEY} is not a private key: expected 64 hex characters, "
                "optionally 0x-prefixed."
            )
        try:
            from eth_account import Account
            from eth_account.messages import encode_typed_data
            from eth_utils.crypto import keccak
        except ImportError:
            raise LoafConfigError(
                'python -m loaf.secret_key needs eth-account: pip install -e ".[wallet]"'
            ) from None
        account = Account.from_key(raw)
        wallet = account.address

        # The key this run replaces must not stop it: LoafClient() refuses a malformed one.
        os.environ.pop(ENV_SECRET_KEY, None)
        with LoafClient() as client:
            if client.api_key is None:
                raise LoafConfigError(
                    f"Set {ENV_API_KEY} (your Loaf API key) so the helper can read your trading "
                    "account."
                )
            # A wallet key exported without 0x has the API key's shape, which LoafClient's own
            # check cannot tell apart; this is the one place that holds both. Searched for, not
            # compared whole: quotes or a pasted line's own name can come with it, and an API key
            # (64 hex) holds it only by being it.
            if raw[-64:].lower() in client.api_key.lower():
                raise LoafConfigError(
                    f"{ENV_API_KEY} holds your wallet's private key. The API key is a different "
                    "secret (API -> Generate API Token in the web app); remove the wallet key "
                    f"from {ENV_API_KEY} wherever you set it — the SDK sends {ENV_API_KEY} to the "
                    "server with every request."
                )
            ticker, market = _vault(client, ticker)
            account_index, _, journaled = read_account(client, ticker)

        for nonce in range(KEY_NONCE_LIMIT):
            typed = l2_key_typed_data(
                wallet,
                chain_id=market.chain_id,
                proxy_address=market.proxy_address,
                l2_chain_id=market.l2_chain_id,
                account_index=account_index,
                key_nonce=nonce,
            )
            # Neither the signature nor its keccak256, the seed, is kept.
            secret = k1.key_from_seed(
                keccak(account.sign_message(encode_typed_data(full_message=typed)).signature)
            )
            if "0x" + k1.public_key(secret).hex() == journaled:
                break
        else:
            raise LoafConfigError(
                "None of the keys this wallet derives for key nonces "
                f"0-{KEY_NONCE_LIMIT - 1} is the trading key in force: is {ENV_WALLET_PRIVATE_KEY} "
                "this account's wallet? A key enabled outside the Loaf web app cannot be derived "
                "here."
            )
        print(f"{ENV_SECRET_KEY}={secret.hex()}")
        print(
            f"Trading key in force for wallet {wallet}, account {account_index} on vault "
            f"{market.proxy_address} (L2 chain {market.l2_chain_id}), key nonce {nonce}: public "
            f"key {journaled}. Put the line above in your .env as {ENV_SECRET_KEY} and restart "
            "your bot. It signs orders for this account on this vault only; keep it as secret as "
            "the wallet key.",
            file=sys.stderr,
        )
    finally:
        # Crash reporters and debuggers record frame locals: none is left holding a key (the
        # client's API key can be the wallet key, refused above).
        raw = account = secret = client = None


def _load_dotenv(wallet_key: str | None) -> None:
    """Load the ``.env`` python-dotenv finds from the working directory up, if it is
    installed: run from your bot's directory, it finds the one your bot loads, whose
    ``LOAF_API_KEY`` and ``LOAF_API_BASE_URL`` are the ones to read the account with.
    Refuses one that sets ``LOAF_WALLET_PRIVATE_KEY``, or holds ``wallet_key`` (the
    wallet key's 64 hex, lowercase) anywhere in it, a comment included."""
    try:
        from dotenv import dotenv_values, find_dotenv, load_dotenv
    except ImportError:
        return
    try:
        path = find_dotenv(usecwd=True)
        if not path:
            return
        # Searched for in the file's text, not its values: under any name, the bot would load it
        # (LoafClient()'s own check knows only LOAF_WALLET_PRIVATE_KEY's name), and in a comment
        # it still sits next to your bot.
        held = wallet_key is not None and wallet_key in Path(path).read_text("utf-8").lower()
    finally:
        wallet_key = None  # crash reporters record frame locals
    # LOAF_WALLET_PRIVATE_KEY is refused by its name, whatever it holds.
    if ENV_WALLET_PRIVATE_KEY in dotenv_values(path):
        raise LoafConfigError(
            f"{ENV_WALLET_PRIVATE_KEY} is set in {path}: delete it from that file. Give the wallet "
            "key to this command only (README §2), so it never sits next to your bot."
        )
    if held:
        raise LoafConfigError(
            f"{path} holds your wallet key: delete it from that file (a commented-out line still "
            "holds it). Give the wallet key to this command only (README §2), so it never sits "
            "next to your bot."
        )
    load_dotenv(path)  # never overrides the environment


def _vault(client: LoafClient, ticker: str | None) -> tuple[str, Market]:
    """The market whose vault the key is for, and the ticker that names it: ``ticker``'s,
    else the first live L2 market's."""
    if ticker is not None:
        return ticker, market_of(client.get(f"/trade/{ticker}", auth=False), ticker)
    listing = client.get("/trade", auth=False)
    rows = listing.get("properties") if isinstance(listing, Mapping) else None
    # The L2 exchange lists hasL2Market on every property; an older server does not, and a host
    # that is no Loaf API lists nothing.
    if not isinstance(rows, list) or any(
        not isinstance(row, Mapping) or "hasL2Market" not in row for row in rows
    ):
        raise LoafConfigError(
            "GET /trade lists no hasL2Market: this server does not run the exchange this SDK "
            f"trades on (check {ENV_BASE_URL})."
        )
    tickers: list[str] = [
        row["ticker"] for row in rows if row.get("status") == "LIVE" and row["hasL2Market"] is True
    ]
    if not tickers:
        raise LoafConfigError(
            "No live market has an L2 market right now, so there is no vault to derive a key for "
            "(pass --ticker to name one)."
        )
    ticker = tickers[0]
    return ticker, market_of(client.get(f"/trade/{ticker}", auth=False), ticker)


class _Parser(argparse.ArgumentParser):
    """argparse, except that a usage error repeats no argument: one may be the wallet key,
    given where it does not belong."""

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(
            2,
            f"{self.prog}: error: the only argument is --ticker, a property's ticker (1-4 "
            "uppercase letters or digits, e.g. MUS). The wallet key is read from "
            f"{ENV_WALLET_PRIVATE_KEY}, never from an argument (README §2).\n",
        )


def _ticker(value: str) -> str:
    """``--ticker``: a bad value is a usage error."""
    if not _TICKER_RE.fullmatch(value):
        raise ValueError
    return value


if __name__ == "__main__":
    raise SystemExit(main())

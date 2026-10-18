"""What the wallet flows (:mod:`loaf.l2.keys`, :mod:`loaf.ipo`) need of an EVM chain: reads over
JSON-RPC (``eth_chainId``, ``eth_call``) and EIP-712 signatures by the wallet's private key.

Needs the ``l2keys`` extra (``pip install "loaf-bot[l2keys]"``: eth-account, which brings eth-abi
and eth-utils).
"""

from __future__ import annotations

import itertools
from typing import Any, Dict, Mapping, Optional, Sequence

import httpx

from .exceptions import LoafL2Error

try:
    from eth_abi import decode as abi_decode
    from eth_abi import encode as abi_encode
    from eth_account import Account
    from eth_account.messages import encode_typed_data
    from eth_utils import keccak, to_checksum_address
except ImportError as e:  # pragma: no cover - exercised only without the extra
    raise ImportError('the wallet flows need eth-account: pip install "loaf-bot[l2keys]"') from e

__all__ = [
    "Account",
    "ChainReader",
    "abi_decode",
    "abi_encode",
    "domain_separator",
    "keccak",
    "selector",
    "sign_typed",
    "to_checksum_address",
    "typed_data",
]


def selector(signature: str) -> bytes:
    """The 4-byte function selector of ``name(type,...)``."""
    return keccak(text=signature)[:4]


def _domain_types(domain: Mapping[str, Any]) -> list:
    order = [("name", "string"), ("version", "string"), ("chainId", "uint256"), ("verifyingContract", "address")]
    return [{"name": k, "type": t} for k, t in order if k in domain]


def typed_data(domain: Dict[str, Any], primary: str, fields: list, message: Dict[str, Any]) -> Dict[str, Any]:
    """An ``eth_signTypedData_v4`` document: the domain's own fields, in EIP-712's order."""
    return {
        "types": {"EIP712Domain": _domain_types(domain), primary: fields},
        "primaryType": primary,
        "domain": domain,
        "message": message,
    }


def sign_typed(private_key: str, typed: Mapping[str, Any]) -> str:
    """``0x`` and 130 hex: r, s and v (27 or 28), as a wallet answers eth_signTypedData_v4."""
    signed = Account.from_key(private_key).sign_message(encode_typed_data(full_message=dict(typed)))
    return "0x" + bytes(signed.signature).hex()


def domain_separator(domain: Mapping[str, Any]) -> bytes:
    """hashStruct(EIP712Domain) of ``domain`` (what a contract's ``DOMAIN_SEPARATOR()`` answers):
    keccak256 of the type hash and each present field, a string as its keccak256 (EIP-712)."""
    fields = _domain_types(domain)
    type_hash = keccak(text="EIP712Domain(" + ",".join(f"{f['type']} {f['name']}" for f in fields) + ")")
    types, values = ["bytes32"], [type_hash]
    for f in fields:
        v = domain[f["name"]]
        if f["type"] == "string":
            types.append("bytes32")
            values.append(keccak(text=v))
        else:
            types.append(f["type"])
            values.append(to_checksum_address(v) if f["type"] == "address" else v)
    return keccak(abi_encode(types, values))


class ChainReader:
    """A chain's reads over JSON-RPC. ``env_name`` names the setting in its errors."""

    def __init__(self, rpc_url: str, http: Optional[httpx.Client] = None, timeout: float = 30.0, env_name: str = "rpc_url") -> None:
        self._url = rpc_url
        self._http = http or httpx.Client(timeout=timeout)
        self._ids = itertools.count(1)
        self._name = env_name

    def _rpc(self, method: str, params: list) -> Any:
        try:
            r = self._http.post(self._url, json={"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params})
            r.raise_for_status()
            answer = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise LoafL2Error(f"{self._name}: {method} failed: {e}") from e
        if not isinstance(answer, Mapping) or "error" in answer or "result" not in answer:
            raise LoafL2Error(f"{self._name}: {method} answered {answer!r}")
        return answer["result"]

    def chain_id(self) -> int:
        return int(self._rpc("eth_chainId", []), 16)

    def call(self, to: str, data: bytes) -> bytes:
        """``eth_call`` at the latest block: the return data."""
        out = self._rpc("eth_call", [{"to": to_checksum_address(to), "data": "0x" + data.hex()}, "latest"])
        if not isinstance(out, str) or not out.startswith("0x"):
            raise LoafL2Error(f"{self._name}: eth_call to {to} answered {out!r}")
        return bytes.fromhex(out[2:])

    def read(self, to: str, signature: str, args: Sequence[Any], arg_types: Sequence[str], out_types: Sequence[str]) -> tuple:
        """Calls ``signature`` on ``to`` and decodes its return as ``out_types``."""
        out = self.call(to, selector(signature) + abi_encode(list(arg_types), list(args)))
        try:
            return tuple(abi_decode(list(out_types), out))
        except Exception as e:
            raise LoafL2Error(f"{self._name}: {signature} on {to} answered {len(out)} bytes that are not {list(out_types)}") from e

    def chain_must_be(self, chain_id: int, what: str) -> None:
        served = self.chain_id()
        if served != chain_id:
            raise LoafL2Error(f"{self._name} serves chain {served}; {what} is on chain {chain_id}")

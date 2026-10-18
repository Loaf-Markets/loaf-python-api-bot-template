"""K1's L2 signer: loaf-matching-engine's native signer library (``crates/l2-signer-ffi``, pinned in
``PROVENANCE.json``) loaded with ctypes.

The library is the web app's L2 signer behind a C ABI: K1's keys, order and withdraw messages and
Schnorr signatures, each checked with the engine's own parse and verify before it is returned, and
K1's batch signature (one signature over the root of 1 to 256 orders of one account, chain id and
epoch). The SDK vendors one build per platform, each the bytes ``PROVENANCE.json`` pins: Linux
x86-64 and aarch64 (glibc 2.28 and newer), macOS arm64 (11.0 and newer) and x86-64 (10.12 and
newer), and Windows x86-64. On any other platform, set ``$LOAF_L2_SIGNER_LIB`` to a build of the
engine's ``crates/l2-signer-ffi``.

Order terms are dicts with the wire keys of k1-sign's KAT and the web app's signer: ``l2ChainId``,
``l2MarketIndex``, ``l2BaseAssetId``, ``l2QuoteAssetId``, ``isAsk``, ``price``, ``l2FeeRateCap``,
``l2AccountIndex``, ``l2OrderIndex``, ``l2Epoch``, ``quantity``, ``l2ExpiryMs``, with ``price`` and
``quantity`` the scaled integers the order signs (price x 1000, quantity x 10; the order body carries
them unscaled). Any other key is refused.
"""

from __future__ import annotations

import ctypes
import functools
import hashlib
import json
import os
import platform
import secrets
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence, Union

from ..exceptions import L2SignerError

ABI_VERSION = 1
MAX_BATCH_ORDERS = 256
LIB_ENV = "LOAF_L2_SIGNER_LIB"
HERE = Path(__file__).resolve().parent
PROVENANCE = HERE / "PROVENANCE.json"
KAT = HERE / "kat" / "k1-sign.json"
_SO, _DYLIB, _DLL = "libloaf_l2_signer.so", "libloaf_l2_signer.dylib", "loaf_l2_signer.dll"
#: ``platform.system()`` and ``platform.machine()``, lowercased, to the vendored library: the
#: engine's ``crates/l2-signer-ffi/release.sh`` builds each.
PLATFORMS = {
    ("linux", "x86_64"): ("linux-x86_64", _SO),
    ("linux", "amd64"): ("linux-x86_64", _SO),
    ("linux", "aarch64"): ("linux-aarch64", _SO),
    ("linux", "arm64"): ("linux-aarch64", _SO),
    ("darwin", "arm64"): ("darwin-aarch64", _DYLIB),
    ("darwin", "aarch64"): ("darwin-aarch64", _DYLIB),
    ("darwin", "x86_64"): ("darwin-x86_64", _DYLIB),
    ("windows", "amd64"): ("windows-x86_64", _DLL),
    ("windows", "x86_64"): ("windows-x86_64", _DLL),
}


def _pinned_sha256(name: str) -> str:
    return json.loads(PROVENANCE.read_text())["files"][name]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def vendored_library(system: Optional[str] = None, machine: Optional[str] = None) -> Path:
    """The vendored library for a platform, this one unless named."""
    key = ((system or platform.system()).lower(), (machine or platform.machine()).lower())
    found = PLATFORMS.get(key)
    if found is None:
        raise L2SignerError(
            f"l2 signer: no vendored library for {key[0]} {key[1]}; "
            f"set ${LIB_ENV} to a build of crates/l2-signer-ffi"
        )
    directory, name = found
    return HERE / "lib" / directory / name


class L2Signer:
    """The native library. The vendored one for this platform is loaded only if its sha256 is the
    one ``PROVENANCE.json`` pins; ``path`` (or ``$LOAF_L2_SIGNER_LIB``) loads another build, for a
    platform without a vendored library."""

    def __init__(self, path: Union[str, "os.PathLike[str]", None] = None) -> None:
        chosen = path or os.environ.get(LIB_ENV)
        if chosen:
            lib_path = Path(chosen)
        else:
            lib_path = vendored_library()
            if _sha256(lib_path) != _pinned_sha256(lib_path.relative_to(HERE).as_posix()):
                raise L2SignerError(f"l2 signer: {lib_path} is not the library PROVENANCE.json pins")
        lib = ctypes.CDLL(str(lib_path))
        lib.loaf_l2_abi_version.restype = ctypes.c_uint32
        lib.loaf_l2_abi_version.argtypes = []
        lib.loaf_l2_free.restype = None
        lib.loaf_l2_free.argtypes = [ctypes.c_void_p]
        text, buf, size = ctypes.c_char_p, ctypes.c_char_p, ctypes.c_size_t
        for name, args in (
            ("loaf_l2_key_from_seed", [buf, size]),
            ("loaf_l2_public_key", [text]),
            ("loaf_l2_order_message", [text]),
            ("loaf_l2_sign_order", [text, text, buf, size]),
            ("loaf_l2_sign_batch", [text, text, buf, size]),
            ("loaf_l2_withdraw_message", [text]),
            ("loaf_l2_sign_withdraw", [text, text, buf, size]),
        ):
            fn = getattr(lib, name)
            fn.restype = ctypes.c_void_p
            fn.argtypes = args
        version = lib.loaf_l2_abi_version()
        if version != ABI_VERSION:
            raise L2SignerError(f"l2 signer: ABI version {version}, this SDK speaks {ABI_VERSION}")
        self.path = lib_path
        self._lib = lib

    def _call(self, name: str, *args: Any) -> Any:
        try:
            ptr = getattr(self._lib, name)(*args)
        finally:
            # Crash reporters and debuggers record a traceback's frame locals: leave no key in them.
            args = ()
        if not ptr:
            raise MemoryError("l2 signer: the library could not allocate its answer")
        try:
            body = ctypes.string_at(ptr).decode("utf-8")
        finally:
            self._lib.loaf_l2_free(ptr)
        answer = json.loads(body)
        if "error" in answer:
            raise L2SignerError(answer["error"])
        return answer["ok"]

    @staticmethod
    def _aux(aux: Optional[bytes]) -> bytes:
        return secrets.token_bytes(32) if aux is None else bytes(aux)

    def key_from_seed(self, seed: bytes) -> str:
        """The secret key of a 32-byte seed (k1-sign keygen), 80 lowercase hex."""
        try:
            seed = bytes(seed)
            return self._call("loaf_l2_key_from_seed", seed, len(seed))
        finally:
            seed = b""

    def public_key(self, sk: str) -> str:
        """``0x`` and 80 hex: the key ``POST /api/l2/keys`` registers as api key 0."""
        try:
            return self._call("loaf_l2_public_key", sk.encode())
        finally:
            sk = ""

    def order_message(self, order: Mapping[str, Any]) -> str:
        """The order's K1 order message, 64 hex."""
        return self._call("loaf_l2_order_message", json.dumps(dict(order)).encode())

    def sign_order(self, sk: str, order: Mapping[str, Any], aux: Optional[bytes] = None) -> dict:
        """An order signed alone: ``{"s", "e"}``, the body's ``l2SigS`` and ``l2SigE``. ``aux`` is
        32 random bytes, fresh from ``secrets`` unless given."""
        a = self._aux(aux)
        try:
            return self._call("loaf_l2_sign_order", sk.encode(), json.dumps(dict(order)).encode(), a, len(a))
        finally:
            sk = ""

    def sign_batch(self, sk: str, orders: Sequence[Mapping[str, Any]], aux: Optional[bytes] = None) -> dict:
        """One signature over the root of 1 to 256 orders of one account, chain id and epoch:
        ``{"s", "e", "count", "treeRoot", "message", "places": [{"position", "siblings"}]}``,
        ``places`` in the order given."""
        a = self._aux(aux)
        body = json.dumps([dict(o) for o in orders]).encode()
        try:
            return self._call("loaf_l2_sign_batch", sk.encode(), body, a, len(a))
        finally:
            sk = ""

    def withdraw_message(self, withdraw: Mapping[str, Any]) -> str:
        """The withdrawal's K1 withdraw message, 64 hex."""
        return self._call("loaf_l2_withdraw_message", json.dumps(dict(withdraw)).encode())

    def sign_withdraw(self, sk: str, withdraw: Mapping[str, Any], aux: Optional[bytes] = None) -> dict:
        """A withdrawal's ``{"s", "e"}``, the ``POST /api/l2/withdrawals`` body's ``l2SigS`` and
        ``l2SigE``."""
        a = self._aux(aux)
        try:
            return self._call("loaf_l2_sign_withdraw", sk.encode(), json.dumps(dict(withdraw)).encode(), a, len(a))
        finally:
            sk = ""


@functools.lru_cache(maxsize=None)
def default_signer() -> L2Signer:
    """The library for this process (the vendored one, or ``$LOAF_L2_SIGNER_LIB``), loaded once."""
    return L2Signer()


def signed_fields(sig: Mapping[str, str]) -> dict:
    """An order signed alone: its ``l2SigS`` and ``l2SigE``; it sends no batch field."""
    return {"l2SigS": sig["s"], "l2SigE": sig["e"]}


def batch_fields(batch: Mapping[str, Any]) -> list:
    """Each order's L2 signature and batch fields, in the batch's order: ``l2SigS``, ``l2SigE``,
    ``l2BatchCount``, ``l2BatchPosition``, ``l2BatchSiblings`` and ``l2BatchTreeRoot``."""
    return [
        {
            "l2SigS": batch["s"],
            "l2SigE": batch["e"],
            "l2BatchCount": batch["count"],
            "l2BatchPosition": place["position"],
            "l2BatchSiblings": list(place["siblings"]),
            "l2BatchTreeRoot": batch["treeRoot"],
        }
        for place in batch["places"]
    ]

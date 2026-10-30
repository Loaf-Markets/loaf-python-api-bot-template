"""K1 signing: the keys, order and login messages and Schnorr signatures of Loaf's L2.

A pure-Python port of K1's reference signer, the one implementation the engine
verifies with and the web app signs with. It reproduces K1's known answers byte
for byte (``tests/k1-sign.json``), and the engine's for the login message
(``tests/k1-login.json``).

* :func:`key_from_seed` and :func:`public_key`: a 40-byte secret key from a
  32-byte seed, and its 40-byte public key, the point ``sk G`` of ECgFp5
  (Pornin, ePrint 2022/274) in K1's encoding;
* :func:`order_message`: the 32 bytes an order signs, K1's Poseidon2 digest of
  its 12 signed fields, once they pass the checks the engine makes; a perps
  order signs its pool and reduce-only bit where a spot order signs its base
  asset (:func:`perps_base_field`);
* :func:`allocate_message` and :func:`exit_message`: the 32 bytes a perps
  allocate (USDC moved between the spot balance and a margin pool, or between
  two pools) and a perps exit (the close of a position in an expired market)
  sign, likewise checked;
* :func:`login_message`: the 32 bytes a login token signs, the same digest of
  its 3 signed fields (the L2 chain, the account and the token's expiry);
* :func:`sign` and :func:`verify`: the Schnorr pair ``(s, e)``, 40 bytes
  little-endian each.

Bytes in, bytes out: the wire's hex (``l2SigS``, ``l2SigE``, the ``0x`` public
key) is the caller's. A refusal raises :class:`K1Error` with the reference's
code and text.

Threat model: Python integers are not constant time, so the secret key and the
nonce go through variable-time arithmetic and secret-indexed table reads. That
is acceptable because the key lives in the bot's own process: an attacker who
can time that process closely is out of scope. As in K1, the nonce is derived
from the key, the message and 32 fresh random bytes, so it stays secret, and
differs between messages, even when those bytes are poor.
"""

from __future__ import annotations

import hashlib
from fractions import Fraction
from functools import lru_cache
from typing import NamedTuple, Sequence

from .exceptions import LoafError


class K1Error(LoafError):
    """A refusal: ``code`` names the rule (K1's name, or the engine's for an order
    field) and the message is the engine's or the web app signer's text."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def key_from_seed(seed: bytes) -> bytes:
    """The secret key of a 32-byte seed, 40 bytes little-endian:
    ``int_le(TH("loaf/k1/keygen", seed)) mod n``."""
    if len(seed) != 32:
        raise K1Error("Seed", "l2 signer: the seed must be 32 bytes")
    sk = int.from_bytes(_tagged_hash(_KEYGEN, seed), "little") % _N
    if sk == 0:  # probability about 2^-319
        raise K1Error("ZeroKey", "l2 signer: ZeroKey, draw new random bytes")
    return sk.to_bytes(40, "little")


def public_key(sk: bytes) -> bytes:
    """The 40-byte public key of a secret key: the encoding of ``sk G``, five
    little-endian limbs (registered as ``"0x" + public_key(sk).hex()``)."""
    return _to_bytes(_encode(_mul_g(_secret(sk))))


def order_message(
    *,
    l2_chain_id: int,
    l2_market_index: int,
    l2_base_asset_id: int,
    l2_quote_asset_id: int,
    is_ask: bool,
    price: int,
    l2_fee_rate_cap: int,
    l2_account_index: int,
    l2_order_index: int,
    l2_epoch: int,
    quantity: int,
    l2_expiry_ms: int,
) -> bytes:
    """K1's order message: the 32 bytes an order's account key signs.

    The arguments are the order's 12 signed fields, its body keys in snake case:
    integers, with ``price`` and ``quantity`` already scaled (x1000 and x10),
    and ``is_ask`` true for a SELL. On a perps market (``l2_market_index`` at
    most 254) ``l2_base_asset_id`` is the order's pool byte, 0 to 15
    (:func:`perps_base_field`); on a spot market it is the base asset, 1 to 62.
    A field the engine would refuse is refused first, as the web app's signer
    refuses it: ``Width`` with ``l2 width: <field> out of range``, or
    ``QuoteAboveK1`` / ``FeeAboveK1`` when the whole quantity at the price, or
    the fee cap's fee on it, does not fit 48 bits.
    """
    if not isinstance(is_ask, bool):
        raise TypeError(f"is_ask must be a bool, not {type(is_ask).__name__}")
    # The engine's widths in its parse order, then K1's bounds on the order's largest fill, then
    # the chain id, which the message carries below the KoalaBear modulus.
    _width("l2_account_index", l2_account_index, 0, _MAX_48)
    _width("l2_market_index", l2_market_index, 0, 4095)
    _width("l2_order_index", l2_order_index, 1, 2**40 - 1)
    _width("l2_epoch", l2_epoch, 0, _MAX_48)
    _width("quantity", quantity, 1, _MAX_48)
    _width("price", price, 1, 2**32 - 1)
    _width("l2_fee_rate_cap", l2_fee_rate_cap, 0, 2**20 - 1)
    _width("l2_expiry_ms", l2_expiry_ms, 0, _MAX_48)
    if l2_market_index <= _PERPS_LAST:  # a perps market has no base asset: the pool byte
        _width("l2_base_asset_id", l2_base_asset_id, 0, _MAX_POOL | _PERPS_REDUCE_ONLY)
    else:
        _width("l2_base_asset_id", l2_base_asset_id, 1, 62)
    _width("l2_quote_asset_id", l2_quote_asset_id, 1, 62)
    if quantity * price // 10 > _MAX_48:
        raise K1Error(
            "QuoteAboveK1",
            "l2 k1 bound: quantity x price / 10 is not below 2^48 (K1's 48-bit fill quote)",
        )
    if -(-quantity * price * l2_fee_rate_cap // 10_000_000) > _MAX_48:
        raise K1Error(
            "FeeAboveK1",
            "l2 k1 bound: the fee-rate cap's fee on quantity x price is not below 2^48 (K1 fees)",
        )
    _width("l2_chain_id", l2_chain_id, 0, _KB_P - 1)
    return _order_message(
        l2_chain_id,
        l2_market_index,
        l2_base_asset_id,
        l2_quote_asset_id,
        is_ask,
        price,
        l2_fee_rate_cap,
        l2_account_index,
        l2_order_index,
        l2_epoch,
        quantity,
        l2_expiry_ms,
    )


def perps_base_field(pool: int, reduce_only: bool) -> int:
    """A perps order's signed base-asset field, :func:`order_message`'s
    ``l2_base_asset_id`` on a perps market: its margin pool (0, the cross pool,
    to 7), plus 8 when it is reduce-only (its fills may only shrink the
    position).

    A perps market has no base asset: the order's body sends ``l2Pool`` and
    ``l2ReduceOnly`` where a spot order's sends ``l2BaseAssetId``. A pool
    outside 0 to 7 is refused, ``Width`` with ``l2 width: l2_pool out of
    range``: its bits would reach the reduce-only bit.
    """
    if not isinstance(reduce_only, bool):
        raise TypeError(f"reduce_only must be a bool, not {type(reduce_only).__name__}")
    _width("l2_pool", pool, 0, _MAX_POOL)
    return pool | (_PERPS_REDUCE_ONLY if reduce_only else 0)


def allocate_message(
    *,
    l2_chain_id: int,
    l2_account_index: int,
    from_side: int,
    to_side: int,
    amount: int,
    l2_nonce: int,
    l2_expiry_ms: int,
) -> bytes:
    """K1's allocate message: the 32 bytes a perps allocate signs with the
    account's api key 0. It moves ``amount`` USDC ticks from ``from_side`` to
    ``to_side``, each a margin pool (0 to 7) or 255, the spot USDC balance.

    The arguments are its signed fields, its body keys in snake case, with
    ``amount`` an integer (the body's decimal string) and ``l2_nonce`` api key
    0's next nonce. A field the engine would refuse is refused first, in the
    engine's order, then the chain id: ``Width`` with ``l2 width: <field> out
    of range``. Two equal sides are refused by the exchange (K1's rule), not
    here: the message carries them.
    """
    _width("l2_account_index", l2_account_index, 0, _MAX_OP_ACCOUNT)
    _side("from_side", from_side)
    _side("to_side", to_side)
    _width("amount", amount, 1, _AMOUNT_LIMIT - 1)
    _width("l2_nonce", l2_nonce, 0, _MAX_48)
    _width("l2_expiry_ms", l2_expiry_ms, 0, _MAX_48)
    _width("l2_chain_id", l2_chain_id, 0, _KB_P - 1)
    return _allocate_message(
        l2_chain_id, l2_account_index, from_side, to_side, amount, l2_nonce, l2_expiry_ms
    )


def exit_message(
    *,
    l2_chain_id: int,
    l2_account_index: int,
    pool: int,
    market: int,
    l2_nonce: int,
    l2_expiry_ms: int,
) -> bytes:
    """K1's exit message: the 32 bytes a perps exit signs with the account's api
    key 0. It closes the position margin pool ``pool`` (0 to 7) holds in perps
    market ``market`` (0 to 254), an expired one.

    The arguments are its signed fields, its body keys in snake case, refused
    as :func:`allocate_message` refuses them.
    """
    _width("l2_account_index", l2_account_index, 0, _MAX_OP_ACCOUNT)
    _width("pool", pool, 0, _MAX_POOL)
    _width("market", market, 0, _PERPS_LAST)
    _width("l2_nonce", l2_nonce, 0, _MAX_48)
    _width("l2_expiry_ms", l2_expiry_ms, 0, _MAX_48)
    _width("l2_chain_id", l2_chain_id, 0, _KB_P - 1)
    return _exit_message(l2_chain_id, l2_account_index, pool, market, l2_nonce, l2_expiry_ms)


def login_message(*, l2_chain_id: int, l2_account_index: int, l2_expiry_ms: int) -> bytes:
    """The login message: the 32 bytes an account's api key 0, its trading key, signs
    for a login token (``loaf_lt_…``), the Bearer that authenticates the account's
    requests other than its signed orders.

    The arguments are the token's three signed fields: the L2 chain id, the account's
    index and the token's expiry (unix ms). A field the message cannot carry is refused
    first, as the engine refuses it, in this order: ``Width`` with ``l2 width: <field>
    out of range`` for a chain id not below the KoalaBear modulus (0 is carried, as an
    order's is), then an account or an expiry wider than 48 bits.
    """
    _width("l2_chain_id", l2_chain_id, 0, _KB_P - 1)
    _width("l2_account_index", l2_account_index, 0, _MAX_48)
    _width("l2_expiry_ms", l2_expiry_ms, 0, _MAX_48)
    return _login_message(l2_chain_id, l2_account_index, l2_expiry_ms)


def sign(sk: bytes, message: bytes, aux: bytes) -> tuple[bytes, bytes]:
    """K1's Schnorr signature of a 32-byte message: ``(s, e)``, 40 bytes
    little-endian each (the body's ``l2SigS`` and ``l2SigE`` are their hex).

    ``aux`` is 32 fresh random bytes per signature (``os.urandom(32)``); fixed
    bytes only reproduce a known answer. The nonce is K1's:
    ``k = int_le(TH("loaf/k1/nonce", t || pk || message)) mod n`` with
    ``t = sk XOR TH("loaf/k1/aux", aux)``. Then ``R = k G``, ``e`` is the
    Poseidon2 challenge of R and the message mod n, and ``s = k - e sk mod n``.

    Unlike the web app's signer, it does not verify its own signature: a
    :func:`verify` costs about three signatures.
    """
    x = _secret(sk)
    if len(aux) != 32:
        raise K1Error("Aux", "l2 signer: the random bytes must be 32 bytes")
    limbs = _message_limbs(message)
    if limbs is None:
        raise K1Error("MessageNoncanonical", _BAD_MESSAGE)
    pk = _to_bytes(_encode(_mul_g(x)))
    mask = int.from_bytes(_tagged_hash(_AUX, aux)[:40], "little")
    t = (x ^ mask).to_bytes(40, "little")
    k = int.from_bytes(_tagged_hash(_NONCE, t, pk, message), "little") % _N
    if k == 0:  # probability about 2^-319
        raise K1Error("ZeroNonce", "l2 signer: ZeroNonce, draw new random bytes")
    e = _challenge(_encode(_mul_g(k)), limbs)
    return ((k - e * x) % _N).to_bytes(40, "little"), e.to_bytes(40, "little")


def verify(pk: bytes, message: bytes, s: bytes, e: bytes) -> None:
    """Check ``(s, e)`` over a 32-byte message under a 40-byte public key, as the
    engine checks an order, or raise :class:`K1Error` with K1's code, checked in
    this order: ``SigLength``, ``SigNoncanonical``, ``Noncanonical``,
    ``PubKeyZero``, ``PubKeyDecode``, ``RIdentity``, ``Sig``."""
    if len(s) != 40 or len(e) != 40:
        raise K1Error("SigLength", _NOT_VERIFIED)
    s_int, e_int = int.from_bytes(s, "little"), int.from_bytes(e, "little")
    if s_int >= _N or e_int >= _N:
        raise K1Error("SigNoncanonical", _BIG_SCALAR)
    w = _key_limbs(pk)
    if w is None:
        raise K1Error("Noncanonical", _NO_KEY)
    limbs = _message_limbs(message)
    if limbs is None:
        raise K1Error("Noncanonical", _BAD_MESSAGE)
    if not any(w):
        raise K1Error("PubKeyZero", _NO_KEY)
    q = _decode(w)
    if q is None:
        raise K1Error("PubKeyDecode", _NO_KEY)
    w_r = _encode(_add_points(_mul_g(s_int), _mul_var(q, e_int)))
    if not any(w_r):
        raise K1Error("RIdentity", _NOT_VERIFIED)
    if _challenge(w_r, limbs) != e_int:
        raise K1Error("Sig", _NOT_VERIFIED)


# The engine's texts for a refused signature. The engine has none for a bad message, since it
# hashes its own.
_NOT_VERIFIED = "l2 signature: the Schnorr signature does not verify"
_BIG_SCALAR = "l2 signature: a signature scalar is not below the ECgFp5 group order"
_NO_KEY = "l2 signature: the api key has no usable public key"
_BAD_MESSAGE = "l2 signature: the message must be 32 bytes of four limbs below p"

_MAX_48 = (1 << 48) - 1
_M24 = (1 << 24) - 1
_M64 = (1 << 64) - 1

# Perps: markets 0 to 254 are perps markets; a pool is 0 (the cross pool) to 7, and a perps
# order's base-asset field carries the reduce-only bit above it. An allocate's side is a pool or
# the spot balance (255), its amount below 2^60; the account of an allocate or exit is at most
# 2^48 - 2, the engine's largest account index.
_PERPS_LAST = 254
_MAX_POOL = 7
_PERPS_REDUCE_ONLY = 8
_SPOT_SIDE = 255
_AMOUNT_LIMIT = 1 << 60
_MAX_OP_ACCOUNT = _MAX_48 - 1
# Element 1 of an allocate and an exit message, api key 0's (its index shifts in above them).
_ALLOCATE_TAG = 23
_EXIT_TAG = 25


def _width(field: str, value: int, low: int, high: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field} must be an int, not {type(value).__name__}")
    if not low <= value <= high:
        raise K1Error("Width", f"l2 width: {field} out of range")


def _side(field: str, value: int) -> None:
    """An allocate's side: a pool, 0 to 7, or the spot balance, 255."""
    _width(field, value, 0, _SPOT_SIDE)
    if _MAX_POOL < value < _SPOT_SIDE:
        raise K1Error("Width", f"l2 width: {field} out of range")


def _order_message(
    chain_id: int,
    market: int,
    base_asset: int,
    quote_asset: int,
    is_ask: bool,
    price: int,
    fee_cap: int,
    account: int,
    index: int,
    epoch: int,
    base_amount: int,
    expiry: int,
) -> bytes:
    """The order message of fields in range, unchecked: the sponge digest of the
    order's 16 elements (two absorbs) as the 32 bytes a key signs."""
    word = 149 + (is_ask << 16) + (base_asset << 17) + (quote_asset << 23)  # 149: a settle order
    elements = (
        chain_id, word, market, price & _M24, price >> 24, fee_cap,
        account & _M24, account >> 24, index & _M24, index >> 24, epoch & _M24, epoch >> 24,
        base_amount & _M24, base_amount >> 24, expiry & _M24, expiry >> 24,
    )
    return _message_bytes(_kb_hash(elements))


def _allocate_message(
    chain_id: int,
    account: int,
    from_side: int,
    to_side: int,
    amount: int,
    nonce: int,
    expiry: int,
) -> bytes:
    """The allocate message of fields in range, unchecked: chain id; tag 23; the
    account (low 24, middle 24 bits); the sides; the amount (low 24, middle 24,
    the bits above 48); the nonce and the expiry (low 24, middle 24 bits each);
    three zeros."""
    return _message_bytes(_kb_hash((
        chain_id, _ALLOCATE_TAG, account & _M24, account >> 24 & _M24, from_side, to_side,
        amount & _M24, amount >> 24 & _M24, amount >> 48,
        nonce & _M24, nonce >> 24 & _M24, expiry & _M24, expiry >> 24 & _M24, 0, 0, 0,
    )))


def _exit_message(
    chain_id: int, account: int, pool: int, market: int, nonce: int, expiry: int
) -> bytes:
    """The exit message of fields in range, unchecked: chain id; tag 25; the
    account (low 24, middle 24 bits); the pool; the market; the nonce and the
    expiry (low 24, middle 24 bits each); six zeros."""
    return _message_bytes(_kb_hash((
        chain_id, _EXIT_TAG, account & _M24, account >> 24 & _M24, pool, market,
        nonce & _M24, nonce >> 24 & _M24, expiry & _M24, expiry >> 24 & _M24, 0, 0, 0, 0, 0, 0,
    )))


def _login_message(chain_id: int, account: int, expiry: int) -> bytes:
    """The login message of fields in range, unchecked: the sponge digest of its 6
    elements (one absorb) as the 32 bytes a key signs. The sponge's initial state holds
    the length, 6, where every other message K1 signs has 16, so no login message is
    another message."""
    # 14: a login, below the orders' 149, its low byte none of the withdraw's 13, the price
    # report's 21, the allocate's 23, the exit's 25, the order's 149 or the batch root's 150 (the
    # engine's crates/rules LOGIN_TAG).
    elements = (chain_id, 14, account & _M24, account >> 24, expiry & _M24, expiry >> 24)
    return _message_bytes(_kb_hash(elements))


def _kb_hash(elements: Sequence[int]) -> list[int]:
    """K1's sponge over KoalaBear (k1-sign ``kb::hash``): rate 8, the input's length in
    lane 8 of the initial state; each chunk of up to 8 elements overwrites that many of
    lanes 0..7, the rest keeping the state, then the state is permuted (once for an
    empty input). The digest m is lanes 0..7."""
    state = [0] * 16
    state[8] = len(elements)
    if not elements:
        state = _permute(state, _KB16)
    for start in range(0, len(elements), 8):
        chunk = elements[start : start + 8]
        state[: len(chunk)] = chunk
        state = _permute(state, _KB16)
    return state[:8]


def _message_bytes(m: Sequence[int]) -> bytes:
    """A digest m as the 32 bytes a key signs (k1-sign ``msg::message_bytes``):
    P_i = m[2i] + 2^31 m[2i+1], each 8 bytes little-endian."""
    return (
        m[0] + (m[1] << 31)
        | (m[2] + (m[3] << 31)) << 64
        | (m[4] + (m[5] << 31)) << 128
        | (m[6] + (m[7] << 31)) << 192
    ).to_bytes(32, "little")


def _message_limbs(message: bytes) -> tuple[int, int, int, int] | None:
    """A message's four little-endian limbs; None unless it is 32 bytes, each limb below p."""
    if len(message) != 32:
        return None
    m = int.from_bytes(message, "little")
    limbs = (m & _M64, m >> 64 & _M64, m >> 128 & _M64, m >> 192)
    return None if max(limbs) >= _P else limbs


def _key_limbs(pk: bytes) -> _Fp5 | None:
    """A public key's five little-endian limbs; None unless it is 40 bytes, each limb below p."""
    if len(pk) != 40:
        return None
    v = int.from_bytes(pk, "little")
    w = (v & _M64, v >> 64 & _M64, v >> 128 & _M64, v >> 192 & _M64, v >> 256)
    return None if max(w) >= _P else w


def _secret(sk: bytes) -> int:
    x = int.from_bytes(sk, "little") if len(sk) == 40 else 0
    if not 0 < x < _N:
        raise K1Error(
            "SecretKey", "l2 signer: the secret key must be 40 bytes of a scalar in [1, n)"
        )
    return x


def _tag(name: bytes) -> bytes:
    digest = hashlib.sha512(name).digest()
    return digest + digest


# TH(tag, x) = SHA-512(SHA-512(tag) || SHA-512(tag) || x), BIP340's tagged hash at SHA-512's block
# size; each constant is its tag's 128-byte prefix.
_KEYGEN = _tag(b"loaf/k1/keygen")
_AUX = _tag(b"loaf/k1/aux")
_NONCE = _tag(b"loaf/k1/nonce")


def _tagged_hash(prefix: bytes, *parts: bytes) -> bytes:
    return hashlib.sha512(b"".join((prefix, *parts))).digest()


def _challenge(w: _Fp5, limbs: tuple[int, int, int, int]) -> int:
    """e: the 320-bit integer of lanes 0..4 of one Poseidon2-GL16 permutation of
    [w | P_0..P_3 | 0 | 0 | tag 1 | 0 0 0 0], mod n (w is R's encoding)."""
    c = _permute([*w, *limbs, 0, 0, 1, 0, 0, 0, 0], _GL16)
    return (c[0] | c[1] << 64 | c[2] << 128 | c[3] << 192 | c[4] << 256) % _N


# --------------------------------------------------------------------------- #
# GF(p^5) = F_p[z] / (z^5 - 3), p = 2^64 - 2^32 + 1
# --------------------------------------------------------------------------- #

# An element is its five limbs. Products and _scale reduce them mod p; sums, differences and small
# multiples leave them unreduced, possibly negative, for the next product to reduce.
_Fp5 = tuple[int, int, int, int, int]

_P = 0xFFFF_FFFF_0000_0001
_ZERO: _Fp5 = (0, 0, 0, 0, 0)
_ONE: _Fp5 = (1, 0, 0, 0, 0)
# z^p = omega z, omega = 3^((p - 1) / 5): limb i of a^(p^k) is a_i omega^(i k).
_OMEGA = pow(3, (_P - 1) // 5, _P)
_FROBENIUS = tuple(tuple(pow(_OMEGA, i * k, _P) for i in range(5)) for k in range(4))


def _mul(a: _Fp5, b: _Fp5) -> _Fp5:
    a0, a1, a2, a3, a4 = a
    b0, b1, b2, b3, b4 = b
    t1, t2, t3, t4 = 3 * a1, 3 * a2, 3 * a3, 3 * a4
    return (
        (a0 * b0 + t1 * b4 + t2 * b3 + t3 * b2 + t4 * b1) % _P,
        (a0 * b1 + a1 * b0 + t2 * b4 + t3 * b3 + t4 * b2) % _P,
        (a0 * b2 + a1 * b1 + a2 * b0 + t3 * b4 + t4 * b3) % _P,
        (a0 * b3 + a1 * b2 + a2 * b1 + a3 * b0 + t4 * b4) % _P,
        (a0 * b4 + a1 * b3 + a2 * b2 + a3 * b1 + a4 * b0) % _P,
    )


def _sqr(a: _Fp5) -> _Fp5:
    a0, a1, a2, a3, a4 = a
    d0, d1, d2, t3, t4 = 2 * a0, 2 * a1, 2 * a2, 3 * a3, 3 * a4
    return (
        (a0 * a0 + 2 * (a1 * t4 + a2 * t3)) % _P,
        (d0 * a1 + d2 * t4 + a3 * t3) % _P,
        (d0 * a2 + a1 * a1 + 2 * a3 * t4) % _P,
        (d0 * a3 + d1 * a2 + a4 * t4) % _P,
        (d0 * a4 + d1 * a3 + a2 * a2) % _P,
    )


def _add(a: _Fp5, b: _Fp5) -> _Fp5:
    a0, a1, a2, a3, a4 = a
    b0, b1, b2, b3, b4 = b
    return (a0 + b0, a1 + b1, a2 + b2, a3 + b3, a4 + b4)


def _sub(a: _Fp5, b: _Fp5) -> _Fp5:
    a0, a1, a2, a3, a4 = a
    b0, b1, b2, b3, b4 = b
    return (a0 - b0, a1 - b1, a2 - b2, a3 - b3, a4 - b4)


def _dbl(a: _Fp5) -> _Fp5:
    a0, a1, a2, a3, a4 = a
    return (2 * a0, 2 * a1, 2 * a2, 2 * a3, 2 * a4)


def _mulz(a: _Fp5, k: int) -> _Fp5:
    """a times k z, for a small integer k."""
    a0, a1, a2, a3, a4 = a
    return (3 * k * a4, k * a0, k * a1, k * a2, k * a3)


def _scale(a: _Fp5, k: int) -> _Fp5:
    a0, a1, a2, a3, a4 = a
    return (a0 * k % _P, a1 * k % _P, a2 * k % _P, a3 * k % _P, a4 * k % _P)


def _frobenius(a: _Fp5, k: int) -> _Fp5:
    a0, a1, a2, a3, a4 = a
    _, w1, w2, w3, w4 = _FROBENIUS[k]
    return (a0 % _P, a1 * w1 % _P, a2 * w2 % _P, a3 * w3 % _P, a4 * w4 % _P)


def _norm(a: _Fp5) -> tuple[int, _Fp5]:
    """(N(a), c): the norm a^(1 + p + ... + p^4), in F_p and 0 only for a = 0, and
    the product c of a's four conjugates, so that 1 / a = c / N(a)."""
    f12 = _mul(_frobenius(a, 1), _frobenius(a, 2))
    c = _mul(f12, _frobenius(f12, 2))
    a0, a1, a2, a3, a4 = a
    c0, c1, c2, c3, c4 = c
    return (a0 * c0 + 3 * (a1 * c4 + a2 * c3 + a3 * c2 + a4 * c1)) % _P, c


def _inv(a: _Fp5) -> _Fp5:
    n, c = _norm(a)
    return _scale(c, pow(n, -1, _P))


def _is_square(a: _Fp5) -> bool:
    """For a != 0: a is a square in GF(p^5) iff its norm is one in F_p."""
    return pow(_norm(a)[0], (_P - 1) // 2, _P) == 1


def _sqrt(a: _Fp5) -> _Fp5 | None:
    """A square root of a != 0, or None. With r = (p^5 - 1) / (p - 1),
    y = a^((r + 1) / 2) has y^2 = a N(a), so y / sqrt(N(a)) is a root; and
    (r + 1) / 2 = 1 + p (1 + p) (1 + p^2) / 2 makes y = a phi(u) phi^3(u) with
    u = a^((p + 1) / 2) and phi the Frobenius."""
    s = _sqrt_fp(_norm(a)[0])
    if s is None:
        return None
    u = a
    for bit in bin((_P + 1) // 2)[3:]:
        u = _sqr(u)
        if bit == "1":
            u = _mul(u, a)
    y = _mul(a, _mul(_frobenius(u, 1), _frobenius(u, 3)))
    return _scale(y, pow(s, -1, _P))


_ODD = (_P - 1) >> 32  # p - 1 = 2^32 (2^32 - 1)
_ROOT_2_32 = pow(7, _ODD, _P)  # 7 is not a square, so this has order 2^32


def _sqrt_fp(n: int) -> int | None:
    """A square root of n != 0 in F_p (Tonelli-Shanks), or None."""
    if pow(n, (_P - 1) // 2, _P) != 1:
        return None
    m, c, t, r = 32, _ROOT_2_32, pow(n, _ODD, _P), pow(n, (_ODD + 1) // 2, _P)
    while t != 1:
        i, t2 = 0, t
        while t2 != 1:
            t2 = t2 * t2 % _P
            i += 1
        b = pow(c, 1 << (m - i - 1), _P)
        m, c, t, r = i, b * b % _P, t * b * b % _P, r * b % _P
    return r


# --------------------------------------------------------------------------- #
# ECgFp5: y^2 = x (x^2 + 2 x + 263 z), of order 2n
# --------------------------------------------------------------------------- #

# Pornin's fractional coordinates (X : Z : U : T), x = X / Z and u = x / y = U / T. A group element
# is held as its representative in the coset N + E[n] (N = (0, 0), the neutral (0 : 1 : 0 : 1)),
# where his formulas are complete, and its encoding is w = T / U. An _Affine point is (x, u).
_Point = tuple[_Fp5, _Fp5, _Fp5, _Fp5]
_Affine = tuple[_Fp5, _Fp5]

_N = 0x7FFFFFFD80000007_7FFFFFF100000016_7FFFFFE6CFB80639_E8885C39D724A09C_E80FD996948BFFE1
_B1 = 263  # b = 263 z
_NEUTRAL: _Point = (_ZERO, _ONE, _ZERO, _ONE)


def _add_points(p: _Point, q: _Point) -> _Point:
    """p + q, complete (10M; Pornin's ``set_add``)."""
    x1, z1, u1, t1 = p
    x2, z2, u2, t2 = q
    a1, a2, a3, a4 = _mul(x1, x2), _mul(z1, z2), _mul(u1, u2), _mul(t1, t2)
    a5 = _sub(_sub(_mul(_add(x1, z1), _add(x2, z2)), a1), a2)
    a6 = _sub(_sub(_mul(_add(u1, t1), _add(u2, t2)), a3), a4)
    b2 = _mulz(a2, _B1)
    a7 = _add(a1, b2)
    a8 = _mul(a4, a7)
    a9 = _mul(a3, _add(_mulz(a5, 2 * _B1), _dbl(a7)))
    a10 = _mul(_add(a4, _dbl(a3)), _add(a5, a7))
    return _mulz(_sub(a10, a8), _B1), _sub(a8, a9), _mul(a6, _sub(b2, a1)), _add(a8, a9)


def _add_affine(p: _Point, q: _Affine) -> _Point:
    """p + q for q with Z = T = 1: the complete addition less its two products by 1 (8M)."""
    x1, z1, u1, t1 = p
    x2, u2 = q
    a1, a3 = _mul(x1, x2), _mul(u1, u2)
    a5 = _add(x1, _mul(x2, z1))
    a6 = _add(u1, _mul(u2, t1))
    b2 = _mulz(z1, _B1)
    a7 = _add(a1, b2)
    a8 = _mul(t1, a7)
    a9 = _mul(a3, _add(_mulz(a5, 2 * _B1), _dbl(a7)))
    a10 = _mul(_add(t1, _dbl(a3)), _add(a5, a7))
    return _mulz(_sub(a10, a8), _B1), _sub(a8, a9), _mul(a6, _sub(b2, a1)), _add(a8, a9)


def _double(p: _Point, times: int) -> _Point:
    """2^times p, times >= 1. Pornin's doubling (``set_double``) gives
    (b m^2 : n^2 : m n : T) with m = 2 Z T U and n = Z T^2 - 2 (X + Z) U^2; on such
    a point the next doubling, its common factor n^2 dropped, needs only m, n and
    T, so each doubling after the first costs 3M + 4S instead of 4M + 5S."""
    x, z, u, t = p
    zt = _mul(z, t)
    zt2 = _mul(zt, t)
    m = _dbl(_mul(zt, u))
    n = _sub(zt2, _mul(_dbl(_add(x, z)), _sqr(u)))
    mm, nn = _sqr(m), _sqr(n)
    t = _sub(_sub(_dbl(_sqr(zt2)), mm), nn)
    for _ in range(times - 1):
        t2 = _sqr(t)
        m, n = _dbl(_mul(_mul(m, n), t)), _sub(t2, _dbl(_mul(mm, _add(_mulz(mm, _B1), nn))))
        mm, nn = _sqr(m), _sqr(n)
        t = _sub(_sub(_dbl(_sqr(t2)), mm), nn)
    return _mulz(mm, _B1), nn, _mul(m, n), t


def _decode(w: _Fp5) -> _Point | None:
    """The element encoded w != 0, or None: x is the root of
    x^2 - (w^2 - 2) x + b = 0 that is not a square (the coset's), and u = 1 / w."""
    e = _sub(_sqr(w), (2, 0, 0, 0, 0))
    r = _sqrt(_sub(_sqr(e), (0, 4 * _B1, 0, 0, 0)))
    if r is None:
        return None
    half = (_P + 1) // 2
    x = _scale(_add(e, r), half)
    if _is_square(x):
        x = _scale(_sub(e, r), half)
    return x, _ONE, _ONE, w


def _encode(p: _Point) -> _Fp5:
    """w = T / U, reduced; 0 for the neutral, the one element with U = 0."""
    n, c = _norm(p[2])
    if n == 0:
        return _ZERO
    return _mul(p[3], _scale(c, pow(n, -1, _P)))


def _to_bytes(w: _Fp5) -> bytes:
    w0, w1, w2, w3, w4 = w
    return (w0 | w1 << 64 | w2 << 128 | w3 << 192 | w4 << 256).to_bytes(40, "little")


def _mul_g(k: int) -> _Point:
    """k G for 0 <= k < 2^320: one table point added per 5-bit window, no doubling."""
    rows = _generator_table()
    x, u = rows[0][k & 31]
    p = (x, _ONE, u, _ONE)
    for row in rows[1:]:
        k >>= 5
        p = _add_affine(p, row[k & 31])
    return p


def _mul_var(q: _Point, k: int) -> _Point:
    """k q for 0 <= k < 2^320 and a public k: 4-bit windows from the top, each
    four doublings and the addition of a multiple 0..15 of q."""
    table = [_NEUTRAL, q]
    for _ in range(14):
        table.append(_add_points(table[-1], q))
    p = table[k >> 316]
    for shift in range(312, -4, -4):
        p = _add_points(_double(p, 4), table[k >> shift & 15])
    return p


@lru_cache(maxsize=None)
def _generator_table() -> tuple[tuple[_Affine, ...], ...]:
    """Row i holds j 32^i G for j < 32, with Z = T = 1. Built on first use (about
    70 ms; about 1.5 MB)."""
    g = _decode((4, 0, 0, 0, 0))  # the conventional generator, encoded w = 4
    assert g is not None
    points: list[_Point] = []
    for _ in range(64):
        points += (_NEUTRAL, g)
        for _ in range(30):
            points.append(_add_points(points[-1], g))
        g = _add_points(points[-1], g)
    # x = X / Z and u = U / T for every point, with one inversion (Montgomery's trick): prefix[i] is
    # the product of dens[:i], inv that of dens[:i + 1] inverted, and dens[i] becomes 1 / dens[i].
    dens = [d for p in points for d in (p[1], p[3])]
    prefix = []
    acc = _ONE
    for d in dens:
        prefix.append(acc)
        acc = _mul(acc, d)
    inv = _inv(acc)
    for i in reversed(range(len(dens))):
        dens[i], inv = _mul(inv, prefix[i]), _mul(inv, dens[i])
    affine = [(_mul(p[0], dens[2 * i]), _mul(p[2], dens[2 * i + 1])) for i, p in enumerate(points)]
    return tuple(tuple(affine[i : i + 32]) for i in range(0, len(affine), 32))


# --------------------------------------------------------------------------- #
# Poseidon2, width 16: Plonky3 0.8.0's standard Goldilocks and KoalaBear instances
# --------------------------------------------------------------------------- #


class _Poseidon2(NamedTuple):
    p: int
    alpha: int  # the S-box x^alpha
    initial: tuple[tuple[int, ...], ...]  # the round constants of the 4 + 4 full rounds
    internal: tuple[int, ...]  # and of the partial rounds
    final: tuple[tuple[int, ...], ...]
    diag: tuple[int, ...]  # the internal layer x_i <- d_i x_i + sum(x)


def _external(s: list[int]) -> list[int]:
    """circ(2 M4, M4, M4, M4), M4 = [[2,3,1,1],[1,2,3,1],[1,1,2,3],[3,1,1,2]]: M4 on
    each block of four lanes, then each lane gains the sum of the blocks' lanes at
    its position. Unreduced; an S-box or the internal layer reduces next."""
    out: list[int] = []
    for i in (0, 4, 8, 12):
        x0, x1, x2, x3 = s[i : i + 4]
        t01, t23 = x0 + x1, x2 + x3
        t0123 = t01 + t23
        t01123, t01233 = t0123 + x1, t0123 + x3
        out += (t01123 + t01, t01123 + 2 * x2, t01233 + t23, t01233 + 2 * x0)
    sums = [out[j] + out[j + 4] + out[j + 8] + out[j + 12] for j in range(4)]
    return [v + sums[i & 3] for i, v in enumerate(out)]


def _permute(state: list[int], h: _Poseidon2) -> list[int]:
    """The permutation (ePrint 2023/323): an external layer, 4 full rounds, the
    partial rounds (lane 0's S-box, then the internal layer), 4 full rounds."""
    p, alpha = h.p, h.alpha
    s = _external(state)
    for rc in h.initial:
        s = _external([pow(x + c, alpha, p) for x, c in zip(s, rc)])
    for c in h.internal:
        s[0] = pow(s[0] + c, alpha, p)
        total = sum(s)
        s = [(x * d + total) % p for x, d in zip(s, h.diag)]
    for rc in h.final:
        s = _external([pow(x + c, alpha, p) for x, c in zip(s, rc)])
    return [x % p for x in s]


def _rounds(flat: tuple[int, ...]) -> tuple[tuple[int, ...], ...]:
    return tuple(flat[i : i + 16] for i in range(0, len(flat), 16))


def _diagonal(p: int, entries: tuple[int | Fraction, ...]) -> tuple[int, ...]:
    return tuple(f.numerator * pow(f.denominator, -1, p) % p for f in map(Fraction, entries))


_KB_P = 0x7F00_0001  # the KoalaBear prime 2^31 - 2^24 + 1

# p3-goldilocks GOLDILOCKS_POSEIDON2_RC_16_EXTERNAL_INITIAL, _INTERNAL and _EXTERNAL_FINAL.
_GL_INITIAL = (
    0x15EBEA3FC73397C3, 0xD73CD9FBFE8E275C, 0x8C096BFCE77F6C26, 0x4E128F68B53D8FEA,
    0x29B779A36B2763F6, 0xFE2ADC6FB65ACD08, 0x8D2520E725AD0955, 0x1C2392B214624D2A,
    0x37482118206DCC6E, 0x2F829BED19BE019A, 0x2FE298CB6F8159B0, 0x2BBAD982DECCDBBF,
    0xBAD568B8CC60A81E, 0xB86A814265BAAD10, 0xBEC2005513B3ACB3, 0x6BF89B59A07C2A94,
    0xA25DEEB835E230F5, 0x3C5BAD8512B8B12A, 0x7230F73C3CB7A4F2, 0xA70C87F095C74D0F,
    0x6B7606B830BB2E80, 0x6CD467CFC4F24274, 0xFEED794DF42A9B0A, 0x8CF7CF6163B7DBD3,
    0x9A6E9DDA597175A0, 0xAA52295A684FAF7B, 0x017B811CC3589D8D, 0x55BFB699B6181648,
    0xC2CCAF71501C2421, 0x1707950327596402, 0xDD2FCDCD42A8229F, 0x8B9D7D5B27778A21,
    0xAC9A05525F9CF512, 0x2BA125C58627B5E8, 0xC74E91250A8147A5, 0xA3E64B640D5BB384,
    0xF53047D18D1F9292, 0xBAAEDDACAE3A6374, 0xF2D0914A808B3DB1, 0x18AF1A3742BFA3B0,
    0x9A621EF50C55BDB8, 0xC615F4D1CC5466F3, 0xB7FBAC19A35CF793, 0xD2B1A15BA517E46D,
    0x4A290C4D7FD26F6F, 0x4F0CF1BB1770C4C4, 0x548345386CD377F5, 0x33978D2789FDDD42,
    0xAB78C59DEB77E211, 0xC485B2A933D2BE7F, 0xBDE3792C00C03C53, 0xAB4CEFE8F893D247,
    0xC5C0E752EAB7F85F, 0xDBF5A76F893BAFEA, 0xA91F6003E3D984DE, 0x099539077F311E87,
    0x097EC52232F9559E, 0x53641BDF8991E48C, 0x2AFE9711D5ED9D7C, 0xA7B13D3661B5D117,
    0x5A0E243FE7AF6556, 0x1076FAE8932D5F00, 0x9B53A83D434934E3, 0xED3FD595A3C0344A,
)
_GL_INTERNAL = (
    0x28EFF4B01103D100, 0x60400CA3E2685A45, 0x1C8636BEB3389B84, 0xAC1332B60E13EFF0,
    0x2ADAFCC364E20F87, 0x79FFC2B14054EA0B, 0x3F98E4C0908F0A05, 0xCDB230BC4E8A06C4,
    0x1BCAF7705B152A74, 0xD9BCA249A82A7470, 0x91E24AF19BF82551, 0xA62B43BA5CB78858,
    0xB4898117472E797F, 0xB3228BCA606CDAA0, 0x844461051BCA39C9, 0xF3411581F6617D68,
    0xF7FD50646782B533, 0x6CA664253C18FB48, 0x2D2FCDEC0886A08F, 0x29DA00DD799B575E,
    0x47D966CC3B6E1E93, 0xDE884E9A17CED59E,
)
_GL_FINAL = (
    0xDACF46DC1C31A045, 0x5D2E3C121EB387F2, 0x51F8B0658B124499, 0x1E7DBD1DAA72167D,
    0x8275015A25C55B88, 0xE8521C24AC7A70B3, 0x6521D121C40B3F67, 0xAC12DE797DE135B0,
    0xAFA28EAD79F6ED6A, 0x685174A7A8D26F0B, 0xEFF92A08D35D9874, 0x3058734B76DD123A,
    0xFA55DCFBA429F79C, 0x559294D4324C7728, 0x7A770F53012DC178, 0xEDD8F7C408F3883B,
    0x39B533CF8D795FA5, 0x160EF9DE243A8C0A, 0x431D52DA6215FE3F, 0x54C51A2A2EF6D528,
    0x9B13892B46FF9D16, 0x263C46FCEE210289, 0xB738C96D25AABDC4, 0x5C33A5203996D38F,
    0x2626496E7C98D8DD, 0xC669E0A52785903A, 0xAECDE726C8AE1F47, 0x039343EF3A81E999,
    0x2615CEAF044A54F9, 0x7E41E834662B66E1, 0x4CA5FD4895335783, 0x64B334D02916F2B0,
    0x87268837389A6981, 0x034B75BCB20A6274, 0x58E658296CC2CD6E, 0xE2D0F759ACC31DF4,
    0x81A652E435093E20, 0x0B72B6E0172EAF47, 0x4AEC43CEC577D66D, 0xDE78365B028A84E6,
    0x444E19569ADC0EE4, 0x942B2451FA40D1DA, 0xE24506623EA5BD6C, 0x082854BF2EF7C743,
    0x69DBBC566F59D62E, 0x248C38D02A7B5CB2, 0x4F4E8F8C09D15EDB, 0xD96682F188D310CF,
    0x6F9A25D56818B54C, 0xB6CEFED606546CD9, 0x5BC07523DA38A67B, 0x7DF5A3C35B8111CF,
    0xAAA2CC5D4DB34BB0, 0x9E673FF22A4653F8, 0xBD8B278D60739C62, 0xE10D20F6925B8815,
    0xF6C87B91DD4DA2BF, 0xFED623E2F71B6F1A, 0xA0F02FA52A94D0D3, 0xBB5794711B39FA16,
    0xD3B94FBA9D005C7F, 0x15A26E89FAD946C9, 0xF3CB87DB8A67CF49, 0x400D2BF56AA2A577,
)
# p3-koala-bear KOALABEAR_POSEIDON2_RC_16_EXTERNAL_INITIAL, _INTERNAL and _EXTERNAL_FINAL.
_KB_INITIAL = (
    0x7EE56A48, 0x11367045, 0x12E41941, 0x7EBBC12B, 0x1970B7D5, 0x662B60E8, 0x3E4990C6, 0x679F91F5,
    0x350813BB, 0x00874AD4, 0x28A0081A, 0x18FA5872, 0x5F25B071, 0x5E5D5998, 0x5E6FD3E7, 0x5B2E2660,
    0x6F1837BF, 0x3FE6182B, 0x1EDD7AC5, 0x57470D00, 0x43D486D5, 0x1982C70F, 0x0EA53AF9, 0x61D6165B,
    0x51639C00, 0x2DEC352C, 0x2950E531, 0x2D2CB947, 0x08256CEF, 0x1A0109F6, 0x1F51FAF3, 0x5CEF1C62,
    0x3D65E50E, 0x33D91626, 0x133D5A1E, 0x0FF49B0D, 0x38900CD1, 0x2C22CC3F, 0x28852BB2, 0x06C65A02,
    0x7B2CF7BC, 0x68016E1A, 0x15E16BC0, 0x5248149A, 0x6DD212A0, 0x18D6830A, 0x5001BE82, 0x64DAC34E,
    0x5902B287, 0x426583A0, 0x0C921632, 0x3FE028A5, 0x245F8E49, 0x43BB297E, 0x7873DBD9, 0x3CC987DF,
    0x286BB4CE, 0x640A8DCD, 0x512A8E36, 0x03A4CF55, 0x481837A2, 0x03D6DA84, 0x73726AC7, 0x760E7FDF,
)
_KB_INTERNAL = (
    0x54DFEB5D, 0x7D40AFD6, 0x722CB316, 0x106A4573, 0x45A7CCDB, 0x44061375, 0x154077A5, 0x45744FAA,
    0x4EB5E5EE, 0x3794E83F, 0x47C7093C, 0x5694903C, 0x69CB6299, 0x373DF84C, 0x46A0DF58, 0x46B8758A,
    0x3241EBCB, 0x0B09D233, 0x1AF42357, 0x1E66CEC2,
)
_KB_FINAL = (
    0x43E7DC24, 0x259A5D61, 0x27E85A3B, 0x1B9133FA, 0x343E5628, 0x485CD4C2, 0x16E269F5, 0x165B60C6,
    0x25F683D9, 0x124F81F9, 0x174331F9, 0x77344DC5, 0x5A821DBA, 0x5FC4177F, 0x54153BF5, 0x5E3F1194,
    0x3BDBF191, 0x088C84A3, 0x68256C9B, 0x3C90BBC6, 0x6846166A, 0x03F4238D, 0x463335FB, 0x5E3D3551,
    0x6E59AE6F, 0x32D06CC0, 0x596293F3, 0x6C87EDB2, 0x08FC60B5, 0x34BCCA80, 0x24F007F3, 0x62731C6F,
    0x1E1DB6C6, 0x0CA409BB, 0x585C1E78, 0x56E94EDC, 0x16D22734, 0x18E11467, 0x7B2C3730, 0x770075E4,
    0x35D1B18C, 0x22BE3DB5, 0x4FB1FBB7, 0x477CB3ED, 0x7D5311C6, 0x5B62AE7D, 0x559C5FA8, 0x77F15048,
    0x3211570B, 0x490FEF6A, 0x77EC311F, 0x2247171B, 0x4E0AC711, 0x2EDF69C9, 0x3B5A8850, 0x65809421,
    0x5619B4AA, 0x362019A7, 0x6BF9D4ED, 0x5B413DFF, 0x617E181E, 0x5E7AB57B, 0x33AD7833, 0x3466C7CA,
)

# The challenge's instance: x^7, 22 partial rounds, MATRIX_DIAG_16_GOLDILOCKS.
_GL16 = _Poseidon2(
    p=_P,
    alpha=7,
    initial=_rounds(_GL_INITIAL),
    internal=_GL_INTERNAL,
    final=_rounds(_GL_FINAL),
    diag=_diagonal(
        _P,
        (-2, 1, 2, Fraction(1, 2), 3, 4, Fraction(-1, 2), -3, -4, Fraction(1, 2**3),
         Fraction(1, 2**4), Fraction(1, 2**5), Fraction(-1, 2**3), Fraction(-1, 2**4),
         Fraction(-1, 2**5), Fraction(1, 2**32)),
    ),
)
# The order message's: x^3, 20 partial rounds, the diagonal of KoalaBearInternalLayerParameters.
_KB16 = _Poseidon2(
    p=_KB_P,
    alpha=3,
    initial=_rounds(_KB_INITIAL),
    internal=_KB_INTERNAL,
    final=_rounds(_KB_FINAL),
    diag=_diagonal(
        _KB_P,
        (-2, 1, 2, Fraction(1, 2), 3, 4, Fraction(-1, 2), -3, -4, Fraction(1, 2**8),
         Fraction(1, 2**3), Fraction(1, 2**24), Fraction(-1, 2**8), Fraction(-1, 2**3),
         Fraction(-1, 2**4), Fraction(-1, 2**24)),
    ),
)

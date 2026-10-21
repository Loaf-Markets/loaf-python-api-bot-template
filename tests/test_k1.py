"""K1 signing, offline: K1's known answers, the order checks the web app's signer
makes, and the curve's own vectors.

``k1-sign.json`` is k1-sign's ``kat/sign.json`` byte for byte, the file the
matching engine and the web app run too: a match here means a signature made by
this SDK verifies there. Its withdraw and batch vectors are out of scope.
"""

from __future__ import annotations

import hashlib
import json
import os
import pickle
import re
from pathlib import Path
from typing import Any

import pytest

from loaf import k1
from loaf.k1 import K1Error, key_from_seed, order_message, public_key, sign, verify

KAT_FILE = Path(__file__).with_name("k1-sign.json")
KAT = json.loads(KAT_FILE.read_text())

# The group order n (k1-sign curve::N).
N = 0x7FFFFFFD80000007_7FFFFFF100000016_7FFFFFE6CFB80639_E8885C39D724A09C_E80FD996948BFFE1
# An order's wire keys in message order (k1-sign tests/kat.rs ORDER_KEYS).
ORDER_KEYS = (
    "l2ChainId", "l2MarketIndex", "l2BaseAssetId", "l2QuoteAssetId", "isAsk", "price",
    "l2FeeRateCap", "l2AccountIndex", "l2OrderIndex", "l2Epoch", "quantity", "l2ExpiryMs",
)
# The field each message refusal code names, as the engine names it (me_rules::msg_refusal).
REFUSED_FIELD = {
    "E_MSG_CHAIN_ID": "l2_chain_id",
    "E_MSG_MARKET": "l2_market_index",
    "E_MSG_BASE_ASSET": "l2_base_asset_id",
    "E_MSG_QUOTE_ASSET": "l2_quote_asset_id",
    "E_MSG_FEE_CAP": "l2_fee_rate_cap",
    "E_MSG_ACCOUNT": "l2_account_index",
    "E_MSG_ORDER_INDEX": "l2_order_index",
    "E_MSG_EPOCH": "l2_epoch",
    "E_MSG_BASE_AMOUNT": "quantity",
    "E_MSG_EXPIRY": "l2_expiry_ms",
}
# The engine's widths of an order's fields, in its parse order (SignedOrder::parse), and the chain
# id's, below the KoalaBear modulus, which comes last.
WIDTHS = [
    ("l2_account_index", 0, 2**48 - 1),
    ("l2_market_index", 0, 4095),
    ("l2_order_index", 1, 2**40 - 1),
    ("l2_epoch", 0, 2**48 - 1),
    ("quantity", 1, 2**48 - 1),
    ("price", 1, 2**32 - 1),
    ("l2_fee_rate_cap", 0, 2**20 - 1),
    ("l2_expiry_ms", 0, 2**48 - 1),
    ("l2_base_asset_id", 1, 62),
    ("l2_quote_asset_id", 1, 62),
    ("l2_chain_id", 0, 2130706432),
]
ORDER_REFUSALS = [v for v in KAT["message_refusals"] if v["kind"] == "order"]
SK0, PK0 = bytes.fromhex(KAT["keys"][0]["sk"]), bytes.fromhex(KAT["keys"][0]["pk"])


def snake(key: str) -> str:
    return re.sub("[A-Z]", lambda c: "_" + c.group().lower(), key)


def fields(vector: dict[str, Any]) -> dict[str, Any]:
    """A vector's order fields as order_message's keywords: its wire keys in snake case."""
    return {snake(k): v for k, v in vector.items() if k not in ("m", "kind", "code")}


ORDER0 = fields(KAT["orders"][0])


def refused(code: str, call: Any, *args: Any, **kwargs: Any) -> K1Error:
    with pytest.raises(K1Error) as info:
        call(*args, **kwargs)
    assert info.value.code == code, str(info.value)
    return info.value


# --------------------------------------------------------------------------- #
# K1's known answers
# --------------------------------------------------------------------------- #


def test_kat_file_is_k1s():
    digest = hashlib.sha256(KAT_FILE.read_bytes()).hexdigest()
    assert digest == "9e3bb38ca080f1046d087fe7b970bd8514a3643628e8d17b481cccb4ee8fed68"


@pytest.mark.parametrize("v", KAT["keys"], ids=lambda v: v["seed"][:8])
def test_kat_keys(v):
    sk = key_from_seed(bytes.fromhex(v["seed"]))
    assert sk.hex() == v["sk"]
    assert public_key(sk).hex() == v["pk"]


@pytest.mark.parametrize("v", KAT["signatures"], ids=lambda v: v["sig"][:8])
def test_kat_signatures(v):
    m = bytes.fromhex(v["m"])
    s, e = sign(bytes.fromhex(v["sk"]), m, bytes.fromhex(v["aux"]))
    assert (s + e).hex() == v["sig"]
    verify(bytes.fromhex(v["pk"]), m, s, e)


def test_kat_signatures_cover_zero_aux():
    assert len(KAT["signatures"]) == 9
    assert any(v["aux"] == "00" * 32 for v in KAT["signatures"])


@pytest.mark.parametrize("v", KAT["refusals"], ids=lambda v: v["code"])
def test_kat_refusals(v):
    if v["code"] == "MessageNoncanonical":  # sign's own: a message limb >= p is never signed
        args = (bytes.fromhex(v["sk"]), bytes.fromhex(v["m"]), bytes.fromhex(v["aux"]))
        refused(v["code"], sign, *args)
    else:
        pk, m, sig = (bytes.fromhex(v[key]) for key in ("pk", "m", "sig"))
        refused(v["code"], verify, pk, m, sig[:40], sig[40:])


@pytest.mark.parametrize("v", KAT["orders"], ids=["typical", "maxima", "zeros"])
def test_kat_order_messages(v):
    assert k1._order_message(*(v[key] for key in ORDER_KEYS)).hex() == v["m"]


def test_kat_orders_outside_the_engines_widths_are_refused_by_name():
    # Order 0 is one the engine admits; 1 and 2 pin K1's encoding at its edges, which the engine,
    # and so the web app's signer, refuse first.
    assert order_message(**ORDER0).hex() == KAT["orders"][0]["m"]
    for v, field in ((KAT["orders"][1], "l2_market_index"), (KAT["orders"][2], "l2_order_index")):
        error = refused("Width", order_message, **fields(v))
        assert str(error) == f"l2 width: {field} out of range"


@pytest.mark.parametrize("v", ORDER_REFUSALS, ids=lambda v: v["code"])
def test_kat_message_refusals(v):
    if v["code"] == "E_UNKNOWN_KEY":  # a key the wire does not have (serde's deny_unknown_fields)
        with pytest.raises(TypeError, match="l2_api_key_index"):
            order_message(**fields(v))
    else:
        error = refused("Width", order_message, **fields(v))
        assert str(error) == f"l2 width: {REFUSED_FIELD[v['code']]} out of range"


def test_kat_message_refusals_cover_every_order_field():
    assert {v["code"] for v in ORDER_REFUSALS} == {*REFUSED_FIELD, "E_UNKNOWN_KEY"}


# --------------------------------------------------------------------------- #
# Orders as the web app signs them
# --------------------------------------------------------------------------- #


def test_kat_order_signs_as_the_web_app_signs_it():
    # loafx-frontend signer.test.ts L2-SG-04: the wasm signer's bytes for KAT order 0, key 0, aux 0.
    m = order_message(**ORDER0)
    s, e = sign(SK0, m, bytes(32))
    assert s.hex() + e.hex() == (
        "ebe0fc1a67c075b88a5db55fb2bce61a7abc9cb127607a1ce99015624578ca923c384694ad02b948"
        "8f5ae71647b2a5e0f733b5072a26360648ad985b4c72344cb782d6aa5d8eaf81e6813cfaeafa6a73"
    )
    verify(PK0, m, s, e)


@pytest.mark.parametrize(
    "edit",
    [
        {"l2_chain_id": 1819238759}, {"l2_market_index": 8}, {"l2_base_asset_id": 2},
        {"l2_quote_asset_id": 4}, {"is_ask": True}, {"price": 123456788}, {"l2_fee_rate_cap": 1001},
        {"l2_account_index": 6}, {"l2_order_index": 10}, {"l2_epoch": 3}, {"quantity": 999999},
        {"l2_expiry_ms": 1700000001},
    ],
    ids=lambda edit: next(iter(edit)),
)
def test_every_signed_field_moves_the_message(edit):
    assert order_message(**{**ORDER0, **edit}).hex() != KAT["orders"][0]["m"]


@pytest.mark.parametrize("field, low, high", WIDTHS)
def test_order_field_widths(field, low, high):
    # The other amount is 1 while one is at its maximum, so that K1's bounds hold.
    other = {"price": "quantity", "quantity": "price"}.get(field)
    base = {**ORDER0, **({other: 1} if other else {})}
    for value in (low, high):
        assert len(order_message(**{**base, field: value})) == 32
    for value in (low - 1, high + 1):
        error = refused("Width", order_message, **{**base, field: value})
        assert str(error) == f"l2 width: {field} out of range"


def test_k1_bounds_on_the_orders_largest_fill():
    # The quote, quantity x price / 10 rounded down: 2^48 - 1 (from 2^48 - 0.8) fits, 2^48 does not.
    no_fee = {**ORDER0, "l2_fee_rate_cap": 0}
    assert len(order_message(**{**no_fee, "quantity": 255886342464232, "price": 11})) == 32
    over = {**no_fee, "quantity": 2**24, "price": 167772160}
    error = refused("QuoteAboveK1", order_message, **over)
    assert str(error) == (
        "l2 k1 bound: quantity x price / 10 is not below 2^48 (K1's 48-bit fill quote)"
    )
    # The fee bound, quantity x price x cap / 10^7 rounded up: 2^48 - 1 (from 2^48 - 1.9) fits,
    # 2^48 (from 2^48 - 0.8) does not.
    fee = {**ORDER0, "price": 11, "l2_fee_rate_cap": 1000000}
    assert len(order_message(**{**fee, "quantity": 255886342464231})) == 32
    error = refused("FeeAboveK1", order_message, **{**fee, "quantity": 255886342464232})
    assert str(error) == (
        "l2 k1 bound: the fee-rate cap's fee on quantity x price is not below 2^48 (K1 fees)"
    )


def test_the_first_refusal_is_the_engines():
    # Every field out of range: each is named in turn, in the engine's order, as the ones before it
    # are put right.
    order = {**ORDER0, **{field: high + 1 for field, _, high in WIDTHS}}
    for field, _, _ in WIDTHS:
        assert str(refused("Width", order_message, **order)) == f"l2 width: {field} out of range"
        order[field] = ORDER0[field]
    # The widths come before K1's bounds, and those before the chain id.
    big = {**ORDER0, "quantity": 2**24, "price": 167772160}
    refused("Width", order_message, **{**big, "l2_quote_asset_id": 63})
    refused("QuoteAboveK1", order_message, **{**big, "l2_chain_id": 2130706433})


@pytest.mark.parametrize(
    "edit", [{"is_ask": 1}, {"price": True}, {"quantity": 10.0}, {"l2_chain_id": "1"}]
)
def test_order_fields_have_their_json_types(edit):
    with pytest.raises(TypeError, match=next(iter(edit))):
        order_message(**{**ORDER0, **edit})


# --------------------------------------------------------------------------- #
# The signer's own refusals, and signatures that do not verify
# --------------------------------------------------------------------------- #


def test_signer_refusals():
    m = order_message(**ORDER0)
    for seed in (bytes(31), bytes(33)):
        refused("Seed", key_from_seed, seed)
    for aux in (bytes(31), bytes(33)):
        refused("Aux", sign, SK0, m, aux)
    for sk in (SK0[1:], SK0 + b"\0", bytes(40), N.to_bytes(40, "little"), b"\xff" * 40):
        refused("SecretKey", public_key, sk)
        refused("SecretKey", sign, sk, m, bytes(32))
    assert len(public_key((N - 1).to_bytes(40, "little"))) == 40  # the largest secret key
    for message in (m[1:], m + b"\0"):
        refused("MessageNoncanonical", sign, SK0, message, bytes(32))
    error = refused("SecretKey", public_key, bytes(40))
    assert str(error) == "l2 signer: the secret key must be 40 bytes of a scalar in [1, n)"
    copy = pickle.loads(pickle.dumps(error))
    assert (copy.code, str(copy)) == (error.code, str(error))


def test_a_signature_binds_its_key_and_message():
    m = order_message(**ORDER0)
    s, e = sign(SK0, m, os.urandom(32))
    verify(PK0, m, s, e)
    refused("Sig", verify, bytes.fromhex(KAT["keys"][1]["pk"]), m, s, e)
    refused("Sig", verify, PK0, order_message(**{**ORDER0, "price": 123456788}), s, e)
    s_plus_1 = ((int.from_bytes(s, "little") + 1) % N).to_bytes(40, "little")
    refused("Sig", verify, PK0, m, s_plus_1, e)
    refused("Noncanonical", verify, PK0, m[1:], s, e)
    refused("Noncanonical", verify, PK0[1:], m, s, e)


def test_aux_reaches_the_nonce():
    m = order_message(**ORDER0)
    aux = os.urandom(32)
    assert sign(SK0, m, aux) == sign(SK0, m, aux)
    assert sign(SK0, m, aux) != sign(SK0, m, bytes(a ^ 1 for a in aux))


def test_fresh_keys_sign_and_verify():
    for _ in range(3):
        sk = key_from_seed(os.urandom(32))
        m = order_message(**{**ORDER0, "l2_order_index": int.from_bytes(os.urandom(4), "big") + 1})
        verify(public_key(sk), m, *sign(sk, m, os.urandom(32)))


# --------------------------------------------------------------------------- #
# The curve and the challenge's permutation
# --------------------------------------------------------------------------- #

# Pornin's reference vectors (github.com/pornin/ecgfp5 python/ecGFp5.py, as K1 pins them in k1-sig
# tests/curve_kat.rs) as 40-byte encodings: P1, P2 = E P1, P3 = P1 + P2, P4 = 2 P1, P5 = 2 P2,
# P6 = 2 P1 + P2 and P7 = P1 + 2 P2; then encodings of no point.
POINTS = [
    "599deb76146104ae660d2722c8d670d700a6e2bfa4af71d1d30bba141eed309e17f8d0f7b7061498",
    "b80dce0616c1ae98729d0cec0f34f7ec961f3ad01e35b1365bd6f6f481ed69d02fce8f992f63004d",
    "88782ad953116d03a22863891bb80344093be693ccc1ae65bd72fc822ab986c1dd5a098e41797e2a",
    "62707a003ce4d36fe6ce33cc369f9fde0771bcea957e5e67e866d03e36f52c9ca20b135c3cfe7056",
    "aaf9948c3eaf09927f59a52e0b2eb89a0ba7a214b226af7df29fde285b7ba4b44adab3b41857e7e4",
    "b42731cc8e3f2b00c6dd4763928c6a5bf51bb4f4d18e4fec632a719d6f50a0eec286131f5bb2908b",
    "8d7a7565e49bbd829b62d9f1194f2b41e60e73f78798d6c8a88ff7d46d46e5b9b9ca3cf8d7a207e3",
]
E = 0x64E443325F264A5C_F3F132FCACC859C5_170E010D29F34525_DBEACF85F04F49FA_4AF128FC27C028A4
NOT_POINTS = [
    "e44b0bc4331927bcbb359652c87774d9dc6f1737312f71762516b2bca4efeeadacdb757780e03146",
    "1d9fe4f92fbfdf0186bf70eb0e5edaefb9db5b04fc53fbb5b782412d6e7df11636e23bf1c5803707",
    "23d2e867d9845e5d0c7e7729af7f43efc9a43e1862fae136b051a46f8498ea0d4d65a338b22c9c3b",
    "5ce2b238e99f8b3fd2bc245dd018aa352de438fcb5d5556a52cc1fec4fe0e383bd4915339439e3a9",
    "2a1b034a006466ea41edb8eedf439f5557c0f5222f99f986ae202db996bef252737648364882eb9d",
    "ba63143d191633bf58bd23feb4c736cc81c01bfd777ffff8c0d6c7cac91ee35522ed1cbab62c92d6",
]


def limbs(encoding: str) -> k1._Fp5:
    w = k1._key_limbs(bytes.fromhex(encoding))
    assert w is not None
    return w


def decoded(encoding: str) -> k1._Point:
    p = k1._decode(limbs(encoding))
    assert p is not None
    return p


def encoded(p: k1._Point) -> str:
    return k1._to_bytes(k1._encode(p)).hex()


def test_pornins_vectors():
    p1, p2, p3, p4, p5, p6, p7 = (decoded(w) for w in POINTS)
    assert [encoded(p) for p in (p1, p2, p3, p4, p5, p6, p7)] == POINTS
    assert encoded(k1._mul_var(p1, E)) == POINTS[1]
    assert encoded(k1._add_points(p1, p2)) == POINTS[2]
    assert encoded(k1._double(p1, 1)) == POINTS[3]
    assert encoded(k1._double(p2, 1)) == POINTS[4]
    assert encoded(k1._add_points(p4, p2)) == POINTS[5]
    assert encoded(k1._add_points(p1, p5)) == POINTS[6]
    assert encoded(k1._NEUTRAL) == "00" * 40
    assert all(k1._decode(limbs(w)) is None for w in NOT_POINTS)


def test_doublings_and_multiplications_agree():
    q = once = decoded(POINTS[2])
    for times in range(1, 9):
        once = k1._double(once, 1)
        assert encoded(k1._double(q, times)) == encoded(once)
    g = decoded("04" + "00" * 39)
    for k in (0, 1, 2, 31, 32, 33, N - 1, N, 2**319, 2**320 - 1, E):
        assert encoded(k1._mul_g(k)) == encoded(k1._mul_var(g, k)), k
    assert encoded(k1._mul_g(N)) == "00" * 40


def test_poseidon2_goldilocks_upstream_known_answer():
    # p3-goldilocks 0.8.0's test_default_goldilocks_poseidon2_width_16, which k1-sign pins.
    state = [
        0x4D3F967FAB9D4979, 0x57E1FBA55677697E, 0x57429A86E75A3774, 0x31D379F3A592B5EB,
        0x497232E1B648E3F1, 0x325A7DB57173C39E, 0xA802252D78BEE916, 0x8920F55E154ADEF8,
        0xA1225BC9C7913658, 0xD687BE5097FFD038, 0x89F514EF0C913E48, 0x21FD4A9CF548CD84,
        0x570A1586ADA436FF, 0x46BFBF38CCD740AE, 0x23651B3F3AB26484, 0xE90F3B02127FA552,
    ]
    assert k1._permute(state, k1._GL16) == [
        0xF0F7717837C7032A, 0xF12FBCC838FEB15B, 0xD8661F6FA4165AD8, 0x351CDC546760D1A9,
        0x99474334BF02445F, 0x46FC4E9CEB376D6A, 0x4601808321FCD920, 0xC58BFD0342DC60DF,
        0xB7F3ACD43F3C029C, 0x5C7AFA6A6997DFC5, 0xECBEF8B82906C887, 0xD490E3B4E945D87C,
        0x31866766B83EBE0B, 0xB32D52F6E7A5BEA2, 0x9522431667B3C5F9, 0xEAF5638A69518F65,
    ]

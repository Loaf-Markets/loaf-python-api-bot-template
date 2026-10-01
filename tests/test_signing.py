"""Order signing and pricing, offline: the reference EIP-712 test vectors, nonces,
key handling, the worst-price rule and the grid validators.

The vectors are shared with the exchange's own reference client; a match here
means an order signed by this SDK recovers to the same address there.
"""

from __future__ import annotations

import copy
import enum
import pickle
import re
import time
from decimal import Decimal
from fractions import Fraction
from typing import Any

import pytest
from eth_account.messages import encode_typed_data
from eth_utils.crypto import keccak

import loaf
from loaf.money import validate_price, validate_quantity, validate_slippage_bps, worst_price
from loaf.signing import (
    LOAF_EIP712_DOMAIN,
    ORDER_EIP712_TYPES,
    OrderSigner,
    _order_amounts,
    new_order_nonce,
)

from conftest import (
    CONTRACT,
    HARDHAT_ADDRESS,
    HARDHAT_KEY,
    VECTOR_NONCE,
    VECTOR_SIGNATURE,
    recover,
)


@pytest.fixture(scope="module")
def signer() -> OrderSigner:
    return OrderSigner(HARDHAT_KEY)


def sign(signer: OrderSigner, **overrides) -> str:
    """Sign the reference vector's order, with any terms replaced."""
    terms: dict[str, Any] = dict(
        contract_address=CONTRACT, side="BUY", price=160, quantity=1.5, nonce=VECTOR_NONCE
    )
    terms.update(overrides)
    return signer.sign_order(**terms)


def _hex(value: bytes) -> str:
    return value.hex().replace("0x", "")  # hexbytes 0.x prefixes, 1.x does not


# --------------------------------------------------------------------------- #
# Test vectors
# --------------------------------------------------------------------------- #


def test_vector_order_signature(signer):
    assert signer.address == HARDHAT_ADDRESS
    assert sign(signer) == VECTOR_SIGNATURE


def test_vector_intermediates():
    nonce_hash = keccak(text=VECTOR_NONCE)
    assert _hex(nonce_hash) == "c88457d1082193cc9487172632402d5ab0e3929a5a9591dcfb8088a91d8e18b5"
    assert _order_amounts(Decimal(160), Decimal("1.5")) == (1500000000000000000, 240000000)

    signable = encode_typed_data(
        LOAF_EIP712_DOMAIN,
        ORDER_EIP712_TYPES,
        {
            "nonceHash": nonce_hash,
            "propertyToken": CONTRACT,
            "tokenAmount": 1500000000000000000,
            "paymentAmount": 240000000,
            "deadline": 0,
            "isBuying": True,
        },
    )
    # The domain separator is the same on every environment (no verifyingContract).
    separator = _hex(signable.header)
    assert separator == "2b6f3594d46b427dc7e3b67ca85df8da4671d4687e399082b50920b4e470728e"
    digest = keccak(b"\x19" + signable.version + signable.header + signable.body)
    assert _hex(digest) == "3d975b251c18f4641a136a7308d2d61e36b09b622a659fc335ad5e01fa33a708"


def test_leg_and_conditional_vectors(signer):
    # A TP/SL leg is signed as a SELL of the parent's quantity at the leg's price.
    tp = sign(signer, side="SELL", price=176.4, nonce="0192a3b4c5d6001122334455667788aa")
    assert tp == (
        "0x2b1fc4b765b18124412bb23fe799134197023c48629db54b2b932dbbaed63bc8"
        "60aa74325084183d63e64483e87cf51a3ac64200ed7bb19b2b6dab25c96d30ce1b"
    )
    sl = sign(signer, side="SELL", price=137.2, nonce="0192a3b4c5d6001122334455667788bb")
    assert sl == (
        "0xa73ae46a6a8d510ff90033eedc910665cbb8a4910fccdb8fafb8de5add4c643d"
        "4c58260a281d36d75c1f2f238e069a6f7ca8cdde772b31dab6041f7ffa6507201c"
    )
    # A conditional signs its side, price and quantity; type and trigger are not signed.
    conditional = sign(signer, side="SELL", price=88.2, quantity=5)
    assert conditional == (
        "0xea0b762f5130d35fe60cb637cb86668d5df3c7b6ae57c7760c2337adf77849e4"
        "709e3c76271176bf90ad09063749ca5d52974cccdbe9bea48d150ff347cbfdcd1b"
    )


def test_extra_vectors(signer):
    # A: the smallest order the exchange accepts.
    assert _order_amounts(Decimal("0.01"), Decimal("0.1")) == (100000000000000000, 1000)
    assert sign(signer, price=0.01, quantity=0.1, nonce="01a0c4506c0000000000000000000000") == (
        "0x0c53438bb47973f43b88852ed7105ae381e7271b5bcebc584b1635bea1f0fb32"
        "5cf8e02d2c724e65a5dcb3b9f813279094285942a7b94ee2dc3b60967f7bd1c81b"
    )

    # B: another key (a public test key — never use for real funds) and a lowercase contract.
    signer_b = OrderSigner("0x" + "11" * 32)
    assert signer_b.address == "0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A"
    assert _order_amounts(Decimal("167.49"), Decimal("47.3")) == (47300000000000000000, 7922277000)
    assert signer_b.sign_order(
        contract_address="0xe7f1725e7734ce288f8367e1bb143e90bb3f0512",
        side="SELL",
        price=167.49,
        quantity=47.3,
        nonce="01a0c4506ca3000000000000000000a3",
    ) == (
        "0x2137d2c5b95926dc6f9ac406beaf3c69526bbca027f9e225d0550ab31d8021c0"
        "05a87bd86b3fffd7738ddb08aeb4c2fc482abbe0ca127604fce08d19f7fe39aa1c"
    )

    # C: the largest price and quantity (public test key — never use for real funds).
    signer_c = OrderSigner("0x" + "22" * 32)
    assert signer_c.address == "0x1563915e194D8CfBA1943570603F7606A3115508"
    assert _order_amounts(Decimal(10**9), Decimal(10**9)) == (10**27, 10**24)
    assert signer_c.sign_order(
        contract_address="0xe7f1725E7734CE288F8367e1Bb143E90bb3F0512",
        side="SELL",
        price=1e9,
        quantity=1e9,
        nonce="01a0c4506cef000000000000000000ef",
    ) == (
        "0xabf5583892c8d147d8575c4458645061677c0de30d3693331f2517d6c51a4e0c"
        "74f6955bf65f34f4b05dd7a7084cc68fe2999a7bcc04cf56533550867bb5cb911b"
    )


def test_price_float_scaling_would_truncate(signer):
    # 2.01 * 1000 == 2009.9999999999998 in binary floating point: the scaling must be exact.
    assert _order_amounts(Decimal("2.01"), Decimal("10")) == (10**19, 20100000)
    terms = {"side": "BUY", "price": 2.01, "quantity": 10, "nonce": VECTOR_NONCE}
    assert recover(terms, sign(signer, **terms)) == HARDHAT_ADDRESS


# --------------------------------------------------------------------------- #
# Signer behaviour
# --------------------------------------------------------------------------- #


def test_signature_format_and_determinism(signer):
    signature = sign(signer, side="SELL", price=167.49, quantity=47.3)
    assert re.fullmatch(r"0x[0-9a-f]{128}1[bc]", signature)  # v = 27 / 28
    assert sign(signer, side="SELL", price=167.49, quantity=47.3) == signature


def test_contract_casing_invariant(signer):
    lower = "0xe7f1725e7734ce288f8367e1bb143e90bb3f0512"
    spellings = (lower, "0xe7f1725E7734CE288F8367e1Bb143E90bb3F0512", "0x" + lower[2:].upper())
    assert len({sign(signer, contract_address=c) for c in spellings}) == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"side": "buy"},
        {"nonce": "XYZ"},
        {"nonce": VECTOR_NONCE[:31]},
        {"nonce": VECTOR_NONCE.upper()},
        {"nonce": VECTOR_NONCE + "\n"},
        {"contract_address": "0x" + "0" * 40},
        {"contract_address": "0x" + "1" * 39},
        {"contract_address": None},
        {"price": 1.234},
        {"quantity": 1.25},
    ],
)
def test_sign_order_input_checks(signer, overrides):
    with pytest.raises(loaf.LoafValidationError) as info:
        sign(signer, **overrides)
    assert info.value.status_code == 0


def test_new_order_nonce():
    nonce = new_order_nonce()
    assert re.fullmatch(r"[0-9a-f]{32}", nonce)
    assert abs(int(nonce[:12], 16) - time.time() * 1000) < 2000  # Unix ms in the first 12 hex
    assert new_order_nonce(0x0192A3B4C5D6)[:12] == "0192a3b4c5d6"
    assert len({new_order_nonce() for _ in range(10_000)}) == 10_000


def test_key_parsing(monkeypatch):
    for spelling in (
        HARDHAT_KEY,
        HARDHAT_KEY[2:],
        "0X" + HARDHAT_KEY[2:].upper(),
        "  " + HARDHAT_KEY + "\n",
    ):
        assert OrderSigner(spelling).address == HARDHAT_ADDRESS

    # Malformed, zero, and at or above the curve order: refused without echoing the key.
    for bad in ("nothex", HARDHAT_KEY[:-2], "", "0x" + "0" * 64, "0x" + "f" * 64):
        with pytest.raises(loaf.LoafConfigError) as info:
            OrderSigner(bad)
        message = str(info.value)
        if bad:
            assert bad not in message and bad.replace("0x", "") not in message
        assert info.value.__cause__ is None and info.value.__context__ is None

    # A good key eth-keys cannot load (here: a broken signing backend) fails the same way.
    monkeypatch.setenv("ECC_BACKEND_CLASS", "no.such.Backend")
    with pytest.raises(loaf.LoafConfigError) as info:
        OrderSigner(HARDHAT_KEY)
    assert info.value.__cause__ is None and info.value.__context__ is None


def test_signer_repr_and_pickle(signer):
    text = repr(signer)
    assert HARDHAT_ADDRESS in text
    assert HARDHAT_KEY not in text and HARDHAT_KEY[2:] not in text
    for leak in (pickle.dumps, copy.copy, copy.deepcopy):
        with pytest.raises(TypeError):
            leak(signer)


# --------------------------------------------------------------------------- #
# Pricing and validation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "reference, side, bps, expected",
    [
        (160, "BUY", 200, 163.20),
        (160, "SELL", 200, 156.80),
        (120, "SELL", 200, 117.60),
        (90, "SELL", 200, 88.20),
        (130, "BUY", 200, 132.60),
        (120, "SELL", 100, 118.80),
        (90, "SELL", 500, 85.50),
        (0.25, "SELL", 200, 0.25),
        (0.75, "BUY", 200, 0.77),
        (100.005, "BUY", 50, 100.51),
        (171.555, "BUY", 0, 171.56),  # half-up to the cent, whatever the side
        (171.555, "SELL", 0, 171.56),
        (0.004, "SELL", 200, 0.01),  # never below 0.01
        (0.01, "SELL", 9999, 0.01),
        (999999999.99, "BUY", 200, 1000000000.00),  # capped at the exchange's maximum
        (10**30, "SELL", 200, 1000000000.00),
        (Fraction(10**400), "BUY", 200, 1000000000.00),  # beyond float range: capped the same
    ],
)
def test_worst_price_table(reference, side, bps, expected):
    assert worst_price(reference, side, bps) == expected


def test_worst_price_needs_max_slippage_and_takes_enum_side():
    assert worst_price(160, loaf.OrderSide.BUY, loaf.DEFAULT_MAX_SLIPPAGE_BPS) == 163.2
    assert worst_price(reference_price=160, side="SELL", max_slippage_bps=100) == 158.4
    assert loaf.DEFAULT_MAX_SLIPPAGE_BPS == 200
    # No default: sizing must name the tolerance the client signs with (client.max_slippage_bps).
    with pytest.raises(TypeError):
        worst_price(160, "BUY")  # type: ignore[call-arg]


@pytest.mark.parametrize(
    "reference, side, bps",
    [
        (0, "BUY", 200),
        (-1, "BUY", 200),
        (float("nan"), "BUY", 200),
        (True, "BUY", 200),
        ("160", "BUY", 200),
        (160, "BUY", -1),
        (160, "BUY", 10_000),
        (160, "BUY", 1.5),
        (160, "BUY", True),
        (160, "buy", 200),
    ],
)
def test_worst_price_rejects(reference, side, bps):
    with pytest.raises(loaf.LoafValidationError):
        worst_price(reference, side, bps)


def test_validate_returns_grid_decimal():
    assert validate_price(167.49) == Decimal("167.49")
    assert validate_price(Decimal("167.490")) == Decimal("167.49")
    quantity = validate_quantity(10)
    assert quantity == Decimal("10.0") and str(quantity) == "10.0"
    # An IntEnum is read as its integer value (its str() is 'Lot.TEN' before Python 3.11).
    lot = enum.IntEnum("Lot", {"TEN": 10})
    assert str(validate_price(lot.TEN)) == "10.00" and str(validate_quantity(lot.TEN)) == "10.0"


def test_validate_rejects():
    bad: Any  # deliberately wrong types among them
    for bad in (
        1.234,
        0.001,
        0.1 + 0.2,
        0,
        -1,
        float("nan"),
        float("inf"),
        float("-inf"),
        True,
        "1",
        None,
        1e9 + 0.01,
        1e300,
        Fraction(10**400),  # beyond float range: still a validation error
    ):
        with pytest.raises(loaf.LoafValidationError):
            validate_price(bad)
    for bad in (1.25, 0.05, 1e9 + 0.1, 0, float("nan")):
        with pytest.raises(loaf.LoafValidationError):
            validate_quantity(bad)
    for bad in (-1, 10_000, 2.5, True):
        with pytest.raises(loaf.LoafValidationError):
            validate_slippage_bps(bad)

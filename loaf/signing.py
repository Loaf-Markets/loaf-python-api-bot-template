"""EIP-712 order signing: what your agent key signs for every order you place.

Every order is signed on your machine with your agent private key
(``agent_private_key=`` / ``$LOAF_AGENT_PRIVATE_KEY``), and the exchange checks
that signature instead of your API token. The key never leaves this process.

The SDK calls this for you on every placement. It is public so you can check it
against the test vectors in ``tests/test_signing.py``, or sign requests of your
own::

    signer = OrderSigner("0x...")
    signer.sign_order(contract_address=..., side="BUY", price=160, quantity=1.5,
                      nonce=new_order_nonce())

What is signed is an ``Order`` in the ``LoafSettlement`` domain over the
property's token contract (``market.property(ticker).property.contractAddress``):
the nonce's keccak hash, the quantity in token wei, the payment in USDC wei,
deadline 0 and the side. A TP/SL leg is signed the same way as a SELL of the
parent's quantity at the leg's price; trigger prices are not signed.
"""

from __future__ import annotations

import re
import secrets
import time
from decimal import Decimal, localcontext
from typing import Any

from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_keys.datatypes import PrivateKey
from eth_utils.address import to_checksum_address
from eth_utils.crypto import keccak

from .exceptions import LoafConfigError, _client_validation_error
from .money import _DECIMAL_CONTEXT, _check_side, validate_price, validate_quantity

#: The signing domain, identical on every environment (no verifyingContract).
LOAF_EIP712_DOMAIN = {"name": "LoafSettlement", "version": "1", "chainId": 421614}

#: The one struct an order signature covers.
ORDER_EIP712_TYPES = {
    "Order": [
        {"name": "nonceHash", "type": "bytes32"},
        {"name": "propertyToken", "type": "address"},
        {"name": "tokenAmount", "type": "uint96"},
        {"name": "paymentAmount", "type": "uint96"},
        {"name": "deadline", "type": "uint64"},
        {"name": "isBuying", "type": "bool"},
    ]
}

_PRICE_SCALE = 1000  # the exchange books prices in thousandths of a dollar
_QUANTITY_SCALE = 10  # and quantities in tenths of a token
_TOKEN_WEI_PER_UNIT = 10**17  # 18-decimal property token / _QUANTITY_SCALE
_USDC_WEI = 10**6
_PAYMENT_DENOMINATOR = _PRICE_SCALE * _QUANTITY_SCALE

_PRIVATE_KEY_RE = re.compile(r"(?:0[xX])?[0-9a-fA-F]{64}")
_ADDRESS_RE = re.compile(r"0x[0-9a-fA-F]{40}")
_NONCE_RE = re.compile(r"[0-9a-f]{32}")
_ZERO_ADDRESS = "0x" + "0" * 40
# A private key must be in [1, n). Checked here because eth-keys 0.7 (what Python 3.9 installs)
# accepts the zero key without complaint.
_SECP256K1_ORDER = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_BAD_KEY = (
    "The agent private key (agent_private_key= or $LOAF_AGENT_PRIVATE_KEY) is not a valid "
    "private key: expected 64 hex characters, optionally 0x-prefixed."
)


def new_order_nonce(now_ms: int | None = None) -> str:
    """A fresh order nonce: 32 lowercase hex, the Unix time in milliseconds in 12
    hex digits, then 20 random ones (80 bits).

    The exchange refuses a nonce more than a day off its clock, and keeps one
    order per nonce, so every signed order needs a new one.
    """
    ms = time.time_ns() // 1_000_000 if now_ms is None else int(now_ms)
    return f"{ms:012x}{secrets.token_hex(10)}"


def _order_amounts(price: Any, quantity: Any) -> tuple[int, int]:
    """(tokenAmount, paymentAmount) for grid Decimals: exact, no rounding anywhere."""
    with localcontext(_DECIMAL_CONTEXT):
        price_scaled = int(price * _PRICE_SCALE)
        quantity_scaled = int(quantity * _QUANTITY_SCALE)
    token_amount = quantity_scaled * _TOKEN_WEI_PER_UNIT
    payment_amount = price_scaled * quantity_scaled * _USDC_WEI // _PAYMENT_DENOMINATOR
    return token_amount, payment_amount


class OrderSigner:
    """Signs orders with one agent key.

    The key is held only inside an eth-keys ``PrivateKey``: the raw string
    is not kept, ``repr`` shows only the address, and the signer refuses to be
    pickled or copied. Signing is deterministic and thread-safe.

    Args:
        private_key: 64 hex characters, optionally ``0x``-prefixed. A malformed
            key raises :class:`~loaf.exceptions.LoafConfigError` (without
            echoing it).
    """

    __slots__ = ("_key", "address")

    def __init__(self, private_key: str) -> None:
        # Crash reporters and debuggers record frame locals: clear each copy of the key before
        # anything can raise (a malformed key is usually the real one with a typo).
        key = private_key.strip() if isinstance(private_key, str) else ""
        private_key = ""
        if not _PRIVATE_KEY_RE.fullmatch(key) or not 0 < int(key, 16) < _SECP256K1_ORDER:
            key = ""
            raise LoafConfigError(_BAD_KEY)
        try:
            # Built once: handing eth-account the raw key re-derives its public key for every
            # signature. Its repr is the key, so it goes straight into the slot, not a local.
            self._key = PrivateKey(bytes.fromhex(key[-64:]))
        except Exception:  # anything else eth-keys refuses
            key = ""
        if not key:  # raised OUTSIDE the except: no __context__ holding the key
            raise LoafConfigError(_BAD_KEY)
        key = ""
        #: The agent's checksummed address: the signer the exchange recovers.
        self.address: str = self._key.public_key.to_checksum_address()

    def sign_order(
        self,
        *,
        contract_address: str,
        side: str,
        price: float | Decimal,
        quantity: float | Decimal,
        nonce: str,
    ) -> str:
        """Sign one order; returns ``0x`` + 130 lowercase hex (v = ``1b`` / ``1c``).

        Args:
            contract_address: the property's token contract.
            side: ``BUY`` or ``SELL`` (a TP/SL leg is a ``SELL``).
            price: dollars on the 0.01 grid, exactly as sent (a MARKET order's
                worst price).
            quantity: tokens on the 0.1 grid, exactly as sent.
            nonce: the body's ``nonce`` (see :func:`new_order_nonce`).
        """
        side = _check_side(side)
        if not isinstance(nonce, str) or not _NONCE_RE.fullmatch(nonce):
            raise _client_validation_error("nonce must be 32 lowercase hex characters")
        if (
            not isinstance(contract_address, str)
            or not _ADDRESS_RE.fullmatch(contract_address)
            or contract_address.lower() == _ZERO_ADDRESS
        ):
            raise _client_validation_error(
                f"not a property token contract address: {contract_address!r}"
            )
        token_amount, payment_amount = _order_amounts(
            validate_price(price), validate_quantity(quantity)
        )
        message = {
            "nonceHash": keccak(text=nonce),  # keccak of the UTF-8 string
            "propertyToken": to_checksum_address(contract_address),
            "tokenAmount": token_amount,
            "paymentAmount": payment_amount,
            "deadline": 0,
            "isBuying": side == "BUY",
        }
        signable = encode_typed_data(LOAF_EIP712_DOMAIN, ORDER_EIP712_TYPES, message)
        signed = Account.sign_message(signable, self._key)
        return "0x" + bytes(signed.signature).hex()  # v = 27/28, any hexbytes version

    def __repr__(self) -> str:
        return f"OrderSigner(address={self.address!r})"

    def __reduce__(self) -> Any:
        raise TypeError("OrderSigner holds a private key and cannot be pickled or copied")

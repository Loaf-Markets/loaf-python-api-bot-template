"""The gasless IPO purchase: the buyer pays from the wallet's own USDC and sends no transaction.

The wallet signs two messages (loafx-frontend ``apps/offerings/src/lib/ipoPurchase.ts``, ported):

1. PurchaseWithFee over the sale (PrimarySale.purchaseWithSignature): the offering's token price
   and amount, the buyer's caps on the base payment and the fee, a nonce and a deadline;
2. an EIP-2612 Permit of the sale's payment token to the sale for the two caps together, under the
   token's own EIP-712 domain, checked against its ``DOMAIN_SEPARATOR()`` before anything is signed.

``POST /offerings/subscribe`` carries both; Loaf's relayer sends [permit, purchaseWithSignature] in
one transaction. Needs the ``l2keys`` extra and an RPC of the sale's chain (``ipoChainId``).
"""

from __future__ import annotations

import os
import secrets
import time
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Callable, Dict, Mapping, Optional

from .evm import Account, ChainReader, domain_separator, keccak, sign_typed, to_checksum_address, typed_data
from .exceptions import LoafL2Error

#: Shared-Types PURCHASE_WITH_FEE_EIP712_* (PrimarySale.sol's typed data).
PURCHASE_DOMAIN_NAME = "LoafPrimarySale"
PURCHASE_DOMAIN_VERSION = "1"
PURCHASE_WITH_FEE_TYPES = [
    {"name": "buyer", "type": "address"},
    {"name": "paymentToken", "type": "address"},
    {"name": "tokenPrice", "type": "uint128"},
    {"name": "tokenAmount", "type": "uint256"},
    {"name": "maxBasePayment", "type": "uint256"},
    {"name": "maxFeeAmount", "type": "uint256"},
    {"name": "nonceHash", "type": "bytes32"},
    {"name": "deadline", "type": "uint64"},
]
#: EIP-2612's Permit, field for field.
PERMIT_TYPES = [
    {"name": "owner", "type": "address"},
    {"name": "spender", "type": "address"},
    {"name": "value", "type": "uint256"},
    {"name": "nonce", "type": "uint256"},
    {"name": "deadline", "type": "uint256"},
]
#: How long both signatures stay valid; the permit's deadline also bounds a leftover allowance.
IPO_PURCHASE_TTL_S = 600
PROPERTY_TOKEN_DECIMALS = 18
QUANTITY_SCALE = 10
USDC_DECIMALS = 6
#: Loaf-Backend stores currency at 3 decimals: the fee is ceiled there.
BACKEND_CURRENCY_UNIT = 1_000
ENV_IPO_RPC_URL = "LOAF_IPO_RPC_URL"
MAX_SAFE_INTEGER = 2**53 - 1


def js_round(x: float) -> int:
    """JavaScript's ``Math.round``."""
    f = int(x // 1)
    return f + (1 if x - f >= 0.5 else 0)


def parse_units(value: float, decimals: int) -> int:
    """viem's ``parseUnits(String(value), decimals)`` for a plain decimal: the value in base units,
    rounded half up past ``decimals``."""
    d = Decimal(repr(float(value)))
    if not d.is_finite() or d < 0:
        raise LoafL2Error(f"not a non-negative price: {value!r}")
    return int((d * (10**decimals)).quantize(Decimal(1), rounding=ROUND_HALF_UP))


@dataclass(frozen=True)
class IpoSaleTerms:
    """The offering fields the purchase is built from (``GET /offerings/{ticker}``)."""

    ipo_id: int
    sale: str
    chain_id: int
    unit_price: float  # USDC per unit, unscaled
    fee_bps: int


def sale_terms(page: Mapping[str, Any]) -> IpoSaleTerms:
    def need(key: str, kind: type) -> Any:
        v = page.get(key)
        if not isinstance(v, kind) or isinstance(v, bool):
            raise LoafL2Error(f"GET /offerings: {key} is missing or malformed: {v!r}")
        return v

    return IpoSaleTerms(
        ipo_id=need("ipoId", int),
        sale=to_checksum_address(need("ipoContractAddress", str)),
        chain_id=need("ipoChainId", int),
        unit_price=float(need("unitPrice", (int, float))),
        fee_bps=need("feeBps", int),
    )


def token_amount(quantity: int) -> int:
    """The property-token amount of ``quantity`` units, 18-decimal base units (Loaf-Backend
    formatters.ts convertScaledQuantityToTokenWei)."""
    return js_round(quantity * QUANTITY_SCALE) * 10**PROPERTY_TOKEN_DECIMALS // QUANTITY_SCALE


def purchase_caps(quantity: int, terms: IpoSaleTerms) -> tuple:
    """``(maxBasePayment, maxFeeAmount)`` in payment-token base units: never below what the backend
    charges (ipoService.ts: tokenAmount x tokenPrice / 1e18, the fee ceiled at 3 decimals)."""
    price = parse_units(terms.unit_price, USDC_DECIMALS)
    base = token_amount(quantity) * price // 10**PROPERTY_TOKEN_DECIMALS
    fee_units = -(-(base * terms.fee_bps) // (10_000 * BACKEND_CURRENCY_UNIT))
    return base, fee_units * BACKEND_CURRENCY_UNIT


def new_nonce() -> str:
    """16 random bytes as 32 lowercase hex (Shared-Types orderNonceSchema)."""
    return secrets.token_hex(16)


def token_domain(chain: ChainReader, token: str, chain_id: int) -> Dict[str, Any]:
    """The payment token's EIP-712 domain (``eip712Domain()``, else ``name()`` and ``version()``),
    refused unless it is on this chain, names the token and hashes to its ``DOMAIN_SEPARATOR()``
    (deposit.ts readTokenDomain)."""
    try:
        (on_chain,) = chain.read(token, "DOMAIN_SEPARATOR()", [], [], ["bytes32"])
        try:
            _, name, version, cid, contract, _, _ = chain.read(
                token, "eip712Domain()", [], [], ["bytes1", "string", "string", "uint256", "address", "bytes32", "uint256[]"]
            )
            domain = {"name": name, "version": version, "chainId": int(cid), "verifyingContract": to_checksum_address(contract)}
        except LoafL2Error:
            (name,) = chain.read(token, "name()", [], [], ["string"])
            (version,) = chain.read(token, "version()", [], [], ["string"])
            domain = {"name": name, "version": version, "chainId": chain_id, "verifyingContract": to_checksum_address(token)}
    except LoafL2Error as e:
        raise LoafL2Error(f"the payment token {token} has no EIP-712 domain this client can check: {e}") from e
    matches = (
        domain["chainId"] == chain_id
        and domain["verifyingContract"] == to_checksum_address(token)
        and domain_separator(domain) == bytes(on_chain)
    )
    if not matches:
        raise LoafL2Error(f"the payment token {token}'s EIP-712 domain does not match its DOMAIN_SEPARATOR(): nothing signed")
    return domain


def sign_purchase(
    private_key: str,
    chain: ChainReader,
    terms: IpoSaleTerms,
    quantity: int,
    now_s: Callable[[], float] = time.time,
    nonce: Callable[[], str] = new_nonce,
) -> Dict[str, Any]:
    """The ``POST /offerings/subscribe`` body: the purchase and the permit, signed by the wallet on
    the sale's chain."""
    if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity < 1:
        raise LoafL2Error(f"an IPO purchase is a positive whole number of units; received {quantity!r}")
    chain.chain_must_be(terms.chain_id, "the sale")
    buyer = Account.from_key(private_key).address
    (payment_token,) = chain.read(terms.sale, "paymentToken()", [], [], ["address"])
    payment_token = to_checksum_address(payment_token)
    max_base, max_fee = purchase_caps(quantity, terms)
    tokens = token_amount(quantity)
    price = parse_units(terms.unit_price, USDC_DECIMALS)
    n = nonce()
    deadline = int(now_s()) + IPO_PURCHASE_TTL_S
    # Both reads before either signature: a token whose domain cannot be checked signs nothing.
    domain = token_domain(chain, payment_token, terms.chain_id)
    (permit_nonce,) = chain.read(payment_token, "nonces(address)", [buyer], ["address"], ["uint256"])
    if max_base > MAX_SAFE_INTEGER or max_fee > MAX_SAFE_INTEGER:
        raise LoafL2Error("the purchase is larger than the API carries (2^53 - 1 base units)")

    signature = sign_typed(
        private_key,
        typed_data(
            {"name": PURCHASE_DOMAIN_NAME, "version": PURCHASE_DOMAIN_VERSION, "chainId": terms.chain_id, "verifyingContract": terms.sale},
            "PurchaseWithFee",
            PURCHASE_WITH_FEE_TYPES,
            {
                "buyer": buyer,
                "paymentToken": payment_token,
                "tokenPrice": price,
                "tokenAmount": tokens,
                "maxBasePayment": max_base,
                "maxFeeAmount": max_fee,
                "nonceHash": keccak(text=n),
                "deadline": deadline,
            },
        ),
    )
    permit_signature = sign_typed(
        private_key,
        typed_data(
            domain,
            "Permit",
            PERMIT_TYPES,
            {"owner": buyer, "spender": terms.sale, "value": max_base + max_fee, "nonce": int(permit_nonce), "deadline": deadline},
        ),
    )
    return {
        "ipoId": terms.ipo_id,
        "quantity": quantity,
        "maxBasePayment": max_base,
        "maxFeeAmount": max_fee,
        "nonce": n,
        "deadline": deadline,
        "signature": signature,
        "permitDeadline": deadline,
        "permitSignature": permit_signature,
    }


def rpc_from_env(env: Optional[Mapping[str, str]] = None) -> str:
    env = os.environ if env is None else env
    url = (env.get(ENV_IPO_RPC_URL) or "").strip()
    if not url:
        raise LoafL2Error(f"{ENV_IPO_RPC_URL} is required: an RPC of the sale's chain (the offering's ipoChainId)")
    return url

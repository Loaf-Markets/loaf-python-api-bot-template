"""Primary market (IPO offerings): browse and subscribe.

Browsing is public. Subscribing is a gasless purchase signed by your wallet (:mod:`loaf.ipo`): it
needs the wallet's private key, an RPC of the sale's chain and the ``l2keys`` extra, and
wholesale-investor verification on the account (private clients, and orders of $500,000 or more
with retail KYC, are exempt).
"""

from __future__ import annotations

import os
from typing import Any, Optional

from ..exceptions import _client_validation_error
from .base import Resource

ENV_WALLET_KEY = "LOAF_WALLET_PRIVATE_KEY"


class OfferingsResource(Resource):
    def list(self) -> Any:
        """``GET /offerings``: every visible offering as a card. (public)

        Returns ``{"ipos": [...]}`` with ``ipoId``, ``ticker``, ``tokenName`` (the display name),
        ``unitPrice``, ``totalUnits``, ``unitsAllocated``, ``status``, ``opensAt`` / ``closesAt``,
        etc.
        """
        return self._client.get("/offerings", auth=False)

    def get(self, ticker: str) -> Any:
        """``GET /offerings/{ticker}``: the full offering page. (public)

        Includes ``ipoId``, ``unitPrice``, ``feeBps`` (raw basis points), the sale contract
        (``ipoContractAddress``) and its chain (``ipoChainId``), allocation stats,
        ``recentOrders`` and an ``ipoList`` selector.
        """
        return self._client.get(f"/offerings/{ticker}", auth=False)

    def subscribe(
        self,
        ticker: str,
        quantity: int,
        *,
        private_key: Optional[str] = None,
        rpc_url: Optional[str] = None,
    ) -> Any:
        """``POST /offerings/subscribe``: buy ``quantity`` whole units of the offering, paid from
        the wallet's USDC with no transaction of yours.

        The wallet signs the purchase (the offering's price and amount, caps on the base payment
        and the fee) and a permit of the payment token to the sale for those caps, both valid 10
        minutes. ``private_key`` defaults to ``$LOAF_WALLET_PRIVATE_KEY`` and must be the wallet of
        this client's user; ``rpc_url`` to ``$LOAF_IPO_RPC_URL``, an RPC of the sale's chain.

        Returns ``{success, subscriptionId, allocatedQuantity}``. Settlement is asynchronous:
        track ``ALLOCATED`` / ``REJECTED`` on the private ``portfolio`` WebSocket channel
        (``offering_order_update``), keyed by ``subscriptionId``.
        """
        from ..evm import ChainReader
        from ..ipo import ENV_IPO_RPC_URL, rpc_from_env, sale_terms, sign_purchase

        if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity < 1:
            raise _client_validation_error("quantity must be a positive whole number of units")
        key = private_key or (os.environ.get(ENV_WALLET_KEY) or "").strip()
        if not key:
            raise _client_validation_error(f"subscribe needs the wallet's private key: pass private_key=... or set ${ENV_WALLET_KEY}")
        terms = sale_terms(self.get(ticker))
        chain = ChainReader(rpc_url or rpc_from_env(), env_name=ENV_IPO_RPC_URL)
        body = sign_purchase(key, chain, terms, quantity)
        return self._client.post("/offerings/subscribe", json=body)

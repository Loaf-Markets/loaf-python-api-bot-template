"""L2 trading: every order is signed by the account's L2 trading key.

* :mod:`loaf.l2.signer`: the engine's native signer library, vendored per platform and pinned by
  ``PROVENANCE.json``;
* :mod:`loaf.l2.pricing`: the integers an order signs;
* :mod:`loaf.l2.trading`: order bodies, order indices and the account's key state
  (:class:`L2Trader`, used by ``LoafClient.orders``);
* :mod:`loaf.l2.keys`: the trading key the web app derives for a wallet, and its registration
  (needs the ``l2keys`` extra; not imported here).
"""

from __future__ import annotations

from .pricing import js_round, scale_price, scale_quantity
from .signer import L2Signer, default_signer
from .trading import L2Config, L2KeyState, L2Market, L2Trader, OrderIntent

__all__ = [
    "L2Config",
    "L2KeyState",
    "L2Market",
    "L2Signer",
    "L2Trader",
    "OrderIntent",
    "default_signer",
    "js_round",
    "scale_price",
    "scale_quantity",
]

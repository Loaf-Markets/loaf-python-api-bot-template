"""Protocol constants for the Loaf API.

These capture the wire-level contract every client must honour: numeric
precision limits, and order limits and defaults.
"""

from __future__ import annotations

from ._version import __version__

# --------------------------------------------------------------------------- #
# Connection
# --------------------------------------------------------------------------- #

#: Default REST base URL — the production Loaf API (every route is mounted under
#: ``/api``). For local development against a dev server, set ``LOAF_API_BASE_URL``
#: (or pass ``base_url=``), e.g. ``http://localhost:8005/api``.
DEFAULT_BASE_URL = "https://api.loafmarkets.com/api"

#: Environment variables the client reads when arguments are omitted.
ENV_API_KEY = "LOAF_API_KEY"
ENV_AGENT_PRIVATE_KEY = "LOAF_AGENT_PRIVATE_KEY"
ENV_BASE_URL = "LOAF_API_BASE_URL"
ENV_WS_URL = "LOAF_WS_URL"
ENV_MAX_SLIPPAGE_BPS = "LOAF_MAX_SLIPPAGE_BPS"

#: User-Agent sent on every request.
USER_AGENT = f"loaf-python-sdk/{__version__}"

#: Default per-request timeout (seconds).
DEFAULT_TIMEOUT = 30.0

#: Placements wait at least this long (seconds): the exchange may try its matching engine twice
#: before it answers, and a timeout is re-sent anyway.
DEFAULT_PLACEMENT_TIMEOUT = 45.0

#: Default number of automatic retries for transient failures: reads (429/503/network) and
#: signed placements (network/5xx, re-sent unchanged).
DEFAULT_MAX_RETRIES = 3

# --------------------------------------------------------------------------- #
# Numeric precision
# --------------------------------------------------------------------------- #

#: Max decimal places accepted for a limit price (cents). Enforced server-side.
MAX_PRICE_DECIMALS = 2

#: Max decimal places accepted for a token quantity (0.1 of a token).
MAX_QUANTITY_DECIMALS = 1

# --------------------------------------------------------------------------- #
# Orders
# --------------------------------------------------------------------------- #

#: Max slippage (basis points) when neither ``max_slippage_bps=`` nor ``$LOAF_MAX_SLIPPAGE_BPS``
#: is set: how far every price the SDK derives for you may sit from its reference (a MARKET
#: order's worst price, a *_MARKET conditional's price and a TP/SL leg's sell price). 200 = 2%.
DEFAULT_MAX_SLIPPAGE_BPS = 200

#: Upper bounds the exchange accepts for a price (dollars) and a quantity (tokens).
ORDER_PRICE_MAX = 1_000_000_000
ORDER_QUANTITY_MAX = 1_000_000_000

#: A signed order's nonce carries its signing time; the exchange refuses one more than this far
#: (milliseconds) from its clock.
ORDER_NONCE_WINDOW_MS = 86_400_000

#: Every order sends deadline 0 (only good-til-cancelled orders exist).
DEFAULT_ORDER_DEADLINE = 0

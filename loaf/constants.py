"""Protocol constants for the Loaf API.

These capture the wire-level contract every client must honour: numeric
precision limits, order limits and defaults, the timing rules of signed
orders, and the perps markets' pools and the life of a signed allocate or exit.
"""

from __future__ import annotations

from decimal import Decimal

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
ENV_SECRET_KEY = "LOAF_SECRET_KEY"
ENV_BASE_URL = "LOAF_API_BASE_URL"
ENV_WS_URL = "LOAF_WS_URL"
ENV_MAX_SLIPPAGE_BPS = "LOAF_MAX_SLIPPAGE_BPS"

#: Your account wallet's private key: read only by ``python -m loaf.secret_key``, for one command.
#: The client refuses to start while it is set.
ENV_WALLET_PRIVATE_KEY = "LOAF_WALLET_PRIVATE_KEY"

#: User-Agent sent on every request.
USER_AGENT = f"loaf-python-sdk/{__version__}"

#: Default per-request timeout (seconds).
DEFAULT_TIMEOUT = 30.0

#: Placements wait at least this long (seconds): the exchange can take a while to confirm an
#: order, and a timeout is re-sent anyway.
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

#: The highest price (dollars) an order can sign: the largest cent price whose x1000 fits 32 bits.
ORDER_PRICE_MAX = Decimal("4294967.29")

#: The largest quantity (tokens) an order can sign: (2**48 - 1) / 10, whose x10 fits 48 bits.
ORDER_QUANTITY_MAX = Decimal("28147497671065.5")

#: The highest fee rate (per million: 7,000 = 70 bps) every order signs its
#: consent to. It also sizes a BUY's hold: price x quantity x (1 + L2_FEE_RATE_CAP / 1,000,000)
#: of the quote asset, whatever fee the fills are charged.
L2_FEE_RATE_CAP = 7_000

# --------------------------------------------------------------------------- #
# Order life (milliseconds)
# --------------------------------------------------------------------------- #

#: An order stops filling at its signed ``l2ExpiryMs`` minus this (72 hours since
#: l2-settle-schema 0.27.0; 24 hours before), and the exchange refuses one whose expiry is already
#: inside it. The SDK signs now + this + the order's ttl.
L2_BUFFER_MS = 259_200_000

#: K1's longest order life (about 49.3 days): an order's expiry may lie at most this far past the
#: reservation of its order number, and at most this less one margin past the exchange's clock.
L2_MAX_ORDER_LIFE_MS = 4_261_412_864

#: One K1 expiry window (about 4.7 hours): the margin kept for clocks that disagree.
L2_ORDER_LIFE_MARGIN_MS = 16_777_216

#: How long an order may rest when ``ttl_ms=`` is omitted: 7 days.
DEFAULT_ORDER_TTL_MS = 604_800_000

#: The shortest ``ttl_ms=`` accepted: one minute.
MIN_ORDER_TTL_MS = 60_000

#: The longest ``ttl_ms=`` accepted (about 45.9 days): the longest order life, less one margin
#: for each clock and the buffer.
MAX_ORDER_TTL_MS = L2_MAX_ORDER_LIFE_MS - 2 * L2_ORDER_LIFE_MARGIN_MS - L2_BUFFER_MS

#: The most order numbers one reservation asks for. Reservations grow from what a process's first
#: order needs up to this, doubling each time, so a process that stops after a few orders leaves
#: few of them unused.
L2_ORDER_INDEX_BLOCK = 64

# --------------------------------------------------------------------------- #
# Perps
# --------------------------------------------------------------------------- #

#: The highest perp's market index: markets 0 to 254 are perps, 2048 to 4094 spot markets.
MAX_PERP_MARKET_INDEX = 254

#: The highest margin pool: pool 0 is the cross pool, 1 to 7 are isolated pools.
MAX_POOL = 7

#: An allocate's side for your spot USDC balance, beside the pools 0 to 7.
PERPS_SPOT_SIDE = 255

#: The most rows one page of a perps history read (funding ticks, closes) holds.
PERPS_HISTORY_MAX = 200

#: The quote asset of every perps market: USDC.
PERPS_QUOTE_ASSET_ID = 3

#: A perps market's ``flags``: its cross pool (0) may trade it.
PERPS_FLAG_CROSS = 1

#: A perps market's ``flags``: it is reduce-only, and no order may add to a position.
PERPS_FLAG_REDUCE_ONLY = 2

#: How long a signed allocate or exit stays valid when ``ttl_ms=`` is omitted: 10 minutes. The
#: exchange applies it only before its ``l2ExpiryMs`` (now + this), so an outcome left unknown
#: is settled once that has passed.
DEFAULT_PERPS_OP_TTL_MS = 600_000

#: The longest ``ttl_ms=`` an allocate or exit accepts: one day.
MAX_PERPS_OP_TTL_MS = 86_400_000

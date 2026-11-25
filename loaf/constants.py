"""Protocol constants for the Loaf API.

These capture the wire-level contract every client must honour: numeric
precision limits, order limits and defaults, the timing rules of signed
orders, an order's controls and the cancels' bounds, the perps markets' pools
and the life of a signed allocate or exit, withdrawals, and the trading key
and its login tokens.
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
ENV_TRADING_KEY = "LOAF_TRADING_KEY"
ENV_BASE_URL = "LOAF_API_BASE_URL"
ENV_WS_URL = "LOAF_WS_URL"
ENV_MAX_SLIPPAGE_BPS = "LOAF_MAX_SLIPPAGE_BPS"

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

#: The longest wait (seconds) a read's retry takes on a 429's or a 503's ``Retry-After`` (a 429's
#: ``RateLimit-Reset``). An answer that asks for longer is raised at once, its
#: ``retry_after`` saying how long, so the caller decides what to do with the time.
MAX_HINTED_WAIT = 10.0

#: The most ``retry_after`` an error carries (seconds): one day. A longer hint is read as this.
MAX_RETRY_AFTER = 86_400.0

#: The response body size (bytes, decompressed) past which the client stops reading: once what
#: it has decoded passes this, a read raises :class:`~loaf.exceptions.LoafConnectionError`, and a
#: signed order's, perps op's or withdrawal's outcome is unknown. httpx hands over a compressed
#: body one decoded network read at a time, so the most held is this plus one decoded read. Every
#: request the SDK sends asks only for gzip and deflate, which expand a read at most about a
#: thousandfold, and an answer that names any other ``Content-Encoding`` (two stacked, or brotli)
#: is refused the same way before any of it is read, whichever ``http_client`` sent it. A body
#: must also arrive within the request's read timeout of its headers.
MAX_RESPONSE_BYTES = 32 * 1024 * 1024

# --------------------------------------------------------------------------- #
# Numeric precision
# --------------------------------------------------------------------------- #

#: The exchange counts every price (dollars) and quantity (tokens) in millionths, on every
#: market, and no price, size or dollar amount it serves has more decimal places. Each
#: market takes fewer decimal places, set at its listing: its ``priceDecimals`` (a tick of
#: 10^-priceDecimals dollars) and ``quantityDecimals`` (a lot of 10^-quantityDecimals tokens), the
#: second at least 1 and the two together at most this.
ORDER_DECIMALS = 6

#: An order on a market signs price x 10^(this - quantityDecimals) and quantity x
#: 10^quantityDecimals (its ``l2.signedPriceDecimals`` and ``l2.signedQuantityDecimals``), so K1's
#: quote, signed price x signed quantity / 10, counts millionths of a dollar on every market.
L2_SIGNED_DECIMALS = 7

# --------------------------------------------------------------------------- #
# Orders
# --------------------------------------------------------------------------- #

#: Max slippage (basis points) when neither ``max_slippage_bps=`` nor ``$LOAF_MAX_SLIPPAGE_BPS``
#: is set: how far every price the SDK derives for you may sit from its reference (a MARKET
#: order's worst price, a *_MARKET conditional's price and a TP/SL leg's sell price). 200 = 2%.
DEFAULT_MAX_SLIPPAGE_BPS = 200

#: The highest price (dollars) and the largest quantity (tokens) the exchange takes on any market.
#: A market's highest price is lower: the largest on its tick whose signed price fits 32 bits. Its
#: largest quantity is lower where its signed quantity would pass 48 bits (quantityDecimals 6).
ORDER_PRICE_MAX = Decimal(1_000_000_000)
ORDER_QUANTITY_MAX = Decimal(1_000_000_000)

#: The highest fee rate (per million: 7,000 = 70 bps) every order signs its
#: consent to: an order whose taker or maker rate is above it is refused
#: (``FEE_RATE_CAP_TOO_LOW``), whatever role it fills in. A spot order's rates are your spot
#: rates, a perps order's your perps rates, each held to the market's fee for its role
#: (``portfolio.component().applicableFees``), so it covers spot rates up to 70 bps and any
#: rates on a perp whose ``l2.takerFee`` and ``l2.makerFee`` are at most 7,000. It also sizes a
#: spot BUY's hold, price x quantity x (1 + L2_FEE_RATE_CAP / 1,000,000) of the quote asset, and
#: the fee a perps order's pool counts against its margin while the order is open, price x
#: unfilled quantity x L2_FEE_RATE_CAP / 1,000,000, whatever fee the fills are charged.
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

#: How long an order may rest when ``ttl_ms=`` is omitted: 7 days. A GTC or POST_ONLY limit, a
#: stop or take, and any order with TP/SL legs, which share its expiry and must outlive it.
DEFAULT_ORDER_TTL_MS = 604_800_000

#: The life an order that cannot rest signs when ``ttl_ms=`` is omitted: 10 minutes. A MARKET
#: order, or a LIMIT order whose time in force is IOC or FOK, with no TP/SL legs. Its signature
#: covers neither its type nor its time in force, so its bytes verify as a resting limit at its
#: price for as long as its life lasts: a short one bounds how long a copy of them could rest. Not
#: the one-minute minimum: the exchange refuses an order whose life has ended by its own clock, and
#: 10 minutes still leaves 5 to an order signed on a clock 5 minutes slow, the most the SDK allows
#: elsewhere (its login tokens).
DEFAULT_IMMEDIATE_ORDER_TTL_MS = 600_000

#: The shortest ``ttl_ms=`` accepted: one minute.
MIN_ORDER_TTL_MS = 60_000

#: The longest ``ttl_ms=`` accepted (about 45.9 days): the longest order life, less one margin
#: for each clock and the buffer.
MAX_ORDER_TTL_MS = L2_MAX_ORDER_LIFE_MS - 2 * L2_ORDER_LIFE_MARGIN_MS - L2_BUFFER_MS

#: The most order numbers one reservation asks for unless ``order_number_block=`` says otherwise.
#: Reservations grow from ``order_number_start=`` up to this, doubling each time, so a process that
#: stops after a few orders leaves few of them unused.
L2_ORDER_INDEX_BLOCK = 64

#: The most order numbers one reservation can ask for: the backend's limit on one call, and so the
#: largest ``order_number_block=``. The exchange takes one reservation a second per account, so a
#: block of N sustains about N orders a second.
L2_ORDER_INDEX_BLOCK_MAX = 4096

# --------------------------------------------------------------------------- #
# Order controls and cancels
# --------------------------------------------------------------------------- #

#: The highest ``order_tag=``: a tag is a whole number from 1 to 2^32 - 1.
ORDER_TAG_MAX = 4_294_967_295

#: The latest ``send_by_ms=`` (Unix ms): 2^48 - 1.
SEND_BY_MS_MAX = 2**48 - 1

#: The highest market index a cancel's scope names.
MAX_MARKET_INDEX = 4095

#: The shortest ``cancel_after`` timeout other than 0 (off): 10 seconds.
CANCEL_AFTER_MIN_MS = 10_000

#: The longest ``cancel_after`` timeout: 2 minutes.
CANCEL_AFTER_MAX_MS = 120_000

#: How long the WebSocket client waits for a stamped frame of your ``portfolio`` channel
#: before it calls the feed stale (``stale_after=``, seconds): three of the exchange's account
#: beats, about one a second, so two in a row went missing.
DEFAULT_STALE_AFTER = 3.0

# --------------------------------------------------------------------------- #
# Perps
# --------------------------------------------------------------------------- #

#: The highest perp's market index: markets 0 to 254 are perps, 2048 to 4094 spot markets.
MAX_PERP_MARKET_INDEX = 254

#: The highest margin pool: pool 0 is the cross pool, 1 to 7 are isolated pools.
MAX_POOL = 7

#: An allocate's side for your spot USDC balance, beside the pools 0 to 7.
PERPS_SPOT_SIDE = 255

#: The quote asset of every perps market: USDC.
PERPS_QUOTE_ASSET_ID = 3

#: A perps market's ``flags``: its cross pool (0) may trade it.
PERPS_FLAG_CROSS = 1

#: A perps market's ``flags``: it is reduce-only, and no order may add to a position.
PERPS_FLAG_REDUCE_ONLY = 2

#: A perps market's leverage while you have saved none, at most the market's most.
PERPS_DEFAULT_LEVERAGE = 3

#: The highest leverage the exchange saves, a risk-1 market's most: a market's is 65,536 // (2 x risk).
MAX_PERPS_LEVERAGE = 32_768

#: How long a signed allocate or exit stays valid when ``ttl_ms=`` is omitted: 10 minutes. The
#: exchange applies it only before its ``l2ExpiryMs`` (now + this), so an outcome left unknown
#: is settled once that has passed.
DEFAULT_PERPS_OP_TTL_MS = 600_000

#: The longest ``ttl_ms=`` an allocate or exit accepts: one day.
MAX_PERPS_OP_TTL_MS = 86_400_000

# --------------------------------------------------------------------------- #
# Withdrawals
# --------------------------------------------------------------------------- #

#: How long the exchange takes a signed withdrawal when ``ttl_ms=`` is omitted: one hour. It
#: signs ``l2ExpiryMs`` = now + :data:`L2_BUFFER_MS` + this, and takes it only until that expiry
#: less the buffer, so an outcome left unknown is settled within this of the signing.
DEFAULT_WITHDRAWAL_TTL_MS = 3_600_000

#: The longest ``ttl_ms=`` a withdrawal accepts: one day.
MAX_WITHDRAWAL_TTL_MS = 86_400_000

# --------------------------------------------------------------------------- #
# The trading key and its login tokens
# --------------------------------------------------------------------------- #

#: How a trading key starts: the one string the web app's Show trading key gives,
#: ``loaf_tk_<l2ChainId>_<l2AccountIndex>_<secret key, 80 lowercase hex>``.
TRADING_KEY_PREFIX = "loaf_tk_"

#: How a login token starts: ``loaf_lt_<l2ChainId>_<l2AccountIndex>_<l2ExpiryMs>_<l2SigS><l2SigE>``,
#: the Bearer of every authenticated request and the WebSocket's ``auth`` frame, signed with the
#: trading key over its account, its chain and its expiry.
L2_LOGIN_TOKEN_PREFIX = "loaf_lt_"

#: How long a login token the SDK signs lives: 10 minutes. It signs a fresh one, for requests and
#: the live WebSocket connection alike, once the one it holds is down to half of that, 5 minutes.
#: The exchange takes one only before its expiry and at most 15 minutes ahead of its own clock (the
#: longest life it takes), so this machine's clock may run up to about 5 minutes fast or 5 slow.
L2_LOGIN_TOKEN_LIFE_MS = 600_000

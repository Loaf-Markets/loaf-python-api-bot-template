# Changelog

What each release changed, and what to change in a bot. While the SDK is
pre-1.0, a breaking change bumps the minor version (README §4). The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## 0.7.0 (unreleased)

For each market's own tick and lot, the perps reads as actual values, the
exchange's perps band around the mark and its coded withdrawal refusals. It
needs the exchange API that serves each market's decimals and its perps values
as exact decimal strings; an older SDK's orders are refused there
(`INVALID_SIGNATURE`: it signs at the old scale). It also takes only an
encrypted base URL (or one on this machine), gives an order that cannot rest a
10-minute life, holds every derived price within your max slippage, bounds what
a read waits for and reads, and makes `bot.py` a template you fill in one
method of.

### Breaking

- **Each market has its own tick and lot.** A market's `priceDecimals` and
  `quantityDecimals` (`market.get(ticker).market`, and each `market.list()`
  row and `marketList` entry), set at its listing, replace the global 2 and 1
  decimal places: a price has at most `priceDecimals` (a tick of
  10^-priceDecimals dollars: $1 on a listing near $75,000, $0.0001 on one near
  $3) and a quantity at most `quantityDecimals` (a lot of 10^-quantityDecimals
  tokens). An order signs price x 10^(7 - quantityDecimals) and quantity x
  10^quantityDecimals, the market's `l2.signedPriceDecimals` and
  `l2.signedQuantityDecimals`, and the SDK refuses a market that serves other
  signed decimals, or none (`LoafServerError`, `unusable market.<field>`). A
  market's highest price is the largest on its tick whose signed price fits
  32 bits, (2^32 - 1) / 10^(7 - quantityDecimals) dollars ($429,496.72 at 3
  quantity and 2 price decimals); its largest quantity 1,000,000,000 tokens
  and 2^48 - 1 lots; and K1's 48-bit fill quote now caps an order's price x
  quantity at about $281.47 million (it was $281 billion). A price or quantity
  that no market takes (not a positive number, more than 6 decimal places,
  above 1,000,000,000) is still refused before any request; one off its
  market's tick or lot, or past its bounds, is refused once the market is read
  (status 0, nothing reserved or sent), so on a market no order of yours has
  named yet it costs one public read. What to change: round prices with the
  market's `priceDecimals` (`round(x, market.priceDecimals)`) and size
  quantities in its lots; a price you derived on the cent grid is off the
  tick of a market with fewer price decimals.
- **`MAX_PRICE_DECIMALS` and `MAX_QUANTITY_DECIMALS` are gone**, with the
  global grid. `loaf.constants.ORDER_PRICE_MAX` and `ORDER_QUANTITY_MAX` are
  the exchange's bounds on any market, 1,000,000,000 each, and
  `loaf.money.validate_price` and `validate_quantity` check only what no
  market takes. What to change: read the market's decimals.
- **`worst_price` takes the market's decimals.** `loaf.worst_price(reference_price,
  side, max_slippage_bps, *, price_decimals, quantity_decimals)` rounds toward
  the reference to the market's tick and refuses a price above the market's
  highest, and a BUY's below one tick that one tick would take beyond its max
  slippage (below). Every price the SDK derives (a MARKET order's
  worst price, a `*_MARKET` stop's or take's booked price, a TP/SL leg's
  limit, a `reference_price` with no slippage) is on the market's tick, and on
  a perp the price on the near side of an off-grid mark is the tick's, not the
  cent's. What to change: pass `price_decimals=market.priceDecimals,
  quantity_decimals=market.quantityDecimals` (by name: swapped, two markets'
  decimals can both be valid).
- **The perps reads are actual values, exact `Decimal`s.** Every price, size
  and dollar amount of a perp's live state (`markPrice`, `indexPrice`,
  `heldPrice.price`, `openInterest`), its order limits (`l2.minBase` in
  tokens, `l2.minQuote` and `l2.quoteLimit` in dollars), a position (`base` in
  tokens; `entryQuote`, `entryPrice`, `liquidationPrice`, `notional` and
  `fundingOwed` in dollars), a pool (`collateral`, `value`, `requirement`) and
  a funding payment (`base`, `payment`) is the `decimal.Decimal` of its actual
  value, read digit for digit from the exchange's exact decimal string, from
  `market.get`, `perps.positions`, `portfolio.summary`,
  `portfolio.component`, `history.funding` and their WebSocket frames
  (`perps_markets_live`, `market_terms_update`, `perps_account_update`,
  `funding_payment`). They were the engine's integers: prices dollars x 1000,
  sizes its lots, quotes dollars x 1000, money extended USDC units, and a
  pool's `requirement` those x 65,536 (now its maintenance margin in dollars).
  The money is in dollars to $0.000001, rounded never in your favour: what you
  hold down, what you need or owe up. A price can have more places than the
  tick (a mark, an index, an entry or a liquidation price falls between ticks,
  to `l2.signedPriceDecimals` places). A position carries its market's
  `signedPriceDecimals` and `signedQuantityDecimals`, and the summary and the
  `perps_account_update` frame its account's `accountMode` and
  `withdrawable` (dollars, rounded down). A perps `fundingIndex` stays the
  engine's integer, a decimal string of extended USDC units per lot. A value
  served in any other form (a number, an exponent) raises `LoafServerError`
  naming it, and the WebSocket client drops and logs its frame: it is never
  read in another unit. What to change: drop every scaling of these reads and
  value a position in dollars, `notional = |base| x markPrice`; only funding
  owed takes the engine's integers, a position's lots, `base x
  10^signedQuantityDecimals`, x the move of `fundingIndex` (README,
  **Perps**). A MARKET order on a perp prices off the mark read so.
- **A fill's `quantity`, `price`, `fee` and `closedPnl` are exact
  `Decimal`s**, on every market (`history.trades()`,
  `portfolio.component().tradeHistory`, `trade_new` frames; they were
  floats), the fee to $0.000001 (it was to $0.001); a perps fill off the book
  can be priced between ticks. A `mark_price` frame's `price` is a `Decimal`
  too (a perp's is its mark). What to change: compare and sum them as
  `Decimal`s.
- **The base URL must be encrypted, or on this machine.** `base_url` /
  `$LOAF_API_BASE_URL` must be `https://`, or `http://` only to this machine
  (`localhost`, 127.0.0.0/8, `[::1]`); `ws_url` / `$LOAF_WS_URL` `wss://`,
  or `ws://` to this machine. Any other scheme, no scheme, no host, a bad
  port, a user name or password in the URL, or plain http to another host
  (a `*.localhost` name included) raises
  `LoafConfigError` when the client is created (it was taken as given). The
  scheme and host are lowercased. The SDK sends that host its login tokens
  (bearer credentials anyone can use for their 10-minute life to read your
  account and cancel your orders) and your signed orders, and signs orders
  from its market data, so plain http to another host put all three on the
  wire unencrypted. What to change: use `https://`; for a dev server on your own
  network, in a container network or on a `*.localhost` name pass
  `LoafClient(allow_http=True)` (below); drop any user name and password from
  the URL.
- **`loaf.request()` / `loaf.get()` never return text.** A 2xx whose body is
  not JSON raises `LoafServerError` (`answered a body that is not JSON`)
  whatever its content type; it was returned as a `str` unless labelled JSON.
  What to change: a caller of `request()` on a route that answers text
  catches `LoafServerError` (its `body` holds the text).
- **A MARKET BUY under one tick is refused where one tick lies beyond your
  max slippage.** A price the SDK derives never lies further against the
  order than its whole `max_slippage_bps` from its reference. A BUY off a
  reference below one tick signs one tick only when one tick lies within the
  whole max slippage of the reference (and, on a perp, inside the band), and
  is refused locally otherwise (`LoafValidationError`, status 0: nothing
  reserved, signed or sent); it was lifted to one tick whatever the slippage
  (on a perp, while inside the band).
  The exact form (`reference_price=X, max_slippage_bps=0`) with X below one
  tick is refused, and `loaf.worst_price()` raises by the same rule. What to
  change: give a LIMIT order's price where you mean to pay one tick for a
  token priced below it.
- **`bot.py`: your strategy goes in `Strategy.trade(bid, ask, mark)`.**
  `on_tick` calls it only while the market trades and your private stream is
  not stale, with the tick's best bid and ask (`None` while the book is not
  known) and the mark. A `TARGET_TICKER` that is not listed stops the bot,
  printing the listed tickers (it picked another market); empty, the bot
  follows the first tradeable spot market, else the first tradeable market of
  any type (it took the first tradeable market, a perp included). What to
  change: move strategy code from your copy's `on_tick` into `trade`.
- **`history.cancelled_orders()` is paged.** Each request examines your next
  1,000 orders by placement, newest first, cancelled or not, and answers the
  cancelled ones among them, `{cancelledOrders, nextCursor}`: a page can be
  short or empty while older orders remain, and only `nextCursor` `None` is
  the end. It answered every cancel at once, newest cancel first; rows now
  come newest order first. What to change: pass each
  `nextCursor` back as `cancelled_orders(cursor=)`, or walk them all with
  `history.iter_cancelled_orders()`; never stop at an empty page.

### Added

- `LoafClient(allow_http=True)` (and `allow_http=` on `LoafWebSocketClient`,
  defaulting to its client's setting) also takes plain `http://` and `ws://`
  to other hosts, for a dev server on your own network or in a container
  network; never for a remote host. No environment variable turns it on.
- A `ws_url=` / `$LOAF_WS_URL` on another host than the base URL warns
  (`UserWarning`) when the client is created: the login token is sent there
  too.
- `loaf.constants.DEFAULT_IMMEDIATE_ORDER_TTL_MS` (10 minutes),
  `MAX_HINTED_WAIT` (10 s), `MAX_RETRY_AFTER` (one day) and
  `MAX_RESPONSE_BYTES` (32 MiB).
- `bot.py`: a `[OLD BOOK]` tick flag (no book frame for more than 15 s: bid
  and ask read `None` that tick, its age taken on the monotonic clock);
  SIGTERM stops the bot cleanly after the current tick, as Ctrl-C does, and a
  second Ctrl-C stops it at once (a second SIGTERM ends the process);
  `Strategy.on_shutdown()` runs as the bot stops and, as shipped, sends
  nothing (the usual choice once you place tagged orders:
  `cancel_all(order_tag=BOT_ORDER_TAG)`); `Strategy.quantity_worth(dollars,
  price)` sizes an order by value on the market's lot, never worth more than
  `dollars` (`None` when one lot is). A `LoafError` the strategy lets out is
  printed and the loop goes on; an `OrderOutcomeUnknownError`,
  `PerpsOutcomeUnknownError` or `WithdrawalOutcomeUnknownError` it lets out
  stops the bot, printing the order's number or the op's nonce and how to
  settle it. The `Following market` line names the market's type.
- A `LICENSE` file (MIT).
- `history.cancelled_orders(cursor=)` and `history.iter_cancelled_orders()`.
- **The withdraw fee.** A withdrawal pays the exchange's withdraw fee on top
  of its amount (a flat amount of USDC the exchange sets, at most 5 USDC; your
  wallet receives the amount whole). `portfolio.summary().withdrawFee` serves
  the fee in force (a `Decimal`), so the most a withdrawal can name is
  `withdrawable - withdrawFee`. `funds.withdraw(max_fee=)` names the most a
  withdrawal accepts, by default the fee the SDK last read from your summary
  (with the collateral: no extra read), sent as the body's `maxWithdrawFee`
  beside the signature, which does not cover it; a higher fee in force is
  refused `OrderErrorCode.WITHDRAW_FEE_ABOVE_MAX` (422, nothing taken, never
  journaled, a resubmit's too), and the SDK reads the fee again for the next
  withdrawal. The 202 names the fee paid (`withdrawFee`, the decimal string
  served) and a transfer row its `fee` in base units. Against an exchange
  that serves no fee the SDK sends none, as before.

### Changed

- **An order that cannot rest signs a 10-minute life by default.** With
  `ttl_ms` omitted, a MARKET order, or a LIMIT order with time in force `IOC`
  or `FOK`, that has no TP/SL legs signs a life of 10 minutes
  (`DEFAULT_IMMEDIATE_ORDER_TTL_MS`); `GTC` and `POST_ONLY` limits, stops and
  takes, and any order with TP/SL legs keep 7 days, and an explicit
  `ttl_ms=` always wins. An order's signature covers neither its type nor its
  time in force, and a placement carries no login token, so a copy of a
  signed order can be placed as a resting limit at its signed price while its
  life lasts and its order number is unused; ten minutes rather than the
  one-minute minimum keeps room for a clock up to 5 minutes slow. What to
  change: `resubmit` an `OrderOutcomeUnknownError`'s body of such an order
  within its 10 minutes, or settle it with `cancel_by_index`.
- **A read waits out a hint of at most 10 s.** A read retries a 429 or a 503
  after its `Retry-After` (a 429's `RateLimit-Reset` without one) only when
  that is at most `MAX_HINTED_WAIT`; a longer hint is raised at once (a 429
  as `LoafRateLimitError`, a 503 as `LoafServiceUnavailableError`, each with
  its `retry_after`), where it waited up to 60 s for each retry. Without a
  usable hint the jittered backoff applies; the reads a placement makes are
  unchanged. `retry_after` (`LoafRateLimitError`, and now every
  `LoafServiceUnavailableError`, `TradingPausedError` included) is finite, 0
  to 86,400 s, or `None`, and
  `client.last_rate_limit` holds only finite values from 0 up. What to
  change: bound your own wait on `retry_after`, e.g.
  `time.sleep(min(e.retry_after or 5, 60))`.
- **What settles a signed op.** A 2xx answers an order only as a JSON object
  whose `success` is not false and whose `orderId` is a whole number, and a
  perps op or a withdrawal only so with a whole-number `journalSeq`; any other
  2xx, and an answer whose body arrived but cannot be interpreted, raises the
  op's outcome-unknown error (unless its status is itself a refusal below
  500), not re-sent, as does an answer of any status whose body was not read
  to its end (see **Bounded answers**). A 503 without a code counts as
  nothing done only when the exchange's JSON error object says so, never a
  page that is not JSON. `LoafAPIError.code` is always a string or `None`.
- **A market detail that names another market is refused.** Every order and
  `orders.warm` refuse (`LoafServerError`, `GET /trade/{ticker} answered an
  unusable market.<field>`) a detail whose `market.ticker` is not the ticker
  asked for, whose `type` is not `PERP` exactly for a `-PERP` ticker (and
  `SPOT` for any other), or whose `marketIndex` does not fit its type (a
  perp's 0 to 254, a spot market's above). `cancel_all(ticker)` and
  `cancel_after(..., ticker=)` refuse such a detail too, and send no cancel.
- **Bounded answers.** A response body over 32 MiB decoded, or still arriving
  past the read timeout, raises `LoafConnectionError` on a read (not
  retried) and is an unknown outcome for a signed op (not re-sent), whatever
  its status. Reading stops once what has been decoded passes 32 MiB, so the
  most held is that plus one decoded network read; every request the SDK
  sends carries `Accept-Encoding: gzip, deflate` whatever decoders are
  installed (never brotli or zstd, whose read decodes to any size), and an
  answer that names any other `Content-Encoding` (two stacked, or brotli) is
  refused the same way before any of it is read, with your own
  `http_client=` too. The
  iterators
  (`history.iter_orders`, `iter_trades`, `iter_funding`,
  `market.iter_candles`) raise `LoafServerError` on a page they cannot page
  on (not an object, its rows not a list, a cursor that is not one, an empty
  page that still points onward), where they stopped or went on. An error
  built from a page that is not JSON has its message cut to about 500
  characters; `.body` keeps it all.
- **The WebSocket beat sees a slow handler.** `on_stale` has a reason
  `"behind"`: a handler is too slow for the feed, so what is being handled is
  older than `stale_after`. It fires once an account snapshot's `engineMs`
  lags this machine's clock by more than `stale_after` plus one second (a
  snapshot waits for the next beat before it is sent) beyond the least lag of
  the connection, which may rise by 0.1% of the time since, for drift between
  the two clocks. While behind, `fresh` is `False`; `on_fresh` fires at the
  next snapshot back within bounds. A connection still behind after 15 s is
  closed (`on_disconnect` 1006, `the handlers stayed behind the feed`) and
  reconnected, without the frames queued on it (with `auto_reconnect=False`
  the run ends there, as after any other ended connection). A frame counts as a beat from
  when it is taken off the socket, before its handlers, and `fresh` reads
  `False` from any thread once `stale_after` seconds pass since the last
  beat, even while a handler blocks the loop; the first frame after such a
  block fires `on_stale("silent")`, then `on_fresh`.
- **A handler cannot end the feed silently.** A handler's
  `asyncio.CancelledError` is logged and swallowed like any exception. A
  handler's `SystemExit`, its `KeyboardInterrupt` on the background thread
  (`start()`), or a cancellation `stop()` did not ask for, ends the run with
  `on_stale("disconnected")` if it was fresh, then `on_disconnect` with
  `code` `None` and the exception's type name as `reason`, and an ERROR log;
  `fresh` is then `False` until `start()`, `run_forever()` or `stop()`, and
  nothing reconnects by itself. `run_forever` raises the `SystemExit` again;
  under it a `KeyboardInterrupt`, in a handler's code too, is Ctrl-C: the
  client stops as with `stop()`, and `run_forever` returns. A run that ends
  after `stop()` stopped waiting for it reports nothing and changes nothing
  of the run `start()` begins next.
- **The exchange bounds its reads.** An order book, the `orderbook:{ticker}`
  frames and `market.get(ticker).orderBook` alike, holds its best 200 levels
  a side. A candles page coarser than `1m` can hold fewer than `count_back`
  while `hasMore` is true (`market.iter_candles` pages on from it). An offset
  page of `history.orders()` with no cursor must start before row 20,000, of
  `history.trades()` before row 10,000 (`(page - 1) * page_size`); one deeper
  is refused with a 400 (`LoafValidationError`). The history reads and `GET
  /l2/keys` count against your account's own rate limit too. What to change:
  page history deeply with the cursor (the iterators do); read a depth past
  200 levels as unknown, not empty; never take a short candles page for the
  end.
- **WebSocket bounds.** The reconnect wait's ceiling doubles after each
  attempt that does not stay live 10 s after `on_connect`, up to 30 s (it
  started over at any `on_connect`). A frame may be up to 16 MiB (it was
  websockets' 1 MiB); a larger one closes the connection with 1009, and
  `on_disconnect` reports the close code the client sent when it received
  none (it reported 1006). A server `error` frame with no `on_error` handler
  registered is logged at WARNING. `stop()` waits up to 5 s, then logs a
  WARNING and returns if the thread is still running, and no frame queued
  before `stop()` reaches a handler after it.
- `examples/03` and `examples/06` use the first tradeable spot market (and
  exit if there is none, or if its `minOrderValue` is missing), place with a
  five-minute life, and always cancel what they placed: by its order number
  (`cancel_by_index`) when even their resubmit cannot tell whether it landed.
- Dependencies have upper bounds (`httpx<1`, `websockets<18`) and floors for
  `h11`, `idna` and `certifi` that exclude versions with published
  advisories. `.gitignore` also covers `.env.*` (not `.env.example`),
  `.envrc` and `*.log`.
- **The prices the SDK derives on a perp follow its band.** The exchange
  holds every perps order's price within the market's band (`market.l2.band`
  basis points) around its mark, and, when it is placed, a perps stop's,
  take's or TP/SL leg's limit within the band around its own trigger (400
  `PRICE_BAND` otherwise). On a perp a MARKET order without `reference_price`
  is now priced off the live mark (`market.live.markPrice`; while that is 0
  or not served, the market reference, though with `live` null the exchange
  may still band the order), and every price the SDK derives (a MARKET order's
  worst price, a `*_MARKET` stop's or take's booked price, a TP/SL leg's
  limit) starts at most half the band from its reference, `min(max
  slippage, band // 2)` basis points, as the web app prices, rounded toward
  the reference to the market's tick. A MARKET order's price that this
  rounding takes across a reference off the tick grid (a mark is finer than
  the tick; a SELL's price above it, a BUY's below it) gives way to the grid
  price on the reference's near side (a SELL's below it, a BUY's above it)
  when that price is positive, inside the band and within your whole max
  slippage; else the crossing price is kept if it is inside the band; else the
  order is refused locally (`LoafValidationError`, status 0: nothing reserved,
  signed or sent). A MARKET BUY off a reference under one tick signs one
  tick, the lowest price, only when that price is inside the band and within
  your whole max slippage of the reference (above), and is refused so too
  otherwise. A trigger
  is on the tick, so a stop's or a leg's price never crosses.
  A price you give exactly (a LIMIT order's, a `*_LIMIT` row's, or
  `reference_price` with `max_slippage_bps=0`) is signed as given, never moved
  or refused for the band, and spot markets are unchanged. A perps order
  refused `PRICE_BAND` after it signed a price the SDK derived (its own or a
  TP/SL leg's limit) makes the next one read the market's terms again; one
  whose prices you all gave keeps them. What to change: a bot that sizes a
  perps MARKET BUY's hold itself takes `loaf.worst_price(mark, "BUY",
  min(max_slippage_bps, band // 2), price_decimals=..., quantity_decimals=...)`,
  or the tick above the mark where that price falls below an off-grid mark
  and the band and the max slippage admit it (see `worst_price`); one that
  wants a perps price farther than half the band from its reference gives the
  price exactly.
- **Withdrawal refusals answer as an order's.** Each carries `e.code`
  (`loaf.OrderErrorCode`): 422 `INSUFFICIENT_BALANCE`, `BELOW_MIN_ORDER_VALUE`,
  `RATE_LIMITED` (too many withdrawals not yet settled), `ORDER_INDEX_USED`
  (the nonce), `ORDER_EXPIRY_INVALID`, `INVALID_REQUEST` or `UNKNOWN_REASON`;
  401 `UNAUTHENTICATED` (the login token) or `INVALID_SIGNATURE`; 403
  `ORDER_NOT_OWNED`; 400 `ORDER_EXPIRY_INVALID` or `INVALID_REQUEST`; 429
  `RATE_LIMITED` (`LoafRateLimitError`, your account's request rate); 503
  `TRADING_PAUSED` or `NOT_PROCESSED` (nothing journaled: withdraw again). The
  messages are the exchange's own texts, without the `L2 money movement
  refused: ` prefix, and a refused signature or key is a 401 (`LoafAuthError`)
  where it was a 422 (`LoafAPIError`). What to change: compare `e.code`, never
  a withdrawal refusal's message, and catch `LoafAuthError` for a refused
  signature.
- **A withdrawal's re-send refused `ORDER_INDEX_USED` settles it.** The
  exchange answers a re-send of a journaled withdrawal as its duplicate before
  it checks the nonce, so a 422 `ORDER_INDEX_USED` to one (its nonce spent by
  another op, or held by another withdrawal) says no attempt was journaled,
  and none will be. After an attempt whose answer was lost, and on
  `funds.resubmit`, it is now raised as itself (`LoafAPIError`) where it was
  `WithdrawalOutcomeUnknownError`. What to change: a bot that resubmits until
  the body's window closes withdraws again on this code instead.

### Removed

- The `e2e/` directory: its full-stack tests are no longer part of this
  repository.

### Fixed

- No login token is left in the locals a traceback records for the SDK's,
  httpx's or httpcore's finished frames, across a raised error's whole chain,
  nor on the request an httpx error raised inside the SDK call carries; crash
  reporters and debuggers record both. An exception you were handling when
  you called the SDK, chained as the error's `__context__`, is left as it
  was.
- A 401 coded `INVALID_SIGNATURE` names your trading key not in force,
  whatever its text: a withdrawal's `l2 money: the signing key is not the key
  the journal set for the api key` had no such hint.
- The docs: a perp's band is measured from its mark, not its index; a fired
  perps stop the band refuses waits `ARMED` until the mark lets it in, or
  until its order's life ends or its account's epoch moves (`FAILED`), or its
  position closes (`CANCELLED`), unless its limit lies outside the band around
  its own trigger (then `FAILED`); a TP/SL leg's trigger may sit anywhere, and
  only its limit is held within the band around it.
- The docs and texts on a stop's life: the exchange FAILS an ARMED stop, take
  or TP/SL leg within about a second once its life ends or its account's epoch
  moves (a key reset among them), fired or not. They said it stayed ARMED, in
  its open-order slot, and FAILED only if it fired later, so `resubmit`'s
  refusal of an order past its life told you to cancel such a row, and the
  hint to a trading key not in force told you to cancel and re-arm your stops
  and takes: it now tells you to place them again.
- The docs on a stop or take that fires short of funds or tokens: the
  exchange, which holds it, FAILS it with the refusal's text as its
  `rejectionReason` and cancels none of your orders. They said it cancelled
  your resting orders on its market and side and tried once more, and listed
  that among the causes of a `CANCELLED` you did not request. What to change:
  when other orders or trades take what a stop or take needs, cancel or
  resize it yourself.

## 0.6.0 (2026-11-03)

For market makers: order controls, scoped and timed cancels, error codes, a
live-stream watchdog, and withdrawals. It needs the exchange API that serves
them.

### Breaking

- **Perps reads moved to the portfolio and the market.** `perps.markets()`,
  `market()`, `markets_live()`, `market_live()`, `funding_ticks()`,
  `funding()` and `closes()` are gone, with the exchange routes they read.
  `portfolio.summary()` holds your perps account (pools and positions at the
  mark, which `perps.positions()` and `perps.collateral()` read);
  `market.get(ticker).market` holds a perp's terms (`l2`) and its live state
  (`live`, the mark beside the index); `history.funding()` lists your funding
  payments.
- **`portfolio.summary()` is `{balances, holdings, perps}`.** The saved perps
  leverage (`perpsLeverage`) and the holdings' details (`holdingDetails`:
  name, total tokens, image) are `portfolio.component()`'s. Fields the
  exchange no longer serves are gone: a holding's `name`, `kind`,
  `totalTokens` and `imageUrl`; a perp's `sem` and `oiCap` (it gains
  `maxLeverage`, as a position does); a live state's `tickAtMs` and
  `premiumSampleBp`; a pool's `initialMarginOk`; an asset's `assetId` and
  `tokenAddress`; a market detail's `recentTrades` rows' `txHash`
  (`market.trades()` keeps it); and its `marketList` rows carry
  `maxLeverage` in place of `risk` and a perp's `live` figures, with no
  `name`, `kind` or `totalTokens`. An offering's row (`type` `IPO`, its
  `marketIndex` `None` without a spot market) stands in for its asset's spot
  market while that is missing or `PENDING`.
- **A holding has no `marketIndex`.** Its `ticker` names its spot market, and
  only an IPO allocation's was ever `None`: an allocation keeps its unit price
  until it trades, so test `isIpoAllocation` where you tested that.
- **A `portfolio.component()` section is `None` when unread.** Every key is
  always there, and a section the exchange could not read just now is `None`,
  which never means empty: read again, and keep what you hold of it meanwhile.
- **A saved leverage names its ticker.** `portfolio.set_perps_leverage(ticker,
  leverage)` takes a perp's ticker only, no longer its `marketIndex`, and
  `perpsLeverage` rows and `perps_leverage_update` frames are `{ticker,
  leverage}`.
- **Market-data frames name the ticker.** `orderbook_update`, `trades_batch`,
  `candle_update`, `mark_price`, `volume_update` and `market_halt` carry
  `ticker`, the channel's own string, and no `marketIndex`: read `msg.ticker`
  where you read `msg.marketIndex`. A tape row (`trades_batch`,
  `market.trades()`, a market detail's `recentTrades`) carries no `marketIndex`
  either: its frame or its route names the market.
- **A liquidation's fill is a trade frame.** `on_liquidation_fill` and the
  `liquidation_fill` frame are gone: a liquidation's take from your resting
  perps order is a `trade_new` (`on_trade`) with `trade.source`
  `LIQUIDATION`, as every perps fill of yours is (a deleverage, an exit, a
  settlement too). `history.trades()` rows are book trades or perps fills;
  key a fill by `(tradeId, side)`.
- **Refusals are classified by their code, not their text.** Every refusal of
  an order request carries `e.code`, one of `loaf.OrderErrorCode`, and the
  exception class follows from the status and the code. Code that matched
  message texts (`l2 epoch: …`, `l2 signature: …`, …) must compare `e.code`
  (`SETTLE_EPOCH_MISMATCH`, `INVALID_SIGNATURE`, …) or catch the class.

### Added

- **Order controls** on `create` and its wrappers, sent with the order and
  not signed: `order_tag=` (your label; cancels select by it), `send_by_ms=`
  (decided by then or refused, `SendByPassedError`, its order number unspent)
  and `replaces_order_id=` (the old order is cancelled in the step that
  decides the new one; `ReplaceTargetGoneError` when it no longer rests, and
  `replaced_order_cancelled` when the new one is refused after the cancel).
  `orders.resubmit(signed_body, send_by_ms=)` re-sends with a new send-by
  time, and sends nothing once a body's send-by time has passed.
- **Cancels:** `cancel_all(ticker=, order_tag=)` cancels one market's or one
  tag's orders; `cancel_after(timeout_ms, ticker=, order_tag=)` is the dead
  man's switch (call it every 15 to 30 s from the loop that quotes);
  `cancel_by_index(order_index)` settles an order by its order number with no
  read, `CANCELLED`, `ENDED` or `VOIDED` (`loaf.CancelByIndexOutcome`), the
  way to settle an `OrderOutcomeUnknownError` you no longer want.
- **Withdrawals:** `loaf.funds.withdraw(amount, ttl_ms=)` sends USDC from your
  spot balance to your own wallet, signed with your trading key; the signed
  withdrawal names no recipient and the exchange pays it only to the wallet
  that opened your account. `funds.resubmit()` and
  `WithdrawalOutcomeUnknownError` settle a lost answer.
- **WebSocket:** the engine's frames carry `engineMs` and `batchSeq`
  (`ws.engine_ms`, the newest); order frames carry `orderTag` and
  `l2OrderIndex`. The beat: `ws.fresh`, `stale_after=` (default 3 s),
  `on_stale` (`silent`, `disconnected` or `unsubscribed`) and `on_fresh`.
- `TradingPausedError` (503 `TRADING_PAUSED`, with `retry_after`): nothing
  was placed; cancels are still taken.
- `LoafClient(placement_timeout=, order_number_start=, order_number_block=,
  reserve_ahead=)`, `orders.warm()` and `client.close()`: shorter placement
  waits, larger order-number blocks, reserved ahead on a background thread.
- `portfolio.set_perps_leverage()`, and the private frames
  `perps_account_update`, `funding_payment`, `holding_update`,
  `transfer_update`, `fees_update`, `l2_keys_update`, `perps_leverage_update`
  and the public `market_terms_update`; a liquidation's fill is a
  `trade_new`.
- Constants `ORDER_TAG_MAX`, `CANCEL_AFTER_MIN_MS`, `CANCEL_AFTER_MAX_MS` and
  `DEFAULT_STALE_AFTER`.

### Fixed

- The README and docstrings: the mark stays within 2% of the index; an SDK
  order is never refused for self-trade (the exchange cancels your resting
  order instead); a frame's `timestamp` is in whole seconds.

## 0.5.0

- **Breaking:** the trading key (`LOAF_TRADING_KEY`) replaces the API key and
  the bare 80-hex `LOAF_SECRET_KEY`; the SDK is trimmed to trading (README §2).
- **A holding has no `marketIndex`.** Its `ticker` names its spot market, and
  only an IPO allocation's was ever `None`: an allocation keeps its unit price
  until it trades, so test `isIpoAllocation` where you tested that.
- **A `portfolio.component()` section is `None` when unread.** Every key is
  always there, and a section the exchange could not read just now is `None`,
  which never means empty: read again, and keep what you hold of it meanwhile.
- **A saved leverage names its ticker.** `portfolio.set_perps_leverage(ticker,
  leverage)` takes a perp's ticker only, no longer its `marketIndex`, and
  `perpsLeverage` rows and `perps_leverage_update` frames are `{ticker,
  leverage}`.
- **Market-data frames name the ticker.** `orderbook_update`, `trades_batch`,
  `candle_update`, `mark_price`, `volume_update` and `market_halt` carry
  `ticker`, the channel's own string, and no `marketIndex`: read `msg.ticker`
  where you read `msg.marketIndex`. A tape row (`trades_batch`,
  `market.trades()`, a market detail's `recentTrades`) carries no `marketIndex`
  either: its frame or its route names the market.

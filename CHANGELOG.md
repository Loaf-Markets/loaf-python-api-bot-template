# Changelog

What each release changed, and what to change in a bot. While the SDK is
pre-1.0, a breaking change bumps the minor version (README §4). The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## 0.7.0 (unreleased)

For each market's own tick and lot, the perps reads as actual values, the
exchange's perps band around the mark and its coded withdrawal refusals. It
needs the exchange API that serves each market's decimals and its perps values
as exact decimal strings; an older SDK's orders are refused there
(`INVALID_SIGNATURE`: it signs at the old scale).

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
  the reference to the market's tick (at least one tick) and refuses a price
  above the market's highest. Every price the SDK derives (a MARKET order's
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

### Changed

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
  signed or sent). A MARKET BUY off a reference under one tick starts at one
  tick, the lowest price, and is refused so too unless that price is inside
  the band (off a mark of half a tick, a band of 10,000 bp or more). A trigger
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

### Fixed

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

# Changelog

What each release changed, and what to change in a bot. While the SDK is
pre-1.0, a breaking change bumps the minor version (README §4). The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

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

# Loaf Python API Bot Template

A batteries-included **Python SDK + bot template** for the [Loaf](https://loafmarkets.com)
trading platform. It wraps the trading-facing REST endpoints and the real-time
WebSocket feed so you can build a bot that reads market data, tracks a
portfolio, and places trades in a few lines.

> [!IMPORTANT]
> **This SDK is experimental and moves fast.** Loaf is in testnet development,
> so expect heavy changes while it is — breaking ones included. Track `main`, or
> pin a tag deliberately and re-read this README before you move off the pin;
> [§4 Updating](#4-updating) has the versioning contract. A stable SDK lands
> with mainnet, and release notes go out on
> [@CohenLoaf](https://x.com/CohenLoaf).

```python
from loaf import LoafClient

loaf = LoafClient()   # reads $LOAF_TRADING_KEY (see §2)

print(loaf.portfolio.summary().balances)                   # your balances (base units)
print(loaf.market.list())                                  # listed markets
loaf.orders.limit_buy("MUS", quantity=10, price=167.49)    # signed locally with your trading key
```

---

## 1. Install

```bash
git clone <this repo> && cd loaf-python-api-bot-template
python -m venv .venv && source .venv/bin/activate

# Install the SDK (httpx + websockets). Add extras as needed:
pip install -e .              # core
pip install -e ".[dotenv]"    # + auto-load a .env file
pip install -e ".[dev]"       # + pytest for the test suite
```

Requires Python 3.9+.

## 2. Get your trading key

The SDK needs **one secret**: your **trading key**, `LOAF_TRADING_KEY` in your
`.env`. It is the one string the Loaf web app's **API** page shows (log in →
**API**):

```
loaf_tk_<l2ChainId>_<l2AccountIndex>_<80 hex characters>
```

- **What it is.** The key your trading account signs its orders with (its K1
  api key 0), with the account it belongs to and the L2 chain of the Loaf
  deployment that showed it. Copy it whole: the SDK refuses anything else,
  a bare 80-hex key included, with one message that never repeats what it was
  given.
- **How to get it.** Trading has to be enabled first: deposit (your first
  deposit opens your trading account), then select **Enable trading** in the
  order panel on a market's Trade page and wait until it is active. Then, on the
  API page, under **Trading key**, select **Show trading key**: your wallet
  signs once, and the page shows the key if it is the one in force (right after
  enabling or a reset, try again in a moment). Your account has one trading
  key, shared by the web app and all your bots: it is the same at every showing
  until you reset it, and the page does not save it.
- **What it does.** It signs every order you place, and every perps margin
  move, exit and withdrawal, on your machine; the key itself is never sent.
  Everything else the SDK does for you (reads, cancels, the account read and
  order-number reservations an order needs, perps margin moves, exits and
  withdrawals and the nonce read before each, and your private WebSocket
  channel) is authenticated by a
  **login token** the SDK signs with it: a short-lived Bearer (`loaf_lt_…`)
  for your account, which lives 10 minutes, and which the SDK signs afresh
  once half of that has passed. The exchange takes one only before its expiry
  and at most 15 minutes ahead of its own clock, so this machine's clock must
  be within about 5 minutes of the exchange's: keep it synchronized (NTP).
- **What it cannot do.** Whoever holds it can read your account and trade its
  balance on its vault (the ledger the markets settle on) away, against orders
  of their own: keep it, and the `.env` that holds it, as secret as your wallet
  key. It can withdraw USDC from your account, but only to your own wallet: a
  withdrawal it signs names no recipient, and the exchange pays your account's
  withdrawals to the wallet whose first deposit opened it, and to no other
  (see **Withdrawals**). It cannot move your wallet's funds: deposits, and
  everything else in the web app, need your wallet or a web-app login.
  **Reset trading key** in the web app (in Account Settings) revokes it, for
  the web app and every bot alike.

```bash
cp .env.example .env
chmod 600 .env   # only you can read it: it holds your trading key
# edit .env:
#   LOAF_TRADING_KEY=<the loaf_tk_... string, whole>
#   LOAF_API_BASE_URL=https://api.loafmarkets.com/api   # or http://localhost:8005/api for local dev
#   LOAF_MAX_SLIPPAGE_BPS=200   # optional: max slippage in basis points (100 = 1%), see Placing orders
```

The SDK reads environment variables; `bot.py` and the examples load `.env`
into them when python-dotenv is installed (`pip install -e ".[dotenv]"`).
Otherwise export the variables.

Without `LOAF_TRADING_KEY` only the public market data works: every other call
raises `LoafConfigError` naming it. `loaf.public_key` is your trading key's
public key (`None` when no key is set): what `GET /l2/keys` names
`journaledKey` while the key is in force.

**When it changes.** After a Reset trading key, show the new key once it is
active (until then the old one stays in force) and restart the bot with it.
Once the new key is active, the old one's login tokens are refused: the bot's
next request raises `LoafAuthError` saying its key is not the one in force. Its
WebSocket connection keeps your private stream only until the server refuses
the connection's next fresh login token, at most 5 minutes later, or, if none
reaches the server, until its token expires (`4001 expired`), at most 10
minutes later; with `portfolio` subscribed the client then reconnects, and
every attempt is refused (see **Real-time WebSocket**). An order carries no
login token, though, and is signed at the account epoch the SDK read before the
reset, so the bot's next order may be refused with code `SETTLE_EPOCH_MISMATCH`
(`LoafValidationError`) or `INVALID_SIGNATURE` (`LoafAuthError`) instead; the
order after it reads the account again and gets the explained `LoafAuthError`. Also cancel and re-arm every stop, take and TP/SL leg
(see **Stop & take orders**): those signed with the old key stay ARMED but FAIL
when they fire.

## 3. Run the bot template

```bash
python bot.py
```

`bot.py` verifies your trading key, prints balances/holdings, opens the live
feed (order book, halt status and your private portfolio stream), and runs a
5-second strategy loop with clearly-marked `# YOUR STRATEGY GOES
HERE` hooks. It only *observes* the market out of the box — drop your logic into
`Strategy.on_tick`.

It follows the market named by `TARGET_TICKER` at the top of the file — a
market's ticker such as `OPRA` — or the first tradeable listed market when that
is empty. While none trades it follows the first `LIVE` one with an L2 market,
whose halt or start the loop waits out (while the platform-wide halt is on,
every market reads halted), else the first listed one.

Each tick runs the strategy only while the market trades: `LIVE`, with an L2
market, not halted and past its `tradingStartsAt`. Otherwise the tick prints why
(`[PENDING]`, `[HALTED]`, `[NOT STARTED]`, ...) and skips it. No frame announces
a listing or a trading start, so until the market trades each tick reads it
again (a halt aside: the feed announces its end). Once it trades, the gate uses
the market as last read: a change only a read shows (unlisted, delisted, a later
start) reaches it when an order is refused for it (set `needs_seed`, as
`bot.py`'s strategy notes say) or when the feed reconnects.

> **Copy it before you edit it.** `bot.py` is tracked by this repo and ships
> alongside the SDK, so it changes when the SDK does. Strategy code written into
> it directly will collide with the next `git pull`. Work in a copy:
>
> ```bash
> cp bot.py my_bot.py   # then run: python my_bot.py
> ```

## 4. Updating

```bash
git pull
```

Step 1 installs the SDK in editable mode, so the working tree *is* the installed
package — a pull is enough for code changes and the next run picks them up with
no reinstall.

Re-run the install after a pull that changes `pyproject.toml`, because an
editable install tracks code but not metadata:

```bash
pip install -e .          # only needed when dependencies change
```

That also refreshes what `pip show loaf-bot` reports. Until you re-run it, `pip`
keeps naming the version you first installed even though the SDK itself is
current — `loaf.__version__` and the `User-Agent` the client sends are always
read from the working tree, so they stay accurate either way.

To pin to a release rather than track `main`:

```bash
git checkout v0.6.0
```

Releases are tagged from `v0.3.0` onward. While the SDK is pre-1.0, a breaking
change bumps the **minor** version — so `0.5.x` → `0.6.0` is the signal to
re-read this README before upgrading. [CHANGELOG.md](CHANGELOG.md) lists what
each release changed and what to change in a bot (0.6.0 moved the perps reads
to the portfolio and the market, and gave every order refusal a `code`; 0.5.0
replaced the API key and the bare 80-hex `LOAF_SECRET_KEY` with the one
`LOAF_TRADING_KEY`, §2). Expect that signal often while Loaf is on testnet; an
old pin is the most common reason a bot stops matching these docs.

---

## The client

Create one `LoafClient` and reach everything through grouped resources:

| Resource | What it covers |
| --- | --- |
| `loaf.market` | markets, market detail (a perp's terms and live state), candle history, trade backfill (all public) |
| `loaf.orders` | place (signed) and replace orders, stop & take (conditional) orders; cancel one, all, a market's or a tag's, on a timer (`cancel_after`) or by order number |
| `loaf.perps` | your margin pools and positions, margin moves and exits (signed) |
| `loaf.funds` | withdraw USDC to your own wallet (signed) |
| `loaf.portfolio` | balances, holdings, perps and your perps leverage setting; recent activity and fees |
| `loaf.history` | paginated order, trade & funding history, cancelled & active orders |

Responses are `LoafObject`s — dicts that also allow attribute access, so
`book.bids[0].price` and `book["bids"][0]["price"]` are equivalent. Unknown
fields the API adds later are preserved automatically. See `loaf/models.py` for
typed descriptions of every documented shape.

### Endpoint coverage

The trading-facing endpoints are wrapped (account management, KYC, referrals,
competitions, IPO offerings and fiat ramps are not part of this SDK):

```
market.list()                         GET    /trade
market.get(ticker)                    GET    /trade/{ticker}         (a perp's market.l2: terms; market.live: live state)
market.candles(ticker, resolution)    GET    /trade/{ticker}/candles
market.iter_candles(ticker, res)      (auto-paginate candle history)
market.trades(ticker)                 GET    /trade/{ticker}/trades

orders.create(...) / limit_buy / ...  POST   /orders                (signed; no login token)
orders.create_conditional(...)        POST   /orders/conditional    (signed; no login token)
orders.stop_loss / take_profit(...)   POST   /orders/conditional    (signed; no login token)
orders.resubmit(signed_body)          POST   /orders or /orders/conditional (re-send unchanged)
orders.cancel(order_id)               POST   /orders/cancel
orders.cancel_conditional(order_id)   POST   /orders/conditional/cancel
orders.cancel_row(order)              (routes a row to the right cancel)
orders.cancel_all(ticker=, order_tag=) POST  /orders/cancel-all     (every order, one market's, one tag's)
orders.cancel_after(timeout_ms, ...)  POST   /orders/cancel-after   (the dead man's switch)
orders.cancel_by_index(order_index)   POST   /orders/cancel-by-index (settle an order by its number)

perps.positions() / pool / position   GET    /portfolio/summary     (its perps: your pools and positions)
perps.collateral()                    GET    /portfolio/summary     (read once: USDC's decimals and tick size)
perps.allocate / add_margin / return_margin
                                      POST   /l2/allocations        (signed; with a login token)
perps.exit(market, pool=)             POST   /l2/exits              (signed; with a login token)
perps.resubmit(signed_body)           POST   /l2/allocations or /l2/exits (re-send unchanged)

funds.withdraw(amount)                POST   /l2/withdrawals        (signed; with a login token)
funds.resubmit(signed_body)           POST   /l2/withdrawals        (re-send unchanged)

portfolio.summary()                   GET    /portfolio/summary     (balances, holdings, perps)
portfolio.component()                 GET    /portfolio/component   (recent activity, fees, lifetime volume, holdings' details, saved leverage)
portfolio.set_perps_leverage(ticker, leverage)
                                      PUT    /portfolio/perps-leverage (your leverage setting on a perp)

history.orders() / trades() / funding()
                                      GET    /history/orders, /history/trades, /history/funding
history.cancelled_orders()            GET    /history/orders/cancelled
history.active_orders()               GET    /history/orders/active
history.iter_orders() / iter_trades() / iter_funding()
                                      (auto-paginate via cursor)
```

Placements are signed with your trading key and carry no login token. Every
other call above that is not public is authenticated by a login token your
trading key signs, and so are the two requests the SDK makes for your orders:
it reads your account's epoch with `GET /l2/keys` and reserves order numbers
with `POST /l2/order-indices`. A perps allocate or exit, and a withdrawal, is
signed with your trading key too, and also carries a login token (the SDK reads
its nonce with `GET /l2/keys` first).

A market (an order book, spot or perp) is named by its ticker: its asset's
ticker, with `-PERP` appended for a perp (`MUS`, `H100-PERP`). `market.get`,
`market.candles`, `market.trades`, orders, `portfolio.set_perps_leverage` and the
WebSocket's market channels take a market's ticker; your holdings name the asset
by its own ticker (a spot market's ticker is its asset's).

`portfolio.component()` answers every section, and one the exchange could not
read just now is `None`, which never means empty: read again, and keep what you
hold of it meanwhile. When no section could be read it raises `LoafServerError`,
or on a 503 `LoafServiceUnavailableError`.

Candle history is a dedicated, paginated endpoint (the market detail response
does not include it):

```python
h = loaf.market.candles("OPRA", "1h", count_back=200)   # 1m|5m|15m|1h|4h|1d|1w
print(h.candles[-1])                  # latest {time, open, high, low, close, volume}
older = loaf.market.candles("OPRA", "1h", to=h.oldestTs)  # page back while h.hasMore
```

---

## Placing orders

Placing an order is a single call:

```python
# Orders are addressed by the market's ticker. Pick a tradeable one from loaf.market.list():
# status "LIVE", not isHalted, hasL2Market, and tradingStartsAt passed (None: no wait).

# LIMIT order (price in dollars, <=2 dp; quantity in tokens, <=1 dp):
res = loaf.orders.limit_buy("MUS", quantity=10, price=167.49)
print(res.orderId, res.status, res.quantityLeft)   # e.g. 991 OPEN 10

# MARKET order: no price. The SDK signs reference x (1 ± max slippage) as your worst fill price:
res = loaf.orders.market_buy("MUS", quantity=2)           # reference fetched; client's max slippage
res = loaf.orders.market_sell("MUS", quantity=2,
                              reference_price=mark, max_slippage_bps=50)   # mark: your markprice; 0.5%
print(res.status, res.quantityLeft)   # FILLED, or CANCELLED with the rest: a MARKET order never rests

# Time in force (create / limit_buy / limit_sell): GTC (default), IOC, FOK or POST_ONLY.
res = loaf.orders.limit_buy("MUS", quantity=10, price=150, time_in_force="POST_ONLY")   # makes, never takes

loaf.orders.cancel_all()      # cancel every open order, stop and take (it sells nothing)
loaf.orders.cancel_all("MUS") # or one market's (see Cancels)
```

**A 200 is sent after the exchange commits the order, so its `status` is
real.** `FILLED` means filled; `OPEN` / `PARTIALLY_FILLED` mean it is resting
with `quantityLeft` tokens to go. `CANCELLED` on a new order means it stopped
early: the unfilled rest of a MARKET or IOC order, a BUY whose hold could not
pay for its next fill, or a SELL stopped at K1's size bound (see **Money &
units**). `attachedLegIds` are the conditional
ids of the TP/SL legs you attached, take-profit first (`[]` without legs). Later
fills and cancels arrive on your private `portfolio` WebSocket channel (see
below).

**Time in force.** `time_in_force=` on `create`, `limit_buy` and `limit_sell`
(`loaf.TimeInForce`): `GTC` (the default) rests what does not fill at once;
`IOC` cancels it, and is refused (400, code `NOT_FILLED`) when nothing fills;
`FOK` fills the whole quantity at once or is refused (400, `FOK_NOT_FILLED`);
`POST_ONLY` (a LIMIT order's alone: the SDK refuses it on a MARKET order) is
refused with `loaf.PostOnlyWouldTakeError` (`POST_ONLY_WOULD_TAKE`) when it
would trade at once, so it only ever makes. Order rows show a post-only order as `GTC`, and a MARKET order
sent as `GTC` as `IOC`. Stops, takes and TP/SL legs book GTC orders.

`duplicate: True` means this exact signed order had already landed (the
exchange's own retry, an SDK re-send or a `resubmit`). Its legs then exist
although `attachedLegIds` is `[]`: find them in
`loaf.portfolio.component().openOrders`, and never attach them again (a second
stop means a second sell).

**MARKET orders take no price: the SDK works out your worst price.**

- You pass the ticker and quantity. Unless you pass `reference_price`, the SDK
  reads the market reference (the order-book mid when both sides exist, else
  the last trade, else the last candle close, else the IPO price) and signs
  `reference × (1 ± max slippage)` (+ for a BUY, − for a SELL), rounded toward
  the reference to the cent, as your worst fill price. With a reference of 160
  and the default 2%, `market_buy` signs 163.20 and `market_sell` 156.80.
- Max slippage is in basis points (100 = 1%). Set it once with
  `LOAF_MAX_SLIPPAGE_BPS` in your `.env` or `LoafClient(max_slippage_bps=150)`;
  the default is 200 (2%), and `loaf.max_slippage_bps` shows the value in use.
  Override it for one order with `max_slippage_bps=`. The same setting prices
  stops, take-profits and TP/SL legs (see **Stop & take orders**).
- The exchange matches a MARKET order against resting orders at their prices,
  never worse than the price you signed, and **never rests it**: what it cannot
  fill at once is cancelled (`CANCELLED`), and one that fills nothing is refused
  (400, code `NOT_FILLED`).
- The reference read is one extra public request; it can lag the live market
  and may not yet show a trade you just made. For price-sensitive orders pass the
  `markprice:{ticker}` value you stream as `reference_price` (once the SDK has
  read the market, that also saves the request).
- If the exchange has no market price yet, the SDK refuses the order locally
  rather than sign it off zero.
- A BUY holds **worst price × quantity × (1 + `L2_FEE_RATE_CAP` / 1,000,000)**
  of your cash: `L2_FEE_RATE_CAP` is the fee-rate cap every order signs (70 bps),
  whatever fee its fills are charged. Size it with the same
  reference and max slippage you sign with:

```python
import math
from loaf import L2_FEE_RATE_CAP, from_base_units, worst_price

quote = loaf.market.get("MUS").market.l2.quoteAssetId   # the market's cash asset
row = next((b for b in loaf.portfolio.summary().balances if b.assetId == quote), None)
cash = float(from_base_units(row.available, row.decimals)) if row else 0.0
worst = worst_price(mark, "BUY", loaf.max_slippage_bps)  # what market_buy(..., reference_price=mark) signs
qty = math.floor(cash / (worst * (1 + L2_FEE_RATE_CAP / 1_000_000)) * 10) / 10
res = loaf.orders.market_buy("MUS", quantity=qty, reference_price=mark)  # same reference
```

A few checks run server-side that the SDK cannot pre-validate, so handle their
rejections rather than trying to avoid them. Every refusal carries a `code`
(`e.code`, one of `loaf.OrderErrorCode`): branch on it, never on the message,
which is for people and may change.

| Rejection | Code | Rule |
| --- | --- | --- |
| `LoafValidationError` (400) | `BELOW_MIN_ORDER_VALUE` | **Minimum order value** — `price x quantity` must be at least $10, measured at the price you **sign** (a MARKET order's worst price). A SELL closing your *entire* available holding is exempt, so dust can always be flattened. |
| `LoafValidationError` (400) | `PRICE_BAND` | **Limit price deviation** — any order, MARKET included, whose signed price is too far from the market reference is refused. The ceiling is a deployment setting; keep your max slippage small. |
| `LoafValidationError` (400) | `PRICE_BAND` | **Daily price band** — a per-market band around the daily reference price, when enabled. The message carries the price, limit, reference and band width. |
| `LoafValidationError` (400) | `INSUFFICIENT_BALANCE`, `OPEN_ORDER_LIMIT` | **Balance and open-order caps** — not enough available balance for the order, or too many open orders (resting stops count too). |
| `LoafValidationError` (400) | `FEE_RATE_CAP_TOO_LOW` | **Fee-rate cap** — your account's fee rate is above the cap every order signs. |

An order that would trade with a resting order of your own is not refused: the
exchange cancels the resting one (its `order_status` is `CANCELLED`) and the
new order goes on matching.

Markets trade around the clock. A market that does not trade (before it is
listed, before its trading starts at its `tradingStartsAt`, or once it is
delisted) refuses placement with `LoafForbiddenError` (403), and an unknown one
with `LoafNotFoundError` (404). After a 404, or a 403 for a market that is not
listed, the SDK reads the ticker's market again for your next order.

If trading is halted — platform-wide or for that market — order placement
raises `TradingHaltedError` (403); a market's own halt raises its subclass
`MarketHaltedError` (below). A platform-wide halt blocks cancels too,
except `orders.cancel_conditional()`, which the matching engine takes under any
halt and stays available (see **Stop & take orders** below). `TradingHaltedError` also covers
the exchange engine's own halts (a 400): wait them out; cancels still work.

The exchange engine also keeps each market's own state, and refuses an order
it does not take now with `MarketStateError` (403), its `.state` the reason:
`MARKET_HALTED` (`MarketHaltedError`, a `TradingHaltedError` too),
`MARKET_CLOSED`, `REDUCE_ONLY`, or `PRICE_STALE`; and `PriceHeldError` (state
`REDUCE_ONLY`) while a perps market's latest price waits for review. A
reduce-only or stale-price market still takes an order that reduces a position.

To see a halt coming instead of discovering it on a rejected order, subscribe to
the market's status channel:

```python
ws.subscribe_market_status("MUS")

@ws.on_market_halt
def on_halt(msg):
    print(msg.ticker, "halted" if msg.isHalted else "resumed")
```

`isHalted` is the **effective** state — the market's own flag OR'd with the
platform-wide kill switch — so assign it straight over the `isHalted` you seeded
from `market.get("MUS").market.isHalted`. Note a global halt lifting does not
resume a market that is individually halted; the frame accounts for that.

### Order life

Every order expires. `ttl_ms=` on any placement sets how long it may rest, from
one minute (`60_000`) to about 45.9 days; it is 7 days when omitted. The SDK
signs the expiry `l2ExpiryMs` = now + 72 hours + `ttl_ms` (`expiryMs` on order
rows): the exchange stops filling the order 72 hours before it and cancels it, a
`CANCELLED` you did not request. TP/SL legs share their
parent's life. An ARMED stop, take or leg is not cancelled when its life ends:
one that fires later FAILS, so cancel and re-arm stops before then (see
**Stop & take orders**). A bot's quotes should die with it, so give them a short
life:

```python
loaf.orders.limit_buy("MUS", quantity=10, price=150, ttl_ms=3_600_000)   # rests for an hour at most
```

The expiry is signed from this machine's clock, so keep it synchronized (NTP):
a clock that is behind shortens every order's life by its lag, and an order
whose `ttl_ms` is shorter than the lag is refused (`LoafValidationError`, 400
`l2 expiry: the expiry is inside the buffer`). One that is ahead lengthens every
order's life by its lead, and an order is refused (`LoafConfigError`) only when
the lead takes its expiry past what the exchange accepts: a few hours for the
longest `ttl_ms`, weeks for the default and shorter.

### Order controls

`create` and its four wrappers (not the stops and takes) take three controls
that travel with the order but are not signed:

```python
import time

quote = loaf.orders.limit_buy("MUS", quantity=10, price=150, ttl_ms=600_000,
                              order_tag=7)                 # your label for the bot's quotes
now_ms = time.time_ns() // 1_000_000
loaf.orders.limit_buy("MUS", quantity=10, price=150.5,
                      order_tag=7,
                      send_by_ms=now_ms + 2_000,           # decided within 2 s, or not at all
                      replaces_order_id=quote.orderId)     # the old quote is cancelled first
```

- **`order_tag=`**, a whole number from 1 to 4,294,967,295: your label for a
  group of orders. The order keeps it while it rests; `cancel_all` and
  `cancel_after` can select by it, and its rows and its `order_status` /
  `order_update` frames carry it as `orderTag`, with its `l2OrderIndex`.
- **`send_by_ms=`**, unix ms: the latest time the exchange may decide the
  order, by its own clock. Arriving later, or decided later, it is refused
  with `loaf.SendByPassedError` (`SEND_BY_PASSED`): nothing is placed and the
  order number is not spent. A quote delayed in transit never lands late.
- **`replaces_order_id=`**: your order resting on the same market, which the
  exchange cancels in the step that decides the new order, before matching
  it: the two never rest together, the old order's hold is free for the new
  one's checks, and the open-order cap counts one order less. If the old
  order no longer rests there (it filled, was cancelled or expired), the new
  one is refused with `loaf.ReplaceTargetGoneError` (409,
  `REPLACE_TARGET_GONE`) and nothing changes. If the cancel went through and
  the new order is then refused for another reason, the old one stays
  cancelled, and the error's `replaced_order_cancelled` is `True`.

### Cancels

```python
loaf.orders.cancel(order_id)                       # one order
loaf.orders.cancel_all()                           # every open order, stop and take
loaf.orders.cancel_all("MUS")                      # one market's
loaf.orders.cancel_all(order_tag=7)                # one tag's (orders only: a stop carries no tag)
loaf.orders.cancel_after(60_000, order_tag=7)      # the dead man's switch: see below
loaf.orders.cancel_by_index(e.signed_body["l2OrderIndex"])   # settle a lost placement
```

`cancel_all` cancels the scope's resting orders in one step and answers
`{requestedCount, cancelledOrderIds, failedOrders}`; an untagged scope also
cancels its `PENDING` / `ARMED` stops and takes. `failedOrders` lists orders on
a market where the request did not complete; they stay open, and calling it
again is safe. `ticker=` costs one public read the first time it names a
market no order of yours was placed on from this client.

**The dead man's switch.** `cancel_after(timeout_ms, ticker=, order_tag=)` asks
the exchange to cancel a scope's orders, as `cancel_all` with that scope
would, unless you call it again within `timeout_ms` (10,000 to 120,000; 0
turns the scope's timer off). Each scope has its own timer, each call
replaces its deadline, and the answer is `{deadlineMs}` on the exchange's
clock. Call it every 15 to 30 s with a timeout of 60,000 to 120,000, **from
the loop that quotes**, never from a thread of its own: it is there to fire
when that loop stops. Scope it to your bot's `order_tag` so it never cancels
the orders you place by hand. The deadline is the exchange's and durable: one
that passes while its engine is down fires as it starts, before any order.
It is taken under a platform-wide halt, which blocks the other cancels, and
the exchange takes at most one call a second per account (`LoafRateLimitError`
past that); a 503 (`NOT_PROCESSED` or `SERVICE_UNAVAILABLE`) leaves the
previous deadline standing, so call again.

**Settling an order by its number.** `cancel_by_index(order_index)` settles
the order that signed `order_index` (`l2OrderIndex`) with no read: `CANCELLED`
(it was resting, and is cancelled now), `ENDED` (it was placed and no longer
rests: `status` says how it ended) or `VOIDED` (no order was placed with the
number, and none ever will be: an attempt still on its way is refused
`ORDER_INDEX_USED` whenever it arrives). It is how to settle an
`OrderOutcomeUnknownError` you no longer want (below). A repeat answers
`ENDED` or `VOIDED`, and the SDK never signs a number you passed it again. A
stop or take that holds the number is refused there (400 `INVALID_REQUEST`,
naming its id): cancel it with `cancel_conditional`.

Every cancel but `cancel_conditional` and `cancel_after` is refused under a
platform-wide halt (`TradingHaltedError`). Cancels are never retried by the
client: a 503 means the exchange did not process it or could not tell, and a
repeat is safe.

### Signing & retries

- The SDK signs every order on your machine with your trading key (K1, the
  exchange's signature scheme); the key itself is never sent. The request
  carries that signature and no login token. The order signs your trading
  key's account and the market's L2 chain: a market on another chain than the
  key's (another deployment's key) is refused with `LoafConfigError` before
  anything is signed.
- The SDK re-sends the **exact** signed order on timeouts and server errors,
  after a short pause, so a re-send can never place it twice: the exchange
  admits one order per account and order number, and answers a repeat with
  the same `orderId` (and `duplicate: True` for a plain order). Each attempt
  is given at least 45 seconds to answer, because the exchange replies only
  once the order is committed. With the default 3 retries a call can
  therefore block for a few minutes while the exchange is struggling; use
  `LoafClient(max_retries=1)` in a latency-sensitive loop. To wait less, set
  `LoafClient(placement_timeout=2.0)`: each attempt then gives up after that many
  seconds. An order you gave up on can still land, so it ends in
  `OrderOutcomeUnknownError` like any other timeout, and its order number stays
  spent.
- Two rules cover every outcome:
  - **Any error other than `OrderOutcomeUnknownError` means the order was not
    placed.** Place it again if you still want it.
  - **`OrderOutcomeUnknownError` means it may be live**, and may already have
    filled, so never answer it with a fresh order for the same intent. While
    you still want it and it can still rest,
    `loaf.orders.resubmit(e.signed_body)`: the answer is that same order if it
    landed; if not, it is placed now, at the price and quantity you signed
    then. A stop or take that landed is answered with its row in whatever
    status it has now (a re-send never re-arms one that was cancelled, fired
    or FAILED): read that row's `status` in `loaf.history.orders()` before
    relying on it, and arm a new one if it is `CANCELLED` or `FAILED`. Anyone
    who holds `e.signed_body` can place it the same way, with no key, so keep
    it out of logs and alerts. Once you no longer want it, settle it in one
    call with `loaf.orders.cancel_by_index(e.signed_body["l2OrderIndex"])`:
    `CANCELLED`, `ENDED` (`status` says how) or `VOIDED` (never placed, and
    never will be; see **Cancels**). Or find it in `loaf.history.orders()` by
    its `expiryMs` (`e.signed_body["l2ExpiryMs"]`): a miss proves nothing while
    it can still rest (until `expiryMs` minus 72 hours, its `ttl_ms` after
    signing). A body whose `sendByMs` has passed is not re-sent, since the
    exchange would refuse it before looking for the first attempt: settle it
    with `cancel_by_index`, or resubmit it with a later `send_by_ms=` to have
    it placed now if it never was.
- **The order-number allowance.** Every order signs an order number reserved to
  your account, and the exchange caps how many an account holds reserved but
  unused. Past the cap every reservation is refused (`l2 order_index: the
  account would hold more than … reserved indices no order uses`), and nothing
  can be placed on the account, web app included, for up to about 49.5 days.
  The SDK reuses the numbers of an order that was refused, but each time the bot
  stops, the rest of the numbers it last reserved stay unused: up to 64, fewer
  after only a few orders. So keep a bot running rather than restarting it
  often, and restart it after a crash with a growing delay. Many stops that never fire, or
  many processes on one account, add up too.
- **Order numbers for a fast bot.** The exchange takes one reservation a second per
  account, so a block of N numbers sustains about N orders a second. The defaults
  start at one number and double up to 64, which keeps a restart cheap but pauses
  a new process's early orders on that limit. A quoting bot can set:
  ```python
  client = LoafClient(
      order_number_start=256,   # the first reservation's size
      order_number_block=256,   # the largest, up to 4096
      reserve_ahead=True,       # reserve the next block on a background thread
  )
  client.orders.warm("BTC-PERP")  # read the market and account, reserve now
  ```
  With `reserve_ahead`, the next block is reserved once the current one is half
  used, so a placement waits on neither that request nor the limit, unless it
  outruns them: then it waits for the one in flight. A background failure is logged
  (logger `loaf._l2`) and retried, and placements still reserve for themselves and
  raise what they meet. Larger blocks leave more numbers unused at each stop, and
  a stop leaves up to two blocks with `reserve_ahead`, so weigh them against the
  allowance above. Call `client.close()` to stop the thread.
- The client keeps the trading key only inside its signer and never logs it,
  and the same goes for its login tokens, which are credentials too until they
  expire: none is ever put in a log, an error or a `repr`. Prefer
  `$LOAF_TRADING_KEY` over `trading_key=`: an explicit argument can show up in
  tools that capture call frames (crash reporters, debuggers), and so can a
  Ctrl-C that lands while an order is being signed, so don't let a crash
  reporter record local variables in a trading bot.

```python
from loaf import OrderOutcomeUnknownError

try:
    res = loaf.orders.market_buy("MUS", quantity=2)
except OrderOutcomeUnknownError as e:
    res = loaf.orders.resubmit(e.signed_body)   # the same order: placed at most once
    # (raises OrderOutcomeUnknownError again if this re-send can't tell either)
    # No longer wanted? Settle it for good instead:
    # loaf.orders.cancel_by_index(e.signed_body["l2OrderIndex"])
```

### Stop & take orders

A stop or take order rests in the backend until the mark price reaches your
trigger, then books as an ordinary limit order signed when you placed it.
Nothing is held until it books, but it does occupy an open-order slot. It has
an order's life too (`ttl_ms=`, 7 days by default), but nothing removes it when
that ends: it stays ARMED, in its slot, until you cancel it or it fires (and
FAILS). So cancel and re-arm a stop before its life ends.

```python
# Protect a long: SELL 5 if the mark falls to 90, take profit if it rises to 120.
# (Booking prices here assume the default 2% max slippage.)
sl = loaf.orders.stop_loss("MUS", quantity=5, trigger_price=90)      # books a SELL at 88.20
tp = loaf.orders.take_profit("MUS", quantity=5, trigger_price=120)   # books a SELL at 117.60

# Any of the eight type x side combinations, spelled out:
loaf.orders.create_conditional("MUS", "BUY", quantity=2,
                               type="STOP_MARKET", trigger_price=130)   # breakout; books at 132.60
loaf.orders.create_conditional("MUS", "SELL", quantity=5,
                               type="STOP_LIMIT", trigger_price=90, price=89.5)

# The same two legs attached to a BUY — these ARE OCO, one firing cancels the other
# (they sell no lower than 117.60 / 88.20):
loaf.orders.limit_buy("MUS", quantity=10, price=100, tp_price=120, sl_price=90)

# More room below the trigger than the client's max slippage, for this order only:
loaf.orders.stop_loss("MUS", quantity=5, trigger_price=90, max_slippage_bps=500)   # books at 85.50

loaf.orders.cancel_conditional(sl.orderId)   # while PENDING / ARMED
```

Direction comes from the type **and** the side, not the type alone:

| Type | A BUY fires when the mark | A SELL fires when the mark |
| --- | --- | --- |
| `STOP_MARKET` / `STOP_LIMIT` | **rises** to the trigger | **falls** to the trigger |
| `TAKE_MARKET` / `TAKE_LIMIT` | **falls** to the trigger | **rises** to the trigger |

**A 200 means the row is ARMED — not that it will ever book.** Your holding and
cash are not checked until the row fires; a row that fires short of funds
cancels your own resting orders on its market and side, and tries once more.
Every trigger-time failure (holding gone, cash gone, price band, limit deviation,
minimum value *again*, engine rejection) raises nothing; it arrives as
`status: "FAILED"` with a `rejectionReason` on your `portfolio` channel. A
`rejectionReason` on a row that is still `ARMED` means deferred-and-retrying,
not dead. The exchange checks a row's price grid and order number only when it
fires too, so a FAILED `order_update` is also how a stale row shows.

| Rejection | Rule |
| --- | --- |
| `LoafValidationError` (400) | **Already through the trigger** — refused if the mark has reached your level. Triggering is level-based, so a trigger *equal* to the mark is refused too. Compare your trigger against the mark yourself (`markprice:{ticker}`); if the mark is already there, place a plain order. |
| `LoafValidationError` (400) | **Open-order caps** — resting conditionals count against the same caps as booked orders. |
| `LoafValidationError` (400) | **Minimum order value** — the same floor as a plain order, measured at the price the row will *book* at — for a `*_MARKET` type the trigger moved by your max slippage. A SELL for your whole holding is exempt. Re-checked when the row fires. |

A `*_MARKET` type is a **protected limit, not a market order**. The SDK signs
`trigger × (1 ± max slippage)` (the client's, or `max_slippage_bps=` on the
call), rounded toward the trigger to the cent, as the price it books at, fixed
for life, so after a gap past that price the booked order rests instead of
executing. The quantity is fixed too and is never clamped — a SELL whose
holding shrank below it FAILS rather than selling what is left. TP/SL legs
likewise sell no lower than `trigger × (1 − max slippage)`, using the client's
value rather than the parent order's `max_slippage_bps=`, so a tight entry
never tightens your stops; pass `leg_max_slippage_bps=` to change it.

**Legs on a MARKET parent are measured against the market reference, not
against your fill.** The server checks `tp_price` / `sl_price` against the
market reference — not your worst price and not the price you end up filling
at. A `tp_price` just above the reference can sit below your fill; the leg then
arms already through its trigger and sells at once, no lower than its signed
price. Leave the take-profit clear of the mark, or place it yourself once you
see the fill. A leg's own price is first checked (deviation, band, minimum
value) when it fires; if that fails the leg becomes FAILED. On a spot market a
parent that ends partly filled (the cancelled rest of a MARKET or IOC order, or
your cancel) FAILs its legs; a perp's legs arm on the parent's first fill.

**Each stop or take holds one order number until it fires**, and one cancelled
or expired unfired keeps counting against your account's allowance (see
**Signing & retries**) for about 49.5 days: move a trailing stop on meaningful
moves, never on every tick.

**After a Reset trading key, or a cancel-all sent on chain** (the web app's
Recovery page), the exchange has cancelled your resting orders, but stops,
takes and TP/SL legs signed before it stay ARMED and FAIL when they fire:
cancel and re-arm them. A leg comes only with a new BUY, so re-arm one as a
separate `stop_loss` / `take_profit`; unlike legs, a separate pair is not OCO,
so once one fires, cancel the other.

**Two ids, two cancel routes.** A `PENDING` / `ARMED` row is cancelled with
`loaf.orders.cancel_conditional(row.id)`; once it reaches `PLACED` the trigger
has booked an ordinary order, which you cancel with
`loaf.orders.cancel(row.placedOrderId)`. Unsure which you are holding? Hand the
row to `loaf.orders.cancel_row(row)` and it picks. `cancel_conditional` still
works during a platform-wide halt while `cancel` and `cancel_all` do not, so it
is your only lever if you don't want stops firing into the reopen.

**Reading rows.** `loaf.portfolio.component().openOrders` and
`loaf.history.orders()` mix both kinds — narrow with
`loaf.is_conditional_order(row)` before touching `filledQuantity`, which a
conditional does not carry. `loaf.history.active_orders()` and
`loaf.history.cancelled_orders()` are booked-orders-only and never show them.

Derive triggers with `round(mark * 0.95, 2)`: an unrounded float product carries
17 decimals and the SDK rejects it locally before the request goes out.

### Perps

A perp (its ticker is its asset's with `-PERP` appended, its `market.type`
`"PERP"`) trades a perpetual future settled in USDC: a BUY opens or grows a
long (or shrinks a short), a SELL the other way round. Your margin is USDC held
in **pools**: pool 0, the cross pool, and the isolated pools 1 to 7, each
holding at most 8 positions margined by that pool's collateral alone. Fund a
pool from your spot USDC, trade from it, and move what is left back:

```python
perp = loaf.market.get("MUS-PERP").market             # type "PERP", its marketIndex, l2 (terms), live (state)
perp.live.markPrice, perp.live.indexPrice             # the mark positions are valued at; the oracle's index
loaf.perps.add_margin(5_000, pool=0)                  # 5,000 USDC: spot -> pool 0
loaf.orders.limit_buy("MUS-PERP", quantity=2, price=101.5, pool=0)   # a long from pool 0
loaf.orders.limit_sell("MUS-PERP", quantity=2, price=104, pool=0, reduce_only=True)
loaf.perps.position(market=perp.marketIndex, pool=0)  # base, entryPrice, notional, liquidationPrice, ...
loaf.history.funding()                                # your funding payments, newest first
loaf.portfolio.set_perps_leverage("MUS-PERP", 5)      # your leverage setting on it (the web app's)
loaf.perps.return_margin(1_000.25, pool=0)            # pool 0 -> spot
```

Orders name a perp by its ticker; the perps ops and a position name it by its
`marketIndex` (0 to 254), as the exchange's perps API does. A position also
carries its `ticker`, and `market.list()` rows, or any detail's `marketList`,
map an index to its ticker for `market.get`. A `marketList` row of a perp also
carries its `live` figures (`markPrice`, `openInterest`, `fundingRateCbp`,
`fundingPeriodMs`), and an offering's row (`type` `IPO`) stands in for its
asset's spot market while that is missing or `PENDING`, its `marketIndex`
then `None` without one.

- **Orders** take `pool=` (0 by default) and `reduce_only=True` (its fills
  may only shrink the pool's position), signed with the order; both are
  refused on a spot market. TP/SL legs (`tp_price=`, `sl_price=`) attach to a
  perps BUY or SELL, never to a reduce-only order: each leg is a reduce-only
  order on the other side in the order's pool. A SELL's are mirrored:
  `tp_price < price < sl_price`, e.g.
  `limit_sell("MUS-PERP", 2, 104, tp_price=95, sl_price=110)`.
- **Allocates** (`add_margin`, `return_margin`, `allocate(amount, from_side=,
  to_side=)`, a side a pool or `loaf.PERPS_SPOT_SIDE`, 255, your spot balance)
  and **exits** (`exit(market, pool=)`: closes your position in an EXPIRED
  market, which takes no orders) are signed with your trading key and sent
  with a login token it signs. Each spends your account's next nonce, the
  counter your withdrawals spend too; the SDK reads it for every op. One
  applied elsewhere between that read and the send makes the exchange refuse
  this one (422, `l2 money: the nonce is not the api key's nonce …`): send it
  again. An op is valid for `ttl_ms` (default 10 minutes).
- **Retries** work as for orders: timeouts and 5xx re-send the same signed
  body, which the exchange applies once at most (`duplicate: True` on a repeat
  of an applied one). Any error but `PerpsOutcomeUnknownError` means it was
  not applied; on that one, `loaf.perps.resubmit(e.signed_body)`, never a
  fresh op for the same intent.
- **Units.** An allocate's `amount` is in USDC. The exchange moves whole
  ticks of it, so the SDK counts the amount in the collateral that
  your portfolio summary serves (`perps.collateral()`: ticks = amount x
  10^`decimals` / `tickSize`). It refuses an amount that is not a whole number
  of ticks, and a reply without a usable collateral: it never assumes one.
  An exit moves no amount, so it never needs the collateral.
  The reads are the engine's units: `positions()` serves amounts as decimal
  strings, collateral, value, funding owed and notional in extended units
  (ticks x the collateral's `extensionMultiplier`), a position's `base` in an
  order's signed quantity units (tokens x 10, negative for a short), prices in
  the market's price units (its docstring has the rest).
- **Market state.** A perp can refuse orders for its own state (403,
  `MarketStateError` with `.state`): halted, outside its trading hours,
  reduce-only, its price stale, or reduce-only while its latest price waits
  for review (`PriceHeldError`). In the last three a `reduce_only=True` order
  is still taken. `market.get(ticker).market.live` (and the `perps:markets`
  channel) serves each market's state as the engine decides it, before you
  send: `tradingState`, `stateReason`, `halted`, `heldPrice`, and `riskOnly`
  (the engine's risk-only mode: only an order that reduces is taken, and no
  withdrawal, whatever `tradingState` says), all as of `asOfMs`.
  An EXPIRED perp's `tradingState` and `stateReason` are the exchange's,
  `HALTED` and `MARKET_CLOSED`.
- **The mark.** A perp's `markPrice` is not its `indexPrice`: margin, PnL and
  liquidations are valued at the mark, within 2 % of the index, and stops and
  takes fire on it. Order price bands, funding (its premium and its payments,
  at a rate in centi-bp of the index) and a final settlement stay on the index.
- **Leverage.** `loaf.portfolio.set_perps_leverage(ticker, leverage)` saves
  your leverage on a perp: a whole number from 1 to the market's `maxLeverage`.
  `portfolio.component().perpsLeverage` lists the ones saved, `{ticker,
  leverage}`, and `ws.on_perps_leverage` brings each one saved; a market with
  none is at `loaf.PERPS_DEFAULT_LEVERAGE` (3), and any leverage reads at most
  the market's most. It is the web app's setting, as on Hyperliquid: the web
  app sizes an order's margin by it, and shows a position's margin
  `notional / leverage` and ROE `unrealized PnL / (entryQuote x 10 x qem /
  leverage)`. The exchange's margin checks never read it: they use the
  market's terms (`risk`, any initial-margin tiers, and a tenth of a pool's
  notional when collateral leaves it), so no order or allocate of the SDK
  reads it.
- **Live, no polling.** `ws.on_perps_account` replaces your pools after every
  commit that touches them, and `on_trade` and `on_funding_payment` bring your
  fills and payments. Between pushes, from each `perps:markets` row
  (`on_perps_markets_live`, about once a second):
  `notional = |base| x markPrice x qem`,
  `fundingOwed = base x (fundingIndex - position.fundingIndex)`,
  `value = collateral + sum(±notional - fundingOwed)`,
  `requirement = sum(risk x notional)`, unrealized PnL
  `= ±(notional - entryQuote x 10 x qem)` (+ long, - short; all extended
  units). A position's liquidation price, every other term held, with
  `k = |base| x qem`, `tro = requirement - risk x notional` and
  `c = value - (±notional)`: a long's
  `ceil((tro - 65536 c) / (k (65536 - risk)))` (none when `tro <= 65536 c`),
  a short's `floor((65536 c - tro) / (k (65536 + risk)))` (0 when that is
  below 0). `requirement` is as served, already x 65,536.
- **History.** `history.trades()` holds every fill of yours (`source`,
  `direction`, `closedPnl`): liquidations, deleverages, exits and settlements
  too, with `txHash` `None` and their journal seq as `tradeId`;
  `history.funding()` your funding payments.

---

## Withdrawals

`loaf.funds.withdraw(amount)` sends USDC from your spot balance to your own
wallet: the wallet whose first deposit opened your exchange account. Nothing
else can receive it. The withdrawal your trading key signs names no recipient,
and the exchange's contract pays a withdrawal of your account to that wallet
alone, so a signed withdrawal can only ever pay you, whoever sends it.

```python
from loaf import WithdrawalOutcomeUnknownError

try:
    res = loaf.funds.withdraw(250)            # 250 USDC: spot balance -> your wallet
except WithdrawalOutcomeUnknownError as e:
    res = loaf.funds.resubmit(e.signed_body)  # the same withdrawal: journaled at most once
print(res.journalSeq, res.amount, res.duplicate)
```

- **What happens.** The exchange journals the withdrawal at once and takes it
  from your spot balance (the 202, `{journalSeq, accountIndex, assetId,
  amount, nonce, duplicate, status}`), then settles it on chain in a batch,
  whose transaction makes it payable to your wallet, and pays it there. Follow
  it in `loaf.portfolio.component().transfers`: a `WITHDRAWAL` row with its
  `journalSeq`, `PENDING` until its batch is on chain, then `CONFIRMED` with
  that batch's `blockchainTxHash`, or `REJECTED` if settlement refused it,
  which leaves the amount in your account. `ws.on_transfer` pushes each change.
- **Units.** `amount` is in USDC (`12.5` is 12.5 USDC). The exchange moves
  whole ticks of it: the SDK counts the amount in the collateral your portfolio
  summary serves (`perps.collateral()`), as an allocate does, and refuses an
  amount that is not a whole number of ticks (nothing is rounded). The 202's
  `amount` is that count of ticks; a transfer row's is in USDC's base units.
- **Life.** `ttl_ms=` (60,000 to 86,400,000; default 3,600,000, an hour) is
  how long the exchange takes the signed withdrawal. It rests nowhere: the
  exchange journals it at once or refuses it.
- **Nonce.** It spends your account's next nonce, the counter perps allocates
  and exits spend too; the SDK reads it for each one. An op sent elsewhere
  (the web app, another process) between that read and the send makes the
  exchange refuse this one (422): send it again.
- **Refusals.** The exchange's rules refuse with 422 (a plain `LoafAPIError`
  with the reason): less than the minimum withdrawal, more than your spot USDC
  available (your resting orders hold some), too many of your withdrawals not
  yet settled, the nonce. A pause is `TradingPausedError` (503, with
  `retry_after`). None of these journaled anything.
- **Retries.** As for orders: a timeout or a 5xx re-sends the same signed
  body, which the exchange journals once at most (`duplicate: True` on a
  repeat). Any error other than `WithdrawalOutcomeUnknownError` means nothing
  was journaled. On that one, `loaf.funds.resubmit(e.signed_body)`, never a
  fresh withdrawal for the same intent: a fresh one signs the next nonce, so
  both could be paid. The exchange takes the body until its `l2ExpiryMs`
  minus 72 hours (its `ttl_ms` after signing); once it refuses a re-send for
  that (400), your account's next nonce (`GET /l2/keys`, `withdrawalNonce`)
  settles it: still the body's `l2Nonce`, it was never journaled and never
  will be.
- **Deposits** are made in the web app: a deposit is signed by your wallet's
  own key, which the SDK never holds.

---

## Money & units

Prices and quantities are plain human units — prices in **dollars**,
quantities in **tokens** — for what you send and the market data you receive:

- Prices take up to 2 decimal places (cents), from 0.01 to 4,294,967.29;
  quantities up to 1 decimal place, from 0.1 to 28,147,497,671,065.5; and an
  order's price × quantity must stay below about $281 billion (K1's 48-bit fill
  quote). All are checked client-side before a request is sent, and off-grid
  input is refused, never rounded.
- The SDK signs every order over scaled integers (price × 1000, quantity × 10)
  internally; nothing you send or receive is scaled.
- **Balances and transfer amounts are the exception**: decimal strings in the
  token's base units (`"1500000"` is 1.5 of a 6-decimal token). Turn one into an
  exact `Decimal` of tokens with `loaf.from_base_units(row.available, row.decimals)`.
- A trade's `fee` is in dollars, to 0.001. A trade row's `closedPnl` is
  dollars, signed, before fees.
- **Perps** amounts are the exchange engine's units (see **Perps**): an
  allocate's `amount` in USDC ticks, and `perps.positions()` values and a
  funding payment's `payment` as decimal strings.
- `*Bps` fields are raw basis points (`30` = 0.30%); use `loaf.bps_to_fraction`.
- `*Percent` / `*Percentage` fields are already percentages (`5.2` = 5.2%).

---

## Real-time WebSocket

A threaded, auto-reconnecting client — register callbacks, subscribe, run. No
asyncio required.

```python
ws = loaf.websocket()

@ws.on_orderbook
def on_book(msg):
    print(msg.ticker, msg.bids[0].price, msg.asks[0].price)

@ws.on_trade            # YOUR fills (private portfolio channel)
def on_fill(msg):
    print("filled", msg.trade.side, msg.trade.quantity, "@", msg.trade.price)

ws.subscribe_orderbook("MUS")
ws.subscribe_trades("MUS")
ws.subscribe_portfolio()            # your private stream

ws.run_forever()                # blocking; or `with loaf.websocket() as ws:` for background
```

Handlers run on the client's event-loop thread, so keep them short. An
exception raised in a handler is logged and swallowed. Don't place orders from
a handler: a placement can block the feed for minutes while the exchange is
slow to answer (long enough for the server to drop the connection), and an
`OrderOutcomeUnknownError` raised there is only logged. Record the event and
act from your own loop, as `bot.py` does.

Channels:

| Channel | Auth | Handler | Payload |
| --- | --- | --- | --- |
| `orderbook:{ticker}` | public | `on_orderbook` | full-depth bid/ask snapshot when the book changes (throttled) and again every 5 s even when it did not, empty books included; no initial frame — seed from `market.get(ticker).orderBook` |
| `trades:{ticker}` | public | `on_trades` | every trade, in at most one frame every 100 ms holding every trade since the previous one, newest first; right after subscribing, the recent trades (up to 20) once, before `on_connect` — dedupe by `tradeId` |
| `chart:{ticker}` | public | `on_candle` | OHLCV candle updates |
| `markprice:{ticker}` | public | `on_mark_price` | canonical mark price (1s, on change) |
| `volume:{ticker}` | public | `on_volume` | rolling-24h traded volume (replaces `volume24h`) |
| `market:{ticker}` | public | `on_market_halt` | halt / resume status for that market |
| `perps:markets` | public | `on_perps_markets_live`, `on_market_terms` | every perp's live state (each row `market.get(ticker).market.live`, with its `markPrice`), once on subscribe, then about once a second — replace what you hold; a perp's terms when they change |
| `portfolio` | **private** | `on_balances`, `on_order_status`, `on_order_update`, `on_trade`, `on_order_queue`, `on_lifetime_volume`, `on_perps_account`, `on_funding_payment`, `on_holding`, `on_transfer`, `on_fees`, `on_l2_keys`, `on_perps_leverage` | your account deltas, and your resting orders' places in their books; every fill of yours is `on_trade`, a liquidation's, a deleverage's and an exit's too (`trade.source`); your perps account as a snapshot about once a second (the beat: `on_stale`, `on_fresh`, below) |

The frames of `orderbook`, `trades`, `chart`, `markprice`, `volume` and `market`
name their market as `ticker`, the channel's own string, so one handler can
serve several markets.

A client with a trading key authenticates each connection with a login token
the key signs (the `auth` frame, never the URL), and sends a fresh one on the
live connection each time the one it holds passes half its life (5 of its 10
minutes), so the connection stays authenticated for as long as it lasts while
the key stays in force. The private channel carries no id: the server resolves
it from the account the login token authenticated, so an anonymous connection
(no trading key) is refused with an `error` frame. It is a **delta stream** —
you receive `balances_update`, `order_status`, `order_update`, etc. as separate
frames:

- `balances_update` carries your **whole** balance book after every change:
  replace yours with it, and treat a missing row as 0. Rows are `{assetId,
  symbol, decimals, isQuote, total, hold, available}`, the amounts in base
  units (`loaf.from_base_units`); the `isQuote` rows are your cash, and a spot
  market's token row is its `market.l2.baseAssetId`.
- Each fill arrives **once**, final when it matches. Each engine update's burst
  carries its `order_status` frames, then its `trade_new` frames, then
  `balances_update`; an update with more than 200 statuses or trades comes as
  several such bursts, `balances_update` in the last only. So match a fill to
  its order by `trade.orderId`, not by frame order; `trade.role` is
  `MAKER` (that order rested) or `TAKER` (it aggressed). `trade.source` is
  `TRADE`, or on a perp a liquidation, deleverage, exit or settlement, which
  is no trade: `txHash` is `None`, and `tradeId` is its journal seq. Key a
  fill by `(trade.tradeId, trade.side)`. `trade.txHash` is a trade's content
  hash, not an on-chain transaction.
- `perps_account_update` (`on_perps_account`) carries your perps pools whole
  after every commit that touches them, `funding_payment`
  (`on_funding_payment`) each funding tick's charge to a position of yours,
  `holding_update` (`on_holding`) a holding, `transfer_update`
  (`on_transfer`) a deposit or withdrawal as it moves on, `fees_update`
  (`on_fees`) the fees that apply to you when they change,
  `l2_keys_update` (`on_l2_keys`) a change to your account's keys (a reset of
  your trading key, say: read `GET /l2/keys` once), and
  `perps_leverage_update` (`on_perps_leverage`) a leverage you saved on a perp,
  `{ticker, leverage}`.
- `order_update` carries the whole committed order, with its `expiryMs`. For an
  order that trades immediately it can arrive after the fill frames and already
  show FILLED. Stop and take orders (TP/SL legs included) ride the same frame,
  so narrow with `loaf.is_conditional_order(msg.order)` before reading
  `filledQuantity`.
- `order_status` `CANCELLED` carries the cancelled remainder in
  `quantityLeft`. It also arrives for orders you never cancelled: the
  order's expiry (72 h before `expiryMs`), an epoch move (a Reset
  trading key, or a cancel-all sent on chain), a delisted (or unlisted)
  market, a BUY that can no longer fund a fill within its hold, your own order
  crossing a resting one of yours (the resting one is cancelled), and a firing
  stop freeing funds by cancelling your resting orders on its side.
- `order_queue` places your resting orders in one market's book:
  `{marketIndex, orders}`, bids best first, then asks, each `{orderId, side,
  price, quantity, quantityAhead, ordersAhead}` (`ordersAhead`: how many orders
  at its price are ahead of it in time priority; `quantityAhead`: their
  unfilled tokens), and optionally `l2OrderIndex` (its signed order number) and
  `orderTag` (your label for it). It replaces what you hold for that market,
  and an empty list means none of yours rests there. Each time a connection
  your trading key authenticated newly subscribes `portfolio`, as the client
  does on every connection (subscribing again while subscribed sends nothing),
  one arrives for each market where you have resting orders (before or after
  `on_connect` on a new connection), then one on every change, and each
  non-empty list again every 5 s, changed or not, so treat a frame as the list,
  not as a change. A market where none rests sends nothing, and no REST read
  serves queue places, so drop every list you hold in `on_disconnect` and,
  after you unsubscribe `portfolio`, at the `subscription_confirmed` that
  answers it (frames sent before it can still arrive;
  `ws.on("subscription_confirmed", handler)` gets it), not in `on_connect`. It
  can arrive before the order's `order_update`.
- Delivery is at most once, so after a reconnect reconcile from
  `loaf.portfolio.summary()`, `loaf.portfolio.component()` and the `history` reads.
- `order_status` and `order_update` carry the order's `orderTag` (when you
  gave it one) and its `l2OrderIndex`, so a bot can match its own quotes
  without a lookup.

**Engine stamps.** A frame that relays a change the exchange's engine made
carries `engineMs`, the engine's clock (unix ms) when the batch that made it
committed, and `batchSeq`, that batch's journal number: the private frames
(`order_status`, `order_update`, `trade_new`, `balances_update`,
`holding_update`, `perps_account_update`, `funding_payment`, `l2_keys_update`)
and `orderbook_update`, whose `engineMs` is its book's. `timestamp` stays the
server's send time in whole unix seconds, so `timestamp * 1000 - engineMs` is a
frame's age when it left, to within a second. `batchSeq` does not order frames:
one batch's frames share it. `ws.engine_ms` is the newest `engineMs` of a
private frame the client received.

**The beat: is your private stream live?** While `portfolio` is subscribed,
the exchange sends your perps account as a snapshot (`perps_account_update`
with `snapshot: True`) about once a second, changed or not, as long as its
engine runs. The client watches it. `ws.fresh` is `None` until a connection's
first snapshot, `True` while stamped private frames keep coming, and `False`
once none came for `stale_after` seconds (`loaf.websocket(stale_after=3)`, the
default: two beats missed; at least 1) or the connection dropped. `on_stale`
fires at the turn to `False`, with `{reason, engineMs, silentSeconds}`:
`reason` is `silent` (the engine or its feed stalled), `disconnected` (before
`on_disconnect`) or `unsubscribed`. `on_fresh` fires at the turn to `True`,
`{engineMs}`, after that frame's own handlers. A new connection starts at
`None` again, and so does a stopped client. A deployment without perps sends
no snapshot, so `fresh` stays `None` there. A missing snapshot means no news,
never no positions.

```python
import threading

pull_quotes = threading.Event()
ws.on_stale(lambda m: pull_quotes.set())   # act from your own loop, never from a handler
ws.on_fresh(lambda m: pull_quotes.clear())
# in the loop that quotes, every tick:
#   if pull_quotes.is_set(): cancel_all(ticker, order_tag=MY_TAG) and quote nothing
#   else: quote, and renew cancel_after(60_000, order_tag=MY_TAG)
```

Pull your quotes on stale, and keep `cancel_after` running as the exchange's
side of the same guard: `on_stale` covers a feed that stops while your bot
runs, `cancel_after` a bot that stops.

**Valuing holdings live.** The server does not push portfolio totals: combine
your balances with the `markprice` channel. A `mark_price` frame is `{ticker,
price}`, so match it to the holding with that ticker; a holding whose
`marketIndex` is `None` has no spot market, so no mark streams for it. An L2
holding is pushed too: `on_holding` upserts it by its ticker, with its average
entry (`None` until the exchange has read your fills, and a later push brings
it: show none meanwhile, never the earlier one), when your fills or a deposit
change it. Its amounts stay its balances row.

```python
held = {h.ticker for h in loaf.portfolio.summary().holdings if h.marketIndex is not None}

@ws.on_mark_price
def on_mark(msg):                       # {ticker, price}, from every markprice channel
    if msg.ticker in held:
        print(msg.ticker, "marked at", msg.price)

for ticker in held:
    ws.subscribe_mark_price(ticker)     # a holding's ticker is its spot market's
```

**Reconnects.** `on_connect` fires once the server has answered the auth and
the subscriptions. Nothing is replayed across a reconnect, so flag a re-seed in
`on_connect` and do the REST reads from your own thread: handlers run on the
socket's thread, and a slow read there stalls it. A public read can be older
than a frame received since `on_connect`, so keep the frame's value over the
read's. `bot.py` does both, and takes the book from a read only if the feed was
live from the request until the answer is applied: a read made before the first
`on_connect`, between an `on_disconnect` and the next `on_connect`, or in flight
when the feed drops or comes back can miss a change that no frame brings. The
private reads (`portfolio.summary()`, `portfolio.component()` and the `history`
reads) first wait, up to 2 s, until every change pushed before them
is readable. The `order_queue` lists have no REST read: drop them in
`on_disconnect` (and, after you unsubscribe `portfolio`, at the
`subscription_confirmed` that answers it), and the next subscription (the
client makes one on every connection while you stay subscribed) sends them
again. `on_disconnect` gets `{code, reason}` after every ended attempt: 1012
with reason `resync` (pushes may have been lost, below) means re-seed once
`on_connect` fires again; 4001 with reason `expired` means the connection's
login token expired before a fresh one reached the server (the machine slept,
or a handler held the socket's thread), and the next connection signs a fresh
one; `code` `None` means no connection was made (`reason` says why); several
without an `on_connect` between them mean the feed is down. The client keeps
retrying, waiting a random time below a ceiling that doubles up to 30 s. With
`portfolio` subscribed, a connection whose login token the server does not
accept is dropped and retried with a fresh one, with the reason `the server
did not accept the login token`. The server also answers so when it cannot
check the token at that moment, and a later attempt then gets through; if it
keeps repeating, your trading key is not the one in force, and REST calls
raise `LoafAuthError` saying why. The server checks the key only when a token
arrives: after a Reset trading key, a live connection keeps your private
stream until the server refuses its next fresh token, at most 5 minutes later,
or, if none reaches it, until its token expires (`4001 expired`), at most 10
minutes later, and every connection after that is refused (§2, **When it
changes**).

The server sends `resync` after a loss on its side: its message bus
reconnected, it was overloaded and shed pushes, or the matching engine dropped
some or restarted without a clean shutdown (a clean restart closes nothing). It
closes every connection holding `portfolio` and, about 2 s after the last bus
reconnect or engine-side loss, every one holding the `trades` channel of a
market that traded since or whose recent trades the server had missed: each
at most once every 30 s, and a connection holding both only once.

**The tape across a reconnect.** A `trades` subscription brings only the
recent trades, up to 20. For the trades missed while the feed was down, page
`market.trades()` forward from the newest `tradeId` you hold, from your own
thread. Its rows come oldest first and trail the live push by up to 2 s, so
read it 2 s or more after `on_connect` and merge it with the frames by
`tradeId`:

```python
after = newest_trade_id   # the newest tradeId you hold
while True:
    page = loaf.market.trades("MUS", after_trade_id=after, limit=1000).trades   # oldest first
    if not page or page[-1].tradeId <= after:
        break             # nothing past `after`
    merge(page)           # by tradeId: the frames since on_connect may hold some of them
    if len(page) < 1000:
        break             # caught up
    after = page[-1].tradeId
```

---

## Error handling

Every failure maps to a specific exception (all subclass `LoafError`):

```python
import time
import loaf
from loaf import LoafClient

client = LoafClient()
try:
    client.orders.limit_buy("MUS", quantity=1, price=167.49)
except loaf.OrderOutcomeUnknownError as e:
    ...   # may be live or filled: resubmit(e.signed_body) if still wanted, else
          # cancel_by_index(e.signed_body["l2OrderIndex"]) settles it
except loaf.TradingPausedError as e:
    ...   # 503: the exchange paused, nothing was placed — send it again after e.retry_after
except loaf.TradingHaltedError:
    ...   # halted — back off and retry later
except loaf.MarketStateError as e:
    ...   # 403: the market's own state (e.state) — reduce-only, price stale or held
          # (PriceHeldError), closed: back off, or send only reduce_only=True orders
except loaf.LoafConfigError:
    ...   # a setup to fix (the message says what): e.g. a market on another deployment's
          # L2 chain than your trading key's — stop and alert
except loaf.LoafAuthError:
    ...   # your trading key's login token was refused, and the message says why: the
          # key is not the one in force (reset, not active yet, another deployment's) or
          # the clock is off — stop and alert (set the key in force, §2, restart, re-arm
          # your stops); or the order's signature or key: the next order re-reads your account
except loaf.PostOnlyWouldTakeError:
    ...   # 400: a POST_ONLY order would have traded at once; nothing was placed
except loaf.SendByPassedError:
    ...   # 400: its send_by_ms passed first; nothing placed, its order number unspent
except loaf.ReplaceTargetGoneError as e:
    ...   # 409: the order it replaces no longer rests; nothing changed
except loaf.LoafValidationError as e:
    print(e.code, e.message, e.details)  # 400 (local ones have status_code 0)
except loaf.LoafRateLimitError as e:
    time.sleep(e.retry_after or 5)     # 429
except loaf.LoafAPIError as e:
    print(e.status_code, e.message, e.code, e.request_id)
```

`LoafConnectionError` means no HTTP response arrived (network error or
timeout), or one arrived whose compressed body could not be decompressed; from
an order placement it means the order never reached the exchange, because
anything that might have landed raises `OrderOutcomeUnknownError` instead.
`LoafConfigError` means a missing or malformed trading key (every
authenticated call and every order needs `LOAF_TRADING_KEY`), or an invalid
client-wide max slippage (`LOAF_MAX_SLIPPAGE_BPS` /
`LoafClient(max_slippage_bps=)`). From a placement it means a setup to fix
before retrying: the market is on another L2 chain than your trading key (the
key is another deployment's), or this machine's clock is hours ahead; the
message says what to do. `LoafAuthError` from a read, a cancel or a placement's
account requests means the server refused your trading key's login token: the
key is not the one in force on its account (reset, not active yet, another
deployment's) or this machine's clock is off by more than about 5 minutes,
which the message names after the server's own text (kept in `e.body`); the
next request signs a fresh token, but nothing changes until you fix the cause.
A bad per-order `max_slippage_bps=` / `leg_max_slippage_bps=` / `ttl_ms=` /
`order_tag=` / `send_by_ms=` / `replaces_order_id=` is a local
`LoafValidationError` (status_code 0).

**Codes.** A refusal of an order request (a placement, a cancel,
`cancel_after`, `cancel_by_index`) carries `e.code`, one of
`loaf.OrderErrorCode`, and the exception class follows from the status and the
code. Branch on the class or the code, never the message: the message is for
people and may change, while a code is never renamed, reused or removed. The
exchange may add codes, so treat one this SDK does not name by its class.
A replace whose cancel went through before the new order was refused has
`e.replaced_order_cancelled` `True`. `TRADING_PAUSED` is
`loaf.TradingPausedError`, both a `TradingHaltedError` and a
`LoafServiceUnavailableError` (503), with `retry_after`: nothing was placed
(or allocated, exited or withdrawn), and cancels are still taken.

A perps allocate or exit raises the same errors, with
`PerpsOutcomeUnknownError` in place of `OrderOutcomeUnknownError` (re-send it
with `loaf.perps.resubmit(e.signed_body)`); its refusals by the exchange's
rules (above your available spot USDC, a pool left under initial margin, or
under a tenth of its positions' notional or an initial-margin tier's extra
(`TRANSFER_MARGIN_FLOOR`), the nonce) are plain `LoafAPIError`s with status
422, and applied nothing. A withdrawal raises the same errors too, with
`WithdrawalOutcomeUnknownError` (re-send it with
`loaf.funds.resubmit(e.signed_body)`); see **Withdrawals**.

## Rate limits & retries

The backend rate limits requests per IP and sends standard `RateLimit-*`
headers (snapshot at `loaf.last_rate_limit`). Order placements, cancels,
order-number reservations and portfolio reads are additionally limited **per
account** (a placement counts for the account it is signed for), so rotating
IPs doesn't raise the ceiling. Reads are retried automatically on 429, 503 and
network errors, with backoff that honours a positive `RateLimit-Reset` /
`Retry-After` (one of 0 or less backs off exponentially, as with none), each
attempt with a login token that has more than half its life left; the reads a
placement makes before it signs raise a 429 at once instead, since a late order
is stale. Order placements are re-sent unchanged on network errors and server
errors after a short jittered pause, not the rate-limit headers (see **Signing
& retries**), but never on a 429 — a rate-limited order raises
`LoafRateLimitError` so you can decide whether to re-issue it at a fresh price.
(A rate-limited `resubmit` raises `OrderOutcomeUnknownError` instead, since the
first attempt may be live.) Withdrawals and perps ops are re-sent as orders
are. Cancels are never retried; `cancel_after` takes one call a second per
account, across every scope. Tune with `LoafClient(max_retries=...)`.

---

## Examples

| File | Shows |
| --- | --- |
| `examples/01_quickstart.py` | confirm credentials, read balances + market |
| `examples/02_market_data.py` | markets, order book, candle history (public) |
| `examples/03_place_order.py` | place → inspect → cancel a limit order (needs your trading key) |
| `examples/04_realtime_market.py` | stream order book + trades + mark price + halts |
| `examples/05_portfolio_stream.py` | stream your private portfolio events |
| `examples/06_stop_and_take.py` | arm a stop far from the mark, read it back, cancel it (needs your trading key) |
| `bot.py` | full strategy-loop template |

## Tests

```bash
pip install -e ".[dev,dotenv]"
pytest
```

The suite runs fully offline against an in-memory mock transport and a local
WebSocket server (no live server, no real key). It covers K1 signing and the
login message (checked against the exchange's known answers), the trading key
and its login tokens (an in-memory exchange checks each one as the real one
does), the order flow, order controls, cancels (scoped, on a timer, by order
number), order-number reservations, stop & take orders, perps orders, reads,
margin moves and exits, withdrawals, retries, input validation, pagination,
error mapping and codes, and the WebSocket client with its beat.

## Notes / intentionally excluded

- This SDK is the **trading-facing** surface. Account management, KYC, referrals
  (`/auth/*`), competitions and the leaderboard, IPO offerings and
  subscriptions, the featured-offering home feed (`/home`), fiat on/off-ramps
  (`/portfolio/onramp|offramp`), and the shareable image cards are **not**
  wrapped, and neither are deposits (your wallet signs them, and the SDK never
  holds its key) or enabling or resetting your trading key. Their public reads
  need no credential: `loaf.get("/leaderboard", auth=False)` works, as do
  `/competition`, `/offerings` and `/home`. The rest need a web-app login, so
  do them in the Loaf web app: a login token works on the trading routes only,
  and the others refuse it (401). Moving USDC between your spot balance and
  your perps pools is wrapped (`loaf.perps`), and so is withdrawing it to your
  own wallet (`loaf.funds`).
- Your trading key is the one credential: its login tokens authenticate the
  private WebSocket channel too, and identify the account it streams.
- The default base URL is the **production API** (`https://api.loafmarkets.com/api`).
  While Loaf is on testnet, set `LOAF_API_BASE_URL` to the testnet API you were
  given. For local dev, set it to your dev server (e.g.
  `http://localhost:8005/api`). For a local server using a self-signed cert, pass
  `LoafClient(verify=False)` — which disables TLS verification (MITM protection),
  so use it only against a trusted localhost, never a remote host.

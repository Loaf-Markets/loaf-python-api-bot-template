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

loaf = LoafClient()   # reads $LOAF_API_KEY and $LOAF_SECRET_KEY (see §2)

print(loaf.portfolio.component().balances)                 # your balances (base units)
print(loaf.market.properties())                            # listed properties
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
pip install -e ".[wallet]"    # + eth-account, only for python -m loaf.secret_key (see §2)
```

Requires Python 3.9+.

## 2. Get your credentials

The bot uses **two secrets**:

| Secret | `.env` variable | What it does |
| --- | --- | --- |
| API key | `LOAF_API_KEY` | Sent as `Authorization: Bearer` on reads, cancels, the WebSocket and the account reads every order needs. 64 hex characters, no prefix. |
| Trading key | `LOAF_SECRET_KEY` | Signs every order you place, on your machine. It is never sent. 80 hex characters, no prefix. |

Both come from the Loaf web app's **API** page (log in → **API**).

1. **The API key**: select **Generate API Token**. It is shown **once**, so
   copy it right away. It has an expiry; once it expires, or you revoke it,
   every request it authenticates gets `401 Invalid credentials`. A bot cannot
   read its own key's expiry, so note the date when you create it.
2. **The trading key** needs trading enabled: deposit (your first deposit opens
   your trading account), then select **Enable trading** in the order panel on
   a property's Trade page and wait until it is active. Then, under **Trading
   key** below your tokens, select **Show trading key**: your wallet signs
   once, and the page shows the key if it is the one in force (right after
   enabling or a reset, try again in a moment). Your account has one trading
   key, shared by the web app and all your bots: it is the same at every
   showing until you reset it, and the page does not save it.

```bash
cp .env.example .env
chmod 600 .env   # only you can read it: it holds both secrets
# edit .env:
#   LOAF_API_KEY=<API key: 64 hex characters, no prefix>
#   LOAF_SECRET_KEY=<trading key: 80 hex characters, no prefix>
#   LOAF_API_BASE_URL=https://api.loafmarkets.com/api   # or http://localhost:8005/api for local dev
#   LOAF_MAX_SLIPPAGE_BPS=200   # optional: max slippage in basis points (100 = 1%), see Placing orders
```

The SDK reads environment variables; `bot.py`, the examples and
`python -m loaf.secret_key` load `.env` into them when python-dotenv is installed
(`pip install -e ".[dotenv]"`). Otherwise export the variables.

### Your trading key

- **What it can do.** Trade the account's balance on its vault, the ledger the
  markets settle on. Whoever holds it can trade that balance away, against
  orders of their own: keep it, and the `.env` that holds it beside your API
  key, as secret as your wallet key. It cannot move your wallet's funds: a
  withdrawal it signs pays only the account's own wallet, and needs the API
  key too.
  **Reset trading key** in the web app (in Account Settings) revokes it, for the
  web app and every bot alike.
- **When it changes.** After a Reset trading key, get the new key once it is
  active (until then the old one stays in force) and restart the bot with it.
  Once the new key is active, a bot still on the old one may have its next order
  refused with `l2 epoch: …` (`LoafValidationError`); after that its orders
  raise `LoafConfigError` (the key is not in force). Also cancel and re-arm
  every stop, take and TP/SL leg (see **Stop & take orders**): those signed with
  the old key stay ARMED but FAIL when they fire.

Without `LOAF_SECRET_KEY` everything works except placing orders.
`loaf.public_key` is your trading key's public key (`None` when no key is
set): what `GET /l2/keys` names `journaledKey` while the key is in force.

### Deriving a trading key on your machine

`python -m loaf.secret_key` derives the same key from your wallet's private
key, without the web app. It needs an account whose wallet private key you
hold, such as a wallet login or a dedicated bot account. It reads your trading
account with `LOAF_API_KEY` (and `LOAF_API_BASE_URL`), so set those first, and
run it from your bot's directory (it loads the `.env` found from the working
directory up), on a trusted machine, once trading is enabled on the market:

```bash
pip install -e ".[wallet]"
read -rs W && LOAF_WALLET_PRIVATE_KEY="$W" python -m loaf.secret_key; unset W
```

`read -rs` waits for you to paste your wallet's private key (not echoed) and
press Enter. The wallet key goes to that one command only: a plain shell
variable is not exported, so nothing started later in the shell inherits it,
even if the command fails or `unset` is skipped. The helper prints one line,
`LOAF_SECRET_KEY=<80 hex>`: paste it into your `.env`. After a Reset trading
key it prints the old key until the new one is active.

Never put the wallet key in `.env` (the helper refuses one that holds it) or in
the bot's environment (`LoafClient()` refuses to start while
`LOAF_WALLET_PRIVATE_KEY` is set).

## 3. Run the bot template

```bash
python bot.py
```

`bot.py` verifies your credentials, prints balances/positions, opens the live
feed (order book, competition round notices and your private portfolio stream),
and runs a 5-second strategy loop with clearly-marked `# YOUR STRATEGY GOES
HERE` hooks. It only *observes* the market out of the box — drop your logic into
`Strategy.on_tick`.

It follows the property named by `TARGET_TICKER` at the top of the file — a
ticker such as `OPRA` — or the first tradeable listed property when that is
empty.

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
git checkout v0.4.0
```

Releases are tagged from `v0.3.0` onward. While the SDK is pre-1.0, a breaking
change bumps the **minor** version — so `0.3.x` → `0.4.0` is the signal to
re-read this README before upgrading. Expect that signal often while Loaf is on
testnet; an old pin is the most common reason a bot stops matching these docs.

---

## The client

Create one `LoafClient` and reach everything through grouped resources:

| Resource | What it covers |
| --- | --- |
| `loaf.market` | properties, property detail, candle history, trade backfill, info pages (all public) |
| `loaf.offerings` | IPO offerings: list, detail |
| `loaf.orders` | place (signed) / cancel / cancel-all orders, stop & take (conditional) orders |
| `loaf.perps` | perps markets, your margin pools and positions, margin moves and exits (signed) |
| `loaf.portfolio` | balances, positions, recent activity |
| `loaf.history` | paginated order & trade history, cancelled & active orders |
| `loaf.leaderboard` | competition leaderboard |
| `loaf.competition` | competition rounds info, your queue position, payout details |

Responses are `LoafObject`s — dicts that also allow attribute access, so
`book.bids[0].price` and `book["bids"][0]["price"]` are equivalent. Unknown
fields the API adds later are preserved automatically. See `loaf/models.py` for
typed descriptions of every documented shape.

### Endpoint coverage

The trading-facing endpoints are wrapped (account management, KYC, referrals,
and fiat ramps are not part of this SDK):

```
market.properties()                   GET    /trade
market.property(ticker)               GET    /trade/{ticker}
market.candles(ticker, resolution)    GET    /trade/{ticker}/candles
market.iter_candles(ticker, res)      (auto-paginate candle history)
market.trades(ticker)                 GET    /trade/{ticker}/trades
market.info_header(ticker)            GET    /info/{ticker}/header
market.info_overview(ticker)          GET    /info/{ticker}/overview
market.info_documents(ticker)         GET    /info/{ticker}/documents

offerings.list()                      GET    /offerings
offerings.get(ticker)                 GET    /offerings/{ticker}

orders.create(...) / limit_buy / ...  POST   /orders                (signed; no API key)
orders.create_conditional(...)        POST   /orders/conditional    (signed; no API key)
orders.stop_loss / take_profit(...)   POST   /orders/conditional    (signed; no API key)
orders.resubmit(signed_body)          POST   /orders or /orders/conditional (re-send unchanged)
orders.cancel(order_id)               POST   /orders/cancel
orders.cancel_conditional(order_id)   POST   /orders/conditional/cancel
orders.cancel_row(order)              (routes a row to the right cancel)
orders.cancel_all()                   POST   /orders/cancel-all

perps.markets() / market(index)       GET    /l2/perps/markets
perps.collateral()                    (its collateral: USDC's decimals and tick size)
perps.markets_live() / market_live    GET    /l2/perps/markets/live (index, funding, trading state)
perps.funding_ticks(market, limit=, before=)
                                      GET    /l2/perps/funding      (applied funding ticks)
perps.closes(limit=, before=)         GET    /l2/perps/closes       (liquidations, ADL, exits; API key)
perps.positions() / pool / position   GET    /l2/perps/positions
perps.funding()                       (each position's funding, from /l2/perps/positions)
perps.allocate / add_margin / return_margin
                                      POST   /l2/allocations        (signed; with API key)
perps.exit(market, pool=)             POST   /l2/exits              (signed; with API key)
perps.resubmit(signed_body)           POST   /l2/allocations or /l2/exits (re-send unchanged)

portfolio.component()                 GET    /portfolio/component

history.orders() / trades()           GET    /history/orders, /history/trades
history.cancelled_orders()            GET    /history/orders/cancelled
history.active_orders()               GET    /history/orders/active
history.iter_orders() / iter_trades() (auto-paginate via cursor)

leaderboard.get()                     GET    /leaderboard

competition.info()                    GET    /competition
competition.queue_position()          GET    /competition/queue-position
competition.payout_details()          GET    /competition/payout-details
competition.submit_payout_details()   POST   /competition/payout-details
```

Placements are signed with your trading key and carry no API key. For your
orders the SDK also reads `GET /l2/keys` and reserves order numbers with
`POST /l2/order-indices`, both with your API key. A perps allocate or exit is
signed with your trading key too, and also carries your API key.

Candle history is a dedicated, paginated endpoint (the property detail response
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
# Orders are addressed by ticker. Pick a tradeable one from loaf.market.properties():
# status "LIVE" and hasL2Market (halts and trading hours still apply).

# LIMIT order (price in dollars, <=2 dp; quantity in tokens, <=1 dp):
res = loaf.orders.limit_buy("MUS", quantity=10, price=167.49)
print(res.orderId, res.status, res.quantityLeft)   # e.g. 991 OPEN 10

# MARKET order: no price. The SDK signs reference x (1 ± max slippage) as your worst fill price:
res = loaf.orders.market_buy("MUS", quantity=2)           # reference fetched; client's max slippage
res = loaf.orders.market_sell("MUS", quantity=2,
                              reference_price=mark, max_slippage_bps=50)   # mark: your markprice; 0.5%
if res.status in ("OPEN", "PARTIALLY_FILLED"):     # the unfilled rest is RESTING at the worst price
    loaf.orders.cancel(res.orderId)

loaf.orders.cancel_all()      # cancel every open order, stop and take (it sells nothing)
```

**A 200 is sent after the exchange commits the order, so its `status` is
real.** `FILLED` means filled; `OPEN` / `PARTIALLY_FILLED` mean it is resting
with `quantityLeft` tokens to go. `CANCELLED` on a new order means it stopped
early: a BUY whose hold could not pay for its next fill, or a SELL stopped at
K1's size bound (see **Money & units**). `attachedLegIds` are the conditional
ids of the TP/SL legs you attached, take-profit first (`[]` without legs). Later
fills and cancels arrive on your private `portfolio` WebSocket channel (see
below).

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
  never worse than the price you signed, and **anything left rests on the
  book** until it fills, expires or you cancel it.
- The reference read is one extra public request; it can lag the live market
  and may not yet show a trade you just made. For price-sensitive orders pass the
  `markprice:{ticker}` value you stream as `reference_price` (once the SDK has
  read the property's market, that also saves the request).
- If the exchange has no market price yet, the SDK refuses the order locally
  rather than sign it off zero.
- A BUY holds **worst price × quantity × (1 + `L2_FEE_RATE_CAP` / 1,000,000)**
  of your cash: `L2_FEE_RATE_CAP` is the fee-rate cap every order signs (70 bps),
  whatever fee its fills are charged. Size it with the same
  reference and max slippage you sign with:

```python
import math
from loaf import L2_FEE_RATE_CAP, from_base_units, worst_price

quote = loaf.market.property("MUS").property.l2Market.quoteAssetId   # the market's cash asset
row = next((b for b in loaf.portfolio.component().balances if b.assetId == quote), None)
cash = float(from_base_units(row.available, row.decimals)) if row else 0.0
worst = worst_price(mark, "BUY", loaf.max_slippage_bps)  # what market_buy(..., reference_price=mark) signs
qty = math.floor(cash / (worst * (1 + L2_FEE_RATE_CAP / 1_000_000)) * 10) / 10
res = loaf.orders.market_buy("MUS", quantity=qty, reference_price=mark)  # same reference
```

A few checks run server-side that the SDK cannot pre-validate, so handle their
rejections rather than trying to avoid them:

| Rejection | Rule |
| --- | --- |
| `LoafValidationError` (400) | **Minimum order value** — `price x quantity` must be at least $10, measured at the price you **sign** (a MARKET order's worst price). A SELL closing your *entire* available position is exempt, so dust can always be flattened. |
| `LoafValidationError` (400) | **Limit price deviation** — any order, MARKET included, whose signed price is too far from the market reference is refused. The ceiling is a deployment setting; keep your max slippage small. |
| `LoafValidationError` (400) | **Daily price band** — a per-property band around the daily reference price, when enabled. The message carries the price, limit, reference and band width. |
| `LoafValidationError` (400) | **Balance and open-order caps** — `Insufficient available balance for this order`, or too many open orders (resting stops count too). |
| `LoafValidationError` (400) | **Fee-rate cap** — `l2 fee: the signed fee-rate cap is below the account's fee rate`: your account's fee rate is above the cap every order signs. |

Markets keep trading hours: outside them placement raises `LoafForbiddenError`
(`Trading is currently closed`); cancels still work.

If trading is halted — platform-wide or for that property — order placement
raises `TradingHaltedError` (403). A platform-wide halt blocks cancels too,
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
the property's status channel:

```python
ws.subscribe_property_status("MUS")

@ws.on_property_halt
def on_halt(msg):
    print(msg.ticker, "halted" if msg.isHalted else "resumed")
```

`isHalted` is the **effective** state — the property's own flag OR'd with the
platform-wide kill switch — so assign it straight over the `isHalted` you seeded
from `market.property("MUS").property.isHalted`. Note a global halt lifting does
not resume a property that is individually halted; the frame accounts for that.

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

### Signing & retries

- The SDK signs every order on your machine with your trading key (K1, the
  exchange's signature scheme); the key itself is never sent. The request
  carries that signature, not your API key.
- The SDK re-sends the **exact** signed order on timeouts and server errors,
  after a short pause, so a re-send can never place it twice: the exchange
  admits one order per account and order number, and answers a repeat with
  the same `orderId` (and `duplicate: True` for a plain order). Each attempt
  is given at least 45 seconds to answer, because the exchange replies only
  once the order is committed. With the default 3 retries a call can
  therefore block for a few minutes while the exchange is struggling; use
  `LoafClient(max_retries=1)` in a latency-sensitive loop.
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
    it out of logs and alerts. Otherwise find it in `loaf.history.orders()` by
    its `expiryMs` (`e.signed_body["l2ExpiryMs"]`): a miss proves nothing while
    it can still rest (until `expiryMs` minus 72 hours, its `ttl_ms` after
    signing).
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
- The client keeps the trading key only inside its signer and never logs it.
  Prefer `$LOAF_SECRET_KEY` over `secret_key=`: an explicit argument can
  show up in tools that capture call frames (crash reporters, debuggers), and
  so can a Ctrl-C that lands while an order is being signed, so don't let a
  crash reporter record local variables in a trading bot.

```python
from loaf import OrderOutcomeUnknownError

try:
    res = loaf.orders.market_buy("MUS", quantity=2)
except OrderOutcomeUnknownError as e:
    res = loaf.orders.resubmit(e.signed_body)   # the same order: placed at most once
    # (raises OrderOutcomeUnknownError again if this re-send can't tell either)
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
cancels your own resting orders on its property and side, and tries once more.
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
value) when it fires; if that fails the leg becomes FAILED. Cancelling a partly
filled parent (for example a resting MARKET remainder) FAILs its legs.

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

A property whose `l2Market` has `kind` `"perps"` trades a perpetual future
settled in USDC: a BUY opens or grows a long (or shrinks a short), a SELL the
other way round. Your margin is USDC held in **pools**: pool 0, the cross pool,
and the isolated pools 1 to 7, each holding at most 8 positions margined by
that pool's collateral alone. Fund a pool from your spot USDC, trade from it,
and move what is left back:

```python
loaf.perps.markets()                                  # every perps market, public
loaf.perps.market_live(4)                             # tradingState, riskOnly, index, funding rate
loaf.perps.add_margin(5_000, pool=0)                  # 5,000 USDC: spot -> pool 0
loaf.orders.limit_buy("PERP", quantity=2, price=101.5, pool=0)   # a long from pool 0
loaf.orders.limit_sell("PERP", quantity=2, price=104, pool=0, reduce_only=True)
loaf.perps.position(market=4, pool=0)                 # base, entryPrice, liquidationPrice, ...
loaf.perps.return_margin(1_000.25, pool=0)            # pool 0 -> spot
```

- **Orders** take `pool=` (0 by default) and `reduce_only=True` (its fills
  may only shrink the pool's position), signed with the order; both are
  refused on a spot market. TP/SL legs (`tp_price=`, `sl_price=`) attach to a
  perps BUY or SELL, never to a reduce-only order: each leg is a reduce-only
  order on the other side in the order's pool. A SELL's are mirrored:
  `tp_price < price < sl_price`, e.g.
  `limit_sell("PERP", 2, 104, tp_price=95, sl_price=110)`.
- **Allocates** (`add_margin`, `return_margin`, `allocate(amount, from_side=,
  to_side=)`, a side a pool or `loaf.PERPS_SPOT_SIDE`, 255, your spot balance)
  and **exits** (`exit(market, pool=)`: closes your position in an EXPIRED
  market, which takes no orders) are signed with your trading key and sent
  with your API key. Each spends your account's next nonce, the counter your
  withdrawals spend too; the SDK reads it for every op. One applied elsewhere
  between that read and the send makes the exchange refuse this one (422,
  `l2 money: the nonce is not the api key's nonce …`): send it again. An op is
  valid for `ttl_ms` (default 10 minutes).
- **Retries** work as for orders: timeouts and 5xx re-send the same signed
  body, which the exchange applies once at most (`duplicate: True` on a repeat
  of an applied one). Any error but `PerpsOutcomeUnknownError` means it was
  not applied; on that one, `loaf.perps.resubmit(e.signed_body)`, never a
  fresh op for the same intent.
- **Units.** An allocate's `amount` is in USDC. The exchange moves whole
  ticks of it, so the SDK counts the amount in the collateral that
  `GET /l2/perps/markets` serves (`perps.collateral()`: ticks = amount x
  10^`decimals` / `tickSize`). It refuses an amount that is not a whole number
  of ticks, and a reply without a usable collateral: it never assumes one.
  An exit moves no amount, so it never needs the collateral.
  The reads are the engine's units: `positions()` serves amounts as decimal
  strings, collateral, value, funding owed and notional in extended units
  (ticks x the collateral's `extensionMultiplier`), a position's `base` in an
  order's signed quantity units (tokens x 10, negative for a short), prices in
  the market's price units (its docstring has the rest).
- **Market state.** A perps market can refuse orders for its own state (403,
  `MarketStateError` with `.state`): halted, outside its trading hours,
  reduce-only, its price stale, or reduce-only while its latest price waits
  for review (`PriceHeldError`). In the last three a `reduce_only=True` order
  is still taken. `perps.markets_live()` (and the `perps:markets` channel)
  serves each market's state as the engine decides it, before you send:
  `tradingState`, `stateReason`, `halted`, `heldPrice`, and `riskOnly` (the
  engine's risk-only mode: only an order that reduces is taken, and no
  withdrawal, whatever `tradingState` says), all as of `asOfMs`.

---

## Competition rounds

A round is normal trading scored over a window: it changes nothing about
trading, your orders or your balances. Once the admission queue admits you,
your trading (your bots' included) from the round's start, or your admission if
later, until its end scores on the leaderboard by its volume and its PnL
(deposits and withdrawals in the window are not PnL). Everyone else trades as
usual. `loaf.competition.queue_position()` gives your place in the queue, or
your `leaderboardPosition` and `leaderboardPoints` on the board being served,
and the public `competition` channel announces each round transition:

```python
ws.subscribe_competition()

@ws.on_round_update
def on_round(msg):
    print(msg.roundNumber, msg.status)
```

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
- A trade's `fee` is in dollars, to 0.001.
- **Perps** amounts are the exchange engine's units (see **Perps**): an
  allocate's `amount` in USDC ticks, and `perps.positions()` values as decimal
  strings.
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
    print(msg.propertyId, msg.bids[0].price, msg.asks[0].price)

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
| `orderbook:{ticker}` | public | `on_orderbook` | full-depth bid/ask snapshot when the book changes (throttled); no initial frame — seed from `market.property(ticker).orderBook` |
| `trades:{ticker}` | public | `on_trades` | every trade, in at most one frame every 100 ms holding every trade since the previous one, newest first; right after subscribing, the recent trades (up to 20) once, before `on_connect` — dedupe by `tradeId` |
| `chart:{ticker}` | public | `on_candle` | OHLCV candle updates |
| `markprice:{ticker}` | public | `on_mark_price` | canonical mark price (1s, on change) |
| `volume:{ticker}` | public | `on_volume` | rolling-24h traded volume (replaces `volume24h`) |
| `property:{ticker}` | public | `on_property_halt` | halt / resume status for that property |
| `leaderboard` | public | `on_leaderboard` | competition leaderboard (top entries), on change |
| `perps:markets` | public | `on_perps_markets_live` | every perps market's live state, as `perps.markets_live()` serves it, about once a second — replace what you hold |
| `competition` | public | `on_round_update` | round transitions (PREPARED, ACTIVE, ENDING, ENDED, DRAFT); no initial frame — seed from `competition.info()` |
| `portfolio` | **private** | `on_balances`, `on_order_status`, `on_order_update`, `on_trade`, `on_liquidation_fill`, `on_order_queue`, `on_lifetime_volume` | your account deltas, and your resting orders' places in their books; a liquidation's take from your resting perps order is a fill but no trade: `on_liquidation_fill`, not `on_trade` |

The private channel carries no id: the server resolves it from the account
your API key authenticated as, so an anonymous connection is refused with an
`error` frame. It is a **delta stream** — you receive `balances_update`,
`order_status`, `order_update`, etc. as separate frames:

- `balances_update` carries your **whole** balance book after every change:
  replace yours with it, and treat a missing row as 0. Rows are `{assetId,
  symbol, decimals, isQuote, total, hold, available}`, the amounts in base
  units (`loaf.from_base_units`); the `isQuote` rows are your cash, and a
  property's row is its `property.l2Market.baseAssetId`.
- Each fill arrives **once**, final when it matches. Each engine update's burst
  carries its `order_status` frames, then its `trade_new` frames, then
  `balances_update`; an update with more than 200 statuses or trades comes as
  several such bursts, `balances_update` in the last only. So match a fill to
  its order by `trade.orderId`, not by frame order; `trade.role` is
  `MAKER` (that order rested) or `TAKER` (it aggressed). There is no position
  frame: your holdings are balances rows. `trade.txHash` is the trade's content
  hash, not an on-chain transaction; `trade.proof` is `{"status": "PENDING"}`, or
  `None` with proofs off.
- `order_update` carries the whole committed order, with its `expiryMs`. For an
  order that trades immediately it can arrive after the fill frames and already
  show FILLED. Stop and take orders (TP/SL legs included) ride the same frame,
  so narrow with `loaf.is_conditional_order(msg.order)` before reading
  `filledQuantity`.
- `order_status` `CANCELLED` carries the cancelled remainder in
  `quantityLeft`. It also arrives for orders you never cancelled: the
  order's expiry (72 h before `expiryMs`), an epoch move (a Reset
  trading key, or a cancel-all sent on chain), a closed market, a BUY that can
  no longer fund a fill within its hold, your own order crossing a resting one
  of yours (the resting one is cancelled), and a firing stop freeing funds by
  cancelling your resting orders on its side.
- `order_queue` places your resting orders in one property's book:
  `{propertyId, orders}`, bids best first, then asks, each `{orderId, side,
  price, quantity, quantityAhead, ordersAhead}` (`ordersAhead`: how many orders
  at its price are ahead of it in time priority; `quantityAhead`: their
  unfilled tokens). It replaces what you hold for that property, and an empty
  list means none of yours rests there. Each time a connection newly
  subscribes `portfolio` with your API key, as the client does on every
  connection (subscribing again while subscribed sends nothing), one arrives
  for each property where you have resting orders (before or after
  `on_connect` on a new connection), then one on every change; a property
  where none rests sends nothing, and no REST read serves queue places, so
  drop every list you hold in `on_disconnect` and, after you unsubscribe
  `portfolio`, at the `subscription_confirmed` that answers it (frames sent
  before it can still arrive; `ws.on("subscription_confirmed", handler)` gets
  it), not in `on_connect`. It can arrive before the order's `order_update`.
- Delivery is at most once, so after a reconnect reconcile from
  `loaf.portfolio.component()` / `loaf.history.trades()`. To value positions
  live, combine your balances with the `markprice` channel (the server does not
  push portfolio totals).

**Reconnects.** `on_connect` fires once the server has answered the auth and
the subscriptions. Nothing is replayed across a reconnect, so flag a re-seed in
`on_connect` and do the REST reads from your own thread: handlers run on the
socket's thread, and a slow read there stalls it. A public read can be older
than a frame received since `on_connect`, so keep the frame's value over the
read's. `bot.py` does both. The private reads (`portfolio.component()` and the
`history` reads) first wait, up to 2 s, until every change pushed before them
is readable. The `order_queue` lists have no REST read: drop them in
`on_disconnect` (and, after you unsubscribe `portfolio`, at the
`subscription_confirmed` that answers it), and the next subscription (the
client makes one on every connection while you stay subscribed) sends them
again. `on_disconnect` gets `{code, reason}` after every ended attempt: 1012
with reason `resync` (pushes may have been lost, below) means re-seed once
`on_connect` fires again; 4001 with reason `revoked` or `expired` means the API
key was revoked or has expired and will not authenticate again: create a new
one; `code` `None` means no connection was made (`reason` says why); several
without an `on_connect` between them mean the feed is down. The
client keeps retrying, waiting a random time below a ceiling that doubles up to
30 s. With `portfolio` subscribed, a connection whose API
key the server does not accept is dropped and retried, with the reason
`the server did not accept the API key`. The server also answers so when it
cannot check the key at that moment, and a later attempt then gets through; if
it keeps repeating, the key has expired or been revoked.

The server sends `resync` after a loss on its side: its message bus
reconnected, it was overloaded and shed pushes, or the matching engine dropped
some or restarted without a clean shutdown (a clean restart closes nothing). It
closes every connection holding `portfolio` and, about 2 s after the last bus
reconnect or engine-side loss, every one holding the `trades` channel of a
property that traded since or whose recent trades the server had missed: each
at most once every 30 s, and a connection holding both only once. An API-key
connection can also get `resync` so that its key is checked again.

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
    ...   # may be live or filled: resubmit(e.signed_body) if still wanted, else check history.orders()
except loaf.TradingHaltedError:
    ...   # halted — back off and retry later
except loaf.MarketStateError as e:
    ...   # 403: the market's own state (e.state) — reduce-only, price stale or held
          # (PriceHeldError), closed: back off, or send only reduce_only=True orders
except loaf.LoafConfigError:
    ...   # e.g. your trading key is no longer in force — stop and alert: set the
          # key in force (§2), restart, and re-arm your stops
except loaf.LoafAuthError:
    ...   # "Invalid credentials": your API key expired or was revoked — stop and alert
          # (create a new one, restart); otherwise the order's signature or key was
          # refused: the next order re-reads your account
except loaf.LoafValidationError as e:
    print(e.message, e.details)        # 400 (local ones have status_code 0)
except loaf.LoafRateLimitError as e:
    time.sleep(e.retry_after or 5)     # 429
except loaf.LoafAPIError as e:
    print(e.status_code, e.message, e.code, e.request_id)
```

`LoafConnectionError` means no HTTP response arrived (network error or
timeout), or one arrived whose compressed body could not be decompressed; from
an order placement it means the order never reached the exchange, because
anything that might have landed raises `OrderOutcomeUnknownError` instead.
`LoafConfigError` means a missing or invalid key (placing orders needs
`LOAF_SECRET_KEY` and `LOAF_API_KEY`), `LOAF_WALLET_PRIVATE_KEY` left in the
bot's environment, or an invalid client-wide max slippage
(`LOAF_MAX_SLIPPAGE_BPS` / `LoafClient(max_slippage_bps=)`). From a placement
it means a setup to fix before retrying: your trading key is not the one in
force, trading is not enabled, you have no trading account yet, or this
machine's clock is hours ahead; the message says what to do. A
bad per-order `max_slippage_bps=` / `leg_max_slippage_bps=` / `ttl_ms=` is a
local `LoafValidationError` (status_code 0).

A perps allocate or exit raises the same errors, with
`PerpsOutcomeUnknownError` in place of `OrderOutcomeUnknownError` (re-send it
with `loaf.perps.resubmit(e.signed_body)`); its refusals by the exchange's
rules (above your available spot USDC, a pool left under initial margin, the
nonce) are plain `LoafAPIError`s with status 422, and applied nothing.

## Rate limits & retries

The backend rate limits requests per IP and sends standard `RateLimit-*`
headers (snapshot at `loaf.last_rate_limit`). Order placements, cancels,
order-number reservations, portfolio reads and competition queue/payout calls
are additionally limited **per account** (a placement counts for the account
it is signed for), so rotating IPs doesn't raise the ceiling. Reads are retried
automatically on 429, 503 and network errors, with backoff that honours a
positive `RateLimit-Reset` / `Retry-After` (one of 0 or less backs off
exponentially, as with none); the reads a placement makes before it signs
raise a 429 at once instead, since a late order is stale. Order placements are
re-sent unchanged on network errors and server errors after a short jittered
pause, not the rate-limit headers (see **Signing & retries**), but never on a
429 — a rate-limited order raises `LoafRateLimitError` so you can decide
whether to re-issue it at a fresh price. (A rate-limited `resubmit` raises
`OrderOutcomeUnknownError` instead, since the first attempt may be live.)
Cancels are never retried. Tune with `LoafClient(max_retries=...)`.

---

## Examples

| File | Shows |
| --- | --- |
| `examples/01_quickstart.py` | confirm credentials, read balances + market |
| `examples/02_market_data.py` | properties, order book, candle history (public) |
| `examples/03_place_order.py` | place → inspect → cancel a limit order (needs your trading key) |
| `examples/04_realtime_market.py` | stream order book + trades + mark price + halts |
| `examples/05_portfolio_stream.py` | stream your private portfolio events |
| `examples/06_stop_and_take.py` | arm a stop far from the mark, read it back, cancel it (needs your trading key) |
| `bot.py` | full strategy-loop template |

## Tests

```bash
pip install -e ".[dev,wallet,dotenv]"
pytest
```

The suite runs fully offline against an in-memory mock transport and a local
WebSocket server (no live server, no real key). It covers K1 signing (checked
against the exchange's known answers), the web app's trading-key derivation
vectors, the order flow, order-number reservations, stop & take orders, perps
orders, reads, margin moves and exits, retries, input validation, pagination,
error mapping, and the WebSocket client. Without
the `wallet` extra the trading-key helper's tests are skipped, and without
`dotenv` its `.env` tests are.

## Notes / intentionally excluded

- This SDK is the **trading-facing** surface. Account management, KYC, referrals
  (`/auth/*`), the featured-offering home feed (`/home`), fiat on/off-ramps
  (`/portfolio/onramp|offramp`), and the shareable image cards
  (`/portfolio/position/{ticker}/pnl-card`, `/leaderboard/card`,
  `/competition/queue-position/card`) are **not** wrapped — do those in the
  Loaf web app. So are deposits, withdrawals, and enabling or resetting your
  trading key. (Moving USDC between your spot balance and your perps pools is
  wrapped: `loaf.perps`.)
- Create your API key in the web app. Nothing else is needed for the private
  WebSocket channel — the API key identifies the account it streams.
- IPO subscriptions are not wrapped: each purchase is signed by your account
  wallet, which neither the API key nor the trading key can do. Subscribe in
  the web app.
- The default base URL is the **production API** (`https://api.loafmarkets.com/api`).
  While Loaf is on testnet, set `LOAF_API_BASE_URL` to the testnet API you were
  given. For local dev, set it to your dev server (e.g.
  `http://localhost:8005/api`). For a local server using a self-signed cert, pass
  `LoafClient(verify=False)` — which disables TLS verification (MITM protection),
  so use it only against a trusted localhost, never a remote host.

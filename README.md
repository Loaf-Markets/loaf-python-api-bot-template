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

loaf = LoafClient()   # reads $LOAF_API_KEY and $LOAF_AGENT_PRIVATE_KEY (see §2)

print(loaf.portfolio.component().cash)                     # available USDC
print(loaf.market.properties())                            # listed properties
loaf.orders.limit_buy("opera", quantity=10, price=167.49)  # signed locally with your agent key
```

---

## 1. Install

```bash
git clone <this repo> && cd loaf-python-api-bot-template
python -m venv .venv && source .venv/bin/activate

# Install the SDK (httpx + websockets + eth-account). Add extras as needed:
pip install -e .              # core
pip install -e ".[dotenv]"    # + auto-load a .env file
pip install -e ".[dev]"       # + pytest for the test suite
pip install -e ".[fast]"      # + native order signing, a few ms less CPU per order (Python 3.9-3.13; no effect on 3.14)
```

Requires Python 3.9+.

## 2. Get your credentials

The bot uses **two secrets**, and you get both when you create an API key in
the Loaf web app (log in → **Settings → API keys → Create**). The page shows
them **once** — copy both into your `.env`.

| Secret | `.env` variable | What it does |
| --- | --- | --- |
| API token | `LOAF_API_KEY` | Sent as `Authorization: Bearer` on reads, cancels and the WebSocket. 64 hex characters, no prefix. |
| Agent private key | `LOAF_AGENT_PRIVATE_KEY` | Signs every order you place, on your machine. It is never sent. `0x` + 64 hex characters. |

Creating the key has your account wallet approve a fresh **agent** with one
signature. The agent can sign orders for your account and nothing else — it
cannot withdraw — but anyone holding it can trade your balance, so treat it
like a password.

- A key lasts **at most 180 days**. At its expiry, or when you delete it, both
  values stop together: reads and cancels get `401 Invalid credentials`, orders
  get `401 Signer is not authorized to trade for any account`. A bot cannot
  read its own expiry, so note the date when you create the key.
- Deleting a key does not cancel anything: orders and stops its agent signed
  stay live until you cancel them.
- Don't use your wallet's own private key in a bot. The exchange would accept
  its signatures, but it controls your funds, not just your trading.

```bash
cp .env.example .env
# edit .env:
#   LOAF_API_KEY=<API token — 64 hex characters, no prefix>
#   LOAF_AGENT_PRIVATE_KEY=<agent private key — 0x + 64 hex characters>
#   LOAF_API_BASE_URL=https://api.loafmarkets.com/api   # or http://localhost:8005/api for local dev
#   LOAF_MAX_SLIPPAGE_BPS=200   # optional: max slippage in basis points (100 = 1%), see Placing orders
```

The SDK reads environment variables; `bot.py` and the examples load `.env` into
them when python-dotenv is installed (`pip install -e ".[dotenv]"`). Otherwise
export the variables.

Without `LOAF_AGENT_PRIVATE_KEY` everything works except placing orders.
`loaf.agent_address` is the address of the agent your orders are signed by
(`None` when no key is set).

## 3. Run the bot template

```bash
python bot.py
```

`bot.py` verifies your credentials, prints balances/positions, opens the live
feed (order book + your private portfolio stream), and runs a 5-second strategy
loop with clearly-marked `# YOUR STRATEGY GOES HERE` hooks. It only *observes*
the market out of the box — drop your logic into `Strategy.on_tick`.

It follows the property named by `TARGET_TOKEN_NAME` at the top of the file — a
tokenName such as `opera` — or the first listed property when that is empty.

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

**0.4.0 adds a dependency (eth-account), so re-run `pip install -e .` after
pulling it.** Until you do, reads keep working, and a client given an agent key
raises `LoafConfigError` saying so.

### Upgrading from 0.3

| 0.3 | 0.4 |
| --- | --- |
| `api_key` placed orders | Placing needs `agent_private_key` / `$LOAF_AGENT_PRIVATE_KEY` instead. `api_key` is for reads, cancels and the WebSocket. |
| MARKET orders were bounded by a server-side slippage cap | Still no price: the SDK signs a worst price, reference ± your max slippage (`LOAF_MAX_SLIPPAGE_BPS` / `max_slippage_bps=`, default 2%). **An unfilled remainder rests on the book** — check `status` and cancel it if you don't want it. |
| `stop_loss` / `*_MARKET` prices were derived by the server | The SDK signs `trigger ± max slippage` (default 2%). |
| `tp_price` / `sl_price` | Same meaning (the triggers). Each leg sells no lower than `trigger − max slippage` (the client's; `leg_max_slippage_bps=` per order). |
| `orders.approve()`, `offerings.approve()`, `offerings.subscribe()` | Removed: signed orders need no approval, and IPO subscriptions are not available through the API. |
| `time_in_force=`, `deadline=`, `TimeInForce.IOC` / `FOK` / `GTD` | Removed: every order is good-til-cancelled. |
| Wrappers (`limit_buy`, `stop_loss`, …) forwarded any keyword to `create` | Each takes only its own keywords; anything else is a `TypeError`. |
| `MARKET_ORDER_PRICE` | Removed: a MARKET order signs a real worst price, not 0. |
| `TradeStatus`, `trade.status` | Removed: a fill is final when it matches and arrives once. |
| A timed-out order raised `LoafConnectionError` | Placements are re-sent unchanged on timeouts and 5xx. If the SDK still can't tell, it raises `OrderOutcomeUnknownError`. |
| `LoafBusinessRuleError` | Removed: no SDK endpoint answers 422. The daily price band is a `LoafValidationError` (400). |
| `KycRequiredError` | Removed: no SDK endpoint has a KYC gate; such a 403 is a plain `LoafForbiddenError`. |
| Existing API keys | No longer work. Create a new key (§2). |

---

## The client

Create one `LoafClient` and reach everything through grouped resources:

| Resource | What it covers |
| --- | --- |
| `loaf.market` | properties, property detail, candle history, info pages (all public) |
| `loaf.offerings` | IPO offerings: list, detail |
| `loaf.orders` | place (signed) / cancel / cancel-all orders, stop & take (conditional) orders |
| `loaf.portfolio` | balances, positions, PnL |
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
market.property(token)                GET    /trade/{token}
market.candles(token, resolution)     GET    /trade/{token}/candles
market.iter_candles(token, res)       (auto-paginate candle history)
market.info_header(token)             GET    /info/{token}/header
market.info_overview(token)           GET    /info/{token}/overview
market.info_documents(token)          GET    /info/{token}/documents

offerings.list()                      GET    /offerings
offerings.get(token)                  GET    /offerings/{token}

orders.create(...) / limit_buy / ...  POST   /orders                (signed; no API key)
orders.create_conditional(...)        POST   /orders/conditional    (signed; no API key)
orders.stop_loss / take_profit(...)   POST   /orders/conditional    (signed; no API key)
orders.resubmit(signed_body)          POST   /orders or /orders/conditional (re-send unchanged)
orders.cancel(order_id)               POST   /orders/cancel
orders.cancel_conditional(order_id)   POST   /orders/conditional/cancel
orders.cancel_row(order)              (routes a row to the right cancel)
orders.cancel_all()                   POST   /orders/cancel-all

portfolio.get() / component()         GET    /portfolio, /portfolio/component

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

Candle history is a dedicated, paginated endpoint (the property detail response
does not include it):

```python
h = loaf.market.candles("opera", "1h", count_back=200)   # 1m|5m|15m|1h|4h|1d|1w
print(h.candles[-1])                  # latest {time, open, high, low, close, volume}
older = loaf.market.candles("opera", "1h", to=h.oldestTs)  # page back while h.hasMore
```

---

## Placing orders

Placing an order is a single call:

```python
# Orders are addressed by tokenName. Pick a tradeable one from loaf.market.properties():
# status "LIVE", isCompetition == competitionModeActive, and a contractAddress.

# LIMIT order (price in dollars, <=2 dp; quantity in tokens, <=1 dp):
res = loaf.orders.limit_buy("opera", quantity=10, price=167.49)
print(res.orderId, res.status, res.quantityLeft)   # e.g. 991 OPEN 10

# MARKET order: no price. The SDK signs reference x (1 ± max slippage) as your worst fill price:
res = loaf.orders.market_buy("opera", quantity=2)          # reference fetched; client's max slippage
res = loaf.orders.market_sell("opera", quantity=2,
                              reference_price=mark, max_slippage_bps=50)   # mark: your markprice; 0.5%
if res.status in ("OPEN", "PARTIALLY_FILLED"):     # the unfilled rest is RESTING at the worst price
    loaf.orders.cancel(res.orderId)

loaf.orders.cancel_all()      # flatten everything
```

**A 200 is sent after the exchange commits the order, so its `status` is
real.** `FILLED` means filled; `OPEN` / `PARTIALLY_FILLED` mean it is resting
with `quantityLeft` tokens to go. `CANCELLED` on a new BUY means it filled
partly and could not pay for the rest within what it froze. Later fills and
cancels arrive on your private `portfolio` WebSocket channel (see below).

**MARKET orders take no price: the SDK works out your worst price.**

- You pass the token and quantity. Unless you pass `reference_price`, the SDK
  reads the market reference (the order-book mid when both sides exist, else
  the last trade, else the last candle close, else the IPO price) and signs
  `reference × (1 ± max slippage)` (+ for a BUY, − for a SELL) as your worst
  fill price. With a reference of 160
  and the default 2%, `market_buy` signs 163.20 and `market_sell` 156.80.
- Max slippage is in basis points (100 = 1%). Set it once with
  `LOAF_MAX_SLIPPAGE_BPS` in your `.env` or `LoafClient(max_slippage_bps=150)`;
  the default is 200 (2%), and `loaf.max_slippage_bps` shows the value in use.
  Override it for one order with `max_slippage_bps=`. The same setting prices
  stops, take-profits and TP/SL legs (see **Stop & take orders**).
- The exchange matches a MARKET order against resting orders at their prices,
  never worse than the price you signed, and **anything left rests on the
  book** until it fills or you cancel it.
- The reference read is one extra public request, served from a shared cache,
  so it can trail the live market by tens of seconds and may not yet show a
  trade you just made. For price-sensitive orders pass the
  `markprice:{tokenName}` value you stream as `reference_price` (it also saves
  the request).
- If the exchange has no market price yet, the SDK refuses the order locally
  rather than sign it off zero.
- A BUY freezes **worst price × quantity + taker fee**, so size it with the
  same reference and max slippage you sign with:

```python
import math
from loaf import bps_to_fraction, worst_price

comp = loaf.portfolio.component()
worst = worst_price(mark, "BUY", loaf.max_slippage_bps)  # what market_buy(..., reference_price=mark) signs
fee = bps_to_fraction(comp.applicableFees.takerFeeBps)
qty = math.floor(comp.cash / (worst * (1 + fee)) * 10) / 10
res = loaf.orders.market_buy("opera", quantity=qty, reference_price=mark)  # same reference
```

A few checks run server-side that the SDK cannot pre-validate, so handle their
rejections rather than trying to avoid them:

| Rejection | Rule |
| --- | --- |
| `LoafValidationError` (400) | **Minimum order value** — `price x quantity` must be at least 10 USDC, measured at the price you **sign** (a MARKET order's worst price). A SELL closing your *entire* available position is exempt, so dust can always be flattened. |
| `LoafValidationError` (400) | **Limit price deviation** — any order, MARKET included, whose signed price is too far from the market reference is refused. The ceiling is a deployment setting; keep your max slippage small. |
| `LoafValidationError` (400) | **Daily price band** — a per-property band around the daily reference price, when enabled. The message carries the price, limit, reference and band width. |
| `LoafValidationError` (400) | **Balance and open-order caps** — `Insufficient available balance for this order`, or too many open orders (resting stops count too). |

While a trading-competition round is **ACTIVE**, only accounts admitted to the
round may place orders — otherwise you get `CompetitionEligibilityError` (check
your standing with `loaf.competition.queue_position()`). Outside a round
trading is unrestricted, apart from brief pauses (the same exception) while a
round is being prepared or finalized and while the market switches modes. If
trading is halted — platform-wide or for that property — order placement
raises `TradingHaltedError` (403). A platform-wide halt blocks cancels too,
except `orders.cancel_conditional()`, which is database-only and stays
available (see **Stop & take orders** below). Outside market hours placement
raises `LoafForbiddenError` (`Trading is currently closed`); cancels still work.

To see a halt coming instead of discovering it on a rejected order, subscribe to
the property's status channel:

```python
ws.subscribe_property_status("opera")

@ws.on_property_halt
def on_halt(msg):
    print(msg.tokenName, "halted" if msg.isHalted else "resumed")
```

`isHalted` is the **effective** state — the property's own flag OR'd with the
platform-wide kill switch — so assign it straight over the `isHalted` you seeded
from `market.property("opera").property.isHalted`. Note a global halt lifting does
not resume a property that is individually halted; the frame accounts for that.

### Signing & retries

- The SDK signs every order on your machine (EIP-712) with your agent key. The
  request carries that signature, not your API token.
- The first order on a property reads its token contract once (a public
  request) and caches it. When a competition round is prepared, competition
  properties get new contracts; the SDK notices the refusal, re-reads the
  contract and re-signs once, automatically.
- Each order carries a fresh nonce, and the SDK re-sends the **exact** signed
  order on timeouts and server errors, after a short pause, so a re-send can
  never place it twice: a repeat of a plain order is answered with
  `duplicate: True` and the same `orderId`. Each attempt is given at least 45
  seconds to answer, because the exchange replies only once the order is
  committed. With the default 3 retries a call can therefore block for a few
  minutes while the exchange is struggling; use `LoafClient(max_retries=1)` in
  a latency-sensitive loop.
- If the SDK still cannot tell whether an order was placed, it raises
  `OrderOutcomeUnknownError`. The order may be live, or may already have
  filled. Don't answer it with a brand-new order:
  - **Still want it?** `loaf.orders.resubmit(e.signed_body)` within 24 hours:
    the answer is that same order if it landed; if not, it is placed now, at
    the price and quantity you signed then.
  - **Otherwise** find it in `loaf.history.orders()` (every status; match
    token, side, quantity, price and a `createdAt` near when you placed it).
    `openOrders` shows only resting orders, so one that already filled is not
    there.
  - **A `resubmit` that fails** raises `OrderOutcomeUnknownError` again (the
    reason is in `e.last_error`) and proves nothing about the first attempt:
    checks such as the open-order cap run first and count the order that
    landed. A lasting refusal (a halt, the cap) won't change by re-sending, so
    go to `history.orders()` then.
  - **After a round switch** do neither: compare positions and cash instead
    (see **Round switches** below).
- Nonces carry the signing time, so your machine's clock must be within a day
  of real time.
- The client keeps the key only inside its signer and never logs it. Prefer
  `$LOAF_AGENT_PRIVATE_KEY` over `agent_private_key=`: the environment variable
  never appears in the SDK's frames, while an explicit `agent_private_key=`
  argument can show up in tools that capture call frames (crash reporters,
  debuggers).

```python
from loaf import OrderOutcomeUnknownError

try:
    res = loaf.orders.market_buy("opera", quantity=2)
except OrderOutcomeUnknownError as e:
    res = loaf.orders.resubmit(e.signed_body)   # the same order: placed at most once
    # (raises OrderOutcomeUnknownError again if this re-send can't tell either)
```

### Stop & take orders

A stop or take order rests in the backend until the mark price reaches your
trigger, then books as an ordinary limit order signed when you placed it.
Nothing is frozen until it books, but it does occupy an open-order slot.

```python
# Protect a long: SELL 5 if the mark falls to 90, take profit if it rises to 120.
# (Booking prices here assume the default 2% max slippage.)
sl = loaf.orders.stop_loss("opera", quantity=5, trigger_price=90)      # books a SELL at 88.20
tp = loaf.orders.take_profit("opera", quantity=5, trigger_price=120)   # books a SELL at 117.60

# Any of the eight type x side combinations, spelled out:
loaf.orders.create_conditional("opera", "BUY", quantity=2,
                               type="STOP_MARKET", trigger_price=130)   # breakout; books at 132.60
loaf.orders.create_conditional("opera", "SELL", quantity=5,
                               type="STOP_LIMIT", trigger_price=90, price=89.5)

# The same two legs attached to a BUY — these ARE OCO, one firing cancels the other
# (they sell no lower than 117.60 / 88.20):
loaf.orders.limit_buy("opera", quantity=10, price=100, tp_price=120, sl_price=90)

# More room below the trigger than the client's max slippage, for this order only:
loaf.orders.stop_loss("opera", quantity=5, trigger_price=90, max_slippage_bps=500)   # books at 85.50

loaf.orders.cancel_conditional(sl.orderId)   # while PENDING / ARMED
```

Direction comes from the type **and** the side, not the type alone:

| Type | A BUY fires when the mark | A SELL fires when the mark |
| --- | --- | --- |
| `STOP_MARKET` / `STOP_LIMIT` | **rises** to the trigger | **falls** to the trigger |
| `TAKE_MARKET` / `TAKE_LIMIT` | **falls** to the trigger | **rises** to the trigger |

**A 200 means the row is ARMED — not that it will ever book.** Every
trigger-time failure (holding gone, cash gone, price band, limit deviation,
minimum value *again*, engine rejection) raises nothing; it arrives as
`status: "FAILED"` with a `rejectionReason` on your `portfolio` channel. A
`rejectionReason` on a row that is still `ARMED` means deferred-and-retrying,
not dead.

| Rejection | Rule |
| --- | --- |
| `LoafValidationError` (400) | **Already through the trigger** — refused if the mark has reached your level. Triggering is level-based, so a trigger *equal* to the mark is refused too. Compare your trigger against the mark yourself (`markprice:{tokenName}`); if the mark is already there, place a plain order. |
| `LoafValidationError` (400) | **Holding / cash** — checked against your **total** balance, not the available one, because a firing trigger frees funds from your own resting orders first (only those on the same property and side). Nothing is frozen until it books. |
| `LoafValidationError` (400) | **Open-order caps** — resting conditionals count against the same caps as booked orders. |
| `LoafValidationError` (400) | **Minimum order value** — the same floor as a plain order, measured at the price the row will *book* at — for a `*_MARKET` type the trigger moved by your max slippage. A SELL for your whole holding is exempt. Re-checked when the row fires. |

A `*_MARKET` type is a **protected limit, not a market order**. The SDK signs
`trigger × (1 ± max slippage)` (the client's, or `max_slippage_bps=` on the
call) as the price it books at, fixed for life, so after a gap past that price
the booked order rests instead of executing. The quantity is fixed too and is
never clamped — a SELL whose holding shrank below it FAILS rather than selling
what is left. TP/SL legs likewise sell no lower than
`trigger × (1 − max slippage)`, using the client's value rather than the parent
order's `max_slippage_bps=`, so a tight entry never tightens your stops; pass
`leg_max_slippage_bps=` to change it.

**Legs on a MARKET parent are measured against the market reference, not
against your fill.** The server checks `tp_price` / `sl_price` against the
market reference — not your worst price and not the price you end up filling
at. A `tp_price` just above the reference can sit below your fill; the leg then
arms already through its trigger and sells at once, no lower than its signed
price. Leave the take-profit clear of the mark, or place it yourself once you
see the fill. A leg's own price is first checked (deviation, band, minimum
value) when it fires; if that fails the leg becomes FAILED. Cancelling a partly
filled parent (for example a resting MARKET remainder) FAILs its legs.

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

**Round switches wipe every order** (booked and conditional, real-market and
competition properties alike) together with your order and trade history
(`history.orders()` / `history.trades()` start empty), and release their
frozen funds. Real-market positions survive. The orders simply vanish: no
`CANCELLED` status, `order_update` or `balances_update` frame tells you, so
after a round change reset your local order state and re-place / re-arm what
you still want. Drop any `OrderOutcomeUnknownError.signed_body` you were
holding too: resubmitting it after a switch places it again. Poll
`loaf.competition.info()`, or watch the transition live on the public
`competition` channel, which has no `subscribe_*` helper but is reachable with
the generic pair:

```python
ws.subscribe("competition")

@ws.on("round_update")
def on_round(msg):
    print(msg.roundNumber, msg.status)   # reset your order state, re-place / re-arm here
```

Derive triggers with `round(mark * 0.95, 2)`: an unrounded float product carries
17 decimals and the SDK rejects it locally before the request goes out.

---

## Money & units

Everything is in plain human units — prices in **dollars**, quantities in
**tokens** — for what you send and what you receive:

- Prices take up to 2 decimal places (cents); quantities up to 1 decimal place.
  Both are checked client-side before a request is sent.
- Prices and quantities are signed exactly as you send them; the SDK scales
  them internally for the signature only.
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

ws.subscribe_orderbook("opera")
ws.subscribe_trades("opera")
ws.subscribe_portfolio()            # your private stream

ws.run_forever()                # blocking; or `with loaf.websocket() as ws:` for background
```

Handlers run on the client's event-loop thread, so keep them short. An
exception raised in a handler is logged and swallowed. Don't place orders from
a handler: a placement can block the feed for minutes while the exchange
struggles (long enough for the server to drop the connection), and an
`OrderOutcomeUnknownError` raised there is only logged. Record the event and
act from your own loop, as `bot.py` does.

Channels:

| Channel | Auth | Handler | Payload |
| --- | --- | --- | --- |
| `orderbook:{tokenName}` | public | `on_orderbook` | full-depth bid/ask snapshot when the book changes (at most every 500 ms); no initial frame — seed from `market.property(token).orderBook` |
| `trades:{tokenName}` | public | `on_trades` | rolling recent-trades batch |
| `chart:{tokenName}` | public | `on_candle` | OHLCV candle updates |
| `markprice:{tokenName}` | public | `on_mark_price` | canonical mark price (1s, on change) |
| `volume:{tokenName}` | public | `on_volume` | rolling-24h traded volume (replaces `volume24h`) |
| `property:{tokenName}` | public | `on_property_halt` | halt / resume status for that property |
| `ipo:{ipoId}` | public | `on_ipo` | primary-market allocation progress |
| `leaderboard` | public | `on_leaderboard` | competition leaderboard (top entries), on change |
| `portfolio` | **private** | `on_balances`, `on_position`, `on_order_status`, `on_order_update`, `on_trade`, `on_lifetime_volume`, `on_transfer`, `on_offering_order` | your account deltas |

The private channel carries no id: the server resolves it from the account
your API key authenticated as, so an anonymous connection is refused with an
`error` frame. It is a **delta stream** —
you receive `balances_update`, `position_update`, `order_status`, etc. as
separate frames. Each fill arrives **once**, final when it matches (`txHash` is
`""` until a batch proof covers the trade), in the same burst as its
`balances_update`, `position_update` and
`order_status`. Balances and positions are absolute snapshots — replace, don't
add. For an order that trades immediately, its `order_update` can arrive after
those frames and already show FILLED. `order_status` `CANCELLED` carries the
cancelled remainder in `quantityLeft`. Delivery is at most once, so after a
reconnect reconcile from `loaf.portfolio.component()` / `loaf.history.trades()`.
To value positions live, combine `position_update` with the `markprice` channel
(the server does not push recomputed portfolio totals on every price tick). Stop
and take orders ride the same `order_update` frame as booked orders, so narrow
with `loaf.is_conditional_order(msg.order)` before reading `filledQuantity`.
`CANCELLED` frames can also arrive for orders you never cancelled: a firing
conditional cancels your own resting orders on that side of that property to
free its funds, your own order crossing a resting one of yours cancels the
resting one, and a BUY that runs out of frozen budget is cancelled.

---

## Error handling

Every failure maps to a specific exception (all subclass `LoafError`):

```python
import time
import loaf
from loaf import LoafClient

client = LoafClient()
try:
    client.orders.limit_buy("opera", quantity=1, price=167.49)
except loaf.OrderOutcomeUnknownError as e:
    ...   # may be live or filled: resubmit(e.signed_body) if still wanted, else check history.orders()
except loaf.CompetitionEligibilityError:
    ...   # not admitted to the active round, or a round is switching
except loaf.TradingHaltedError:
    ...   # halted — back off and retry later
except loaf.LoafAuthError:
    ...   # order signature refused: your agent key is expired/deleted — stop and alert
except loaf.LoafValidationError as e:
    print(e.message, e.details)        # 400 (local ones have status_code 0)
except loaf.LoafRateLimitError as e:
    time.sleep(e.retry_after or 5)     # 429
except loaf.LoafAPIError as e:
    print(e.status_code, e.message, e.code, e.request_id)
```

`LoafConnectionError` means no HTTP response arrived (network error or
timeout); from an order placement it means the order never reached the
exchange, because anything that might have landed raises
`OrderOutcomeUnknownError` instead. `LoafConfigError` means a missing or
invalid key (for placing orders: `LOAF_AGENT_PRIVATE_KEY`) or an invalid
client-wide max slippage (`LOAF_MAX_SLIPPAGE_BPS` /
`LoafClient(max_slippage_bps=)`); a bad per-order `max_slippage_bps=` /
`leg_max_slippage_bps=` is a local `LoafValidationError` (status_code 0).

## Rate limits & retries

The backend rate limits requests per IP and sends standard `RateLimit-*`
headers (snapshot at `loaf.last_rate_limit`). Order placement, cancels,
portfolio reads and competition queue/payout calls are additionally limited
**per account** — your API key and your agent share one budget, so rotating
IPs doesn't raise the ceiling. Reads are retried automatically on 429, 503 and
network errors, with backoff that honours `RateLimit-Reset` / `Retry-After`.
Order placements are re-sent unchanged on network errors and server errors
after a short jittered pause, not the rate-limit headers (see **Signing &
retries**), but never on a 429 — a rate-limited order raises
`LoafRateLimitError` so you can decide whether to re-issue it at a fresh
price. (A rate-limited `resubmit` raises `OrderOutcomeUnknownError` instead,
since the first attempt may be live.) Cancels are never retried. Tune with
`LoafClient(max_retries=...)`.

---

## Examples

| File | Shows |
| --- | --- |
| `examples/01_quickstart.py` | confirm credentials, read balances + market |
| `examples/02_market_data.py` | properties, order book, candle history (public) |
| `examples/03_place_order.py` | place → inspect → cancel a limit order (needs your agent key) |
| `examples/04_realtime_market.py` | stream order book + trades + mark price + halts |
| `examples/05_portfolio_stream.py` | stream your private portfolio events |
| `examples/06_stop_and_take.py` | arm a stop far from the mark, read it back, cancel it (needs your agent key) |
| `bot.py` | full strategy-loop template |

## Tests

```bash
pip install -e ".[dev]"
pytest
```

The suite runs fully offline against an in-memory mock transport (no live
server, no real key) and covers auth, order signing (checked against the
exchange's reference EIP-712 test vectors), the order flow, stop & take orders,
input validation, pagination, error mapping, and retry behaviour.

## Notes / intentionally excluded

- This SDK is the **trading-facing** surface. Account management, KYC, referrals
  (`/auth/*`), the featured-offering home feed (`/home`), fiat on/off-ramps
  (`/portfolio/onramp|offramp`), and the shareable image cards
  (`/portfolio/position/{tokenName}/pnl-card`, `/leaderboard/card`,
  `/competition/queue-position/card`) are **not** wrapped — do those in the
  Loaf web app.
- Create your API key (and its agent) in the web app. Nothing else is needed
  for the private WebSocket channel — the API token identifies the account it
  streams.
- IPO subscriptions are not wrapped: they are closed for now, and they need
  your account wallet's own signature, which an agent key cannot give.
- The default base URL is the **production API** (`https://api.loafmarkets.com/api`).
  For local dev, set `LOAF_API_BASE_URL` to your dev server (e.g.
  `http://localhost:8005/api`). For a local server using a self-signed cert, pass
  `LoafClient(verify=False)` — which disables TLS verification (MITM protection),
  so use it only against a trusted localhost, never a remote host.

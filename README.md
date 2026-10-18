# Loaf Python API Bot Template

A batteries-included **Python SDK + bot template** for the [Loaf](https://loafmarkets.com)
trading platform. It wraps the trading-facing REST endpoints and the real-time
WebSocket feed so you can build a bot that reads market data, tracks a
portfolio, and places trades in a few lines.

> [!IMPORTANT]
> **This SDK is experimental and moves fast.** Loaf is in testnet development,
> so expect heavy changes while it is — breaking ones included. Track `main`, or
> pin a tag deliberately and re-read this README before you move off the pin;
> [§5 Updating](#5-updating) has the versioning contract. A stable SDK lands
> with mainnet, and release notes go out on
> [@CohenLoaf](https://x.com/CohenLoaf).

```python
from loaf import LoafClient

loaf = LoafClient()   # reads $LOAF_API_KEY and $LOAF_L2_SECRET_KEY (see §2 and §3)

for row in loaf.portfolio.component().balances:            # your L2 book, in base units
    print(row.symbol, row.available)
print(loaf.market.properties())                            # listed properties
loaf.orders.limit_buy("OPRA", quantity=10, price=167.49)   # signed locally with your L2 trading key
```

---

## 1. Install

```bash
git clone <this repo> && cd loaf-python-api-bot-template
python -m venv .venv && source .venv/bin/activate

# Install the SDK (httpx + websockets). Add extras as needed:
pip install -e .              # core
pip install -e ".[l2keys]"    # + eth-account: derive and register your L2 trading key, buy in an IPO
pip install -e ".[dotenv]"    # + auto-load a .env file
pip install -e ".[dev]"       # + pytest and eth-account for the test suite
```

Requires Python 3.9+. Orders are signed by the matching engine's own signer, a
native library the SDK ships for Linux x86_64 and aarch64 (glibc 2.28 and
newer), macOS arm64 (11.0 and newer) and x86_64 (10.12 and newer), and Windows
x86_64. On any other platform, point `$LOAF_L2_SIGNER_LIB` at a build of the
matching engine's `crates/l2-signer-ffi`.

## 2. Get your API key

Create an API key in the Loaf web app (log in → **Settings → API keys →
Create**). The page shows it **once** — copy it into your `.env`. The bot uses
**two secrets**: this API token, and the L2 trading key you set up in §3.

| Secret | `.env` variable | What it does |
| --- | --- | --- |
| API token | `LOAF_API_KEY` | Sent as `Authorization: Bearer` on reads, cancels, the WebSocket and the account calls an order needs (its key state, its order indices). Never sent with an order. 64 hex characters, no prefix. |
| L2 trading key | `LOAF_L2_SECRET_KEY` | Signs every order you place, on your machine. It is never sent. 80 hex characters ([§3](#3-enable-trading-your-l2-trading-key)). |

- An API key lasts **at most 180 days**, and you can hold 5 at once. At its
  expiry, or when you delete it, reads, cancels and the order-index
  reservations get `401 Invalid credentials`, so placing stops too once the
  indices the SDK holds run out. A bot cannot read its own expiry, so note the
  date when you create the key.
- Deleting a key does not cancel anything: your orders and stops stay live
  until you cancel them.

```bash
cp .env.example .env
# edit .env:
#   LOAF_API_KEY=<API token — 64 hex characters, no prefix>
#   LOAF_L2_SECRET_KEY=<L2 trading key — 80 hex characters, see §3>
#   LOAF_API_BASE_URL=https://api.loafmarkets.com/api   # or http://localhost:8005/api for local dev
#   LOAF_MAX_SLIPPAGE_BPS=200   # optional: max slippage in basis points (100 = 1%), see Placing orders
```

The SDK reads environment variables; `bot.py` and the examples load `.env` into
them when python-dotenv is installed (`pip install -e ".[dotenv]"`). Otherwise
export the variables.

## 3. Enable trading: your L2 trading key

Every order is signed by your account's **L2 trading key** before it is sent;
the exchange refuses an order that is not. It is the same key the Loaf web app
derives from your wallet when you enable trading there, so a wallet has one key
wherever it trades. Your L2 account opens with your first deposit (in the web
app); then derive the key once and register it:

```bash
pip install -e ".[l2keys]"
export LOAF_WALLET_PRIVATE_KEY=0x...   # the wallet of your API key's user
export LOAF_L1_RPC_URL=https://...     # an RPC of the custody chain (property.l2Market.chainId)
python -m loaf.l2.keys enable          # prints LOAF_L2_SECRET_KEY=<80 hex>: put it in .env
```

| Command | Does |
| --- | --- |
| `python -m loaf.l2.keys derive` | prints the key in force; exits 1 unless Loaf holds it |
| `python -m loaf.l2.keys enable` | registers the key in force and waits until Loaf holds it |
| `python -m loaf.l2.keys rotate` | a new key, registered; orders signed by the old one are refused |

The commands use your `LOAF_API_KEY` (§2) too. The wallet key is read from the
environment only (`--wallet-key-env` names another variable), never from the
command line. Registration costs you no gas: your wallet signs the key change
and Loaf sends it. Anyone who holds the trading key can trade for your
account, so keep it as secret as the wallet key. A competition round's market
has its own vault, account and key: `--ticker <round ticker>` derives that
one, which signs your orders in the round.

Without `LOAF_L2_SECRET_KEY` everything works except placing orders.
`loaf.l2_public_key` is the public key of the trading key your orders are
signed by (`None` when no key is set).

## 4. Run the bot template

```bash
python bot.py
```

`bot.py` verifies your credentials, prints balances/positions, opens the live
feed (order book + your private portfolio stream), and runs a 5-second strategy
loop with clearly-marked `# YOUR STRATEGY GOES HERE` hooks. It only *observes*
the market out of the box — drop your logic into `Strategy.on_tick`.

It follows the property named by `TARGET_TICKER` at the top of the file — a
ticker such as `OPRA` — or, when that is empty, the first listed property with
an L2 market.

> **Copy it before you edit it.** `bot.py` is tracked by this repo and ships
> alongside the SDK, so it changes when the SDK does. Strategy code written into
> it directly will collide with the next `git pull`. Work in a copy:
>
> ```bash
> cp bot.py my_bot.py   # then run: python my_bot.py
> ```

## 5. Updating

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
git checkout v0.5.0
```

Releases are tagged from `v0.3.0` onward. While the SDK is pre-1.0, a breaking
change bumps the **minor** version — so `0.4.x` → `0.5.0` is the signal to
re-read this README before upgrading. `0.5.0` is the first release that signs
orders with your L2 trading key; an older pin cannot place orders on the
current exchange. Expect that signal often while Loaf is on testnet; an old pin
is the most common reason a bot stops matching these docs.

---

## The client

Create one `LoafClient` and reach everything through grouped resources:

| Resource | What it covers |
| --- | --- |
| `loaf.market` | properties, property detail, candle history, info pages (all public) |
| `loaf.offerings` | IPO offerings: list, detail, a purchase signed by your wallet |
| `loaf.orders` | place (signed) / cancel / cancel-all orders, stop & take (conditional) orders, signed batches |
| `loaf.portfolio` | your L2 balances, positions, fees, recent activity |
| `loaf.history` | paginated order & trade history, cancelled & active orders |
| `loaf.leaderboard` | competition leaderboard |
| `loaf.competition` | competition rounds info, your queue position, payout details |

A property is named by its **ticker**: 1 to 4 of `A-Z 0-9`, exactly as
listed, e.g. `"OPRA"`.

Responses are `LoafObject`s — dicts that also allow attribute access, so
`book.bids[0].price` and `book["bids"][0]["price"]` are equivalent. Unknown
fields the API adds later are preserved automatically. See `loaf/models.py` for
typed descriptions of every documented shape.

### Endpoint coverage

The trading-facing endpoints are wrapped (account management, KYC, referrals,
deposits and withdrawals, and fiat ramps are not part of this SDK):

```
market.properties()                   GET    /trade
market.property(ticker)               GET    /trade/{ticker}
market.candles(ticker, resolution)    GET    /trade/{ticker}/candles
market.iter_candles(ticker, res)      (auto-paginate candle history)
market.info_header(ticker)            GET    /info/{ticker}/header
market.info_overview(ticker)          GET    /info/{ticker}/overview
market.info_documents(ticker)         GET    /info/{ticker}/documents

offerings.list()                      GET    /offerings
offerings.get(ticker)                 GET    /offerings/{ticker}
offerings.subscribe(ticker, qty)      POST   /offerings/subscribe   (signed by your wallet)

orders.create(...) / limit_buy / ...  POST   /orders                (signed; no API key)
orders.create_many([...])             POST   /orders, once per order (one signature for all)
orders.create_conditional(...)        POST   /orders/conditional    (signed; no API key)
orders.stop_loss / take_profit(...)   POST   /orders/conditional    (signed; no API key)
orders.resubmit(signed_body)          POST   /orders or /orders/conditional (re-send unchanged)
orders.cancel(order_id)               POST   /orders/cancel
orders.cancel_conditional(order_id)   POST   /orders/conditional/cancel
orders.cancel_row(order)              (routes a row to the right cancel)
orders.cancel_all()                   POST   /orders/cancel-all

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

To sign an order the SDK also reads `GET /l2/keys` (your account and the key in
force) and reserves order indices with `POST /l2/order-indices`, both with your
API key; it does both for you (see **Signing & retries**).

Candle history is a dedicated, paginated endpoint (the property detail response
does not include it):

```python
h = loaf.market.candles("OPRA", "1h", count_back=200)   # 1m|5m|15m|1h|4h|1d|1w
print(h.candles[-1])                  # latest {time, open, high, low, close, volume}
older = loaf.market.candles("OPRA", "1h", to=h.oldestTs)  # page back while h.hasMore
```

---

## Placing orders

Placing an order is a single call; the SDK signs it with your L2 trading key:

```python
# Orders are addressed by ticker. Pick a tradeable one from loaf.market.properties():
# status "LIVE", isCompetition == competitionModeActive, hasL2Market, and outside
# a round a contractAddress.

# LIMIT order (price in dollars, <=2 dp; quantity in tokens, <=1 dp):
res = loaf.orders.limit_buy("OPRA", quantity=10, price=167.49)
print(res.orderId, res.status, res.quantityLeft)   # e.g. 991 OPEN 10

# MARKET order: no price. The SDK signs reference x (1 ± max slippage) as your worst fill price:
res = loaf.orders.market_buy("OPRA", quantity=2)           # reference fetched; client's max slippage
res = loaf.orders.market_sell("OPRA", quantity=2,
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

- You pass the ticker and quantity. Unless you pass `reference_price`, the SDK
  reads the market reference (the order-book mid when both sides exist, else
  the last trade, else the last candle close, else the IPO price) and signs
  `reference × (1 ± max slippage)` (+ for a BUY, − for a SELL) as your worst
  fill price, rounded to the cent toward the reference. With a reference of
  160 and the default 2%, `market_buy` signs 163.20 and `market_sell` 156.80.
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
  `markprice:{ticker}` value you stream as `reference_price` (it also saves
  the request).
- If the exchange has no market price yet, the SDK refuses the order locally
  rather than sign it off zero.
- A BUY freezes **worst price × quantity + taker fee** of your L2 cash, so size
  it with the same reference and max slippage you sign with:

```python
import math
from loaf import bps_to_fraction, worst_price

comp = loaf.portfolio.component()
cash = next(row for row in comp.balances if row.isQuote)   # base units, as decimal strings
available = int(cash.available) / 10 ** cash.decimals      # dollars
worst = worst_price(mark, "BUY", loaf.max_slippage_bps)  # what market_buy(..., reference_price=mark) signs
fee = bps_to_fraction(comp.applicableFees.takerFeeBps)
qty = math.floor(available / (worst * (1 + fee)) * 10) / 10
res = loaf.orders.market_buy("OPRA", quantity=qty, reference_price=mark)  # same reference
```

A few checks run server-side that the SDK cannot pre-validate, so handle their
rejections rather than trying to avoid them:

| Rejection | Rule |
| --- | --- |
| `LoafValidationError` (400) | **Balance** — the engine admits an order on your L2 book's `available` amount (cash for a BUY, the property's tokens for a SELL); `Insufficient available balance for this order` otherwise. |
| `LoafValidationError` (400) | **Minimum order value** — `price x quantity` must be at least 10 USDL, measured at the price you **sign** (a MARKET order's worst price). A SELL closing your *entire* available position is exempt, so dust can always be flattened. |
| `LoafValidationError` (400) | **Limit price deviation** — any order, MARKET included, whose signed price is too far from the market reference is refused (`Limit price must be within 50% of the current market price …`). The ceiling is a deployment setting, 50% by default; keep your max slippage small. |
| `LoafValidationError` (400) | **Daily price band** — a per-property band around the daily reference price, when enabled. The message carries the price, limit, reference and band width. |
| `LoafValidationError` (400) | **Fee consent** — every order consents to a fee rate (`$LOAF_L2_FEE_RATE_CAP`, 7000 per million = 70 bps by default); an account whose maker or taker rate is above it is refused (`l2 fee: the signed fee-rate cap is below the account's fee rate`). |
| `LoafValidationError` (400) | **Open-order caps** — too many open orders in total or on the property (resting stops count too); the message names the limit. |

While a trading-competition round is **ACTIVE**, only accounts admitted to the
round may place orders — otherwise you get `CompetitionEligibilityError` (check
your standing with `loaf.competition.queue_position()`). The market's other
mode refusals are 403 `LoafForbiddenError`s with their own `code`:
`COMPETITION_NOT_STARTED` before the round starts, and `NOT_TRADEABLE_IN_MODE`
for a property the current mode does not trade. While the market switches
modes, placement answers a 503 `VENUE_SWITCHING`: nothing was placed, and the
SDK re-sends the order like any 503 that placed nothing. Outside a round
trading is unrestricted. If trading is halted — platform-wide or for that
property — order placement raises `TradingHaltedError` (403). A platform-wide
halt blocks cancels too, except `orders.cancel_conditional()`, which is
database-only and stays available (see **Stop & take orders** below). Outside
market hours placement raises `LoafForbiddenError` (`Trading is currently
closed`); cancels still work.

To see a halt coming instead of discovering it on a rejected order, subscribe to
the property's status channel:

```python
ws.subscribe_property_status("OPRA")

@ws.on_property_halt
def on_halt(msg):
    print(msg.ticker, "halted" if msg.isHalted else "resumed")
```

`isHalted` is the **effective** state — the property's own flag OR'd with the
platform-wide kill switch — so assign it straight over the `isHalted` you seeded
from `market.property("OPRA").property.isHalted`. Note a global halt lifting does
not resume a property that is individually halted; the frame accounts for that.

### Signing & retries

- The SDK signs every order on your machine with your L2 trading key: a
  Schnorr signature over the order's terms, made by the matching engine's own
  signer library. The request carries that signature, not your API token.
- Before signing it reads the property's L2 market from its public detail (at
  most every 30 seconds) and, with your API key, the account's key state (at
  most every 60 seconds) and a block of 64 order indices, one per order. A
  refusal that names an index drops the block; one that names the epoch, the
  key or the signature reads the key state again before the next order.
- Each order has its own index, and the SDK re-sends the **exact** signed order
  on timeouts and server errors, after a short pause, so a re-send can never
  place it twice: the exchange keys an order by its account and index, and
  answers a repeat of a plain order with `duplicate: True` and the same
  `orderId`. Each attempt is given at least 45 seconds to answer, because the
  exchange replies only once the order is committed. With the default 3
  retries a call can therefore block for a few minutes while the exchange is
  struggling; use `LoafClient(max_retries=1)` in a latency-sensitive loop.
- If the SDK still cannot tell whether an order was placed, it raises
  `OrderOutcomeUnknownError`. The order may be live, or may already have
  filled. Don't answer it with a brand-new order:
  - **Still want it?** `loaf.orders.resubmit(e.signed_body)` more than a day
    before its `l2ExpiryMs` (about a week, with the default order life): the
    answer is that same order if it landed; if not, it is placed now, at the
    price and quantity you signed then.
  - **Otherwise** find it in `loaf.history.orders()` (every status; match
    ticker, side, quantity, price and a `createdAt` near when you placed it).
    `openOrders` shows only resting orders, so one that already filled is not
    there.
  - **A `resubmit` that fails** raises `OrderOutcomeUnknownError` again (the
    reason is in `e.last_error`) and proves nothing about the first attempt:
    checks such as the open-order cap run first and count the order that
    landed. A lasting refusal (a halt, the cap) won't change by re-sending, so
    go to `history.orders()` then.
  - **Across a round switch** do neither: compare positions and cash instead
    (see **Round switches** below).
- Each order's expiry is signed from your machine's clock: a day's buffer plus
  the order's life (`LOAF_L2_ORDER_TTL_MS`, 7 days by default). The exchange
  refuses an order within a day of its expiry, so a clock running behind by
  more than the order's life signs orders it refuses.
- The client keeps the trading key only inside its signer and never logs it,
  and the SDK's own frames drop it once read. Prefer `$LOAF_L2_SECRET_KEY` over
  `l2_secret_key=`: the environment variable never appears in a call frame,
  while an explicit `l2_secret_key=` argument stays in your own calling frame,
  which tools that capture call frames (crash reporters, debuggers) record.

```python
from loaf import OrderOutcomeUnknownError

try:
    res = loaf.orders.market_buy("OPRA", quantity=2)
except OrderOutcomeUnknownError as e:
    res = loaf.orders.resubmit(e.signed_body)   # the same order: placed at most once
    # (raises OrderOutcomeUnknownError again if this re-send can't tell either)
```

### Many orders, one signature

A maker quoting several levels signs them together: one signature over a root
of the orders, each body carrying its place in the tree. The SDK then posts
them one by one, in the order given:

```python
from loaf import OrderIntent

results = loaf.orders.create_many([
    OrderIntent("OPRA", "BUY", 1, price=150.00),
    OrderIntent("OPRA", "BUY", 1, price=149.50),
    OrderIntent("OPRA", "SELL", 1, price=170.00),
])
for r in results:            # each order's answer, or the exception it was refused with
    print(r)
```

A batch takes 1 to 256 LIMIT and MARKET orders and no legs. A MARKET one is
priced off its `reference_price` (default: the market reference) at the
call's `max_slippage_bps=` (default: the client's). One order's refusal does
not stop the others.

### Stop & take orders

A stop or take order rests in the backend until the mark price reaches your
trigger, then books as an ordinary limit order signed when you placed it.
Nothing is frozen until it books, but it does occupy an open-order slot.

```python
# Protect a long: SELL 5 if the mark falls to 90, take profit if it rises to 120.
# (Booking prices here assume the default 2% max slippage.)
sl = loaf.orders.stop_loss("OPRA", quantity=5, trigger_price=90)      # books a SELL at 88.20
tp = loaf.orders.take_profit("OPRA", quantity=5, trigger_price=120)   # books a SELL at 117.60

# Any of the eight type x side combinations, spelled out:
loaf.orders.create_conditional("OPRA", "BUY", quantity=2,
                               type="STOP_MARKET", trigger_price=130)   # breakout; books at 132.60
loaf.orders.create_conditional("OPRA", "SELL", quantity=5,
                               type="STOP_LIMIT", trigger_price=90, price=89.5)

# The same two legs attached to a BUY — these ARE OCO, one firing cancels the other
# (they sell no lower than 117.60 / 88.20):
loaf.orders.limit_buy("OPRA", quantity=10, price=100, tp_price=120, sl_price=90)

# More room below the trigger than the client's max slippage, for this order only:
loaf.orders.stop_loss("OPRA", quantity=5, trigger_price=90, max_slippage_bps=500)   # books at 85.50

loaf.orders.cancel_conditional(sl.orderId)   # while PENDING / ARMED
```

Direction comes from the type **and** the side, not the type alone:

| Type | A BUY fires when the mark | A SELL fires when the mark |
| --- | --- | --- |
| `STOP_MARKET` / `STOP_LIMIT` | **rises** to the trigger | **falls** to the trigger |
| `TAKE_MARKET` / `TAKE_LIMIT` | **falls** to the trigger | **rises** to the trigger |

**A 200 means the row is ARMED — not that it will ever book.** Every
trigger-time failure (balance gone, price band, limit deviation, minimum value
*again*, engine rejection) raises nothing; it arrives as `status: "FAILED"`
with a `rejectionReason` on your `portfolio` channel. A `rejectionReason` on a
row that is still `ARMED` means deferred-and-retrying, not dead.

| Rejection | Rule |
| --- | --- |
| `LoafValidationError` (400) | **Already through the trigger** — refused if the mark has reached your level. Triggering is level-based, so a trigger *equal* to the mark is refused too. Compare your trigger against the mark yourself (`markprice:{ticker}`); if the mark is already there, place a plain order. |
| `LoafValidationError` (400) | **Open-order caps** — resting conditionals count against the same caps as booked orders. |
| `LoafValidationError` (400) | **Minimum order value** — the same floor as a plain order, measured at the price the row will *book* at — for a `*_MARKET` type the trigger moved by your max slippage. A SELL for your whole holding is exempt. Re-checked when the row fires. |

Your balance is not checked when you arm a row: the engine admits the order
when it fires, on what is `available` then.

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

**Round switches.** When a competition round is prepared, the exchange cancels
every order on the real market (booked and conditional) and releases what they
held; your real-market balances stay. While the round is prepared or active,
every request is served in the round's own market — its orders, history and
balances, under its own account and key (§3) — and the real market's come back
when it ends. So after a round change re-read your portfolio and re-place /
re-arm what you still want. Poll `loaf.competition.info()`, or watch the
transition live on the public `competition` channel:

```python
ws.subscribe_competition()

@ws.on_round_update
def on_round(msg):
    print(msg.roundNumber, msg.status)   # re-read your portfolio, re-place / re-arm here
```

Derive triggers with `round(mark * 0.95, 2)`: an unrounded float product carries
17 decimals and the SDK rejects it locally before the request goes out.

---

## Money & units

Prices and quantities are plain human units — prices in **dollars**,
quantities in **tokens** — for what you send and for the orders and trades you
receive:

- Prices take up to 2 decimal places (cents); quantities up to 1 decimal place.
  Both are checked client-side before a request is sent.
- Prices and quantities are signed exactly as you send them; the SDK scales
  them internally for the signature only.
- **Balances are in base units**: `portfolio.component().balances` has one row
  per asset of your L2 book (`symbol`, `decimals`, `isQuote` for the cash) with
  `total`, `hold` (held by open orders) and `available` as decimal strings.
  USDC has 6 decimals, so `"1500000"` is 1.5 USDC:
  `int(row.available) / 10 ** row.decimals`.
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

ws.subscribe_orderbook("OPRA")
ws.subscribe_trades("OPRA")
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
| `orderbook:{ticker}` | public | `on_orderbook` | full-depth bid/ask snapshot when the book changes (at most every 500 ms); no initial frame — seed from `market.property(ticker).orderBook` |
| `trades:{ticker}` | public | `on_trades` | rolling recent-trades batch |
| `chart:{ticker}` | public | `on_candle` | OHLCV candle updates |
| `markprice:{ticker}` | public | `on_mark_price` | canonical mark price (1s, on change) |
| `volume:{ticker}` | public | `on_volume` | rolling-24h traded volume (replaces `volume24h`) |
| `property:{ticker}` | public | `on_property_halt` | halt / resume status for that property |
| `ipo:{ipoId}` | public | `on_ipo` | primary-market allocation progress |
| `leaderboard` | public | `on_leaderboard` | competition leaderboard (top entries), on change |
| `competition` | public | `on_round_update` | a competition round changed state |
| `portfolio` | **private** | `on_balances`, `on_position`, `on_order_status`, `on_order_update`, `on_trade`, `on_lifetime_volume`, `on_transfer`, `on_offering_order` | your account deltas |

A channel for a ticker the server does not know is refused with an `error`
frame (`UNKNOWN_CHANNEL`).

The private channel carries no id: the server resolves it from the account
your API key authenticated as, so an anonymous connection is refused with an
`error` frame. It is a **delta stream** — you receive `balances_update`,
`order_status`, `order_update`, `trade_new` and the rest as separate frames:

- `balances_update` carries your whole L2 book as the engine committed it:
  replace the rows you hold, don't add.
- `trade_new` is each of your fills, **once**, final when it matches, with its
  `proof`: `PENDING` until a proven batch includes it, then `IN_BATCH` (with
  the transaction that executed the batch once there is one). Read the trade
  again (`history.trades()`) for its progress.
- An L2 holding's position is not pushed: read `portfolio.component()` again
  after your own fills, or when a `balances` row appears that no position
  names. `position_update` is an IPO allocation's.
- For an order that trades immediately, its `order_update` can arrive after
  its `order_status` / `trade_new` frames and already show FILLED.
  `order_status` `CANCELLED` carries the cancelled remainder in `quantityLeft`.
- Delivery is at most once, so after a reconnect reconcile from
  `loaf.portfolio.component()` / `loaf.history.trades()`.
- To value positions live, combine your balances with the `markprice` channel
  (the server does not push recomputed portfolio totals on every price tick).
- Stop and take orders ride the same `order_update` frame as booked orders, so
  narrow with `loaf.is_conditional_order(msg.order)` before reading
  `filledQuantity`.
- `CANCELLED` frames can also arrive for orders you never cancelled: a firing
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
    client.orders.limit_buy("OPRA", quantity=1, price=167.49)
except loaf.OrderOutcomeUnknownError as e:
    ...   # may be live or filled: resubmit(e.signed_body) if still wanted, else check history.orders()
except loaf.CompetitionEligibilityError:
    ...   # not admitted to the active round
except loaf.TradingHaltedError:
    ...   # halted — back off and retry later
except loaf.LoafAuthError:
    ...   # order signature refused: your trading key is not in force (rotated or disabled) — stop and alert
except loaf.LoafValidationError as e:
    print(e.message, e.details)        # 400 (local ones have status_code 0)
except loaf.LoafRateLimitError as e:
    time.sleep(e.retry_after or 5)     # 429
except loaf.LoafL2Error as e:
    print(e)                           # no order can be signed: the text names why
except loaf.LoafAPIError as e:
    print(e.status_code, e.message, e.code, e.request_id)
```

`LoafConnectionError` means no HTTP response arrived (network error or
timeout); from an order placement it means the order never reached the
exchange, because anything that might have landed raises
`OrderOutcomeUnknownError` instead. `LoafL2Error` is a setup no order can be
signed under, and its text names it: no L2 market for the property, a trading
key Loaf does not hold (enable or rotate it, §3), no L2 account yet (deposit
first), an unusable index reservation. `L2SignerError` is its subclass for the
native signer's own refusals. `LoafConfigError` means a missing or invalid key
(placing an order needs both `LOAF_L2_SECRET_KEY` and `LOAF_API_KEY`) or an
invalid client-wide max slippage (`LOAF_MAX_SLIPPAGE_BPS` /
`LoafClient(max_slippage_bps=)`); a bad per-order `max_slippage_bps=` /
`leg_max_slippage_bps=` is a local `LoafValidationError` (status_code 0).
`LoafBusinessRuleError` (422) is the engine refusing an L2 request by its
rules, such as an order-index reservation the SDK could not wait out.
`KycRequiredError` (403) means `offerings.subscribe()` needs your KYC first.

## Rate limits & retries

The backend rate limits requests per IP and sends standard `RateLimit-*`
headers (snapshot at `loaf.last_rate_limit`). Order placement, cancels,
order-index reservations, portfolio reads and competition queue/payout calls
are additionally limited **per account** — your API key and your trading key
share one budget, so rotating IPs doesn't raise the ceiling. Reads are retried
automatically on 429, 503 and network errors, with backoff that honours
`RateLimit-Reset` / `Retry-After`; the reads an order makes before signing
raise a 429 at once instead of waiting it out, because a late order is stale.
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
| `examples/03_place_order.py` | place → inspect → cancel a limit order (needs your L2 trading key and API key) |
| `examples/04_realtime_market.py` | stream order book + trades + mark price + halts |
| `examples/05_portfolio_stream.py` | stream your private portfolio events |
| `examples/06_stop_and_take.py` | arm a stop far from the mark, read it back, cancel it (needs your L2 trading key and API key) |
| `bot.py` | full strategy-loop template |

## Tests

```bash
pip install -e ".[dev]"
pytest
```

The suite runs fully offline against an in-memory mock transport (no live
server, no real key) and covers auth, order signing (checked against the
engine signer's known-answer vectors and the web app's own pricing and wallet
signatures), the order flow, stop & take orders, input validation, pagination,
error mapping, and retry behaviour.

## Notes / intentionally excluded

- This SDK is the **trading-facing** surface. Account management, KYC, referrals
  (`/auth/*`), the featured-offering home feed (`/home`), deposits and
  withdrawals (`/l2/deposits`, `/l2/withdrawals`), fiat on/off-ramps
  (`/portfolio/onramp|offramp`), the batch explorer (`/explorer/*`), and the
  shareable image cards (`/portfolio/position/{ticker}/pnl-card`,
  `/portfolio/performance/card`, `/leaderboard/card`,
  `/competition/queue-position/card`) are **not** wrapped — do those in the
  Loaf web app.
- Create your API key in the web app. Nothing else is needed for the private
  WebSocket channel — the API token identifies the account it streams.
- `offerings.subscribe()` needs your wallet's private key
  (`$LOAF_WALLET_PRIVATE_KEY`, with the `l2keys` extra) and an RPC of the
  sale's chain (`$LOAF_IPO_RPC_URL`): the wallet signs the purchase and a
  permit of its payment, and Loaf sends it, with no transaction of yours.
- The default base URL is the **production API** (`https://api.loafmarkets.com/api`).
  For local dev, set `LOAF_API_BASE_URL` to your dev server (e.g.
  `http://localhost:8005/api`). For a local server using a self-signed cert, pass
  `LoafClient(verify=False)` — which disables TLS verification (MITM protection),
  so use it only against a trusted localhost, never a remote host.

# Loaf Python API Bot Template

A batteries-included **Python SDK + bot template** for the [Loaf](https://loafmarkets.com)
trading platform. It wraps the trading-facing REST endpoints and the real-time
WebSocket feed so you can build a bot that reads market data, tracks a
portfolio, and places trades in a few lines.

```python
from loaf import LoafClient

loaf = LoafClient(api_key="your-api-key")          # or set $LOAF_API_KEY

print(loaf.portfolio.component().cash)             # available USDL
print(loaf.market.properties())                    # what's tradeable
loaf.orders.limit_buy("opera", quantity=10, price=167.49)  # trade (by tokenName)
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

## 2. Get an API key

The bot authenticates with a **user API key**, sent as
`Authorization: Bearer <key>` on every request.

> API keys can only be **created** while logged in to the Loaf web app
> (this is enforced server-side so a leaked key can't mint more keys).
> Log in → **Settings → API keys → Create**, copy the key (shown once), and
> paste it into your `.env`. A bot then uses that key for everything below.

```bash
cp .env.example .env
# edit .env:
#   LOAF_API_KEY=<your key — a 64-character hex string, no prefix>
#   LOAF_API_BASE_URL=https://api.loafmarkets.com/api   # or http://localhost:8005/api for local dev
```

## 3. Run the bot template

```bash
python bot.py
```

`bot.py` verifies your credentials, prints balances/positions, opens the live
feed (order book + your private portfolio stream), and runs a 5-second strategy
loop with clearly-marked `# YOUR STRATEGY GOES HERE` hooks. It only *observes*
the market out of the box — drop your logic into `Strategy.on_tick`.

---

## The client

Create one `LoafClient` and reach everything through grouped resources:

| Resource | What it covers |
| --- | --- |
| `loaf.market` | properties, property detail, candle history, info pages (all public) |
| `loaf.offerings` | IPO offerings: list, detail, subscribe, pre-approve |
| `loaf.orders` | nonce, place / cancel / cancel-all orders, pre-approve |
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
valuations, and fiat ramps are not part of this SDK):

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
offerings.subscribe(ipo_id, qty)      POST   /offerings/subscribe
offerings.approve(ipo_id)             POST   /offerings/approve

orders.nonce()                        POST   /orders/nonce
orders.create(...) / limit_buy / ...  POST   /orders
orders.cancel(order_id)               POST   /orders/cancel
orders.cancel_all()                   POST   /orders/cancel-all
orders.approve(token_name)            POST   /orders/approve

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
no longer inlines it):

```python
h = loaf.market.candles("opera", "1h", count_back=200)   # 1m|5m|15m|1h|4h|1d|1w
print(h.candles[-1])                  # latest {time, open, high, low, close, volume}
older = loaf.market.candles("opera", "1h", to=h.oldestTs)  # page back while h.hasMore
```

---

## Placing orders

Orders use a two-step nonce protocol; the SDK handles it for you:

```python
# Orders are addressed by tokenName (list the tradeable ones via loaf.market.properties()):

# LIMIT order (price in dollars, <=2 dp; quantity in tokens, <=1 dp):
res = loaf.orders.limit_buy("opera", quantity=10, price=167.49)
print(res.orderId)            # accepted into the book (NOT necessarily filled)

# MARKET order (price is forced to 0, slippage-bounded server-side):
loaf.orders.market_sell("opera", quantity=2.5)

loaf.orders.cancel(res.orderId)
loaf.orders.cancel_all()      # flatten everything
```

**A 200 means the exchange accepted the order — not that it filled.**
Fills and cancellations arrive asynchronously on your private `portfolio`
WebSocket channel (see below).

Three checks run server-side that the SDK cannot pre-validate, so handle their
rejections rather than trying to avoid them:

| Rejection | Rule |
| --- | --- |
| `LoafValidationError` (400) | **Minimum order value** — `price x quantity` must be at least 10 USDL. A SELL closing your *entire* available position is exempt, so dust can always be flattened. |
| `LoafValidationError` (400) | **Limit price deviation** — a LIMIT price too far from the current market reference is refused. The ceiling is a deployment setting; the message quotes it and the reference used. MARKET orders skip this — they are slippage-bounded instead. |
| `LoafBusinessRuleError` (422) | **Daily price band** — a per-property band around the daily reference price. The message carries the side, limit, reference and band width. |

While a trading-competition round is **ACTIVE**, only accounts admitted to the
round may place orders — otherwise you get `CompetitionEligibilityError` (check
your standing with `loaf.competition.queue_position()`). Outside an active
round trading is unrestricted. If trading is halted platform-wide,
order placement/cancels raise `TradingHaltedError` (403).

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

---

## Money & units

Everything is in plain human units — prices in **dollars**, quantities in
**tokens** — for what you send and what you receive:

- Prices take up to 2 decimal places (cents); quantities up to 1 decimal place.
  Both are checked client-side before a request is sent.
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

Channels:

| Channel | Auth | Handler | Payload |
| --- | --- | --- | --- |
| `orderbook:{tokenName}` | public | `on_orderbook` | full bid/ask snapshot (~500ms) |
| `trades:{tokenName}` | public | `on_trades` | rolling recent-trades batch |
| `chart:{tokenName}` | public | `on_candle` | OHLCV candle updates |
| `markprice:{tokenName}` | public | `on_mark_price` | canonical mark price (1s, on change) |
| `volume:{tokenName}` | public | `on_volume` | session volume (replaces `volume24h`) |
| `property:{tokenName}` | public | `on_property_halt` | halt / resume status for that property |
| `ipo:{ipoId}` | public | `on_ipo` | primary-market allocation progress |
| `leaderboard` | public | `on_leaderboard` | competition leaderboard (top entries), on change |
| `portfolio` | **private** | `on_balances`, `on_position`, `on_order_status`, `on_order_update`, `on_trade`, `on_lifetime_volume`, `on_transfer`, `on_offering_order` | your account deltas |

The private channel carries no id: the server resolves it from the account
your API key authenticated as, so an anonymous connection is refused with an
`error` frame. It is a **delta stream** —
you receive `balances_update`, `position_update`, `order_status`, etc. as
separate frames. Each fill is delivered twice under one `tradeId` (`SETTLING`,
then `SETTLED`), so dedupe on it before accumulating anything. To value
positions live, combine `position_update` with the `markprice` channel (the
server does not push recomputed portfolio totals on every price tick).

---

## Error handling

Every failure maps to a specific exception (all subclass `LoafError`):

```python
import loaf, time

try:
    loaf.orders.limit_buy("opera", quantity=1, price=167.49)
except loaf.CompetitionEligibilityError:
    ...   # not admitted to the active competition round
except loaf.TradingHaltedError:
    ...   # platform-wide trading halt — back off and retry later
except loaf.LoafValidationError as e:
    print(e.message, e.details)        # 400 field errors
except loaf.LoafRateLimitError as e:
    time.sleep(e.retry_after or 5)     # 429
except loaf.LoafAPIError as e:
    print(e.status_code, e.message, e.code, e.request_id)
```

`LoafConnectionError` covers network/timeout failures (no HTTP response).
`LoafConfigError` is raised if you call an authenticated endpoint without a key.

## Rate limits & retries

The backend rate limits requests per IP and sends standard
`RateLimit-*` headers (snapshot at `loaf.last_rate_limit`). Sensitive
endpoints (orders, offering subscriptions, competition queue/payout) are
additionally rate limited **per account**, so rotating IPs doesn't raise the
ceiling. The client
automatically retries transient failures (429, 503, network errors) on
idempotent (read) requests, with backoff that honours `RateLimit-Reset` /
`Retry-After`. Non-idempotent calls (e.g. placing an order) are never
auto-retried — a rate-limited order raises `LoafRateLimitError` so you can
re-issue it with a fresh nonce rather than risk reusing a stale one. Tune with
`LoafClient(max_retries=...)`.

---

## Examples

| File | Shows |
| --- | --- |
| `examples/01_quickstart.py` | confirm credentials, read balances + market |
| `examples/02_market_data.py` | properties, order book, candle history (public) |
| `examples/03_place_order.py` | place → inspect → cancel a limit order |
| `examples/04_realtime_market.py` | stream order book + trades + mark price + halts |
| `examples/05_portfolio_stream.py` | stream your private portfolio events |
| `bot.py` | full strategy-loop template |

## Tests

```bash
pip install -e ".[dev]"
pytest
```

The suite runs fully offline against an in-memory mock transport (no live
server, no real key) and covers auth, the order flow, input validation,
pagination, error mapping, and retry behaviour.

## Notes / intentionally excluded

- This SDK is the **trading-facing** surface. Account management, KYC, referrals
  (`/auth/*`), property valuations (`/valuations/*`), the featured-offering
  home feed (`/home`), fiat on/off-ramps (`/portfolio/onramp|offramp`), and the
  shareable image cards (`/portfolio/position/{tokenName}/pnl-card`,
  `/leaderboard/card`, `/competition/queue-position/card`) are **not** wrapped —
  do those in the Loaf web app.
- Create your API key in the web app. Nothing else is needed for the private
  WebSocket channel — the key identifies the account it streams.
- The default base URL is the **production API** (`https://api.loafmarkets.com/api`).
  For local dev, set `LOAF_API_BASE_URL` to your dev server (e.g.
  `http://localhost:8005/api`). For a local server using a self-signed cert, pass
  `LoafClient(verify=False)` — which disables TLS verification (MITM protection),
  so use it only against a trusted localhost, never a remote host.

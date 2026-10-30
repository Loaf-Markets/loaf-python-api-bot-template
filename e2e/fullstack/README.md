# Full-stack perps e2e

`e2e.py` drives the whole stack with the Python SDK as the only trading client: the backend's REST, WebSocket and
admin routes in front of the engine, settled, the K1 prover and the devnet (anvil L1 with ZkLoaf on TEST keys), with
the kit's price pump moving the indexes. Money that moves on chain is checked on L1 too.

On the P5 kit's fullstack phase (box 3):

    ./p5-fullstack.sh <sdk tree> <log file>
    E2E_STEPS=restore ./p5-fullstack.sh <sdk tree> <log file>     # the one-shot restore (below)

`p5-fullstack.sh` runs only once the phase is up (`fullstack.ok`, `round.log` ending `==== DONE`) and while the
kit's `round.sh` is not running, under a shared hold of the box's heavy-job lock and an 8G memory scope. It reads
the phase's `run/fullstack.env` (only its `P5_*` lines), passes the secret files by path (`lb-admin.key`, the DSN
files, or the kit's `run/psql`), keeps the run's wallet seed in `state/wallet.seed` (mode 600, made once) and calls
`run.sh`. Elsewhere:

    E2E_API=... E2E_RPC=... E2E_PSQL=... E2E_WALLET_SEED_FILE=... [more E2E_*] ./run.sh <sdk tree> <log file>

`run.sh` makes a venv from `lock.txt` (hash-pinned wheels; the SDK kit's lock) once, puts the SDK tree on its path
and runs `python -I e2e.py`, teeing the log. The exit status is the driver's.

One driver at a time on the stack: this e2e and the browser pass share the price control's seq, the backstop knob
and the funder. `p5-fullstack.sh` takes an exclusive flock(2) on `P5_DRIVER_LOCK` (default
`/mnt/zk/g/locks/p5-fullstack-driver.lock`, or the phase's `fullstack.env`) at once or exits 2 naming the lock,
and holds it through every exec until the run ends. Every driver takes the same lock the same way (`flock -n -x
<lock> <command>` does), never a POSIX record lock (`lockf`, `fcntl` F_SETLK), which flock(2) does not see. The
holder writes the file's first line, `pid <pid> <who> since <UTC time>`; a refused driver names it while that pid
lives.

## Log

Each step prints `==== step <name>`, an `ok <assertion>` line for each assertion that holds, and
`==== exit <name> <rc>`: 0 passed, 1 an assertion failed (`FAIL <step>: ...`), 2 an error (`ERROR`, with the
traceback's tail), 3 incomplete (`INCOMPLETE <step>: ...`: a leg the configuration cannot drive, the rest of the
step passed). If setup does not pass, every later step is skipped. The run ends with
`==== VERDICT PASS|FAIL <step>=<rc> ...` and `==== DONE`; it exits 0 only when every step that ran passed.
Every wait has a deadline (E2E_WAIT_S and the longer ones below). Nothing secret is printed or put in an argv.

Whatever its verdict, each step then puts back what it moved (`restore: ...`): the run's orders are cancelled, each
market whose state it controlled (the admin halt, the operator's controls, a silenced price) is reopened, each market
it walked or silenced walks home (`E2E_HOME_INDEX`, else its index before the step's first move), and the backstop's
knob goes back to 0 if the step raised it. Each part is tried whatever the others did; one that fails is logged
`ERROR <step>: not restored: ...` and makes a passing step an error (2). A stop (SIGTERM) runs the running step's
restore on the way out, logs `STOPPED (exit 143)`, the verdict FAIL, and exits 143.

`E2E_STEPS=restore`, alone, runs no setup and no other step: every role market reopened if it is not OPEN and
walked home to `E2E_HOME_INDEX`, the knob at 0, then each checked OPEN at home; the backstop's free collateral and
positions are logged. Its positions stay: the kit's `backstop_flatten` closes them, and the liquidation step
refuses a stack whose backstop holds one on `liq1`.

## Environment

| Variable | Default | What |
|---|---|---|
| `E2E_API` | required | the backend's REST base, ending `/api` |
| `E2E_WS` | from `E2E_API` | the backend's WebSocket URL |
| `E2E_RPC` | required | the devnet L1's JSON-RPC (anvil: `anvil_setBalance` funds gas) |
| `E2E_WALLET_SEED_FILE` | | a file holding a hex seed (16 bytes or more): user X's wallet key is keccak(seed, "/X") |
| `E2E_MNEMONIC` | | else a mnemonic: the run's wallets are its HD accounts from `E2E_HD_BASE` |
| `E2E_HD_BASE` | 100 | the first HD index: A, B, C, MM take four in a row |
| `E2E_FUNDER` | anvil account 5 | an unlocked account that may `mint(address,uint256)` the test tokens |
| `E2E_PSQL` | | a psql command on the backend's database, e.g. `docker exec -i <pg> psql -U <user> -d <db>` |
| `E2E_PSQL_DSN_FILE` | | else (or with it) a file holding the DSN: psql runs with its PG* variables, never an argv |
| `E2E_JOURNAL_PSQL` / `E2E_JOURNAL_DSN_FILE` | the backend's | the same for the journal's database (closes, settled's refusals) |
| `E2E_ADMIN_API_KEY_FILE` | | a file holding the backend's admin API key (halt step; the operator's controls) |
| `E2E_ADMIN_API_KEY` | | else the key itself |
| `E2E_PRICE_CONTROL` / `E2E_PRICE_STATUS` | none | the pump's control and status files (below); without them every price leg is INCOMPLETE |
| `E2E_OPERATOR` | none | `sql`: the operator's controls emulated (below); none: those legs are INCOMPLETE |
| `E2E_HOME_INDEX` | none | setup walks every role market to this index first |
| `E2E_SETTLESUB_METRICS` | none | settlesub's Prometheus URL (`settlesub_last_verified_batch`) |
| `E2E_ENGINE_METRICS` | none | the engine's Prometheus URL (`matching_engine_perps_liq_pending`), for the backstop's guard |
| `E2E_MARKETS` | `trade=0,liq1=6,liq0=2,halt=3,carry_pos=8,carry_neg=9` | the perps market each role uses (liq1: the deleverage and mode 1; liq0: mode 0; carry_pos/carry_neg: the markets with a positive / negative funding carry) |
| `E2E_BACKSTOP_DOLLARS` | 50000 | the backstop's free collateral for the mode 1 leg |
| `E2E_SPOT` | the first LIVE spot market with an L2 | the spot market; none listed: the spot step is INCOMPLETE (so the verdict is FAIL); its deposits failing in setup FAIL the spot step |
| `E2E_SPOT_PRICE` | 10 | the spot reference when the market has no price yet |
| `E2E_PRICE_DECIMALS` / `E2E_SIZE_DECIMALS` | 3 / 1 | an index's decimals (dollars x 1000) and a size's (tokens x 10) |
| `E2E_NOTIONAL` | 1500 | dollars per position |
| `E2E_MIXED_SECS` | 30 | the mixed step's length: spot and perps IOC fills at once for that long |
| `E2E_STEPS` | all | a comma list of steps (setup always runs), or `restore` alone (no setup) |
| `E2E_KEEP_BACKSTOP` | unset | `1`: a step's restore leaves the backstop's knob (and the position it took) on the stack for the desk leg; `E2E_STEPS=restore` puts it back later |
| `E2E_TAG` | e2e | the run's users' display-name prefix |
| `E2E_WAIT_S` / `E2E_FINAL_WAIT_S` / `E2E_PROOF_WAIT_S` | 180 / 2400 / 5400 | a wait's deadline; an L1 execution's; the proofs' |

### The price pump's control

The driver writes each control whole to a temporary file beside `E2E_PRICE_CONTROL` and renames it onto it:
`{"seq": n, "markets": {"<m>": {"mode": "walk", "index": i} | {"mode": "quiet"}}, "backstop": {"free_ticks": t}}`,
`n` above the file's last `seq` and the status's `applied_seq`. A control names only what it changes: a market it
does not name keeps its mode, and a control without `backstop` leaves the backstop as it is. The driver waits until
`E2E_PRICE_STATUS` reads `applied_seq` >= `n` (an `error` there fails the step; a control the pump cannot read
leaves `applied_seq` and sets `file_error`, which the timeout names), then, for a walk, until the backend's live
index is within 1 bp of the target. For the backstop it waits until the kit's knob record answers that control
(`backstop.applied_seq` == `n`) with `done`: `error` fails, and `pending` (an allocate K1 refused, which the kit
retries each tick for 120 s before its error) waits. `free_ticks` is the backstop's live free margin, which a mark or
funding move shifts, so it decides only for a status with no knob record. A walk stays inside the move bound, so
every report is accepted; quiet sends nothing, and the market turns PRICE_STALE once its stale window passes.

The backstop's guard: before a leg raises `free_ticks`, the driver sends a no-op control (`"markets": {}`) and
reads the status that answers it. Its `backstop.below` lists each pool under its maintenance requirement or in
debt, as the engine values it: `[{"account": a, "pool": p, "value": v, "requirement": r, "markets": [m, ...]}]`
(the requirement x 65,536, as a pool read gives it; `markets`, those of its positions, empty for a flat pool).
The leg fails, naming them, if any pool but its victim is one the backstop acts on once funded: a flat pool in
debt (its write-off, row 70), or one under 2/3 of its requirement or holding a position on an expired market
(row 64 mode 1). Without that list the driver reads the flat pools in debt from the read model
(`l2_perps_pools`) and the engine's `matching_engine_perps_liq_pending`, and fails on any debt or any pending pool,
as nothing names them; with neither, the leg is INCOMPLETE and the backstop is not raised.

### The operator's controls (`E2E_OPERATOR=sql`)

The backend takes a market control (OPEN, REDUCE_ONLY) and a resume from an operator's Privy session only. For a
stack without Privy the driver does what `POST /admin/perps/markets/:m/control` does: it writes the market row's
`is_halted` (false) and `is_reduce_only`, then has the engine load the market, which adopts both (LoadOrderBook),
and the backend reload its markets, through the admin API key's `POST /admin/markets/status` LIVE on the LIVE
market. The API key's own resume is still asserted refused. The run's log says so in a NOTE: the backend's operator
route and its Privy auth are not exercised.

## What each step asserts

**setup**
- every role's market is listed ACTIVE with a ticker; the collateral is asset 3 (USDC); spot and perps share one
  vault and L2 chain; the RPC serves that chain; ZkLoaf has code; L1's USDC tick and extension are the backend's;
- with `E2E_HOME_INDEX`, every role market's index walks there (a market left unreported is reported again);
- A, B, C (INVESTOR) and MM (MARKET_MAKER, which the backend's per-user rate limiter exempts) are upserted (the users
  id sequence moved past the largest id first) before any deposit to them: the engine binds an address's L2 account
  to the user whose wallet the deposit names when it journals the deposit, and nothing else binds it for the driver
  (`POST /l2/keys` is Privy-only). It compares the wallets without case and binds nothing when users in two cases
  hold one, so the wallet is written lowercase and a user holding it in any case is the one upserted, never doubled.
  An address whose first deposit was journaled before its users row existed (a backend database reset under a kept
  chain) is never bound: its login is refused, and setup fails naming the account's binding;
- nothing is read on L2 until each user's trading key is in force, since a login token authenticates only once the
  engine has journaled the account (bound) and its key 0. A user with no L2 account on L1
  (`getAccountIndexFromAddress`) opens one: its wallet deposits 100 USDC (mint, approve, `deposit`). Its trading key
  is derived from its wallet's signature as the web app derives it (the `L2Key` typed data at the key nonce in
  force); with none yet (L1's key nonce 0) the wallet registers it on L1 itself, ZkLoaf's
  `changePubKey(account, 0, key)` with no fee, as the P5 kit's traders do. Every key change is sent before the
  first wait. The user's client is the SDK's on the web app's trading key, `loaf_tk_<L2 chain>_<account>_<secret>`;
  its login token is refused 401 until the key is in force, then `GET /l2/keys` reads it journaled on the user's L1
  account (pending false), `l2_accounts` binds the account to the upserted user, and L1's key nonce agrees: every
  trading key logs in. A re-run sends nothing; one after a run that stopped between an account's first deposit and
  its key change sends the change;
- each user deposits on L1 (spot route): mint, approve, `deposit`; the backend's L2 balance rises by exactly the
  deposit (USDC to all; the spot market's base to B and MM, its quote to A and C when it is not USDC); each run
  deposits at least 100 and tops up to the targets;
- MM's WebSocket connects on its portfolio channel; settled's refusal count is read.

**spot**: every order is a whole number of tokens, sized so the least of them, the bid, is worth 2.5 times the
backend's least order value (10 USDL; under it an order is refused). A limit bid rests whole, holds cash, and its
cancel releases exactly its hold; MM's post-only ask rests and
A's post-only bid at it is refused POST_ONLY_WOULD_TAKE; an IOC with nothing to cross and a FOK for twice the
resting size are refused with the backend's texts; an IOC for twice the ask fills its size and rests nothing; a FOK
for the ask fills whole; C's bid takes B's ask: the tokens move from B to C, C pays the notional plus at most 1 %
fee, and the fill is in C's history.

**perps** (market `trade`)
- allocates: into pool 0 and isolated pool 1 and back out of pool 1; the spot USDC moves by exactly the ticks, and a
  flat pool's collateral by ticks x extension;
- the market is OPEN; A's market buy and B's market sell fill against MM: A long, B short by exactly the size; A's
  entry price is the ask's;
- a crossing limit buy and sell from isolated pool 1 open and close a long;
- post-only rests below the ask and is refused at it; B's IOC above every bid and A's FOK for twice the ask are
  refused, and the FOK fills nothing;
- a reduce-only sell of half A's long fills; a reduce-only buy in a flat pool opens nothing (with MM's ask resting at
  its price);
- a buy with TP/SL legs from pool 3: two legs, both ARMED once the parent fills; the index walks above the
  take-profit, the leg fires and closes the long into MM's bid;
- A's and B's positions read entry, entry quote, notional, funding owed, liquidation price (on the losing side of
  the entry) and adlRank; uPnL is logged; their pools are healthy; the market's live state reads the index, open
  interest (at least the run's size), funding index, next tick, period and trading state.

**mixed** (spot and perps at once, `E2E_MIXED_SECS` long): in one thread C takes MM's spot ask and then its bid, one
order each, over and over; in another B takes A's perps ask at the index + 0.1 % and then its bid at the index
- 0.1 % (pool 0), over and over; no client is shared between the threads. An IOC refused NOT_FILLED (its quote not
yet resting) is counted dry and the quote withdrawn; a partial fill or any other refusal fails the step. Both sides
filled, dry takes fewer than fills; C's and MM's spot base moved by exactly the fills, C paid the net notional plus
at most 1 % of the gross in fees; B long and A short by exactly the open perps fills; both flat again. The fill rates
go in the verdict's notes, and the proof step then finds every one of these trades IN_BATCH.

**funding** (markets `carry_pos` and `carry_neg`): A opens a long at the index from isolated pools 6 and 7 (no
premium); two ticks that move each market carry the carry's sign; A's funding owed has it (positive pays) and MM's
short the other; each pool's value is exactly collateral + notional - funding owed; the close at the index, away
from a tick, leaves the collateral at the pool's value less the close's fee (within a cent a token): the funding
was realised.

**liquidation** (C's longs from isolated pools with their initial margin plus 10 %, opened against MM's ask at the
index, with no bid left on the book)
- first, the backstop holds no position on `liq1` (the read model's `l2_perps_positions`, account 1): a long it took
  in an earlier run's mode 1 would ride this run's deleverage walk with its free collateral at 0 and take its pool 0
  under initial margin, so the step runs once per stack;
- deleverage (market `liq1`, the backstop's free collateral at 0): the index walks C's pool 1 to -1/5 of its
  maintenance (below 0; K1 deleverages no pool above 0); C's closes are DELEVERAGEs only, not adl, the newest
  leaving 0, and the pool holds nothing; in the journal each names its counter, whose own close (adl) is under the
  same journalSeq; when MM is the counter, MM's closes read it (DELEVERAGE, adl);
- mode 1 (market `liq1`, the backstop funded with `E2E_BACKSTOP_DOLLARS` once the backstop's guard finds no other
  pool waiting for it): the index walks C's pool 2 to 2/5 of its maintenance; C's close is a LIQUIDATION of the whole
  long, not an ADL, and no order of MM's filled it;
- mode 0 (market `liq0`, as the P5 kit's mode0_leg): the index walks C's pool 4 to 17/20 of its maintenance and
  nothing is liquidated without a bid; MM bids just under the index and far asks run batches; C's close is a
  LIQUIDATION of the whole long; MM's portfolio holds the liquidation fill (its bid, MAKER, no fee, no tradeId, no
  txHash, perpsSeq == the close's journalSeq), its WebSocket got the `liquidation_fill` frame, and the market's tape
  prints the take as isLiquidation with tradeId == perpsSeq and no txHash;
- the step's restore walks each market home and puts the backstop's free collateral back to 0.

**halt** (market `halt`, reopened first if an earlier run left it otherwise): A long from pool 5 against B's short;
MM rests a far ask; the admin API key halts the market (200, engine loaded): the live state is HALTED,
`GET /trade/{ticker}` reads isHalted, an opening and a reducing order are refused MARKET_HALTED, MM's cancel is taken;
the API key's resume is refused 403 (operator session needed). The operator's REDUCE_ONLY lifts the halt
(isHalted false), refuses an order adding to A's long (REDUCE_ONLY) and lets B's reduce-only bid and A's
reduce-only sell trade half; its OPEN takes an opening order again. Then the pump stops reporting the market: it
turns PRICE_STALE, refuses an order adding to A's long (PRICE_STALE) and lets a reduce-only pair trade a quarter;
the reports resume and the market is OPEN again.

**exit**: A's pool 0 closed and its whole ticks allocated to spot. The driver no longer makes an L2 withdrawal (nor
its L1 claim): `POST /l2/withdrawals` is Privy-only since the trading-key change, and the driver has no Privy login.
The run's verdict says so in a NOTE.

**proof**: every trade of the run's users (newer than each user's last trade before the run, read in pages of at
most 100, the backend's largest) is IN_BATCH and final;
the newest batch is past L1's committed batch at the start; L1's verified and executed pointers reach it;
settlesub's last verified batch equals L1's; settled's refusal count is unchanged (the newest refusals are logged
if not). It proves the run's own batches, so it needs a step that trades in `E2E_STEPS`: exit makes no withdrawal
any more, and `E2E_STEPS=exit,proof` alone has no batch to prove and fails.

## Laptop checks

`test_e2e.py` (pytest) checks the driver's own parts with no stack: the trading key's derivation against the web
app's vectors (viem's signatures of its `L2Key` typed data, addresses in any case), the setup's account and key
sent on L1 before any L2 read with the SDK's login refused until the key is in force (a fresh address, a re-run, a
key changed since, a run stopped between the first deposit and the key change), the exit's allocate-out with no
withdrawal, `index_for` (below 0 too), the SQL (the users matched without case, no API key), the DSN files, the
price control's writes and answers, the seed wallets, the L1 reads' decoding, the calls against ZkLoaf's ABI
(`E2E_ZKLOAF_ABI`, `changePubKey` among them), the step log, the verdict and the exit codes; and `p5-fullstack.sh`'s
mapping of the phase, its refusals, and its driver lock (a second driver refused while one runs, the lock held
through the run and free after it); the backstop's guard on a fake status (another waiting pool named, the victim
and the book's tier passed, the debts and the engine's gauge without a list); every numeric wait ending on 0 as on
any number (a sweep that empties a pool); the spot leg's order size against the backend's least order value; the
proof step's history pages within the backend's largest; each step's restore (a failing step's markets reopened
and walked home and the knob put at 0, with and without `E2E_HOME_INDEX`; a restore that fails named, a passing
step then an error; a stop's restore) and the one-shot restore (alone, no setup). `E2E_SDK` names an SDK tree when the venv has none installed.

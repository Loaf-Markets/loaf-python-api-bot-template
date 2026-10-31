"""Perps against an in-memory exchange: the perps reads, a perps order's signed
pool and reduce-only bit, allocates and exits (their bodies, signatures and
nonce), their retries and resubmit, and the market-state refusals.

Signatures are checked with ``conftest.verify_order`` and ``conftest.verify_op``,
which rebuild the signed terms from the posted JSON as the exchange reads it.
"""

from __future__ import annotations

import json
import re
import threading
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

import loaf
from loaf import PerpsOutcomeUnknownError, k1
from loaf._l2 import market_of
from loaf.constants import DEFAULT_PERPS_OP_TTL_MS, MAX_PERPS_OP_TTL_MS, MIN_ORDER_TTL_MS

from conftest import (
    ACCOUNT_INDEX,
    BAD_SIGNATURE,
    KAT,
    KEY_DISABLED,
    L2_SECRET,
    MARKET_L2,
    MARKET_STATES,
    NONCE_REFUSAL,
    PERPS_ACCOUNT,
    PERPS_COLLATERAL,
    PERPS_LIVE,
    PERP_L2,
    UNKNOWN_KEY,
    WRONG_CHAIN,
    freeze,
    market_detail,
    routes,
    secret_locals,
    verify_login,
    verify_op,
    verify_order,
)

# The SDK sends an allocate's and an exit's keys in this fixed order.
ALLOCATE_KEYS = (
    "l2ChainId l2AccountIndex fromSide toSide amount l2Nonce l2ExpiryMs l2SigS l2SigE".split()
)
EXIT_KEYS = "l2ChainId l2AccountIndex pool market l2Nonce l2ExpiryMs l2SigS l2SigE".split()
# A perps order's: its pool and reduce-only bit where a spot order's base asset is.
PERPS_ORDER_KEYS = (
    "price quantity side type timeInForce l2ChainId l2AccountIndex l2MarketIndex l2OrderIndex "
    "l2Epoch l2FeeRateCap l2ExpiryMs l2Pool l2ReduceOnly l2QuoteAssetId l2SigS l2SigE"
).split()
# Shared-Types' signedLegSchema (0.30.0): a leg's own keys, in the order the SDK sends them, and
# its l2-constants MAX_ORDER_INDEX and MAX_48.
LEG_KEYS = "triggerPrice price l2OrderIndex l2ExpiryMs l2SigS l2SigE".split()
MAX_ORDER_INDEX = 1_099_511_627_775
MAX_48 = 2**48 - 1
# OPRA-PERP, perp 4.
PERPS_DETAIL = market_detail("OPRA-PERP", market_index=4, l2=PERP_L2, market_price=101.5)

NOW_MS = 1_800_000_000_000
# The login token's check, before the op reaches the engine: its account could not be read.
ACCOUNT_UNREAD = (
    "l2 account: the trading account could not be read; nothing was done, try again in a moment"
)


def _bearer(request: httpx.Request) -> str:
    """The login token a request carries as its ``Authorization: Bearer``."""
    return request.headers["authorization"][len("Bearer ") :]

PAUSED ="Trading is paused. Your perps request was not submitted — please try again shortly."
NOT_TAKEN = (
    "perps: the engine did not take the op and nothing was journaled; send it again in a moment"
)
NOT_CONFIRMED = (
    "perps: the engine did not confirm the op; resend the same signed body (a journaled one is "
    "answered as a duplicate)"
)


def error_reply(status: int, message: str) -> httpx.Response:
    return httpx.Response(status, json={"error": message})


@pytest.fixture
def perps_exchange(exchange):
    """The exchange with OPRA-PERP listed too."""
    exchange.details["OPRA-PERP"] = PERPS_DETAIL
    return exchange


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #


def test_positions_and_pools_are_the_summarys_perps(exchange, trading_client):
    perps = trading_client().perps
    assert perps.positions() == PERPS_ACCOUNT
    cross, isolated = PERPS_ACCOUNT["pools"]
    assert perps.pool() == cross
    assert perps.pool(2) == isolated
    assert perps.pool(1) is None
    assert perps.position(4) == cross["positions"][0]
    assert perps.position(9, pool=2) == isolated["positions"][0]
    assert perps.position(9) is None  # pool 0 holds none there
    assert perps.position(4, pool=1) is None
    gets = exchange.gets("/portfolio/summary")
    assert len(gets) == 8
    for request in gets:
        assert verify_login(_bearer(request))["l2AccountIndex"] == ACCOUNT_INDEX


def test_the_collateral_is_read_once_and_needs_the_trading_key(exchange, trading_client):
    perps = trading_client().perps
    assert perps.collateral() == PERPS_COLLATERAL
    perps.add_margin(5)
    assert perps.collateral() == PERPS_COLLATERAL
    assert len(exchange.gets("/portfolio/summary")) == 1
    with pytest.raises(loaf.LoafConfigError, match="needs your trading key"):
        trading_client(trading_key=None).perps.collateral()


def test_a_null_perps_is_unavailable_and_never_no_positions(exchange, trading_client):
    exchange.perps_account = None
    perps = trading_client().perps
    for read in (perps.positions, perps.pool, lambda: perps.position(4), perps.collateral,
                 lambda: perps.add_margin(5)):
        with pytest.raises(loaf.LoafServiceUnavailableError, match="does not mean you hold none"):
            read()
    assert exchange.posts("/l2/allocations") == []
    exchange.perps_account = json.loads(json.dumps(PERPS_ACCOUNT))
    assert perps.position(4) == PERPS_ACCOUNT["pools"][0]["positions"][0]  # nothing kept from the failure


def test_a_perps_live_state_is_its_markets(perps_exchange, trading_client):
    # Public: a perp's detail carries its live state, whose mark is not its index.
    live = trading_client(trading_key=None).market.get("OPRA-PERP").market.live
    assert live == PERPS_LIVE
    assert live.markPrice != live.indexPrice
    (request,) = perps_exchange.requests
    assert "authorization" not in request.headers
    assert "live" not in trading_client().market.get("OPRA").market  # a spot market has none


def test_positions_need_the_trading_key_in_force(exchange, trading_client):
    with pytest.raises(loaf.LoafConfigError, match="needs your trading key"):
        trading_client(trading_key=None).perps.positions()
    assert exchange.requests == []
    exchange.key_in_force = None
    with pytest.raises(loaf.LoafAuthError, match=re.escape(UNKNOWN_KEY)):
        trading_client().perps.positions()


@pytest.mark.parametrize(
    "read",
    [
        lambda perps: perps.pool(8),
        lambda perps: perps.pool(1.0),
        lambda perps: perps.position(255),
        lambda perps: perps.position(4, pool=-1),
    ],
)
def test_reads_check_their_arguments_locally(exchange, trading_client, read):
    with pytest.raises(loaf.LoafValidationError) as info:
        read(trading_client().perps)
    assert info.value.status_code == 0
    assert exchange.requests == []


def test_market_of_a_perp():
    assert market_of(PERPS_DETAIL, "OPRA-PERP") == (
        421614, "0x" + "11" * 20, 4242, 4, 0, 3, "PERP", "ACTIVE"
    )
    assert market_of(market_detail(), "OPRA")[3:] == (2050, 6, 3, "SPOT", None)


@pytest.mark.parametrize(
    "l2, field",
    [
        ({**PERP_L2, "chainId": 0}, "chainId"),
        ({**PERP_L2, "quoteAssetId": 4}, "quoteAssetId"),  # a perp's quote asset is USDC, 3
        ({**PERP_L2, "quoteAssetId": "3"}, "quoteAssetId"),
        ({k: v for k, v in PERP_L2.items() if k != "status"}, "status"),
        ({**PERP_L2, "status": "HALTED"}, "status"),
        ({**PERP_L2, "status": "active"}, "status"),
        ({k: v for k, v in PERP_L2.items() if k != "quoteAssetId"}, "quoteAssetId"),
    ],
)
def test_unusable_perp_market(exchange, trading_client, l2, field):
    exchange.details["OPRA-PERP"] = market_detail("OPRA-PERP", market_index=4, l2=l2)
    message = f"GET /trade/OPRA-PERP answered an unusable market.l2.{field}"
    with pytest.raises(loaf.LoafServerError, match=re.escape(message)):
        trading_client().orders.limit_buy("OPRA-PERP", 2, 101.5)
    assert routes(exchange.requests) == ["GET /trade/OPRA-PERP"]


def test_a_perps_l2_is_no_spot_l2_and_back(exchange, trading_client):
    # The type decides what an order sends: a perp's l2 served for a spot market is unusable
    # (no base asset), and a spot l2 served for a perp (no status) too.
    exchange.details["OPRA"] = market_detail(l2=PERP_L2)
    with pytest.raises(loaf.LoafServerError, match=re.escape("unusable market.l2.baseAssetId")):
        trading_client().orders.limit_buy("OPRA", 10, 167.49)
    exchange.details["OPRA-PERP"] = market_detail("OPRA-PERP", market_index=4, l2=MARKET_L2)
    with pytest.raises(loaf.LoafServerError, match=re.escape("unusable market.l2.status")):
        trading_client().orders.limit_buy("OPRA-PERP", 2, 101.5)
    assert exchange.posts("/orders") == []


# --------------------------------------------------------------------------- #
# The leverage setting
# --------------------------------------------------------------------------- #


def test_a_leverage_is_saved_by_market_index_or_ticker(perps_exchange, trading_client):
    portfolio = trading_client().portfolio
    assert portfolio.summary().perpsLeverage == []
    assert portfolio.set_perps_leverage(4, 5) == {"marketIndex": 4, "leverage": 5}
    assert portfolio.set_perps_leverage("OPRA-PERP", 32_768) == {"marketIndex": 4, "leverage": 32_768}
    assert portfolio.set_perps_leverage(0, 1) == {"marketIndex": 0, "leverage": 1}
    assert portfolio.summary().perpsLeverage == [
        {"marketIndex": 0, "leverage": 1}, {"marketIndex": 4, "leverage": 32_768}
    ]
    assert routes(perps_exchange.requests) == [
        "GET /portfolio/summary",
        "PUT /portfolio/perps-leverage",
        "GET /trade/OPRA-PERP",  # a ticker names its market by the detail's marketIndex
        "PUT /portfolio/perps-leverage",
        "PUT /portfolio/perps-leverage",
        "GET /portfolio/summary",
    ]
    puts = [r for r in perps_exchange.requests if r.method == "PUT"]
    # Shared-Types' perpsLeverageBodySchema: strict, both whole numbers; sent with a login token.
    assert [json.loads(r.content) for r in puts] == [
        {"marketIndex": 4, "leverage": 5}, {"marketIndex": 4, "leverage": 32_768},
        {"marketIndex": 0, "leverage": 1},
    ]
    assert all(list(json.loads(r.content)) == ["marketIndex", "leverage"] for r in puts)
    for request in puts:
        assert verify_login(_bearer(request))["l2AccountIndex"] == ACCOUNT_INDEX
    assert "authorization" not in perps_exchange.requests[2].headers  # the detail is public
    assert loaf.PERPS_DEFAULT_LEVERAGE == 3  # a market with none saved, at most its most


@pytest.mark.parametrize(
    "market, leverage, message",
    [
        (4, 0, "leverage must be"),
        (4, 32_769, "leverage must be"),
        (4, 2.5, "leverage must be"),
        (4, "3", "leverage must be"),
        (4, True, "leverage must be"),
        (4, None, "leverage must be"),
        (255, 3, "market must be a perps market index"),
        (-1, 3, "market must be a perps market index"),
        (4.0, 3, "market must be a perps market index"),
        (None, 3, "market must be a perps market index"),
        ("OPRA", 3, "OPRA is a spot market"),
        ("opra-perp", 3, "ticker must be a market's ticker"),
    ],
)
def test_a_leverage_is_checked_locally(exchange, trading_client, market, leverage, message):
    with pytest.raises(loaf.LoafValidationError, match=re.escape(message)) as info:
        trading_client().portfolio.set_perps_leverage(market, leverage)
    assert info.value.status_code == 0
    assert exchange.requests == []


def test_a_leverage_the_exchange_refuses_saves_nothing(perps_exchange, trading_client):
    with pytest.raises(loaf.LoafConfigError, match="needs your trading key"):
        trading_client(trading_key=None).portfolio.set_perps_leverage(4, 3)
    assert perps_exchange.requests == []
    refusal = "perps: leverage is malformed"
    perps_exchange.queue("/portfolio/perps-leverage", error_reply(400, refusal))
    portfolio = trading_client().portfolio
    with pytest.raises(loaf.LoafValidationError, match=re.escape(refusal)) as info:
        portfolio.set_perps_leverage(4, 10)
    assert info.value.status_code == 400
    with pytest.raises(loaf.LoafNotFoundError, match="Market not found"):
        portfolio.set_perps_leverage("MUS-PERP", 3)
    # A detail that serves no perp's index: the SDK never guesses one.
    perps_exchange.details["OPRA-PERP"] = market_detail("OPRA-PERP", market_index=2050)
    message = "GET /trade/OPRA-PERP answered an unusable market.marketIndex"
    with pytest.raises(loaf.LoafServerError, match=re.escape(message)):
        portfolio.set_perps_leverage("OPRA-PERP", 3)
    assert routes(perps_exchange.requests) == [
        "PUT /portfolio/perps-leverage", "GET /trade/MUS-PERP", "GET /trade/OPRA-PERP"
    ]
    assert perps_exchange.perps_leverage == {}


# --------------------------------------------------------------------------- #
# Allocates and exits
# --------------------------------------------------------------------------- #


def test_add_margin(exchange, trading_client, l2_clock):
    l2_clock.time_ns = lambda: NOW_MS * 1_000_000
    exchange.keys["withdrawalNonce"] = 3
    answer = trading_client().perps.add_margin(50, pool=2)
    assert answer == {"journalSeq": 1001, "duplicate": False}
    (body,) = exchange.bodies("/l2/allocations")
    assert list(body) == ALLOCATE_KEYS
    assert body["amount"] == "5000"  # 50 USDC in the collateral's ticks of 0.01 USDC
    assert {key: body[key] for key in ALLOCATE_KEYS[:7]} == {
        "l2ChainId": 4242,
        "l2AccountIndex": ACCOUNT_INDEX,
        "fromSide": loaf.PERPS_SPOT_SIDE,
        "toSide": 2,
        "amount": "5000",
        "l2Nonce": 3,
        "l2ExpiryMs": NOW_MS + DEFAULT_PERPS_OP_TTL_MS,
    }
    verify_op(body)
    assert routes(exchange.requests) == [
        "GET /portfolio/summary",
        "GET /l2/keys",
        "POST /l2/allocations",
    ]
    assert verify_login(_bearer(exchange.requests[0]))["l2AccountIndex"] == ACCOUNT_INDEX
    assert exchange.requests[1].url.params["apiKeyIndex"] == "0"
    # Signed with the trading key, sent with a login token it signs: the exchange checks the
    # account is yours.
    assert verify_login(_bearer(exchange.requests[2]))["l2AccountIndex"] == ACCOUNT_INDEX
    assert exchange.keys["withdrawalNonce"] == 4


def test_return_margin_and_a_move_between_pools(exchange, trading_client):
    perps = trading_client().perps
    perps.return_margin(7)
    perps.allocate(Decimal("11529215046068469.75"), from_side=0, to_side=7)  # 2^60 - 1 ticks
    first, second = exchange.bodies("/l2/allocations")
    assert [(b["fromSide"], b["toSide"], b["amount"], b["l2Nonce"]) for b in (first, second)] == [
        (0, 255, "700", 0),
        (0, 7, str(2**60 - 1), 1),
    ]
    for body in (first, second):
        verify_op(body)
    # The collateral is read once; the nonce before every op.
    assert routes(exchange.requests) == [
        "GET /portfolio/summary",
        "GET /l2/keys",
        "POST /l2/allocations",
        "GET /l2/keys",
        "POST /l2/allocations",
    ]


def test_exit(exchange, trading_client, l2_clock):
    l2_clock.time_ns = lambda: NOW_MS * 1_000_000
    perps = trading_client().perps
    answer = perps.exit(9, pool=2, ttl_ms=MIN_ORDER_TTL_MS)
    assert answer == {"journalSeq": 1001, "duplicate": False}
    perps.exit(254, ttl_ms=MAX_PERPS_OP_TTL_MS)
    first, second = exchange.bodies("/l2/exits")
    assert list(first) == EXIT_KEYS
    assert {key: first[key] for key in EXIT_KEYS[:6]} == {
        "l2ChainId": 4242,
        "l2AccountIndex": ACCOUNT_INDEX,
        "pool": 2,
        "market": 9,
        "l2Nonce": 0,
        "l2ExpiryMs": NOW_MS + MIN_ORDER_TTL_MS,
    }
    assert (second["pool"], second["market"], second["l2Nonce"]) == (0, 254, 1)
    assert second["l2ExpiryMs"] == NOW_MS + MAX_PERPS_OP_TTL_MS
    for body in (first, second):
        verify_op(body)


def test_an_op_signs_fresh_randomness(exchange, trading_client):
    # Two ops of the same terms at the same nonce sign the same message, never the same signature.
    perps = trading_client().perps
    exchange.queue("/l2/exits", error_reply(422, "x"))
    with pytest.raises(loaf.LoafAPIError):
        perps.exit(9, pool=2)
    perps.exit(9, pool=2)
    first, second = exchange.bodies("/l2/exits")
    assert first["l2Nonce"] == second["l2Nonce"] == 0
    assert first["l2SigS"] != second["l2SigS"]
    for body in (first, second):
        verify_op(body)


@pytest.mark.parametrize(
    "call, message",
    [
        (lambda p: p.allocate(5, from_side=2, to_side=2), "from_side and to_side must differ"),
        (lambda p: p.allocate(5, from_side=255, to_side=255), "must differ (both 255)"),
        (lambda p: p.allocate(5, from_side=8, to_side=0), "from_side must be a margin pool"),
        (lambda p: p.allocate(5, from_side=255, to_side=254), "to_side must be a margin pool"),
        (lambda p: p.allocate(5, from_side=True, to_side=0), "from_side must be a margin pool"),
        (lambda p: p.allocate(0, from_side=255, to_side=0), "amount must be positive"),
        (lambda p: p.add_margin(-1.5), "amount must be positive"),
        (lambda p: p.add_margin("5"), "amount must be a number"),
        (lambda p: p.add_margin(True), "amount must be a number"),
        (lambda p: p.add_margin(float("inf")), "amount must be a finite number"),
        (lambda p: p.add_margin(5, pool=8), "pool must be a margin pool"),
        (lambda p: p.return_margin(5, pool=True), "pool must be a margin pool"),
        (lambda p: p.return_margin(5, pool=255), "pool must be a margin pool"),
        (lambda p: p.exit(255), "market must be a perps market index"),
        (lambda p: p.exit(4, pool=-1), "pool must be a margin pool"),
        (lambda p: p.add_margin(5, ttl_ms=MIN_ORDER_TTL_MS - 1), "ttl_ms must be"),
        (lambda p: p.exit(4, ttl_ms=MAX_PERPS_OP_TTL_MS + 1), "ttl_ms must be"),
        (lambda p: p.exit(4, ttl_ms=60_000.0), "ttl_ms must be"),
    ],
)
def test_op_arguments_are_checked_locally(exchange, trading_client, call, message):
    with pytest.raises(loaf.LoafValidationError, match=re.escape(message)) as info:
        call(trading_client().perps)
    assert info.value.status_code == 0
    assert exchange.requests == []


def test_an_amount_is_whole_ticks_of_the_served_collateral(exchange, trading_client):
    perps = trading_client().perps
    for amount in (Decimal("12.34"), 0.01, 1.5, 7):
        perps.add_margin(amount)
    amounts = [body["amount"] for body in exchange.bodies("/l2/allocations")]
    assert amounts == ["1234", "1", "150", "700"]  # ticks of 0.01 USDC
    # Not a whole number of ticks, or past one allocate's range: refused once the collateral is
    # read, and nothing is signed or sent.
    for amount, message in (
        (
            Decimal("12.345"),
            "amount must be a whole number of ticks of 0.01 USDC (got Decimal('12.345'))",
        ),
        (0.001, "amount must be from 0.01 USDC (one tick) to 11,529,215,046,068,469.75 USDC"),
        (Decimal("11529215046068469.76"), "to 11,529,215,046,068,469.75 USDC (2^60 - 1 ticks)"),
    ):
        with pytest.raises(loaf.LoafValidationError, match=re.escape(message)) as info:
            perps.add_margin(amount)
        assert info.value.status_code == 0
    assert len(exchange.bodies("/l2/allocations")) == 4
    assert len(exchange.gets("/portfolio/summary")) == 1


def test_an_amount_is_counted_in_the_collateral_served_not_an_assumed_one(
    exchange, trading_client
):
    # 18 decimals and a tick of 10^12 base units (0.000001): what the reply says, not USDC's 6.
    exchange.perps_collateral = dict(PERPS_COLLATERAL, decimals=18, tickSize=str(10**12))
    trading_client().perps.add_margin(Decimal("0.000003"))
    (body,) = exchange.bodies("/l2/allocations")
    assert body["amount"] == "3"
    verify_op(body)


@pytest.mark.parametrize(
    "collateral",
    [
        None,
        "USDC",
        {k: v for k, v in PERPS_COLLATERAL.items() if k != "tickSize"},
        dict(PERPS_COLLATERAL, assetId=2),
        dict(PERPS_COLLATERAL, assetId=3.0),
        dict(PERPS_COLLATERAL, symbol=""),
        dict(PERPS_COLLATERAL, decimals=256),
        dict(PERPS_COLLATERAL, decimals="6"),
        dict(PERPS_COLLATERAL, decimals=True),
        dict(PERPS_COLLATERAL, tickSize="0"),
        dict(PERPS_COLLATERAL, tickSize=10000),
        dict(PERPS_COLLATERAL, tickSize="010000"),
        dict(PERPS_COLLATERAL, tickSize=str(2**128)),
        dict(PERPS_COLLATERAL, extensionMultiplier=str(2**56)),
        dict(PERPS_COLLATERAL, extensionMultiplier=None),
    ],
    ids=[
        "null", "not-an-object", "no-tickSize", "assetId-2", "assetId-float", "symbol-empty",
        "decimals-256", "decimals-text", "decimals-bool", "tickSize-0", "tickSize-number",
        "tickSize-leading-zero", "tickSize-2^128", "extension-2^56", "extension-missing",
    ],
)
def test_a_reply_without_a_usable_collateral_is_a_server_error(
    exchange, trading_client, collateral
):
    exchange.perps_collateral = collateral
    perps = trading_client().perps
    # Never a silent default: no amount is counted, and no allocate signed, without the served
    # one; each try reads the reply again.
    for call in (lambda: perps.add_margin(5), perps.collateral, lambda: perps.add_margin(5)):
        with pytest.raises(loaf.LoafServerError, match="answered no usable collateral"):
            call()
    assert exchange.posts("/l2/allocations") == []
    assert routes(exchange.requests) == ["GET /portfolio/summary"] * 3


def test_an_exit_needs_no_collateral(exchange, trading_client):
    # An exit moves no amount: it never reads the summary, so a perps read the exchange cannot
    # make never blocks it, as nothing unrelated may block a position's way out.
    exchange.perps_account = None
    perps = trading_client().perps
    assert perps.exit(4, pool=1) == {"journalSeq": 1001, "duplicate": False}
    (body,) = exchange.bodies("/l2/exits")
    assert (body["pool"], body["market"], body["l2ChainId"]) == (1, 4, 4242)
    verify_op(body)
    assert routes(exchange.requests) == ["GET /l2/keys", "POST /l2/exits"]


def test_ops_need_the_trading_key(exchange, trading_client):
    perps = trading_client(trading_key=None).perps
    with pytest.raises(loaf.LoafConfigError, match="needs your trading key"):
        perps.add_margin(5)
    with pytest.raises(loaf.LoafConfigError, match="needs your trading key"):
        perps.exit(4)
    assert exchange.requests == []


@pytest.mark.parametrize(
    "key_in_force, reason",
    [("0x" + KAT["keys"][1]["pk"], BAD_SIGNATURE), (None, UNKNOWN_KEY), ("0x" + "0" * 80, KEY_DISABLED)],
)
def test_an_op_is_never_signed_while_the_trading_key_is_not_in_force(
    exchange, trading_client, key_in_force, reason
):
    # Reset in the web app, never enabled, or disabled: the nonce read's login token is refused,
    # before anything is signed.
    exchange.key_in_force = key_in_force
    with pytest.raises(loaf.LoafAuthError, match=re.escape(reason)) as info:
        trading_client().perps.add_margin(5)
    assert "is not the trading key in force" in str(info.value)
    assert exchange.posts("/l2/allocations") == []


def test_an_op_is_never_signed_with_another_deployments_key(exchange, trading_client):
    # The trading key names its L2 chain: the nonce read's login token is refused for it.
    client = trading_client(trading_key=f"loaf_tk_4243_{ACCOUNT_INDEX}_{L2_SECRET}")
    with pytest.raises(loaf.LoafAuthError, match=re.escape(WRONG_CHAIN)):
        client.perps.exit(4)
    assert routes(exchange.requests) == ["GET /l2/keys"]


@pytest.mark.parametrize("index", [6, None, "5", True, -1])
def test_a_nonce_read_naming_another_account_is_a_server_error(exchange, trading_client, index):
    exchange.keys["accountIndex"] = index
    with pytest.raises(loaf.LoafServerError, match="/l2/keys answered an unusable accountIndex"):
        trading_client().perps.add_margin(5)
    assert exchange.posts("/l2/allocations") == []


@pytest.mark.parametrize("nonce", [None, -1, 2**48, "3", 3.0, True])
def test_an_unusable_nonce_is_a_server_error(exchange, trading_client, nonce):
    exchange.keys["withdrawalNonce"] = nonce
    with pytest.raises(loaf.LoafServerError, match="/l2/keys answered an unusable withdrawalNonce"):
        trading_client().perps.add_margin(5)
    assert exchange.posts("/l2/allocations") == []


def test_an_op_sent_elsewhere_first_is_refused_and_the_next_signs_the_new_nonce(
    exchange, trading_client, monkeypatch
):
    serve = exchange._serve

    def web_app_spends_the_nonce(request: httpx.Request) -> httpx.Response:
        reply = serve(request)
        if request.url.path == "/api/l2/keys" and exchange.keys["withdrawalNonce"] == 0:
            exchange.keys["withdrawalNonce"] = 1  # after this read: the SDK signs nonce 0
        return reply

    monkeypatch.setattr(exchange, "_serve", web_app_spends_the_nonce)
    perps = trading_client().perps
    with pytest.raises(loaf.LoafAPIError) as info:
        perps.add_margin(5)
    assert (info.value.status_code, info.value.message) == (422, NONCE_REFUSAL)
    assert not isinstance(info.value, PerpsOutcomeUnknownError)
    assert perps.add_margin(5)["duplicate"] is False
    assert [body["l2Nonce"] for body in exchange.bodies("/l2/allocations")] == [0, 1]


def test_concurrent_ops_sign_consecutive_nonces(exchange, trading_client):
    perps = trading_client().perps
    errors: list[BaseException] = []

    def move(amount: int) -> None:
        try:
            perps.add_margin(amount)
        except BaseException as exc:  # reported below: a thread's exception fails nothing itself
            errors.append(exc)

    threads = [threading.Thread(target=move, args=(amount,)) for amount in range(1, 6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    nonces = sorted(body["l2Nonce"] for body in exchange.bodies("/l2/allocations"))
    assert nonces == [0, 1, 2, 3, 4]
    assert len(exchange.gets("/portfolio/summary")) == 1


# --------------------------------------------------------------------------- #
# Retries and resubmit
# --------------------------------------------------------------------------- #


def test_a_lost_answer_is_resent_and_answered_as_its_duplicate(
    exchange, trading_client, monkeypatch
):
    serve = exchange._serve
    lost: list[httpx.Request] = []

    def lose_the_first_answer(request: httpx.Request) -> httpx.Response:
        reply = serve(request)  # applied ...
        if request.url.path == "/api/l2/allocations" and not lost:
            lost.append(request)
            raise httpx.ReadTimeout("timed out")  # ... but the answer never arrives
        return reply

    monkeypatch.setattr(exchange, "_serve", lose_the_first_answer)
    assert trading_client().perps.add_margin(5) == {"journalSeq": 1001, "duplicate": True}
    first, second = exchange.posts("/l2/allocations")
    assert first == second  # the same signed bytes
    assert exchange.keys["withdrawalNonce"] == 1  # applied once


@pytest.mark.parametrize("message", [PAUSED, NOT_TAKEN, ACCOUNT_UNREAD])
def test_a_503_that_applied_nothing_is_final(exchange, trading_client, message):
    perps = trading_client(max_retries=1).perps
    exchange.queue("/l2/allocations", error_reply(503, message), error_reply(503, message))
    with pytest.raises(loaf.LoafServiceUnavailableError) as info:
        perps.add_margin(5)
    assert info.value.message == message
    first, second = exchange.posts("/l2/allocations")
    assert first == second
    # Never applied, so its nonce is unspent: the next op signs it.
    perps.add_margin(5)
    assert [body["l2Nonce"] for body in exchange.bodies("/l2/allocations")] == [0, 0, 0]


def test_an_unconfirmed_op_is_an_unknown_outcome_that_resubmit_settles(exchange, trading_client):
    perps = trading_client(max_retries=1).perps
    exchange.queue(
        "/l2/allocations", error_reply(503, NOT_CONFIRMED), httpx.ReadTimeout("timed out")
    )
    with pytest.raises(PerpsOutcomeUnknownError) as info:
        perps.add_margin(5, pool=1)
    error = info.value
    assert error.attempts == 2
    assert type(error.last_error) is loaf.LoafConnectionError
    assert error.__cause__ is error.last_error
    sent = exchange.posts("/l2/allocations")
    assert len(sent) == 2 and sent[0] == sent[1]
    assert json.dumps(error.signed_body, separators=(",", ":")).encode() == sent[0]
    # Neither attempt was applied: a resubmit applies it now, and a second one is its duplicate.
    # A resubmit signs nothing new: a fresh client re-sends it with a login token alone.
    reads = len(exchange.gets("/l2/keys"))
    resend = trading_client().perps.resubmit
    assert resend(error.signed_body) == {"journalSeq": 1001, "duplicate": False}
    assert resend(error.signed_body) == {"journalSeq": 1001, "duplicate": True}
    assert exchange.posts("/l2/allocations")[2:] == sent
    assert len(exchange.gets("/l2/keys")) == reads


def test_a_refused_resubmit_is_still_unknown(exchange, trading_client):
    perps = trading_client(max_retries=0).perps
    exchange.queue("/l2/exits", httpx.ReadTimeout("timed out"))
    with pytest.raises(PerpsOutcomeUnknownError) as info:
        perps.exit(9, pool=2)
    assert info.value.attempts == 1
    body = info.value.signed_body
    exchange.queue("/l2/exits", error_reply(422, NONCE_REFUSAL))
    with pytest.raises(PerpsOutcomeUnknownError) as again:
        perps.resubmit(body)  # an exit goes back to /l2/exits
    assert again.value.signed_body == body
    assert again.value.last_error.status_code == 422
    assert len(exchange.posts("/l2/exits")) == 2


SIGNED_EXIT = {
    "l2ChainId": 4242,
    "l2AccountIndex": ACCOUNT_INDEX,
    "pool": 2,
    "market": 9,
    "l2Nonce": 0,
    "l2ExpiryMs": NOW_MS,
    "l2SigS": "ab" * 40,
    "l2SigE": "cd" * 40,
}


@pytest.mark.parametrize(
    "body",
    [
        None,
        "x",
        {},
        {k: v for k, v in SIGNED_EXIT.items() if k != "l2SigE"},
        {k: v for k, v in SIGNED_EXIT.items() if k != "market"},
        {**SIGNED_EXIT, "l2Nonce": "0"},
        {**SIGNED_EXIT, "l2ExpiryMs": True},
        {**SIGNED_EXIT, "l2SigS": None},
    ],
)
def test_resubmit_needs_a_signed_allocate_or_exit(exchange, trading_client, body: Any):
    with pytest.raises(
        loaf.LoafValidationError, match="resubmit needs a signed allocate or exit body"
    ) as info:
        trading_client().perps.resubmit(body)
    assert info.value.status_code == 0
    assert exchange.requests == []


# --------------------------------------------------------------------------- #
# Perps orders
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("pool, reduce_only", [(None, False), (0, True), (3, False), (7, True)])
def test_a_perps_order_signs_its_pool_and_reduce_only_bit(
    perps_exchange, trading_client, pool, reduce_only
):
    terms: dict[str, Any] = {} if pool is None else {"pool": pool}
    trading_client().orders.limit_sell("OPRA-PERP", 2, 101.5, reduce_only=reduce_only, **terms)
    (body,) = perps_exchange.bodies("/orders")
    assert list(body) == PERPS_ORDER_KEYS
    assert (body["l2MarketIndex"], body["l2Pool"], body["l2ReduceOnly"]) == (
        4,
        pool or 0,
        reduce_only,
    )
    assert body["l2QuoteAssetId"] == 3
    verify_order(body)
    # Its signed base-asset field is the pool byte: another pool, or the other bit, does not verify.
    for edit in ({"l2Pool": (body["l2Pool"] + 1) % 8}, {"l2ReduceOnly": not reduce_only}):
        with pytest.raises(k1.K1Error):
            verify_order({**body, **edit})


def test_a_perps_market_order(perps_exchange, trading_client):
    trading_client().orders.market_buy("OPRA-PERP", 3, pool=5)
    (body,) = perps_exchange.bodies("/orders")
    assert (body["type"], body["l2Pool"], body["l2ReduceOnly"]) == ("MARKET", 5, False)
    verify_order(body)


def test_a_perps_order_sends_its_time_in_force(perps_exchange, trading_client):
    # A perps order takes time_in_force on a spot order's path: the same body key beside its
    # signed pool byte, and the same local refusal of POST_ONLY on a MARKET order.
    orders = trading_client().orders
    orders.limit_sell("OPRA-PERP", 2, 101.5, pool=3, reduce_only=True, time_in_force="POST_ONLY")
    orders.limit_buy("OPRA-PERP", 2, 101.5, time_in_force="IOC", tp_price=110, sl_price=95)
    orders.create(
        "OPRA-PERP", "SELL", 2, type="MARKET", reference_price=101.5, time_in_force="FOK", pool=1
    )
    post_only, ioc, fok = perps_exchange.bodies("/orders")
    assert [body["timeInForce"] for body in (post_only, ioc, fok)] == ["POST_ONLY", "IOC", "FOK"]
    assert [(b["type"], b["l2Pool"], b["l2ReduceOnly"]) for b in (post_only, ioc, fok)] == [
        ("LIMIT", 3, True),
        ("LIMIT", 0, False),
        ("MARKET", 1, False),
    ]
    assert list(ioc) == PERPS_ORDER_KEYS + ["tp", "sl"]
    for body in (post_only, ioc, fok):
        verify_order(body)
    for leg in ("tp", "sl"):
        verify_order(ioc, leg)
    sent = len(perps_exchange.requests)
    with pytest.raises(loaf.LoafValidationError) as info:
        orders.create("OPRA-PERP", "BUY", 2, type="MARKET", time_in_force="POST_ONLY", pool=1)
    assert (info.value.status_code, info.value.message) == (
        0,
        "time_in_force: POST_ONLY requires a LIMIT order",
    )
    assert len(perps_exchange.requests) == sent


def test_a_perps_stop_signs_its_pool_and_reduce_only_bit(perps_exchange, trading_client):
    trading_client().orders.stop_loss("OPRA-PERP", 2, 95, pool=1, reduce_only=True)
    (body,) = perps_exchange.bodies("/orders/conditional")
    assert (body["l2Pool"], body["l2ReduceOnly"]) == (1, True)
    assert "l2BaseAssetId" not in body
    verify_order(body)


@pytest.mark.parametrize("terms", [{"pool": 0}, {"pool": 3}, {"reduce_only": True}])
def test_a_spot_order_takes_no_pool_or_reduce_only(exchange, trading_client, terms):
    message = (
        "OPRA is a spot market: pool= and reduce_only= apply to perps only (their tickers end in "
        "-PERP)"
    )
    with pytest.raises(loaf.LoafValidationError, match=re.escape(message)) as info:
        trading_client().orders.limit_buy("OPRA", 10, 167.49, **terms)
    assert info.value.status_code == 0
    assert routes(exchange.requests) == ["GET /trade/OPRA"]


@pytest.mark.parametrize(
    "terms, message",
    [
        ({"pool": 8}, "pool must be a margin pool"),
        ({"pool": -1}, "pool must be a margin pool"),
        ({"pool": True}, "pool must be a margin pool"),
        ({"pool": 1.0}, "pool must be a margin pool"),
        ({"reduce_only": 1}, "reduce_only must be True or False"),
        ({"reduce_only": None}, "reduce_only must be True or False"),
    ],
)
def test_perps_terms_are_checked_before_anything_is_read(
    perps_exchange, trading_client, terms, message
):
    orders = trading_client().orders
    for place in (
        lambda: orders.limit_buy("OPRA-PERP", 2, 101.5, **terms),
        lambda: orders.take_profit("OPRA-PERP", 2, 110, **terms),
    ):
        with pytest.raises(loaf.LoafValidationError, match=message) as info:
            place()
        assert info.value.status_code == 0
    assert perps_exchange.requests == []


def test_an_expired_perp_takes_no_orders(exchange, trading_client):
    exchange.details["OPRA-PERP"] = market_detail(
        "OPRA-PERP", market_index=4, l2={**PERP_L2, "status": "EXPIRED"}
    )
    message = (
        "OPRA-PERP (perps market 4) has expired and takes no orders: close a position in it with "
        "perps.exit()"
    )
    with pytest.raises(loaf.LoafValidationError, match=re.escape(message)) as info:
        trading_client().orders.limit_sell("OPRA-PERP", 2, 101.5, reduce_only=True)
    assert info.value.status_code == 0
    assert routes(exchange.requests) == ["GET /trade/OPRA-PERP"]


def check_legs_as_shared_types(body: dict) -> None:
    """A perps body's legs against Shared-Types 0.30.0: signedLegSchema (each leg strict, its
    six fields in range) and orderBodySchema's leg rules (the take-profit beyond the stop-loss
    and, on a LIMIT parent, the parent's price between them, mirrored on a SELL; every order
    number its own; never on a reduce-only parent)."""
    legs = [body[leg] for leg in ("tp", "sl") if leg in body]
    for leg in legs:
        assert list(leg) == LEG_KEYS
        assert leg["triggerPrice"] > 0 and leg["price"] > 0
        assert 1 <= leg["l2OrderIndex"] <= MAX_ORDER_INDEX
        assert 0 <= leg["l2ExpiryMs"] <= MAX_48
        assert all(re.fullmatch("[0-9a-f]{80}", leg[sig]) for sig in ("l2SigS", "l2SigE"))
    short = body["side"] == "SELL"

    def beyond(a: float, b: float) -> bool:
        return a < b if short else a > b

    tp, sl = body.get("tp"), body.get("sl")
    if tp and sl:
        assert beyond(tp["triggerPrice"], sl["triggerPrice"])
    if body["type"] == "LIMIT":
        assert not sl or beyond(body["price"], sl["triggerPrice"])
        assert not tp or beyond(tp["triggerPrice"], body["price"])
    numbers = [body["l2OrderIndex"]] + [leg["l2OrderIndex"] for leg in legs]
    assert len(set(numbers)) == len(numbers)
    assert body["l2ReduceOnly"] is False


@pytest.mark.parametrize("side, pool", [("BUY", 0), ("SELL", 3)])
def test_a_perps_orders_legs_are_reduce_only_on_its_pool_and_side_opposite(
    perps_exchange, trading_client, side, pool
):
    orders = trading_client().orders
    if side == "BUY":
        res = orders.limit_buy("OPRA-PERP", 2, 101.5, pool=pool, tp_price=110, sl_price=95)
    else:
        res = orders.limit_sell("OPRA-PERP", 2, 101.5, pool=pool, tp_price=95, sl_price=110)
    (body,) = perps_exchange.bodies("/orders")
    assert list(body) == PERPS_ORDER_KEYS + ["tp", "sl"]
    assert (body["l2Pool"], body["l2ReduceOnly"]) == (pool, False)
    check_legs_as_shared_types(body)
    # Each leg trades the other way, 2% past its trigger (the client's max slippage): a BUY's
    # legs sell below theirs, a SELL's buy above.
    prices = {"BUY": (107.8, 93.1), "SELL": (96.9, 112.2)}[side]
    assert (body["tp"]["price"], body["sl"]["price"]) == prices
    assert body["tp"]["l2ExpiryMs"] == body["sl"]["l2ExpiryMs"] == body["l2ExpiryMs"]
    assert [body["tp"]["l2OrderIndex"], body["sl"]["l2OrderIndex"]] == [
        body["l2OrderIndex"] + 1,
        body["l2OrderIndex"] + 2,
    ]
    verify_order(body)
    other = "SELL" if side == "BUY" else "BUY"
    for leg in ("tp", "sl"):
        verify_order(body, leg)
        # A leg is the reduce-only order of the other side in the parent's pool, its own price,
        # number and expiry: signed as that, it verifies; without the bit, or on the parent's
        # side, it does not.
        own = {k: body[leg][k] for k in ("price", "l2OrderIndex", "l2ExpiryMs", "l2SigS", "l2SigE")}
        verify_order({**body, **own, "side": other, "l2ReduceOnly": True})
        for edit in ({"side": other, "l2ReduceOnly": False}, {"side": side, "l2ReduceOnly": True}):
            with pytest.raises(k1.K1Error):
                verify_order({**body, **own, **edit})
    assert res.attachedLegIds == [res.orderId + 1, res.orderId + 2]


def test_a_perps_market_sell_with_legs(perps_exchange, trading_client):
    trading_client().orders.market_sell(
        "OPRA-PERP", 2, pool=1, tp_price=90, sl_price=115, leg_max_slippage_bps=500
    )
    (body,) = perps_exchange.bodies("/orders")
    check_legs_as_shared_types(body)
    assert (body["tp"]["price"], body["sl"]["price"]) == (94.5, 120.75)  # 5% above, buying
    for leg in ("tp", "sl"):
        verify_order(body, leg)


@pytest.mark.parametrize(
    "legs, message",
    [
        ({"tp_price": 110, "sl_price": 95}, "tp_price must be below sl_price"),
        ({"tp_price": 100, "sl_price": 100}, "tp_price must be below sl_price"),
        ({"sl_price": 101.5}, "sl_price must be above the order price"),
        ({"sl_price": 95}, "sl_price must be above the order price"),
        ({"tp_price": 101.5}, "tp_price must be below the order price"),
        ({"tp_price": 110}, "tp_price must be below the order price"),
    ],
)
def test_a_sells_leg_rules_are_mirrored(perps_exchange, trading_client, legs, message):
    with pytest.raises(loaf.LoafValidationError, match=message) as info:
        trading_client().orders.limit_sell("OPRA-PERP", 2, 101.5, **legs)
    assert info.value.status_code == 0
    assert perps_exchange.requests == []


def test_a_reduce_only_order_takes_no_legs(perps_exchange, trading_client):
    orders = trading_client().orders
    message = (
        "Take-profit and stop-loss cannot be attached to a reduce-only order: it opens nothing "
        "for them to close"
    )
    for place in (
        lambda: orders.limit_sell("OPRA-PERP", 2, 101.5, reduce_only=True, tp_price=95),
        lambda: orders.limit_buy("OPRA-PERP", 2, 101.5, pool=2, reduce_only=True, sl_price=95),
    ):
        with pytest.raises(loaf.LoafValidationError, match=re.escape(message)) as info:
            place()
        assert info.value.status_code == 0
    assert perps_exchange.requests == []


@pytest.mark.parametrize("message, cls, state", MARKET_STATES, ids=lambda x: str(x)[:24])
def test_a_market_state_refusal_of_an_order(perps_exchange, trading_client, message, cls, state):
    perps_exchange.queue("/orders", error_reply(403, message))
    orders = trading_client().orders
    with pytest.raises(cls) as info:
        orders.limit_buy("OPRA-PERP", 2, 101.5)
    assert type(info.value) is cls and info.value.state == state
    # Nothing was placed: the next order signs the same order number.
    orders.limit_buy("OPRA-PERP", 2, 101.5, reduce_only=True)
    first, second = perps_exchange.bodies("/orders")
    assert first["l2OrderIndex"] == second["l2OrderIndex"]


# --------------------------------------------------------------------------- #
# An op's login token
# --------------------------------------------------------------------------- #


def _op_posts(exchange: Any) -> list[httpx.Request]:
    return [r for r in exchange.requests if r.method == "POST" and r.url.path == "/api/l2/allocations"]


def test_a_refused_op_login_token_is_forgotten_and_explained(exchange, trading_client):
    # The op's own 401 (the backend's refusal of its signature or key), past the nonce read.
    perps = trading_client().perps
    exchange.queue("/l2/allocations", error_reply(401, BAD_SIGNATURE))
    with pytest.raises(loaf.LoafAuthError, match=re.escape(BAD_SIGNATURE)) as info:
        perps.add_margin(5)
    assert "is not the trading key in force" in str(info.value)
    (refused,) = _op_posts(exchange)  # a 401 is never re-sent
    perps.add_margin(5)
    assert _bearer(_op_posts(exchange)[1]) != _bearer(refused)  # signed afresh


def test_an_ops_login_token_is_in_no_traceback_local(exchange, trading_client):
    perps = trading_client().perps
    exchange.queue("/l2/allocations", error_reply(401, BAD_SIGNATURE))
    with pytest.raises(loaf.LoafAuthError) as refused:
        perps.add_margin(5)
    exchange.queue("/l2/allocations", *(httpx.ReadTimeout("slow") for _ in range(4)))
    with pytest.raises(loaf.PerpsOutcomeUnknownError) as unknown:
        perps.add_margin(5)
    sent = [_bearer(r) for r in _op_posts(exchange)]
    for error in (refused.value, unknown.value):
        assert secret_locals(error, *sent) == []


def test_each_attempt_of_an_op_carries_its_own_login_token(
    exchange, trading_client, monkeypatch, l2_clock
):
    # A backoff can outlast the token's half-life: the re-send carries a fresh one.
    now = freeze(l2_clock)
    perps = trading_client().perps
    monkeypatch.setattr(
        "loaf.client.time", SimpleNamespace(sleep=lambda seconds: freeze(l2_clock, now + 300_000))
    )
    exchange.queue("/l2/allocations", httpx.ConnectError("unreachable"))
    perps.add_margin(5)
    first, second = (verify_login(_bearer(r))["l2ExpiryMs"] for r in _op_posts(exchange))
    assert (first, second) == (now + 600_000, now + 300_000 + 600_000)

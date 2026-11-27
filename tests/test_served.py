"""The exchange's served decimals as the SDK reads them: every perps and market-terms price, size
and dollar amount, and every fill's, is the exact Decimal of its served string on each read that
carries one, and one in any other form is refused naming it, never read in another unit (the
WebSocket frames: test_ws.py)."""

from __future__ import annotations

import copy
from decimal import Decimal, localcontext
from typing import Any, Callable

import httpx
import pytest

import loaf
from loaf import LoafClient
from loaf._served import Malformed, decimal

from conftest import (
    BASE,
    PERP_L2,
    PERPS_ACCOUNT,
    PERPS_ACCOUNT_READ,
    PERPS_LIVE,
    PERPS_LIVE_READ,
    TRADING_KEY,
    as_decimals,
    market_detail,
    verify_login,
)

FILL_DECIMALS = ("quantity", "price", "fee", "closedPnl")
FUNDING_DECIMALS = ("base", "payment")


def client_of(body: Any) -> tuple[LoafClient, list[httpx.Request]]:
    """A client whose every request is answered ``body`` (200), and the requests it sends."""
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        if "authorization" in request.headers:
            verify_login(request.headers["authorization"][len("Bearer ") :])
        return httpx.Response(200, json=body)

    http = httpx.Client(transport=httpx.MockTransport(handler), base_url=BASE)
    return LoafClient(TRADING_KEY, base_url=BASE, http_client=http, max_retries=0), sent


@pytest.mark.parametrize(
    "text",
    ["0", "7", "-7", "0.5", "-0.000001", "18446744073709551.615", "281474976.710655",
     "123456789012345678901234567890123456789.123456"],
)
def test_a_served_decimal_is_the_decimal_of_exactly_its_digits(text):
    # Exact at any width and whatever the caller's decimal context: no float, no rounding.
    with localcontext() as context:
        context.prec = 2
        value = decimal(text, "price")
    assert type(value) is Decimal and str(value) == text and value == Decimal(text)


@pytest.mark.parametrize(
    "value",
    [2_510_000, 2.51, True, None, [], "", "-", "-0", "+1", "01", "00", "-01.5", "1.", ".5", "1.50",
     "1.0", "0.0", "-0.0", "1e3", "1E3", "2.51e0", " 1", "1 ", "1\n", "1_000", "1,000", "0x1f",
     "\u0661", "NaN", "Infinity", "1.0000001", "-0.0000001"],
)
def test_anything_else_is_malformed_never_read(value):
    # A number is refused too: the old reads served numbers in other units (2510000 was $2.51).
    with pytest.raises(Malformed) as info:
        decimal(value, "market.live.markPrice")
    assert str(info.value) == (
        f"a malformed market.live.markPrice: {value!r} is not an exact decimal (digits, a '-' "
        "before a negative, no exponent, no leading or trailing zero, '0' for zero, at most 6 "
        "decimal places)"
    )


@pytest.mark.parametrize(
    "value",
    ["1" * 500_000 + ".0", ["x" * 1000] * 10_000, {f"k{i}": "v" * 1000 for i in range(10_000)},
     [[[[[["deep"]]]]]], "1." + "0" * 100 + "1"],
    ids=["long text", "long list", "long object", "nested", "long fraction"],
)
def test_a_malformed_values_message_shows_a_bounded_part_of_it(value):
    # A served value of 500 KB is not a 500 KB log line.
    with pytest.raises(Malformed) as info:
        decimal(value, "market.live.markPrice")
    assert len(str(info.value)) < 400
    assert str(info.value).startswith("a malformed market.live.markPrice: ")


def perp_detail() -> dict[str, Any]:
    """OPRA-PERP's detail: its live state, its terms with order limits, and a marketList whose
    perp row has its live figures and whose spot row has none."""
    l2 = {**PERP_L2, "minBase": "0.001", "minQuote": "10", "quoteLimit": "281474976.710655"}
    detail = market_detail("OPRA-PERP", market_index=4, l2=l2, market_price=101.5)
    detail["market"]["live"] = PERPS_LIVE
    detail["marketList"][0]["live"] = None  # MUS, a spot market
    detail["marketList"][1]["live"] = {"markPrice": "101.5049", "openInterest": "0",
                                       "fundingRateCbp": 12, "fundingPeriodMs": 3_600_000}
    return detail


def test_a_perps_detail_serves_its_live_state_terms_and_selector_figures_as_decimals():
    detail = perp_detail()
    client, _ = client_of(detail)
    got = client.market.get("OPRA-PERP")
    assert got.market.live == PERPS_LIVE_READ
    l2 = got.market.l2
    assert (l2.minBase, l2.minQuote, l2.quoteLimit) == (
        Decimal("0.001"), Decimal(10), Decimal("281474976.710655")
    )
    assert {k: v for k, v in l2.items() if k not in ("minBase", "minQuote", "quoteLimit")} == {
        k: v for k, v in detail["market"]["l2"].items() if k not in ("minBase", "minQuote", "quoteLimit")
    }
    mus, perp = got.marketList
    assert mus == detail["marketList"][0] and mus.live is None
    assert perp.live == {"markPrice": Decimal("101.5049"), "openInterest": Decimal(0),
                         "fundingRateCbp": 12, "fundingPeriodMs": 3_600_000}
    assert perp.marketPrice == 101.5  # the selector's reference stays a number, as served


def test_a_spot_detail_has_no_perps_value_to_read():
    detail = market_detail("OPRA")
    client, _ = client_of(detail)
    assert client.market.get("OPRA") == detail


def test_the_summary_serves_its_perps_withdrawable_and_withdraw_fee_as_decimals():
    balances = [{"assetId": 3, "symbol": "USDC", "decimals": 6, "isQuote": True, "total": "5000000000",
                 "hold": "0", "available": "5000000000"}]
    body = {"balances": balances, "holdings": [], "perps": PERPS_ACCOUNT, "accountMode": "UNIFIED",
            "withdrawable": "4990.000001", "withdrawFee": "0.2"}
    client, _ = client_of(body)
    summary = client.portfolio.summary()
    assert summary == {**body, "perps": PERPS_ACCOUNT_READ, "withdrawable": Decimal("4990.000001"),
                       "withdrawFee": Decimal("0.2")}
    assert summary.withdrawable - summary.withdrawFee == Decimal("4989.800001")  # the most to withdraw
    assert summary.balances == balances  # base units, decimal strings, as served
    assert client.perps.positions() == PERPS_ACCOUNT_READ
    # Unreadable now (or no USDC row, for the fee): None, not 0.
    client, _ = client_of({**body, "perps": None, "accountMode": None, "withdrawable": None,
                           "withdrawFee": None})
    assert client.portfolio.summary().withdrawable is None
    assert client.portfolio.summary().withdrawFee is None


def test_the_component_serves_its_fills_and_funding_payments_as_decimals():
    spot = {"tradeId": 12, "orderId": 991, "marketIndex": 2050, "ticker": "OPRA", "txHash": "0x" + "3f" * 32,
            "side": "BUY", "role": "TAKER", "pool": None, "quantity": "1.5", "price": "160.25",
            "fee": "0.168262", "closedPnl": None, "source": "TRADE", "direction": "BUY", "executedAt": 0}
    close = {**spot, "tradeId": 13, "orderId": None, "ticker": "OPRA-PERP", "txHash": None, "pool": 1,
             "price": "98.7654", "fee": "0", "closedPnl": "-4.123456", "source": "LIQUIDATION",
             "direction": "CLOSE_LONG"}
    funding = {"journalSeq": 7, "marketIndex": 4, "ticker": "OPRA-PERP", "pool": 0, "base": "-2.004",
               "payment": "0.000001", "rateCbp": -250, "atMs": 1_800_000_000_000}
    order = {"id": 991, "marketIndex": 2050, "quantity": 1.5, "price": 160.25, "filledQuantity": 0}
    fees = {"spot": {"takerFeeBps": 70, "makerFeeBps": 40}, "perps": {"takerFeeBps": 5, "makerFeeBps": 2}}
    body = {"lifetimeVolume": 12.5, "applicableFees": fees, "openOrders": [order], "tradeHistory": [spot, close],
            "fundingHistory": [funding], "orderHistory": [order], "transfers": None}
    client, _ = client_of(body)
    got = client.portfolio.component()
    assert got == {
        **body,
        "tradeHistory": [as_decimals(spot, FILL_DECIMALS), as_decimals(close, FILL_DECIMALS)],
        "fundingHistory": [as_decimals(funding, FUNDING_DECIMALS)],
    }
    assert got.openOrders == [order] and got.lifetimeVolume == 12.5  # numbers, as served
    # Your fee rates are whole bps, the ints served (a Decimal 5 == 5 too).
    assert {type(rate) for rates in got.applicableFees.values() for rate in rates.values()} == {int}
    assert got.applicableFees.perps.takerFeeBps == 5


def test_the_history_pages_and_their_iterators_serve_their_rows_as_decimals():
    fill = {"tradeId": 13, "quantity": "0.001", "price": "75012", "fee": "0.026254", "closedPnl": "1.5"}
    payment = {"journalSeq": 7, "base": "0.5", "payment": "-0.12"}
    client, sent = client_of({"trades": [fill], "funding": [payment], "nextCursor": None})
    assert client.history.trades()["trades"] == [as_decimals(fill, FILL_DECIMALS)]
    assert client.history.funding()["funding"] == [as_decimals(payment, FUNDING_DECIMALS)]
    assert list(client.history.iter_trades()) == [as_decimals(fill, FILL_DECIMALS)]
    assert list(client.history.iter_funding()) == [as_decimals(payment, FUNDING_DECIMALS)]
    assert [r.url.path for r in sent] == ["/api/history/trades", "/api/history/funding"] * 2


def _with(body: Any, path: tuple[Any, ...], value: Any) -> Any:
    """A copy of ``body`` with the field at ``path`` set to ``value``."""
    body = copy.deepcopy(body)
    *parents, last = path
    target = body
    for key in parents:
        target = target[key]
    target[last] = value
    return body


SUMMARY = {"balances": [], "holdings": [], "perps": PERPS_ACCOUNT, "accountMode": "MANUAL",
           "withdrawable": "0"}
FILLS = {"trades": [{"tradeId": 1, "quantity": "1", "price": "100", "fee": "0", "closedPnl": None}],
         "nextCursor": None}
PAYMENTS = {"funding": [{"journalSeq": 1, "base": "1", "payment": "0"}], "nextCursor": None}
COMPONENT = {"tradeHistory": FILLS["trades"], "fundingHistory": PAYMENTS["funding"]}
READS: dict[str, Callable[[LoafClient], Any]] = {
    "GET /trade/OPRA-PERP": lambda c: c.market.get("OPRA-PERP"),
    "GET /portfolio/summary": lambda c: c.portfolio.summary(),
    "GET /portfolio/summary (perps)": lambda c: c.perps.position(4),
    "GET /portfolio/component": lambda c: c.portfolio.component(),
    "GET /history/trades": lambda c: c.history.trades(),
    "GET /history/trades (pages)": lambda c: list(c.history.iter_trades()),
    "GET /history/funding": lambda c: c.history.funding(),
}


@pytest.mark.parametrize(
    "read, body, path, field, value",
    [
        ("GET /trade/OPRA-PERP", perp_detail(), ("market", "live", "markPrice"), "market.live.markPrice",
         2_510_000),
        ("GET /trade/OPRA-PERP", perp_detail(), ("market", "live", "heldPrice", "price"),
         "market.live.heldPrice.price", "2.6E0"),
        ("GET /trade/OPRA-PERP", perp_detail(), ("market", "l2", "minQuote"), "market.l2.minQuote", 10),
        ("GET /trade/OPRA-PERP", perp_detail(), ("marketList", 1, "live", "openInterest"),
         "marketList[1].live.openInterest", "1.50"),
        ("GET /portfolio/summary", SUMMARY, ("withdrawable",), "withdrawable", 12.5),
        ("GET /portfolio/summary", SUMMARY, ("withdrawFee",), "withdrawFee", "0.20"),
        ("GET /portfolio/summary", SUMMARY, ("perps", "pools", 1, "collateral"), "perps.pools[1].collateral",
         None),
        ("GET /portfolio/summary (perps)", SUMMARY, ("perps", "pools", 0, "positions", 0, "entryPrice"),
         "perps.pools[0].positions[0].entryPrice", "101.50"),
        ("GET /portfolio/component", COMPONENT, ("tradeHistory", 0, "price"), "tradeHistory[0].price", 100),
        ("GET /portfolio/component", COMPONENT, ("fundingHistory", 0, "payment"),
         "fundingHistory[0].payment", "-0"),
        ("GET /history/trades", FILLS, ("trades", 0, "fee"), "trades[0].fee", "0.0000001"),
        ("GET /history/trades (pages)", FILLS, ("trades", 0, "quantity"), "trades[0].quantity", "1e0"),
        ("GET /history/funding", PAYMENTS, ("funding", 0, "base"), "funding[0].base", "+1"),
    ],
)
def test_a_malformed_value_is_a_server_error_naming_it(read, body, path, field, value):
    answer = _with(body, path, value)
    client, _ = client_of(answer)
    with pytest.raises(loaf.LoafServerError) as info:
        READS[read](client)
    what = read.split(" (")[0]
    assert info.value.status_code == 200 and info.value.message == (
        f"{what} answered {Malformed(field, value)}"
    )
    assert type(info.value) is loaf.LoafServerError

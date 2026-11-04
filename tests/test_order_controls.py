"""The market-maker controls against an in-memory exchange: an order's unsigned
controls (``order_tag``, ``send_by_ms``, ``replaces_order_id``), resubmit's
``send_by_ms`` and its send-by guard, the scoped cancels (``cancel_all``,
``cancel_after``), ``cancel_by_index`` and the order numbers it settles, and the
refusals the SDK reads by their code alone.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

import loaf
from loaf import OrderOutcomeUnknownError
from loaf._l2 import L2State
from loaf.constants import CANCEL_AFTER_MAX_MS, CANCEL_AFTER_MIN_MS, DEFAULT_ORDER_TTL_MS, ORDER_TAG_MAX

from conftest import (
    ACCOUNT_INDEX,
    MARKET_INDEX,
    FakeExchange,
    freeze,
    market_detail,
    routes,
    verify_login,
    verify_order,
)

# The signed body's keys, in the SDK's order; the controls follow them, unsigned.
ORDER_KEYS = (
    "price quantity side type timeInForce l2ChainId l2AccountIndex l2MarketIndex l2OrderIndex "
    "l2Epoch l2FeeRateCap l2ExpiryMs l2BaseAssetId l2QuoteAssetId l2SigS l2SigE"
).split()
CONTROL_KEYS = ["orderTag", "sendByMs", "replacesOrderId"]
NOW_MS = 1_800_000_000_000
SEND_BY_PASSED = "The order's send-by time has passed"
REPLACE_TARGET_GONE = "The order to replace no longer rests on this market"


def error_reply(status: int, message: str, code: str | None = None, **extra: Any) -> httpx.Response:
    body = {"error": message} if code is None else {"error": message, "code": code}
    return httpx.Response(status, json={**body, **extra})


def ok(body: dict) -> httpx.Response:
    return httpx.Response(200, json=body)


def bearer(request: httpx.Request) -> str:
    return request.headers["authorization"][len("Bearer ") :]


def indices(exchange: FakeExchange) -> list[int]:
    return [body["l2OrderIndex"] for body in exchange.bodies("/orders")]


def counts(exchange: FakeExchange) -> list[int]:
    return [body["count"] for body in exchange.bodies("/l2/order-indices")]


def unknown_order(exchange: FakeExchange, trading_client: Any, **controls: Any) -> dict:
    """The signed body of an order whose one attempt timed out."""
    exchange.queue("/orders", httpx.ReadTimeout("timed out"))
    with pytest.raises(OrderOutcomeUnknownError) as info:
        trading_client(max_retries=0).orders.limit_buy("OPRA", 10, 167.49, **controls)
    return info.value.signed_body


# --------------------------------------------------------------------------- #
# An order's controls
# --------------------------------------------------------------------------- #


def test_the_controls_are_sent_beside_the_signature_and_never_signed(exchange, trading_client):
    orders = trading_client().orders
    orders.limit_buy(
        "OPRA", 10, 167.49, order_tag=ORDER_TAG_MAX, send_by_ms=NOW_MS, replaces_order_id=42
    )
    (body,) = exchange.bodies("/orders")
    assert list(body) == ORDER_KEYS + CONTROL_KEYS
    assert {key: body[key] for key in CONTROL_KEYS} == {
        "orderTag": 4_294_967_295,
        "sendByMs": NOW_MS,
        "replacesOrderId": 42,
    }
    verify_order(body)  # the signature covers the order's terms alone


def test_only_the_controls_given_are_sent(exchange, trading_client):
    orders = trading_client().orders
    orders.limit_buy("OPRA", 10, 167.49)
    orders.limit_buy("OPRA", 10, 167.49, order_tag=1)
    orders.limit_buy("OPRA", 10, 167.49, send_by_ms=1)
    orders.limit_buy("OPRA", 10, 100, tp_price=120, replaces_order_id=7)
    none, tag, send_by, replace = exchange.bodies("/orders")
    assert list(none) == ORDER_KEYS
    assert list(tag) == ORDER_KEYS + ["orderTag"]
    assert list(send_by) == ORDER_KEYS + ["sendByMs"]
    # A leg follows the controls: they are the parent order's, never a leg's.
    assert list(replace) == ORDER_KEYS + ["replacesOrderId", "tp"]
    assert "replacesOrderId" not in replace["tp"]


@pytest.mark.parametrize(
    "place",
    [
        lambda o, **c: o.limit_buy("OPRA", 10, 167.49, **c),
        lambda o, **c: o.limit_sell("OPRA", 10, 167.49, **c),
        lambda o, **c: o.market_buy("OPRA", 10, **c),
        lambda o, **c: o.market_sell("OPRA", 10, **c),
        lambda o, **c: o.create("OPRA", "BUY", 10, price=167.49, **c),
    ],
    ids=["limit_buy", "limit_sell", "market_buy", "market_sell", "create"],
)
def test_every_placement_forwards_the_controls(exchange, trading_client, place):
    place(trading_client().orders, order_tag=9, send_by_ms=NOW_MS, replaces_order_id=3)
    (body,) = exchange.bodies("/orders")
    assert [body[key] for key in CONTROL_KEYS] == [9, NOW_MS, 3]
    verify_order(body)


@pytest.mark.parametrize(
    "controls, field",
    [
        ({"order_tag": 0}, "order_tag"),
        ({"order_tag": ORDER_TAG_MAX + 1}, "order_tag"),
        ({"order_tag": True}, "order_tag"),
        ({"order_tag": 7.0}, "order_tag"),
        ({"order_tag": "7"}, "order_tag"),
        ({"send_by_ms": 0}, "send_by_ms"),
        ({"send_by_ms": 2**48}, "send_by_ms"),
        ({"send_by_ms": float(NOW_MS)}, "send_by_ms"),
        ({"send_by_ms": False}, "send_by_ms"),
        ({"replaces_order_id": 0}, "replaces_order_id"),
        ({"replaces_order_id": -3}, "replaces_order_id"),
        ({"replaces_order_id": 2**53}, "replaces_order_id"),
        ({"replaces_order_id": True}, "replaces_order_id"),
        ({"replaces_order_id": "42"}, "replaces_order_id"),
    ],
)
def test_a_control_is_checked_before_anything_is_read(exchange, trading_client, controls, field):
    # Never rounded or read from text: 7.0 or True would name another tag or order.
    with pytest.raises(loaf.LoafValidationError) as info:
        trading_client().orders.limit_buy("OPRA", 10, 167.49, **controls)
    assert info.value.status_code == 0
    assert info.value.message.startswith(f"{field} must be ")
    assert info.value.message.endswith(f"(got {next(iter(controls.values()))!r})")
    assert exchange.requests == []


def test_a_signed_body_passed_as_a_control_stays_out_of_the_refusal(exchange, trading_client):
    signed = {"l2OrderIndex": 4097, "l2SigS": "ab" * 40, "l2SigE": "cd" * 40}
    with pytest.raises(loaf.LoafValidationError) as info:
        trading_client().orders.limit_buy("OPRA", 10, 167.49, replaces_order_id=signed)
    assert str(info.value).endswith("(got dict)")
    assert exchange.requests == []


def test_the_largest_controls_are_taken(exchange, trading_client):
    trading_client().orders.limit_buy(
        "OPRA", 10, 167.49, order_tag=1, send_by_ms=2**48 - 1, replaces_order_id=2**53 - 1
    )
    (body,) = exchange.bodies("/orders")
    assert [body[key] for key in CONTROL_KEYS] == [1, 2**48 - 1, 2**53 - 1]


@pytest.mark.parametrize(
    "reply, cls",
    [
        (lambda: error_reply(400, SEND_BY_PASSED, "SEND_BY_PASSED"), loaf.SendByPassedError),
        (
            lambda: error_reply(409, REPLACE_TARGET_GONE, "REPLACE_TARGET_GONE"),
            loaf.ReplaceTargetGoneError,
        ),
    ],
    ids=["send-by-passed", "replace-target-gone"],
)
def test_a_send_by_or_replace_refusal_gives_its_numbers_back(exchange, trading_client, reply, cls):
    # Nothing was placed and the order number was not spent: the next order signs it.
    orders = trading_client().orders
    exchange.queue("/orders", reply())
    with pytest.raises(cls) as info:
        orders.limit_buy("OPRA", 10, 167.49, send_by_ms=NOW_MS, replaces_order_id=5)
    assert type(info.value) is cls and info.value.replaced_order_cancelled is False
    assert len(exchange.posts("/orders")) == 1  # a 4xx is never re-sent
    orders.limit_buy("OPRA", 10, 167.49)
    assert indices(exchange) == [4097, 4097]
    assert counts(exchange) == [1]


def test_a_replace_whose_new_order_is_refused_says_the_old_one_is_cancelled(
    exchange, trading_client
):
    exchange.queue(
        "/orders",
        error_reply(
            400, "Insufficient balance", "INSUFFICIENT_BALANCE", replacedOrderCancelled=True
        ),
    )
    with pytest.raises(loaf.LoafValidationError) as info:
        trading_client().orders.limit_buy("OPRA", 10, 167.49, replaces_order_id=5)
    assert (info.value.code, info.value.replaced_order_cancelled) == ("INSUFFICIENT_BALANCE", True)


def test_a_send_by_refusal_after_a_lost_answer_leaves_the_outcome_unknown(
    exchange, trading_client
):
    # The first attempt may have landed: the backend refuses the re-send for its send-by time
    # before it looks for that one, so the refusal says nothing about it, and its numbers are kept.
    orders = trading_client().orders
    exchange.queue(
        "/orders",
        httpx.ReadTimeout("timed out"),
        error_reply(400, SEND_BY_PASSED, "SEND_BY_PASSED"),
    )
    with pytest.raises(OrderOutcomeUnknownError) as info:
        orders.limit_buy("OPRA", 10, 167.49, send_by_ms=NOW_MS)
    assert type(info.value.last_error) is loaf.SendByPassedError
    assert info.value.attempts == 2
    orders.limit_buy("OPRA", 10, 167.49)
    assert indices(exchange) == [4097, 4097, 4098]


def test_a_paused_engine_is_a_definite_refusal_that_gives_the_numbers_back(
    exchange, trading_client
):
    paused = error_reply(
        503, "Trading is paused. Your order was not placed — please try again shortly.",
        "TRADING_PAUSED",
    )
    orders = trading_client(max_retries=1).orders
    exchange.queue("/orders", paused, paused)
    with pytest.raises(loaf.TradingPausedError):
        orders.limit_buy("OPRA", 10, 167.49)
    orders.limit_buy("OPRA", 10, 167.49)
    assert indices(exchange) == [4097, 4097, 4097]


# --------------------------------------------------------------------------- #
# resubmit's send-by time
# --------------------------------------------------------------------------- #


def test_resubmit_sends_a_new_send_by_time_and_the_same_signed_order(
    exchange, trading_client, l2_clock
):
    now = freeze(l2_clock)
    body = unknown_order(exchange, trading_client, send_by_ms=now + 1_000, order_tag=5)
    kept = json.loads(json.dumps(body))
    exchange.queue("/orders", ok({"success": True, "orderId": 7, "duplicate": False}))
    trading_client().orders.resubmit(body, send_by_ms=now + 30_000)
    first, again = exchange.bodies("/orders")
    assert again == {**first, "sendByMs": now + 30_000}
    assert list(again) == list(first)  # in place: the keys keep their order
    verify_order(again)
    assert body == kept  # the caller's body is left as it was


def test_resubmit_adds_a_send_by_time_to_a_body_that_had_none(exchange, trading_client):
    body = unknown_order(exchange, trading_client)
    exchange.queue("/orders", ok({"success": True, "orderId": 7, "duplicate": True}))
    trading_client().orders.resubmit(body, send_by_ms=2**48 - 1)
    first, again = exchange.bodies("/orders")
    assert "sendByMs" not in first
    assert again == {**first, "sendByMs": 2**48 - 1}


@pytest.mark.parametrize("send_by_ms", [0, 2**48, True, float(NOW_MS), "1"])
def test_resubmit_refuses_a_send_by_time_that_is_none(exchange, trading_client, send_by_ms):
    body = unknown_order(exchange, trading_client)
    sent = len(exchange.requests)
    with pytest.raises(loaf.LoafValidationError) as info:
        trading_client().orders.resubmit(body, send_by_ms=send_by_ms)
    assert info.value.status_code == 0
    assert info.value.message.startswith("send_by_ms must be ")
    assert len(exchange.requests) == sent


def test_resubmit_takes_no_send_by_time_for_a_stop_or_take(exchange, trading_client):
    exchange.queue("/orders/conditional", httpx.ReadTimeout("timed out"))
    with pytest.raises(OrderOutcomeUnknownError) as info:
        trading_client(max_retries=0).orders.stop_loss("OPRA", 5, 90)
    sent = len(exchange.requests)
    with pytest.raises(loaf.LoafValidationError, match="send_by_ms applies to orders only"):
        trading_client().orders.resubmit(info.value.signed_body, send_by_ms=NOW_MS)
    assert len(exchange.requests) == sent


def test_resubmit_sends_nothing_once_the_send_by_time_has_passed(
    exchange, trading_client, l2_clock
):
    now = freeze(l2_clock)
    body = unknown_order(exchange, trading_client, send_by_ms=now + 5_000)
    sent = len(exchange.requests)
    # Up to its send-by time it is sent ...
    freeze(l2_clock, now + 5_000)
    exchange.queue("/orders", ok({"success": True, "orderId": 7, "duplicate": True}))
    trading_client().orders.resubmit(body)
    assert len(exchange.requests) == sent + 1
    # ... and after it, nothing: the exchange would refuse it before it looks for the first
    # attempt, so the answer would say nothing. The outcome is still unknown.
    freeze(l2_clock, now + 5_001)
    with pytest.raises(OrderOutcomeUnknownError) as info:
        trading_client().orders.resubmit(body)
    assert (info.value.attempts, info.value.signed_body) == (0, body)
    refusal = info.value.last_error
    assert type(refusal) is loaf.SendByPassedError and info.value.__cause__ is refusal
    assert (refusal.status_code, refusal.code) == (0, "SEND_BY_PASSED")
    assert f"orders.cancel_by_index({body['l2OrderIndex']})" in refusal.message
    assert len(exchange.requests) == sent + 1
    # A later send-by time sends it again, placing it now if the first never landed.
    exchange.queue("/orders", ok({"success": True, "orderId": 7, "duplicate": False}))
    trading_client().orders.resubmit(body, send_by_ms=now + 60_000)
    assert exchange.bodies("/orders")[-1]["sendByMs"] == now + 60_000


def test_resubmit_names_the_end_of_the_orders_life_before_its_send_by_time(
    exchange, trading_client, l2_clock
):
    now = freeze(l2_clock)
    body = unknown_order(exchange, trading_client, send_by_ms=now + 5_000)
    freeze(l2_clock, body["l2ExpiryMs"])  # both have passed
    with pytest.raises(OrderOutcomeUnknownError) as info:
        trading_client().orders.resubmit(body)
    assert type(info.value.last_error) is loaf.LoafValidationError
    assert "could rest only until" in info.value.last_error.message


# --------------------------------------------------------------------------- #
# cancel_all and cancel_after: their scope
# --------------------------------------------------------------------------- #

CANCELLED = {"requestedCount": 1, "cancelledOrderIds": [9], "failedOrders": []}


def test_cancel_all_scopes_by_market_and_tag(exchange, trading_client):
    orders = trading_client().orders
    orders.limit_buy("OPRA", 10, 167.49)  # the market is known from here on
    sent = len(exchange.requests)
    for _ in range(3):
        exchange.queue("/orders/cancel-all", ok(CANCELLED))
    assert orders.cancel_all("OPRA") == CANCELLED
    orders.cancel_all(order_tag=ORDER_TAG_MAX)
    orders.cancel_all("OPRA", order_tag=1)
    posts = exchange.requests[sent:]
    assert routes(posts) == ["POST /orders/cancel-all"] * 3  # nothing read
    assert [json.loads(r.content) for r in posts] == [
        {"marketIndex": MARKET_INDEX},
        {"orderTag": ORDER_TAG_MAX},
        {"marketIndex": MARKET_INDEX, "orderTag": 1},
    ]
    assert list(json.loads(posts[2].content)) == ["marketIndex", "orderTag"]
    for request in posts:
        assert verify_login(bearer(request))["l2AccountIndex"] == ACCOUNT_INDEX


def test_a_market_no_order_was_placed_on_is_read_once_without_a_login(exchange, trading_client):
    exchange.details["MUS"] = market_detail("MUS", market_index=7)
    orders = trading_client().orders
    for _ in range(2):
        exchange.queue("/orders/cancel-all", ok(CANCELLED))
    orders.cancel_all("MUS")
    orders.cancel_all("MUS")
    assert routes(exchange.requests) == [
        "GET /trade/MUS", "POST /orders/cancel-all", "POST /orders/cancel-all"
    ]
    assert "authorization" not in exchange.requests[0].headers  # a public read
    assert [json.loads(r.content) for r in exchange.requests[1:]] == [{"marketIndex": 7}] * 2


def test_a_scope_is_checked_before_the_market_read(exchange, trading_client):
    orders = trading_client().orders
    for call in (
        lambda: orders.cancel_all("OPRA", order_tag=0),
        lambda: orders.cancel_all("OPRA", order_tag=True),
        lambda: orders.cancel_after(60_000, ticker="OPRA", order_tag=2**32),
        lambda: orders.cancel_all("opra"),
        lambda: orders.cancel_all(7),
    ):
        with pytest.raises(loaf.LoafValidationError) as info:
            call()
        assert info.value.status_code == 0
    assert exchange.requests == []


def test_a_scope_on_an_unknown_market_sends_no_cancel(exchange, trading_client):
    with pytest.raises(loaf.LoafNotFoundError):
        trading_client().orders.cancel_all("NOPE")
    assert routes(exchange.requests) == ["GET /trade/NOPE"]


@pytest.mark.parametrize("market_index", [4096, -1, None, "2050", True, ...])
def test_a_market_read_without_a_usable_index_is_a_server_error(
    exchange, trading_client, market_index
):
    exchange.details["OPRA"] = market_detail(market_index=market_index)
    with pytest.raises(loaf.LoafServerError, match="answered no market index"):
        trading_client().orders.cancel_all("OPRA")
    assert routes(exchange.requests) == ["GET /trade/OPRA"]


def test_cancel_after_sets_its_scopes_deadline(exchange, trading_client):
    orders = trading_client().orders
    orders.limit_buy("OPRA", 10, 167.49)
    sent = len(exchange.requests)
    answers = [{"deadlineMs": NOW_MS + 60_000}, {"deadlineMs": NOW_MS}, {"deadlineMs": 0}]
    exchange.queue("/orders/cancel-after", *(ok(answer) for answer in answers))
    assert orders.cancel_after(CANCEL_AFTER_MAX_MS) == answers[0]
    assert orders.cancel_after(CANCEL_AFTER_MIN_MS, ticker="OPRA", order_tag=3) == answers[1]
    assert orders.cancel_after(0, order_tag=3) == answers[2]
    posts = exchange.requests[sent:]
    assert routes(posts) == ["POST /orders/cancel-after"] * 3
    assert [json.loads(r.content) for r in posts] == [
        {"timeoutMs": 120_000},
        {"timeoutMs": 10_000, "marketIndex": MARKET_INDEX, "orderTag": 3},
        {"timeoutMs": 0, "orderTag": 3},
    ]
    for request in posts:
        verify_login(bearer(request))


@pytest.mark.parametrize(
    "timeout_ms",
    # False is no 0: a bool is never a number of milliseconds, so it never turns a timer off.
    [1, CANCEL_AFTER_MIN_MS - 1, CANCEL_AFTER_MAX_MS + 1, -1, True, False, 60_000.0, "60000", None],
)
def test_cancel_after_refuses_a_timeout_it_cannot_set(exchange, trading_client, timeout_ms):
    with pytest.raises(loaf.LoafValidationError) as info:
        trading_client().orders.cancel_after(timeout_ms)
    assert info.value.status_code == 0
    assert info.value.message.startswith("timeout_ms must be 0 (off) or ")
    assert exchange.requests == []


@pytest.mark.parametrize(
    "reply, cls, code",
    [
        (error_reply(429, "Too many requests"), loaf.LoafRateLimitError, None),
        (
            error_reply(503, "Not processed", "NOT_PROCESSED"),
            loaf.LoafServiceUnavailableError,
            "NOT_PROCESSED",
        ),
        (
            error_reply(503, "Unavailable", "SERVICE_UNAVAILABLE"),
            loaf.LoafServiceUnavailableError,
            "SERVICE_UNAVAILABLE",
        ),
    ],
)
def test_cancel_after_refusals_reach_the_caller_unretried(
    exchange, trading_client, reply, cls, code
):
    exchange.queue("/orders/cancel-after", reply)
    with pytest.raises(cls) as info:
        trading_client().orders.cancel_after(60_000)
    assert (type(info.value), info.value.code) == (cls, code)
    assert len(exchange.posts("/orders/cancel-after")) == 1


# --------------------------------------------------------------------------- #
# cancel_by_index
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "answer",
    [
        {"outcome": "CANCELLED", "orderId": 77, "status": "CANCELLED"},
        {"outcome": "ENDED", "orderId": 77, "status": "FILLED"},
        {"outcome": "VOIDED", "orderId": None, "status": None},
    ],
    ids=lambda answer: answer["outcome"],
)
def test_cancel_by_index_names_the_keys_account_and_the_number(exchange, trading_client, answer):
    exchange.queue("/orders/cancel-by-index", ok(answer))
    result = trading_client().orders.cancel_by_index(4097)
    assert result == answer
    assert result.outcome in {member.value for member in loaf.CancelByIndexOutcome}
    (request,) = exchange.requests  # no read: the number and your trading key's account
    assert routes([request]) == ["POST /orders/cancel-by-index"]
    assert list(json.loads(request.content)) == ["l2AccountIndex", "l2OrderIndex"]
    assert json.loads(request.content) == {"l2AccountIndex": ACCOUNT_INDEX, "l2OrderIndex": 4097}
    assert verify_login(bearer(request))["l2AccountIndex"] == ACCOUNT_INDEX


def test_cancel_by_index_settles_an_unknown_outcome(exchange, trading_client):
    body = unknown_order(exchange, trading_client)
    exchange.queue("/orders/cancel-by-index", ok({"outcome": "VOIDED", "orderId": None, "status": None}))
    trading_client().orders.cancel_by_index(body["l2OrderIndex"])
    assert json.loads(exchange.posts("/orders/cancel-by-index")[0])["l2OrderIndex"] == 4097


@pytest.mark.parametrize("order_index", [0, -1, 2**40, True, 4097.0, "4097", None])
def test_cancel_by_index_refuses_what_is_not_an_order_number(
    exchange, trading_client, order_index
):
    with pytest.raises(loaf.LoafValidationError) as info:
        trading_client().orders.cancel_by_index(order_index)
    assert info.value.status_code == 0
    assert info.value.message.startswith("order_index must be an order number")
    assert exchange.requests == []


def test_cancel_by_index_takes_the_largest_number_and_never_shows_a_signed_body(
    exchange, trading_client
):
    orders = trading_client().orders
    exchange.queue("/orders/cancel-by-index", ok({"outcome": "VOIDED", "orderId": None, "status": None}))
    orders.cancel_by_index(2**40 - 1)
    with pytest.raises(loaf.LoafValidationError) as info:
        orders.cancel_by_index({"l2OrderIndex": 4097, "l2SigS": "ab" * 40})  # type: ignore[arg-type]
    assert str(info.value).endswith("(got dict)")
    assert len(exchange.requests) == 1


def test_cancel_by_index_needs_the_trading_key(exchange, trading_client):
    with pytest.raises(loaf.LoafConfigError, match="needs your trading key"):
        trading_client(trading_key=None).orders.cancel_by_index(4097)
    assert exchange.requests == []


@pytest.mark.parametrize(
    "reply, cls",
    [
        (error_reply(400, "Order number not reserved", "ORDER_INDEX_NOT_RESERVED"), loaf.LoafValidationError),
        (error_reply(403, "Not your order number", "ORDER_NOT_OWNED"), loaf.LoafForbiddenError),
        (
            error_reply(400, "Conditional order 12 holds this order number", "INVALID_REQUEST"),
            loaf.LoafValidationError,
        ),
        (error_reply(403, "Trading is currently halted", "TRADING_HALTED"), loaf.TradingHaltedError),
        (error_reply(503, "Not processed", "NOT_PROCESSED"), loaf.LoafServiceUnavailableError),
    ],
    ids=["not-reserved", "not-owned", "conditional", "halted", "not-processed"],
)
def test_cancel_by_index_refusals_by_code(exchange, trading_client, reply, cls):
    exchange.queue("/orders/cancel-by-index", reply)
    with pytest.raises(cls) as info:
        trading_client().orders.cancel_by_index(4097)
    assert type(info.value) is cls
    assert info.value.code == reply.json()["code"]


def test_a_number_given_back_and_then_settled_is_never_signed_again(exchange, trading_client):
    orders = trading_client().orders
    exchange.queue("/orders", error_reply(400, "Insufficient balance", "INSUFFICIENT_BALANCE"))
    with pytest.raises(loaf.LoafValidationError):
        orders.limit_buy("OPRA", 10, 167.49)  # 4097, given back
    # Settled whatever the answer (here a 503): voided, a later order on it would be refused.
    exchange.queue("/orders/cancel-by-index", error_reply(503, "Not processed", "NOT_PROCESSED"))
    with pytest.raises(loaf.LoafServiceUnavailableError):
        orders.cancel_by_index(4097)
    orders.limit_buy("OPRA", 10, 167.49)
    assert indices(exchange) == [4097, 4098]
    assert counts(exchange) == [1, 2]


def _l2(trading_client: Any) -> tuple[L2State, Any]:
    client = trading_client()
    l2 = client.orders._l2
    return l2, l2.account(ACCOUNT_INDEX)


@pytest.mark.parametrize("number", [4100, 4101, 4102])
def test_spent_drops_a_span_given_back_whole(exchange, trading_client, number):
    l2, account = _l2(trading_client)
    l2.take(account, 3, DEFAULT_ORDER_TTL_MS)  # reserves 3: 4097-4099
    first, block, _ = l2.take(account, 3, DEFAULT_ORDER_TTL_MS)  # reserves 6: 4100-4105
    l2.take(account, 1, DEFAULT_ORDER_TTL_MS)  # 4103: a later order holds the next number
    l2.give_back(block, first, 3)  # an order with two legs, refused
    assert block.returned == [(4100, 3)]
    l2.spent(number)  # the order's number or a leg's: the order and its legs go together
    assert block.returned == []
    assert l2.take(account, 3, DEFAULT_ORDER_TTL_MS)[0] != first


def test_spent_keeps_a_span_given_back_beside_the_number(exchange, trading_client):
    l2, account = _l2(trading_client)
    l2.take(account, 3, DEFAULT_ORDER_TTL_MS)  # reserves 3: 4097-4099
    first, block, _ = l2.take(account, 3, DEFAULT_ORDER_TTL_MS)  # reserves 6: 4100-4105
    l2.take(account, 1, DEFAULT_ORDER_TTL_MS)  # 4103
    l2.give_back(block, first, 3)
    l2.spent(4099)  # just before the span
    l2.spent(4103)  # just past it: the later order's
    assert block.returned == [(4100, 3)]
    assert l2.take(account, 3, DEFAULT_ORDER_TTL_MS)[0] == first  # signed again, never wasted


def test_spent_ends_the_block_before_a_number_not_handed_out(exchange, trading_client):
    l2, account = _l2(trading_client)
    l2.take(account, 1, DEFAULT_ORDER_TTL_MS)  # reserves 1: 4097
    first, block, _ = l2.take(account, 1, DEFAULT_ORDER_TTL_MS)  # reserves 2: 4098-4099
    assert (first, block.next, block.end) == (4098, 4099, 4100)
    l2.spent(4099)
    assert block.end == 4099
    # The block has nothing left before it: the next order reserves afresh.
    assert l2.take(account, 1, DEFAULT_ORDER_TTL_MS)[0] == 4100
    assert counts(exchange) == [1, 2, 4]


def test_spent_leaves_numbers_it_does_not_hold_alone(exchange, trading_client):
    l2, account = _l2(trading_client)
    l2.take(account, 1, DEFAULT_ORDER_TTL_MS)
    first, block, _ = l2.take(account, 1, DEFAULT_ORDER_TTL_MS)
    l2.give_back(block, first, 1)
    before = (block.next, block.end, list(block.returned))
    for number in (4097, 4100, 2**40 - 1):  # handed out, past the block, another range's
        l2.spent(number)
    assert (block.next, block.end, block.returned) == before


def test_spent_reaches_the_block_reserved_ahead(exchange, trading_client):
    l2, account = _l2(trading_client)
    l2.take(account, 1, DEFAULT_ORDER_TTL_MS)  # reserves 1: 4097
    standby = l2._reserved(account, l2._post_reservation(4))  # 4098-4101
    l2._standby = standby
    l2.spent(4099)
    assert (standby.next, standby.end) == (4098, 4099)


# --------------------------------------------------------------------------- #
# Refusals read by their code alone
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "reply, cls",
    [
        (error_reply(400, "Stale settle epoch", "SETTLE_EPOCH_MISMATCH"), loaf.LoafValidationError),
        (error_reply(401, "Invalid signature", "INVALID_SIGNATURE"), loaf.LoafAuthError),
    ],
    ids=["epoch", "signature"],
)
def test_a_context_code_rereads_the_market_and_the_account(exchange, trading_client, reply, cls):
    orders = trading_client().orders
    orders.limit_buy("OPRA", 10, 167.49)
    exchange.queue("/orders", reply)
    with pytest.raises(cls):
        orders.limit_buy("OPRA", 10, 167.49)  # reserves 2 and takes 4098
    sent = len(exchange.requests)
    orders.limit_buy("OPRA", 10, 167.49)
    assert routes(exchange.requests[sent:]) == ["GET /trade/OPRA", "GET /l2/keys", "POST /orders"]
    assert indices(exchange) == [4097, 4098, 4098]


def test_an_index_code_drops_the_block(exchange, trading_client):
    orders = trading_client().orders
    orders.limit_buy("OPRA", 10, 167.49)
    exchange.queue("/orders", error_reply(400, "Order number already used", "ORDER_INDEX_USED"))
    with pytest.raises(loaf.LoafValidationError):
        orders.limit_buy("OPRA", 10, 167.49)  # reserves 2 and takes 4098
    sent = len(exchange.requests)
    orders.limit_buy("OPRA", 10, 167.49)
    # A new block, and nothing given back from the old one; the market and account are kept.
    assert routes(exchange.requests[sent:]) == ["POST /l2/order-indices", "POST /orders"]
    assert indices(exchange) == [4097, 4098, 4100]


@pytest.mark.parametrize(
    "code", ["PRICE_BAND", "BELOW_MIN_ORDER_VALUE", "OPEN_ORDER_LIMIT", "SELF_TRADE"]
)
def test_a_pre_check_code_shows_nothing_stale(exchange, trading_client, code):
    orders = trading_client().orders
    orders.limit_buy("OPRA", 10, 167.49)
    exchange.queue("/orders", error_reply(400, "refused", code))
    with pytest.raises(loaf.LoafValidationError) as info:
        orders.limit_buy("OPRA", 10, 167.49)
    assert info.value.code == code
    sent = len(exchange.requests)
    orders.limit_buy("OPRA", 10, 167.49)
    assert routes(exchange.requests[sent:]) == ["POST /orders"]
    assert indices(exchange) == [4097, 4098, 4098]  # given back

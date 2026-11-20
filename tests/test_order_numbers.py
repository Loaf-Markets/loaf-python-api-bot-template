"""The settings for how orders get their order numbers and how long a placement waits:
``placement_timeout``, ``order_number_block``, ``order_number_start``, ``reserve_ahead`` and
``orders.warm``. What a block is and how numbers are given back is in ``test_orders.py``.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import httpx
import pytest

import loaf
from loaf import LoafClient
from loaf.constants import DEFAULT_ORDER_TTL_MS, L2_MAX_ORDER_LIFE_MS, L2_ORDER_INDEX_BLOCK_MAX

from conftest import (
    ACCOUNT_INDEX,
    BASE,
    L2_CHAIN_ID,
    MARKET_L2,
    TRADING_KEY,
    FakeExchange,
    freeze,
    market_detail,
)

AHEAD_THREAD = "loaf-order-numbers"
WAIT_S = 10.0
INDEX_REFUSED = "l2 order_index: already used by the account; re-sign with the next index"
RESERVED_TOO_SOON = "l2 order_index: the account's last reservation is less than 1 s old"


def SERVER_ERROR() -> httpx.Response:
    return httpx.Response(500, json={"error": "try later"})


def counts(exchange: FakeExchange) -> list[int]:
    return [body["count"] for body in exchange.bodies("/l2/order-indices")]


def indices(exchange: FakeExchange) -> list[int]:
    return [body["l2OrderIndex"] for body in exchange.bodies("/orders")]


def state(client: LoafClient) -> tuple[Any, Any]:
    l2 = client.orders._l2
    return l2, l2.account(ACCOUNT_INDEX)


def take(l2: Any, account: Any, span: int = 1) -> int:
    return l2.take(account, span, DEFAULT_ORDER_TTL_MS)[0]


def eventually(condition: Any) -> None:
    """Wait on real time for what the background thread does."""
    deadline = time.monotonic() + WAIT_S
    while not condition():
        assert time.monotonic() < deadline, "the background reservation did not happen"
        time.sleep(0.002)


def ahead_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == AHEAD_THREAD]


@pytest.fixture(autouse=True)
def _stops_its_threads():
    before = set(ahead_threads())
    yield
    for thread in set(ahead_threads()) - before:
        thread.join(WAIT_S)  # every test closes its client


@pytest.fixture
def gated(exchange: FakeExchange):
    """A client whose reservations by the background thread wait for ``gate``, and the names of
    the threads that sent each reservation."""
    gate = threading.Event()
    senders: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/l2/order-indices":
            senders.append(threading.current_thread().name)
            if threading.current_thread().name == AHEAD_THREAD:
                assert gate.wait(WAIT_S)
        return exchange.handler(request)

    def make(**kwargs: Any) -> LoafClient:
        http = httpx.Client(transport=httpx.MockTransport(handler), base_url=BASE)
        return LoafClient(TRADING_KEY, base_url=BASE, http_client=http, **kwargs)

    return make, gate, senders


# --------------------------------------------------------------------------- #
# placement_timeout
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("seconds", [0.25, 2, 44.9, 60.0])
def test_placement_timeout_is_the_read_wait_as_set(exchange, trading_client, seconds):
    trading_client(placement_timeout=seconds).orders.limit_buy("OPRA", 10, 167.49)
    *reads, post = exchange.requests
    assert post.extensions["timeout"]["read"] == seconds
    # The reads keep the client's timeout, and only the placement's read wait changes.
    assert {r.extensions["timeout"]["read"] for r in reads} == {30.0}
    assert (post.extensions["timeout"]["connect"], post.extensions["timeout"]["write"]) == (
        30.0,
        30.0,
    )


def test_placement_timeout_covers_a_client_with_no_timeout(exchange, trading_client):
    trading_client(timeout=None, placement_timeout=3.0).orders.limit_buy("OPRA", 10, 167.49)
    assert exchange.requests[-1].extensions["timeout"]["read"] == 3.0


def test_placement_timeout_left_out_keeps_the_45_s_floor(exchange, trading_client):
    trading_client(timeout=5.0).orders.limit_buy("OPRA", 10, 167.49)
    assert exchange.requests[-1].extensions["timeout"]["read"] == 45.0


def test_placement_timeout_past_it_the_outcome_is_unknown(exchange, trading_client):
    exchange.queue(
        "/orders",
        *[httpx.ReadTimeout("slow") for _ in range(2)],
    )
    with pytest.raises(loaf.OrderOutcomeUnknownError):
        trading_client(placement_timeout=0.5, max_retries=1).orders.limit_buy("OPRA", 10, 167.49)
    first, again = exchange.bodies("/orders")
    assert first == again  # the exact signed order, so it can land only once


@pytest.mark.parametrize("bad", [0, -1, True, False, "2", float("inf"), float("nan")])
def test_placement_timeout_refuses_what_is_not_a_wait(trading_client, bad):
    with pytest.raises(loaf.LoafConfigError, match="placement_timeout"):
        trading_client(placement_timeout=bad)


# --------------------------------------------------------------------------- #
# order_number_block, order_number_start
# --------------------------------------------------------------------------- #


def test_block_caps_the_doubling(exchange, trading_client):
    l2, account = state(trading_client(order_number_block=8))
    for _ in range(31):
        take(l2, account)
    assert counts(exchange) == [1, 2, 4, 8, 8, 8]


def test_block_above_the_default_doubles_past_64(exchange, trading_client):
    l2, account = state(trading_client(order_number_block=256))
    for _ in range(257):
        take(l2, account)
    assert counts(exchange) == [1, 2, 4, 8, 16, 32, 64, 128, 256]


def test_start_is_the_first_reservation(exchange, trading_client):
    l2, account = state(trading_client(order_number_start=16, order_number_block=64))
    for _ in range(17):
        take(l2, account)
    assert counts(exchange) == [16, 32]


def test_start_at_the_block_means_no_second_reservation_soon(exchange, trading_client):
    client = trading_client(order_number_start=64, order_number_block=64)
    for _ in range(64):
        client.orders.limit_buy("OPRA", 10, 167.49)
    assert counts(exchange) == [64]
    assert indices(exchange) == list(range(4097, 4097 + 64))


def test_an_order_with_legs_gets_its_span_past_the_block(exchange, trading_client):
    orders = trading_client(order_number_block=2).orders
    for _ in range(2):
        orders.limit_buy("OPRA", 10, 100, tp_price=120, sl_price=90)
    assert counts(exchange) == [3, 3]


@pytest.mark.parametrize(
    "kwargs, field",
    [
        ({"order_number_block": 0}, "order_number_block"),
        ({"order_number_block": L2_ORDER_INDEX_BLOCK_MAX + 1}, "order_number_block"),
        ({"order_number_block": 8.0}, "order_number_block"),
        ({"order_number_block": True}, "order_number_block"),
        ({"order_number_start": 0}, "order_number_start"),
        ({"order_number_start": "1"}, "order_number_start"),
        ({"order_number_start": L2_ORDER_INDEX_BLOCK_MAX + 1}, "order_number_start"),
        ({"order_number_start": 65}, "order_number_start"),  # above the default block
        ({"order_number_start": 9, "order_number_block": 8}, "order_number_start"),
    ],
)
def test_order_number_settings_refused(trading_client, kwargs, field):
    with pytest.raises(loaf.LoafConfigError, match=field):
        trading_client(**kwargs)


def test_the_largest_block_is_asked_for_whole(exchange, trading_client):
    l2, account = state(
        trading_client(
            order_number_start=L2_ORDER_INDEX_BLOCK_MAX, order_number_block=L2_ORDER_INDEX_BLOCK_MAX
        )
    )
    take(l2, account)
    assert counts(exchange) == [L2_ORDER_INDEX_BLOCK_MAX]


# --------------------------------------------------------------------------- #
# reserve_ahead
# --------------------------------------------------------------------------- #


def test_no_thread_unless_asked(exchange, trading_client):
    before = ahead_threads()
    client = trading_client(order_number_start=8, order_number_block=8)
    l2, account = state(client)
    for _ in range(8):
        take(l2, account)
    assert ahead_threads() == before
    assert l2._ahead_thread is None
    client.close()


def test_no_thread_until_half_used(exchange, trading_client):
    client = trading_client(order_number_start=8, order_number_block=8, reserve_ahead=True)
    l2, account = state(client)
    for _ in range(3):
        take(l2, account)
    assert l2._ahead_thread is None
    assert counts(exchange) == [8]
    take(l2, account)
    assert l2._ahead_thread is not None
    eventually(lambda: l2._standby is not None)
    assert counts(exchange) == [8, 8]
    client.close()


def test_a_placement_with_one_reserved_ahead_sends_no_reservation(gated):
    make, gate, senders = gated
    gate.set()
    client = make(order_number_start=8, order_number_block=8, reserve_ahead=True)
    l2, account = state(client)
    numbers = []
    for _ in range(8):
        numbers.append(take(l2, account))
    eventually(lambda: l2._standby is not None and not l2._ahead_busy)
    assert senders == ["MainThread", AHEAD_THREAD]
    numbers.append(take(l2, account))  # the first of the block reserved ahead, with no request
    assert senders == ["MainThread", AHEAD_THREAD]
    assert l2._standby is None
    assert numbers == list(range(4097, 4097 + 9))
    client.close()


def test_outrunning_the_thread_waits_for_the_one_in_flight(exchange, gated):
    make, gate, senders = gated
    client = make(order_number_start=4, order_number_block=4, reserve_ahead=True)
    l2, account = state(client)
    numbers = [take(l2, account) for _ in range(4)]  # half used at 2: the thread is in its request
    eventually(lambda: senders == ["MainThread", AHEAD_THREAD])

    got: list[int] = []
    waiter = threading.Thread(target=lambda: got.append(take(l2, account)))
    waiter.start()
    waiter.join(0.05)
    assert waiter.is_alive() and got == []  # it waits, and sends nothing of its own
    assert senders == ["MainThread", AHEAD_THREAD]

    gate.set()
    waiter.join(WAIT_S)
    assert got == [4097 + 4]
    assert numbers == [4097, 4098, 4099, 4100]
    assert senders == ["MainThread", AHEAD_THREAD]  # one reservation served it
    client.close()


def test_the_first_block_from_the_thread_is_taken_not_reserved_again(exchange, gated):
    make, gate, senders = gated
    client = make(order_number_start=4, order_number_block=4, reserve_ahead=True)
    l2, account = state(client)
    take(l2, account)
    take(l2, account)
    eventually(lambda: senders == ["MainThread", AHEAD_THREAD])
    l2._block = None  # as a refusal drops it, while the thread is in its request

    got: list[int] = []
    waiter = threading.Thread(target=lambda: got.append(take(l2, account)))
    waiter.start()
    gate.set()
    waiter.join(WAIT_S)
    assert got == [4097 + 4]
    assert senders == ["MainThread", AHEAD_THREAD]
    client.close()


def test_the_thread_spaces_its_requests_a_second_from_the_last(exchange, trading_client, l2_clock):
    freeze(l2_clock)  # the clock stands still, so no time has passed
    client = trading_client(order_number_start=2, order_number_block=2, reserve_ahead=True)
    l2, account = state(client)
    take(l2, account)
    eventually(lambda: l2._standby is not None)
    assert l2_clock.slept == [1.0]
    assert counts(exchange) == [2, 2]
    client.close()


def test_the_thread_does_not_pause_when_a_second_has_passed(exchange, trading_client, l2_clock):
    now = freeze(l2_clock)
    client = trading_client(order_number_start=2, order_number_block=2, reserve_ahead=True)
    l2, account = state(client)
    l2.warm(account)
    freeze(l2_clock, now + 1000)
    take(l2, account)
    eventually(lambda: l2._standby is not None)
    assert l2_clock.slept == []
    client.close()


def test_the_doubling_carries_on_in_the_background(exchange, trading_client):
    client = trading_client(reserve_ahead=True)  # the defaults otherwise: 1, 2, 4, ...
    l2, account = state(client)
    numbers = []
    for _ in range(40):
        numbers.append(take(l2, account))
        eventually(lambda: l2._standby is not None or not l2._block.half_used())
    assert numbers == list(range(4097, 4097 + 40))
    grown = counts(exchange)
    assert grown[:6] == [1, 2, 4, 8, 16, 32]
    assert max(grown) <= 64
    client.close()


def test_a_failure_is_logged_and_retried_slower(exchange, trading_client, l2_clock, caplog):
    client = trading_client(order_number_start=2, order_number_block=2, reserve_ahead=True)
    l2, account = state(client)
    l2.warm(account)
    exchange.queue("/l2/order-indices", *[SERVER_ERROR() for _ in range(3)])
    with caplog.at_level(logging.WARNING, logger="loaf._l2"):
        take(l2, account)
        eventually(lambda: l2._standby is not None)
    assert counts(exchange) == [2, 2, 2, 2, 2]
    assert [s for s in l2_clock.slept if s in (1.0, 2.0, 4.0, 8.0)] == [1.0, 2.0, 4.0]
    assert sum("Reserving order numbers ahead failed" in r.message for r in caplog.records) == 3
    client.close()


def test_a_failure_leaves_placements_reserving_for_themselves(exchange, trading_client):
    client = trading_client(order_number_start=2, order_number_block=2, reserve_ahead=True)
    l2, account = state(client)
    l2.warm(account)
    exchange.queue("/l2/order-indices", SERVER_ERROR())
    assert take(l2, account) == 4097  # half used: the thread meets that error
    eventually(lambda: len(counts(exchange)) >= 2)
    assert take(l2, account) == 4098
    # The thread's retry may be in flight or done; either way the placement gets numbers.
    assert take(l2, account) == 4099
    client.close()


def test_the_thread_waits_out_too_soon_like_a_placement(
    exchange, trading_client, l2_clock, caplog
):
    client = trading_client(order_number_start=2, order_number_block=2, reserve_ahead=True)
    l2, account = state(client)
    l2.warm(account)
    # Another process on this account reserved just now, so spacing from this one's last is not
    # enough: the answer is the limit's, and the thread pauses and asks once more.
    exchange.queue("/l2/order-indices", httpx.Response(422, json={"error": RESERVED_TOO_SOON}))
    with caplog.at_level(logging.WARNING, logger="loaf._l2"):
        take(l2, account)
        eventually(lambda: l2._standby is not None and not l2._ahead_busy)
    assert counts(exchange) == [2, 2, 2]
    assert 1.0 in l2_clock.slept
    assert not caplog.records
    client.close()


def test_a_placement_raises_what_the_exchange_answers_it(exchange, trading_client):
    # The thread's failures are only logged: a placement's own reservation still raises.
    client = trading_client(order_number_start=1, order_number_block=1, reserve_ahead=True)
    client.close()  # no thread: this reservation is the placement's own
    l2, account = state(client)
    l2.warm(account)
    take(l2, account)
    exchange.queue("/l2/order-indices", SERVER_ERROR())
    with pytest.raises(loaf.LoafAPIError):
        take(l2, account)


def test_one_reserved_ahead_past_its_life_is_not_used(exchange, trading_client, l2_clock):
    now = freeze(l2_clock)
    client = trading_client(order_number_start=2, order_number_block=2, reserve_ahead=True)
    l2, account = state(client)
    take(l2, account)
    take(l2, account)
    eventually(lambda: l2._standby is not None and not l2._ahead_busy)
    client.close()  # no more from the thread
    stale = l2._standby
    freeze(l2_clock, now + L2_MAX_ORDER_LIFE_MS)
    exchange.reserved_at_ms = now + L2_MAX_ORDER_LIFE_MS
    first = take(l2, account)
    assert l2._block is not stale
    assert first >= stale.end  # a new reservation, and the old one is let go
    assert l2._standby is None


def test_a_refusal_of_numbers_leaves_the_one_reserved_ahead(exchange, trading_client):
    client = trading_client(order_number_start=4, order_number_block=4, reserve_ahead=True)
    orders = client.orders
    l2 = orders._l2
    orders.limit_buy("OPRA", 10, 167.49)
    orders.limit_buy("OPRA", 10, 167.49)
    eventually(lambda: l2._standby is not None and not l2._ahead_busy)
    exchange.queue("/orders", httpx.Response(422, json={"error": INDEX_REFUSED}))
    with pytest.raises(loaf.LoafAPIError):
        orders.limit_buy("OPRA", 10, 167.49)
    assert l2._block is None
    orders.limit_buy("OPRA", 10, 167.49)
    assert indices(exchange)[-1] == 4097 + 4  # from the block reserved ahead
    assert counts(exchange) == [4, 4]  # with no new request
    client.close()


def test_close_stops_the_thread_and_later_takes_reserve_for_themselves(exchange, trading_client):
    client = trading_client(order_number_start=2, order_number_block=2, reserve_ahead=True)
    l2, account = state(client)
    take(l2, account)
    eventually(lambda: l2._standby is not None and not l2._ahead_busy)
    thread = l2._ahead_thread
    assert thread.is_alive()
    client.close()
    thread.join(WAIT_S)
    assert not thread.is_alive()
    assert thread.daemon  # a process that never calls close still exits


def test_many_threads_never_share_a_number(exchange, trading_client):
    client = trading_client(order_number_start=8, order_number_block=8, reserve_ahead=True)
    l2, account = state(client)
    numbers: list[int] = []
    guard = threading.Lock()
    errors: list[BaseException] = []
    start = threading.Barrier(8)

    def run() -> None:
        try:
            start.wait()
            for _ in range(100):
                first = take(l2, account)
                with guard:
                    numbers.append(first)
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(WAIT_S * 3)
    client.close()
    assert not errors
    assert len(numbers) == 800
    assert len(set(numbers)) == 800


def test_a_placement_with_legs_and_reserve_ahead_is_signed_on_its_span(exchange, trading_client):
    client = trading_client(order_number_start=16, order_number_block=16, reserve_ahead=True)
    for _ in range(8):
        client.orders.limit_buy("OPRA", 10, 100, tp_price=120, sl_price=90)
    used = indices(exchange)
    assert all(later - earlier >= 3 for earlier, later in zip(used, used[1:]))
    client.close()


# --------------------------------------------------------------------------- #
# orders.warm
# --------------------------------------------------------------------------- #


def test_warm_leaves_a_placement_only_the_order(exchange, trading_client):
    client = trading_client(order_number_start=8, order_number_block=8)
    client.orders.warm("OPRA")
    assert [r.url.path for r in exchange.requests] == [
        "/api/trade/OPRA",
        "/api/l2/keys",
        "/api/l2/order-indices",
    ]
    del exchange.requests[:]
    client.orders.limit_buy("OPRA", 10, 167.49)
    assert [r.url.path for r in exchange.requests] == ["/api/orders"]


def test_warm_with_no_ticker_reads_the_account_and_reserves(exchange, trading_client):
    trading_client(order_number_start=8, order_number_block=8).orders.warm()
    assert [r.url.path for r in exchange.requests] == ["/api/l2/keys", "/api/l2/order-indices"]


def test_warm_twice_reserves_once(exchange, trading_client):
    orders = trading_client(order_number_start=8, order_number_block=8).orders
    orders.warm("OPRA")
    orders.warm("OPRA")
    assert counts(exchange) == [8]


def test_warm_needs_the_trading_key(exchange, trading_client, monkeypatch):
    monkeypatch.delenv("LOAF_TRADING_KEY", raising=False)
    with pytest.raises(loaf.LoafConfigError, match="trading key"):
        trading_client(trading_key=None).orders.warm("OPRA")
    assert exchange.requests == []


def test_warm_reserves_nothing_for_another_deployments_market(exchange, trading_client):
    exchange.details["OPRA"] = market_detail(l2={**MARKET_L2, "l2ChainId": L2_CHAIN_ID + 1})
    with pytest.raises(loaf.LoafConfigError, match="another Loaf"):
        trading_client().orders.warm("OPRA")
    assert counts(exchange) == []


def test_warm_refuses_a_bad_ticker_before_any_request(exchange, trading_client):
    with pytest.raises(loaf.LoafValidationError):
        trading_client().orders.warm("not a ticker")
    assert exchange.requests == []

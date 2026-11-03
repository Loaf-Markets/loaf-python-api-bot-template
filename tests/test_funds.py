"""Withdrawals against an in-memory exchange: the signed body (its terms, its
signature, the nonce perps ops spend too, the expiry), the amount in the
collateral's ticks, what is refused locally, the retries, resubmit, and the
exchange's refusals as the backend sends them.

Signatures are checked with ``conftest.verify_op``, which rebuilds K1's withdraw
message from the posted JSON as the exchange reads it.
"""

from __future__ import annotations

import copy
import json
import pickle
import re
from decimal import Decimal
from typing import Any

import httpx
import pytest

import loaf
from loaf import WithdrawalOutcomeUnknownError, models
from loaf.constants import (
    DEFAULT_WITHDRAWAL_TTL_MS,
    L2_BUFFER_MS,
    MAX_WITHDRAWAL_TTL_MS,
    MIN_ORDER_TTL_MS,
    PERPS_QUOTE_ASSET_ID,
)

from conftest import (
    ACCOUNT_INDEX,
    BAD_SIGNATURE,
    EXPIRY_INSIDE_BUFFER,
    KAT,
    KEY_DISABLED,
    L2_SECRET,
    NONCE_REFUSAL,
    PERPS_COLLATERAL,
    UNKNOWN_KEY,
    WRONG_CHAIN,
    routes,
    secret_locals,
    verify_login,
    verify_op,
)

# The SDK sends a withdrawal's keys in this fixed order.
WITHDRAW_KEYS = (
    "l2ChainId l2AccountIndex l2ApiKeyIndex l2AssetId l2Route amount l2Nonce l2ExpiryMs l2SigS "
    "l2SigE"
).split()
NOW_MS = 1_800_000_000_000
# The backend's 503s for a withdrawal (Loaf-Backend l2WithdrawalService.ts): paused, which
# journaled nothing; its account unread, before the engine; the engine's answer lost.
PAUSED = "Trading is paused. Your withdrawal was not submitted — please try again shortly."
ACCOUNT_UNREAD = (
    "l2 account: the trading account could not be read; nothing was done, try again in a moment"
)
UNDECIDED = "the engine did not decide the withdrawal; resend the same signed body"
NOT_YOURS = "l2 account: the account is not this user's"
BELOW_MINIMUM = "l2 money: the amount is below the asset's minimum withdrawal"


def error_reply(status: int, message: str, code: str | None = None, **kwargs: Any) -> httpx.Response:
    body = {"error": message} if code is None else {"error": message, "code": code}
    return httpx.Response(status, json=body, **kwargs)


def bearer(request: httpx.Request) -> str:
    return request.headers["authorization"][len("Bearer ") :]


def withdrawal_posts(exchange: Any) -> list[httpx.Request]:
    return [r for r in exchange.requests if r.method == "POST" and r.url.path == "/api/l2/withdrawals"]


# --------------------------------------------------------------------------- #
# The signed withdrawal
# --------------------------------------------------------------------------- #


def test_a_withdrawal_signs_its_terms_and_spends_the_next_nonce(exchange, trading_client, l2_clock):
    l2_clock.time_ns = lambda: NOW_MS * 1_000_000
    exchange.now_ms = NOW_MS
    exchange.keys["withdrawalNonce"] = 3
    answer = trading_client().funds.withdraw(250)
    assert answer == {
        "journalSeq": 1001,
        "duplicate": False,
        "accountIndex": ACCOUNT_INDEX,
        "assetId": PERPS_QUOTE_ASSET_ID,
        "amount": 25_000,
        "nonce": 3,
        "status": {"status": "PENDING"},
    }
    assert set(answer) == set(models.WithdrawalAccepted.__annotations__)
    (body,) = exchange.bodies("/l2/withdrawals")
    assert list(body) == WITHDRAW_KEYS
    assert {key: body[key] for key in WITHDRAW_KEYS[:8]} == {
        "l2ChainId": 4242,
        "l2AccountIndex": ACCOUNT_INDEX,
        "l2ApiKeyIndex": 0,  # the trading key
        "l2AssetId": 3,  # USDC
        "l2Route": 1,  # the spot balance, the one route the exchange takes
        "amount": "25000",  # 250 USDC in the collateral's ticks of 0.01 USDC, a decimal string
        "l2Nonce": 3,
        # It rests nowhere: the exchange takes it for an hour, and it stays valid for settlement
        # 72 hours past that.
        "l2ExpiryMs": NOW_MS + L2_BUFFER_MS + DEFAULT_WITHDRAWAL_TTL_MS,
    }
    verify_op(body)  # K1's withdraw message, signed with the trading key
    assert "recipient" not in json.dumps(body) and "0x" not in json.dumps(body)
    assert routes(exchange.requests) == [
        "GET /portfolio/summary",
        "GET /l2/keys",
        "POST /l2/withdrawals",
    ]
    assert exchange.requests[1].url.params["apiKeyIndex"] == "0"
    # Each request carries a login token the trading key signs: the exchange checks the account is
    # yours.
    for request in exchange.requests:
        assert verify_login(bearer(request))["l2AccountIndex"] == ACCOUNT_INDEX
    assert exchange.keys["withdrawalNonce"] == 4


def test_withdrawals_and_perps_ops_spend_one_nonce_and_one_collateral_read(
    exchange, trading_client
):
    client = trading_client()
    client.perps.add_margin(5)
    client.funds.withdraw(Decimal("12.34"))
    client.perps.exit(4)
    client.funds.withdraw(0.01)
    nonces = [
        body["l2Nonce"]
        for path in ("/l2/allocations", "/l2/withdrawals", "/l2/exits")
        for body in exchange.bodies(path)
    ]
    assert sorted(nonces) == [0, 1, 2, 3]
    assert [b["amount"] for b in exchange.bodies("/l2/withdrawals")] == ["1234", "1"]
    for body in exchange.bodies("/l2/withdrawals"):
        verify_op(body)
    assert len(exchange.gets("/portfolio/summary")) == 1
    assert len(exchange.gets("/l2/keys")) == 4  # the nonce before every op


@pytest.mark.parametrize("ttl_ms", [MIN_ORDER_TTL_MS, 90_000, MAX_WITHDRAWAL_TTL_MS])
def test_ttl_ms_sets_how_long_the_exchange_takes_it(exchange, trading_client, l2_clock, ttl_ms):
    l2_clock.time_ns = lambda: NOW_MS * 1_000_000
    exchange.now_ms = NOW_MS
    trading_client().funds.withdraw(1, ttl_ms=ttl_ms)
    (body,) = exchange.bodies("/l2/withdrawals")
    assert body["l2ExpiryMs"] == NOW_MS + L2_BUFFER_MS + ttl_ms


def test_a_withdrawal_signs_fresh_randomness(exchange, trading_client):
    # The same terms at the same nonce sign the same message, never the same signature.
    funds = trading_client().funds
    exchange.queue("/l2/withdrawals", error_reply(422, BELOW_MINIMUM))
    with pytest.raises(loaf.LoafAPIError):
        funds.withdraw(5)
    funds.withdraw(5)
    first, second = exchange.bodies("/l2/withdrawals")
    assert first["l2Nonce"] == second["l2Nonce"] == 0
    assert first["l2SigS"] != second["l2SigS"]
    for body in (first, second):
        verify_op(body)


@pytest.mark.parametrize(
    "call, message",
    [
        (lambda f: f.withdraw(0), "amount must be positive"),
        (lambda f: f.withdraw(-2.5), "amount must be positive"),
        (lambda f: f.withdraw("5"), "amount must be a number"),
        (lambda f: f.withdraw(True), "amount must be a number"),
        (lambda f: f.withdraw(None), "amount must be a number"),
        (lambda f: f.withdraw(float("nan")), "amount must be a finite number"),
        (lambda f: f.withdraw(float("inf")), "amount must be a finite number"),
        (lambda f: f.withdraw(5, ttl_ms=MIN_ORDER_TTL_MS - 1), "ttl_ms must be"),
        (lambda f: f.withdraw(5, ttl_ms=MAX_WITHDRAWAL_TTL_MS + 1), "ttl_ms must be"),
        (lambda f: f.withdraw(5, ttl_ms=60_000.0), "ttl_ms must be"),
        (lambda f: f.withdraw(5, ttl_ms=True), "ttl_ms must be"),
    ],
)
def test_arguments_are_checked_before_anything_is_read(exchange, trading_client, call, message):
    with pytest.raises(loaf.LoafValidationError, match=re.escape(message)) as info:
        call(trading_client().funds)
    assert info.value.status_code == 0
    assert exchange.requests == []


def test_an_amount_is_whole_ticks_of_the_served_collateral(exchange, trading_client):
    funds = trading_client().funds
    most = Decimal("90071992547409.91")  # 2^53 - 1 ticks of 0.01 USDC
    for amount in (Decimal("0.01"), 7, 1.5, most):
        funds.withdraw(amount)
    amounts = [body["amount"] for body in exchange.bodies("/l2/withdrawals")]
    assert amounts == ["1", "700", "150", str(2**53 - 1)]
    # Not a whole number of ticks, or past a withdrawal's range: refused once the collateral is
    # read, and nothing is signed or sent (nothing is rounded).
    for amount, text in (
        (Decimal("12.345"), "amount must be a whole number of ticks of 0.01 USDC"),
        (0.001, "amount must be from 0.01 USDC (one tick) to 90,071,992,547,409.91 USDC"),
        (most + Decimal("0.01"), "(2^53 - 1 ticks)"),
    ):
        with pytest.raises(loaf.LoafValidationError, match=re.escape(text)) as info:
            funds.withdraw(amount)
        assert info.value.status_code == 0
    assert len(exchange.bodies("/l2/withdrawals")) == 4
    assert len(exchange.gets("/l2/keys")) == 4
    assert len(exchange.gets("/portfolio/summary")) == 1


def test_an_amount_is_counted_in_the_collateral_served(exchange, trading_client):
    # 18 decimals and a tick of 10^12 base units: what the reply says, never an assumed USDC 6.
    exchange.perps_collateral = dict(PERPS_COLLATERAL, decimals=18, tickSize=str(10**12))
    trading_client().funds.withdraw(Decimal("0.000003"))
    (body,) = exchange.bodies("/l2/withdrawals")
    assert body["amount"] == "3"
    verify_op(body)


def test_an_unreadable_collateral_signs_nothing(exchange, trading_client):
    exchange.perps_collateral = dict(PERPS_COLLATERAL, tickSize="0")
    with pytest.raises(loaf.LoafServerError, match="answered no usable collateral"):
        trading_client().funds.withdraw(5)
    assert routes(exchange.requests) == ["GET /portfolio/summary"]


# --------------------------------------------------------------------------- #
# The trading key
# --------------------------------------------------------------------------- #


def test_a_withdrawal_needs_the_trading_key(exchange, trading_client):
    funds = trading_client(trading_key=None).funds
    with pytest.raises(loaf.LoafConfigError, match="Withdrawing needs your trading key"):
        funds.withdraw(5)
    signed = {key: 0 for key in WITHDRAW_KEYS} | {"amount": "1", "l2SigS": "a", "l2SigE": "b"}
    with pytest.raises(loaf.LoafConfigError, match="needs your trading key"):
        funds.resubmit(signed)  # its login token needs the key
    assert exchange.requests == []


@pytest.mark.parametrize(
    "key_in_force, reason",
    [("0x" + KAT["keys"][1]["pk"], BAD_SIGNATURE), (None, UNKNOWN_KEY), ("0x" + "0" * 80, KEY_DISABLED)],
)
def test_a_withdrawal_is_never_signed_while_the_trading_key_is_not_in_force(
    exchange, trading_client, key_in_force, reason
):
    # Reset in the web app, never enabled, or disabled: the nonce read's login token is refused
    # before anything is signed.
    exchange.key_in_force = key_in_force
    with pytest.raises(loaf.LoafAuthError, match=re.escape(reason)) as info:
        trading_client().funds.withdraw(5)
    assert "is not the trading key in force" in str(info.value)
    assert withdrawal_posts(exchange) == []


def test_a_withdrawal_is_never_signed_with_another_deployments_key(exchange, trading_client):
    client = trading_client(trading_key=f"loaf_tk_4243_{ACCOUNT_INDEX}_{L2_SECRET}")
    with pytest.raises(loaf.LoafAuthError, match=re.escape(WRONG_CHAIN)):
        client.funds.withdraw(5)
    assert withdrawal_posts(exchange) == []


def test_no_credential_is_left_in_a_traceback_local(exchange, trading_client):
    funds = trading_client().funds
    exchange.queue("/l2/withdrawals", error_reply(401, BAD_SIGNATURE))
    with pytest.raises(loaf.LoafAuthError) as refused:
        funds.withdraw(5)
    exchange.queue("/l2/withdrawals", *(httpx.ReadTimeout("slow") for _ in range(4)))
    with pytest.raises(WithdrawalOutcomeUnknownError) as unknown:
        funds.withdraw(5)
    sent = [bearer(r) for r in exchange.requests]
    for error in (refused.value, unknown.value):
        assert secret_locals(error, L2_SECRET, *sent) == []
    # The unknown outcome's message names the account, nonce and expiry, never the signatures.
    body = unknown.value.signed_body
    assert body["l2SigS"] not in str(unknown.value) and body["l2SigE"] not in str(unknown.value)


# --------------------------------------------------------------------------- #
# The exchange's answers
# --------------------------------------------------------------------------- #


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
    funds = trading_client().funds
    with pytest.raises(loaf.LoafAPIError) as info:
        funds.withdraw(5)
    assert (info.value.status_code, info.value.message) == (422, NONCE_REFUSAL)
    assert not isinstance(info.value, WithdrawalOutcomeUnknownError)
    assert funds.withdraw(5)["nonce"] == 1


@pytest.mark.parametrize(
    "reply, cls",
    [
        (error_reply(422, BELOW_MINIMUM), loaf.LoafAPIError),
        (error_reply(403, NOT_YOURS), loaf.LoafForbiddenError),
        (error_reply(400, EXPIRY_INSIDE_BUFFER), loaf.LoafValidationError),
    ],
    ids=["422-rules", "403-not-yours", "400-expiry"],
)
def test_a_4xx_is_final_and_never_resent(exchange, trading_client, reply, cls):
    exchange.queue("/l2/withdrawals", reply)
    with pytest.raises(loaf.LoafAPIError) as info:
        trading_client().funds.withdraw(5)
    assert type(info.value) is cls
    assert len(withdrawal_posts(exchange)) == 1
    assert exchange.applied == {}


def test_a_lost_answer_is_resent_and_answered_as_its_duplicate(
    exchange, trading_client, monkeypatch
):
    serve = exchange._serve
    lost: list[httpx.Request] = []

    def lose_the_first_answer(request: httpx.Request) -> httpx.Response:
        reply = serve(request)  # journaled ...
        if request.url.path == "/api/l2/withdrawals" and not lost:
            lost.append(request)
            raise httpx.ReadTimeout("timed out")  # ... but the answer never arrives
        return reply

    monkeypatch.setattr(exchange, "_serve", lose_the_first_answer)
    answer = trading_client().funds.withdraw(5)
    assert (answer["journalSeq"], answer["duplicate"]) == (1001, True)
    first, second = (r.content for r in withdrawal_posts(exchange))
    assert first == second  # the same signed bytes
    assert exchange.keys["withdrawalNonce"] == 1  # journaled once


@pytest.mark.parametrize(
    "reply, cls",
    [
        (error_reply(503, PAUSED, "TRADING_PAUSED", headers={"Retry-After": "7"}), loaf.TradingPausedError),
        (error_reply(503, ACCOUNT_UNREAD), loaf.LoafServiceUnavailableError),
    ],
    ids=["paused", "account-unread"],
)
def test_a_503_that_journaled_nothing_is_final(exchange, trading_client, reply, cls):
    funds = trading_client(max_retries=1).funds
    exchange.queue("/l2/withdrawals", reply, reply)
    with pytest.raises(loaf.LoafServiceUnavailableError) as info:
        funds.withdraw(5)
    assert type(info.value) is cls
    first, second = (r.content for r in withdrawal_posts(exchange))
    assert first == second
    if isinstance(info.value, loaf.TradingPausedError):
        assert info.value.retry_after == 7.0
    # Never journaled, so its nonce is unspent: the next withdrawal signs it.
    funds.withdraw(5)
    assert [body["l2Nonce"] for body in exchange.bodies("/l2/withdrawals")] == [0, 0, 0]


def test_an_undecided_withdrawal_is_an_unknown_outcome_that_resubmit_settles(
    exchange, trading_client
):
    funds = trading_client(max_retries=1).funds
    exchange.queue("/l2/withdrawals", error_reply(503, UNDECIDED), httpx.ReadTimeout("timed out"))
    with pytest.raises(WithdrawalOutcomeUnknownError) as info:
        funds.withdraw(5)
    error = info.value
    assert error.attempts == 2
    assert type(error.last_error) is loaf.LoafConnectionError
    assert error.__cause__ is error.last_error
    sent = [r.content for r in withdrawal_posts(exchange)]
    assert len(sent) == 2 and sent[0] == sent[1]
    assert json.dumps(error.signed_body, separators=(",", ":")).encode() == sent[0]
    # Neither attempt was journaled: a resubmit journals it now, and a second is its duplicate. It
    # signs nothing new: a fresh client re-sends it with a login token alone.
    reads = len(exchange.gets("/l2/keys"))
    resend = trading_client().funds.resubmit
    assert resend(error.signed_body)["duplicate"] is False
    assert resend(error.signed_body)["duplicate"] is True
    assert [r.content for r in withdrawal_posts(exchange)][2:] == sent
    assert len(exchange.gets("/l2/keys")) == reads
    assert exchange.keys["withdrawalNonce"] == 1


def test_a_resubmit_past_the_exchanges_window_stays_unknown_and_the_nonce_settles_it(
    exchange, trading_client, l2_clock
):
    l2_clock.time_ns = lambda: NOW_MS * 1_000_000
    exchange.now_ms = NOW_MS
    funds = trading_client(max_retries=0).funds
    exchange.queue("/l2/withdrawals", httpx.ReadTimeout("timed out"))
    with pytest.raises(WithdrawalOutcomeUnknownError) as info:
        funds.withdraw(5, ttl_ms=MIN_ORDER_TTL_MS)
    body = info.value.signed_body
    # A minute later (the exchange's clock), it is refused for its expiry: still unknown, since
    # the first attempt may have been journaled ...
    later = NOW_MS + MIN_ORDER_TTL_MS + 1
    l2_clock.time_ns = lambda: later * 1_000_000
    exchange.now_ms = later
    with pytest.raises(WithdrawalOutcomeUnknownError) as again:
        funds.resubmit(body)
    assert again.value.signed_body == body
    last = again.value.last_error
    assert isinstance(last, loaf.LoafValidationError)
    assert (last.status_code, last.message) == (400, EXPIRY_INSIDE_BUFFER)
    # ... and the account's next nonce settles it: still its l2Nonce, never journaled.
    assert exchange.keys["withdrawalNonce"] == body["l2Nonce"] == 0


def test_a_200_without_a_journal_seq_is_unknown(exchange, trading_client):
    exchange.queue("/l2/withdrawals", httpx.Response(202, json={"status": "PENDING"}))
    with pytest.raises(WithdrawalOutcomeUnknownError) as info:
        trading_client().funds.withdraw(5)
    assert info.value.attempts == 1
    assert isinstance(info.value.last_error, loaf.LoafServerError)


SIGNED_WITHDRAWAL = {
    "l2ChainId": 4242,
    "l2AccountIndex": ACCOUNT_INDEX,
    "l2ApiKeyIndex": 0,
    "l2AssetId": 3,
    "l2Route": 1,
    "amount": "500",
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
        {k: v for k, v in SIGNED_WITHDRAWAL.items() if k != "l2SigE"},
        {k: v for k, v in SIGNED_WITHDRAWAL.items() if k != "l2Route"},
        {**SIGNED_WITHDRAWAL, "amount": 500},
        {**SIGNED_WITHDRAWAL, "l2Nonce": "0"},
        {**SIGNED_WITHDRAWAL, "l2ExpiryMs": True},
        {**SIGNED_WITHDRAWAL, "l2SigS": None},
    ],
)
def test_resubmit_needs_a_signed_withdrawal(exchange, trading_client, body: Any):
    with pytest.raises(
        loaf.LoafValidationError, match="resubmit needs a signed withdrawal body"
    ) as info:
        trading_client().funds.resubmit(body)
    assert info.value.status_code == 0
    assert exchange.requests == []


def test_the_unknown_outcome_survives_pickle_and_names_how_to_settle_it():
    error = WithdrawalOutcomeUnknownError(
        signed_body=dict(SIGNED_WITHDRAWAL),
        attempts=3,
        last_error=loaf.LoafServerError("x", status_code=502),
    )
    text = str(error)
    for part in (
        f"account {ACCOUNT_INDEX} at nonce 0",
        "after 3 attempt(s)",
        "funds.resubmit(error.signed_body)",
        f"l2ExpiryMs {NOW_MS} minus 72 hours",
        "withdrawalNonce): still 0",
    ):
        assert part in text
    for clone in (pickle.loads(pickle.dumps(error)), copy.copy(error), copy.deepcopy(error)):
        assert type(clone) is WithdrawalOutcomeUnknownError
        assert (clone.signed_body, clone.attempts, str(clone)) == (SIGNED_WITHDRAWAL, 3, text)

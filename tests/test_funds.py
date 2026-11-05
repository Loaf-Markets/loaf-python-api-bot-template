"""Withdrawals against an in-memory exchange: the signed body (its terms, its
signature, the nonce perps ops spend too, the expiry), the amount in the
collateral's ticks, what is refused locally, the retries, resubmit, and the
exchange's refusals as it sends them, each with its code and the error it raises.

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
    EXPIRED,
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
# The exchange's 503s for a withdrawal: paused (TRADING_PAUSED) and not processed
# (NOT_PROCESSED), which journaled nothing; its account unread before anything was sent on
# (SERVICE_UNAVAILABLE, "nothing was done"); undecided, its answer lost (SERVICE_UNAVAILABLE).
PAUSED = "Trading is paused. Your withdrawal was not submitted — please try again shortly."
NOT_PROCESSED = (
    "Trading service is temporarily unavailable. Your withdrawal was not submitted — please send "
    "it again in a moment."
)
ACCOUNT_UNREAD = (
    "l2 account: the trading account could not be read; nothing was done, try again in a moment"
)
UNDECIDED = "the engine did not decide the withdrawal; resend the same signed body"
NOT_YOURS = "l2 account: the account is not this user's"
KEY_HELD = "l2 money: the idempotency key already names another movement"
ABOVE_BALANCE = "l2 money: the withdrawal is above the account's available L2 balance"
IN_FLIGHT = (
    "l2 money: the account has 16 L2 withdrawals journaled and not yet in an executed batch, the "
    "cap per account; try again once the chain executes them"
)
# The per-account request limit's 429, before the exchange's rules.
TOO_MANY_REQUESTS = "Too many requests for this account. Please slow down."
OTHER_CHAIN = "l2 chain id: not the engine's L2 chain"
BELOW_MINIMUM = "l2 money: the amount is below the asset's minimum withdrawal"
# Every 401 INVALID_SIGNATURE the exchange answers a withdrawal whose login token it took: the
# withdrawal's own signature, or the key it was signed with, refused.
SIGNATURE_REFUSALS = [
    BAD_SIGNATURE,
    "l2 signature: a signature scalar is not below the ECgFp5 group order",
    "l2 signature: the api key has no usable public key",
    "l2 key: no L1 change-pubkey is journaled for the account's api_key_index",
    "l2 money: the signing key is not the key the journal set for the api key",
    UNKNOWN_KEY,
    KEY_DISABLED,
]
# Every 422 by the exchange's rules, with its code: nothing was journaled.
RULE_REFUSALS = [
    (NONCE_REFUSAL, "ORDER_INDEX_USED"),
    (KEY_HELD, "ORDER_INDEX_USED"),
    (ABOVE_BALANCE, "INSUFFICIENT_BALANCE"),
    ("l2 money: the debited account's balance is below the amount", "INSUFFICIENT_BALANCE"),
    (BELOW_MINIMUM, "BELOW_MIN_ORDER_VALUE"),
    (IN_FLIGHT, "RATE_LIMITED"),
    (
        "l2 money: 1024 L2 withdrawals are journaled and not yet in an executed batch, the "
        "engine's cap; try again once the chain executes them",
        "RATE_LIMITED",
    ),
    (EXPIRY_INSIDE_BUFFER, "ORDER_EXPIRY_INVALID"),
    ("l2 money: the withdrawn asset is not listed (its leaf is empty)", "INVALID_REQUEST"),
    (
        "l2 money: the withdrawn asset's minimum withdrawal is unknown to the engine",
        "UNKNOWN_REASON",
    ),
]


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
    exchange.queue("/l2/withdrawals", error_reply(422, BELOW_MINIMUM, "BELOW_MIN_ORDER_VALUE"))
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
    exchange.queue("/l2/withdrawals", error_reply(401, BAD_SIGNATURE, "INVALID_SIGNATURE"))
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
    assert (info.value.status_code, info.value.code, info.value.message) == (
        422,
        "ORDER_INDEX_USED",  # a withdrawal's number is its nonce
        NONCE_REFUSAL,
    )
    assert not isinstance(info.value, WithdrawalOutcomeUnknownError)
    assert funds.withdraw(5)["nonce"] == 1


@pytest.mark.parametrize(
    "reply, cls",
    [
        (error_reply(403, NOT_YOURS, "ORDER_NOT_OWNED"), loaf.LoafForbiddenError),
        (error_reply(400, EXPIRY_INSIDE_BUFFER, "ORDER_EXPIRY_INVALID"), loaf.LoafValidationError),
        (error_reply(400, OTHER_CHAIN, "INVALID_REQUEST"), loaf.LoafValidationError),
        (
            httpx.Response(
                400,
                json={
                    "error": "Validation failed",
                    "code": "INVALID_REQUEST",
                    "details": ["l2Route: l2 withdrawal: l2Route must be 1 (spot)"],
                },
            ),
            loaf.LoafValidationError,
        ),
        (
            error_reply(429, TOO_MANY_REQUESTS, "RATE_LIMITED", headers={"Retry-After": "3"}),
            loaf.LoafRateLimitError,
        ),
    ],
    ids=["403-not-yours", "400-expiry", "400-chain", "400-body", "429-rate"],
)
def test_a_4xx_is_final_and_never_resent(exchange, trading_client, reply, cls):
    exchange.queue("/l2/withdrawals", reply)
    with pytest.raises(loaf.LoafAPIError) as info:
        trading_client().funds.withdraw(5)
    assert type(info.value) is cls
    sent = reply.json()
    assert (info.value.code, info.value.message) == (sent["code"], sent["error"])
    assert info.value.details == sent.get("details", [])
    if isinstance(info.value, loaf.LoafRateLimitError):
        assert info.value.retry_after == 3.0
    assert len(withdrawal_posts(exchange)) == 1
    assert exchange.applied == {}


@pytest.mark.parametrize(
    "message, code", RULE_REFUSALS, ids=[f"{code}-{i}" for i, (_, code) in enumerate(RULE_REFUSALS)]
)
def test_a_refusal_by_the_exchanges_rules_is_a_422_with_its_code(
    exchange, trading_client, message, code
):
    # Refused by name: a plain LoafAPIError with the exchange's code and text, never re-sent.
    # Nothing was journaled and the nonce did not move, so the next withdrawal signs it again.
    funds = trading_client().funds
    exchange.queue("/l2/withdrawals", error_reply(422, message, code))
    with pytest.raises(loaf.LoafAPIError) as info:
        funds.withdraw(5)
    error = info.value
    assert type(error) is loaf.LoafAPIError
    assert (error.status_code, error.code, error.message) == (422, code, message)
    assert code in {known.value for known in loaf.OrderErrorCode}
    assert len(withdrawal_posts(exchange)) == 1
    assert funds.withdraw(5)["nonce"] == 0
    assert [body["l2Nonce"] for body in exchange.bodies("/l2/withdrawals")] == [0, 0]


@pytest.mark.parametrize("reason", SIGNATURE_REFUSALS)
def test_a_signature_or_key_refusal_names_the_trading_key_not_in_force(
    exchange, trading_client, reason
):
    # Its login token taken, the withdrawal's own signature or key is refused: 401
    # INVALID_SIGNATURE, as an order's, whatever the text. The message says what it means; the
    # exchange's own body stays in body. Never re-sent, nothing journaled.
    exchange.queue("/l2/withdrawals", error_reply(401, reason, "INVALID_SIGNATURE"))
    with pytest.raises(loaf.LoafAuthError) as info:
        trading_client().funds.withdraw(5)
    error = info.value
    assert (error.status_code, error.code) == (401, "INVALID_SIGNATURE")
    assert error.message.startswith(f"{reason} — ")
    assert "is not the trading key in force on its account here" in error.message
    assert error.body == {"error": reason, "code": "INVALID_SIGNATURE"}
    assert len(withdrawal_posts(exchange)) == 1
    assert exchange.applied == {}


@pytest.mark.parametrize(
    "change, reason, meaning, not_meant",
    [
        # This machine's clock fell 11 minutes behind the exchange's: the token has expired.
        (
            {"now_ms": NOW_MS + 660_000},
            EXPIRED,
            "this machine's clock is off",
            "trading key in force",
        ),
        # The trading key was reset in the web app: the token's signature is not the new key's.
        (
            {"key_in_force": "0x" + KAT["keys"][1]["pk"]},
            BAD_SIGNATURE,
            "is not the trading key in force",
            "clock",
        ),
    ],
    ids=["clock", "key-reset"],
)
def test_a_refused_login_token_of_the_withdrawal_says_what_it_means(
    exchange, trading_client, l2_clock, monkeypatch, change, reason, meaning, not_meant
):
    # The nonce read's login token is taken, the withdrawal's own refused: 401 UNAUTHENTICATED,
    # as the exchange codes every refused login token, and what it means follows its text. Never
    # re-sent, nothing journaled.
    l2_clock.time_ns = lambda: NOW_MS * 1_000_000
    serve = exchange._serve

    def then_change(request: httpx.Request) -> httpx.Response:
        reply = serve(request)
        if request.url.path == "/api/l2/keys":
            for name, value in change.items():
                setattr(exchange, name, value)
        return reply

    monkeypatch.setattr(exchange, "_serve", then_change)
    with pytest.raises(loaf.LoafAuthError) as info:
        trading_client().funds.withdraw(5)
    error = info.value
    assert (error.status_code, error.code) == (401, "UNAUTHENTICATED")
    assert error.message.startswith(f"{reason} — ")
    assert meaning in error.message and not_meant not in error.message
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
        (error_reply(503, NOT_PROCESSED, "NOT_PROCESSED"), loaf.LoafServiceUnavailableError),
        (error_reply(503, ACCOUNT_UNREAD, "SERVICE_UNAVAILABLE"), loaf.LoafServiceUnavailableError),
    ],
    ids=["paused", "not-processed", "account-unread"],
)
def test_a_503_that_journaled_nothing_is_final(exchange, trading_client, reply, cls):
    # Re-sent as any 5xx is, but each answer says nothing was journaled: the last one is raised,
    # a definite refusal, never an unknown outcome.
    funds = trading_client(max_retries=1).funds
    exchange.queue("/l2/withdrawals", reply, reply)
    with pytest.raises(loaf.LoafServiceUnavailableError) as info:
        funds.withdraw(5)
    assert type(info.value) is cls
    assert info.value.code == reply.json()["code"]
    first, second = (r.content for r in withdrawal_posts(exchange))
    assert first == second
    if isinstance(info.value, loaf.TradingPausedError):
        assert info.value.retry_after == 7.0
    # Never journaled, so its nonce is unspent: the next withdrawal signs it.
    funds.withdraw(5)
    assert [body["l2Nonce"] for body in exchange.bodies("/l2/withdrawals")] == [0, 0, 0]


@pytest.mark.parametrize("message", [NONCE_REFUSAL, KEY_HELD], ids=["nonce", "key-held"])
@pytest.mark.parametrize(
    "first",
    [
        lambda: httpx.ReadTimeout("timed out"),
        lambda: error_reply(503, UNDECIDED, "SERVICE_UNAVAILABLE"),
    ],
    ids=["timeout", "undecided"],
)
def test_a_resend_refused_order_index_used_after_a_lost_answer_was_never_journaled(
    exchange, trading_client, first, message
):
    # The first attempt's answer was lost, but the exchange answers a re-send of a journaled
    # withdrawal as its duplicate before it checks the nonce: a 422 ORDER_INDEX_USED to the re-send
    # (its nonce spent by another op, or its key held by another withdrawal) says no attempt was
    # journaled, and none will be. It is raised as itself, a definite refusal: withdraw again.
    exchange.queue("/l2/withdrawals", first(), error_reply(422, message, "ORDER_INDEX_USED"))
    with pytest.raises(loaf.LoafAPIError) as info:
        trading_client(max_retries=1).funds.withdraw(5)
    error = info.value
    assert type(error) is loaf.LoafAPIError
    assert (error.status_code, error.code, error.message) == (422, "ORDER_INDEX_USED", message)
    sent, resent = (r.content for r in withdrawal_posts(exchange))
    assert sent == resent
    assert exchange.applied == {}


@pytest.mark.parametrize(
    "refusal, cls",
    [
        (error_reply(422, ABOVE_BALANCE, "INSUFFICIENT_BALANCE"), loaf.LoafAPIError),
        (error_reply(422, IN_FLIGHT, "RATE_LIMITED"), loaf.LoafAPIError),
        (error_reply(429, TOO_MANY_REQUESTS, "RATE_LIMITED"), loaf.LoafRateLimitError),
    ],
    ids=["balance", "in-flight", "429"],
)
def test_any_other_refusal_of_a_resend_after_a_lost_answer_is_still_unknown(
    exchange, trading_client, refusal, cls
):
    # These speak for the re-send alone: the first attempt, still on its way, could be journaled
    # after it (a spent nonce could not).
    exchange.queue("/l2/withdrawals", httpx.ReadTimeout("timed out"), refusal)
    with pytest.raises(WithdrawalOutcomeUnknownError) as info:
        trading_client(max_retries=1).funds.withdraw(5)
    last = info.value.last_error
    assert type(last) is cls
    assert (last.code, info.value.attempts) == (refusal.json()["code"], 2)


def test_not_processed_after_a_lost_answer_is_still_unknown(exchange, trading_client):
    # The first attempt's answer was lost, so it may have been journaled: a NOT_PROCESSED to the
    # re-send speaks for the re-send alone.
    funds = trading_client(max_retries=1).funds
    exchange.queue(
        "/l2/withdrawals",
        httpx.ReadTimeout("timed out"),
        error_reply(503, NOT_PROCESSED, "NOT_PROCESSED"),
    )
    with pytest.raises(WithdrawalOutcomeUnknownError) as info:
        funds.withdraw(5)
    last = info.value.last_error
    assert type(last) is loaf.LoafServiceUnavailableError
    assert (last.code, info.value.attempts) == ("NOT_PROCESSED", 2)


def test_an_undecided_withdrawal_is_an_unknown_outcome_that_resubmit_settles(
    exchange, trading_client
):
    funds = trading_client(max_retries=1).funds
    exchange.queue(
        "/l2/withdrawals",
        error_reply(503, UNDECIDED, "SERVICE_UNAVAILABLE"),
        httpx.ReadTimeout("timed out"),
    )
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
    assert (last.status_code, last.code, last.message) == (
        400,
        "ORDER_EXPIRY_INVALID",
        EXPIRY_INSIDE_BUFFER,
    )
    # ... and the account's next nonce settles it: still its l2Nonce, never journaled.
    assert exchange.keys["withdrawalNonce"] == body["l2Nonce"] == 0


def test_a_resubmit_refused_order_index_used_settles_it_and_the_next_signs_the_new_nonce(
    exchange, trading_client
):
    funds = trading_client(max_retries=0).funds
    exchange.queue("/l2/withdrawals", httpx.ReadTimeout("timed out"))
    with pytest.raises(WithdrawalOutcomeUnknownError) as info:
        funds.withdraw(5)
    body = info.value.signed_body
    exchange.keys["withdrawalNonce"] = 1  # another op (the web app's, say) spent nonce 0 since
    # A journaled body would have been answered as its duplicate: this one never was, nor will be.
    with pytest.raises(loaf.LoafAPIError) as refused:
        funds.resubmit(body)
    error = refused.value
    assert type(error) is loaf.LoafAPIError
    assert (error.status_code, error.code) == (422, "ORDER_INDEX_USED")
    assert error.message == NONCE_REFUSAL
    assert exchange.applied == {}
    assert funds.withdraw(5)["nonce"] == 1


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
        "a 422 ORDER_INDEX_USED (never journaled: withdraw again)",
        f"l2ExpiryMs {NOW_MS} minus 72 hours",
        "withdrawalNonce): still 0",
    ):
        assert part in text
    for clone in (pickle.loads(pickle.dumps(error)), copy.copy(error), copy.deepcopy(error)):
        assert type(clone) is WithdrawalOutcomeUnknownError
        assert (clone.signed_body, clone.attempts, str(clone)) == (SIGNED_WITHDRAWAL, 3, text)

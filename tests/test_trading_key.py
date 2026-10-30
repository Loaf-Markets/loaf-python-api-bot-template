"""The trading key and the login tokens it signs, offline: the one string the web
app's Show trading key gives, what the client refuses and never keeps in a
traceback, the login tokens (their life, the fresh one at half-life, the clock
skew they bear, one signature for concurrent callers, the one a refusal drops),
the token each request attempt carries, and what a refused token's 401 says.

The exchange is ``conftest.FakeExchange``, which checks each login token as the
backend does.
"""

from __future__ import annotations

import copy
import json
import logging
import pickle
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

import loaf
from loaf import LoafClient, k1
from loaf._l2 import LoginTokens, TradingKey

from conftest import (
    BAD_SIGNATURE,
    BASE,
    EXPIRED,
    KAT,
    KEY_DISABLED,
    L2_PUBLIC_KEY,
    L2_SECRET,
    LOGIN_TOKEN_RE,
    TOO_LONG,
    TRADING_KEY,
    UNKNOWN_ACCOUNT,
    UNKNOWN_KEY,
    WRONG_CHAIN,
    FakeExchange,
    freeze,
    login_refusal,
    secret_locals,
    verify_login,
)

LOGIN_KAT = json.loads(Path(__file__).with_name("k1-login.json").read_text())
# K1's group order n: 40 bytes little-endian, not a secret key.
N = 0x7FFFFFFD80000007_7FFFFFF100000016_7FFFFFE6CFB80639_E8885C39D724A09C_E80FD996948BFFE1
# Another trading key (K1's second known-answer key): in force on the account once yours was reset.
OTHER_KEY = "0x" + KAT["keys"][1]["pk"]
BAD_KEY = (
    "LOAF_TRADING_KEY (or trading_key=) is not a trading key from the Loaf web app: expected the "
    "one string its Show trading key gives, loaf_tk_<chain>_<account>_<80 hex>, whole and "
    "unchanged."
)
# What a refused login token's 401 says after the server's text.
NOT_IN_FORCE = (
    "LOAF_TRADING_KEY is not the trading key in force on its account here: it was reset in the web "
    "app, the account's key is not active yet (enable trading on a market's Trade page and wait "
    "for it to take effect), or it is another Loaf deployment's key (check LOAF_API_BASE_URL). Set "
    "LOAF_TRADING_KEY to the web app's Show trading key (README §2) and restart; after a reset, "
    "also cancel and re-arm your stops and takes."
)
CLOCK_OFF = (
    "this machine's clock is off: the exchange takes a login token only before its expiry and at "
    "most 15 minutes ahead of its own clock, and the SDK signs ones that live 10 minutes from this "
    "machine's, so the two clocks must agree within about 5 minutes. Synchronize the clock (NTP)."
)
PRIVY_ONLY = (
    "l2 login: this route needs a web-app login; a trading-key login token works only on trading "
    "routes"
)
MINUTE_MS = 60_000


def bearers(exchange: FakeExchange, path: str) -> list[str]:
    """The login token each request to ``path`` carried, in order."""
    return [
        r.headers["authorization"][len("Bearer ") :]
        for r in exchange.requests
        if r.url.path == "/api" + path
    ]


# --------------------------------------------------------------------------- #
# The trading key
# --------------------------------------------------------------------------- #


def test_the_trading_key_from_the_argument_or_the_environment(monkeypatch):
    key = LoafClient(TRADING_KEY, base_url=BASE)._l2_key
    assert key is not None
    assert (key.public_key, key.l2_chain_id, key.l2_account_index) == (L2_PUBLIC_KEY, 4242, 5)
    assert repr(key) == f"TradingKey(public_key={L2_PUBLIC_KEY!r}, l2_account_index=5)"

    # Pasted with surrounding whitespace (a .env value, a file), it is read stripped.
    monkeypatch.setenv("LOAF_TRADING_KEY", f"  {TRADING_KEY}\n")
    with LoafClient(base_url=BASE) as client:
        assert client.public_key == L2_PUBLIC_KEY
        assert repr(client) == (
            f"<LoafClient base_url='{BASE}' authenticated public_key={L2_PUBLIC_KEY} "
            f"v{loaf.__version__}>"
        )

    monkeypatch.setenv("LOAF_TRADING_KEY", "  ")  # blank counts as unset
    with LoafClient(base_url=BASE) as client:
        assert client.public_key is None and client._login_tokens is None
        assert repr(client) == f"<LoafClient base_url='{BASE}' anonymous v{loaf.__version__}>"


@pytest.mark.parametrize(
    "chain_id, account_index", [(1, 0), (2_130_706_432, 2**48 - 1), (4242, 9_007_199_254_740)]
)
def test_any_chain_id_and_account_the_messages_carry_are_taken(chain_id, account_index):
    key = TradingKey(f"loaf_tk_{chain_id}_{account_index}_{L2_SECRET}")
    assert (key.l2_chain_id, key.l2_account_index) == (chain_id, account_index)


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param(L2_SECRET, id="the bare 80 hex of an SDK before 0.5.0"),
        pytest.param("0x" + L2_SECRET, id="0x + 80 hex"),
        pytest.param(f"loaf_tk_4242_5_0x{L2_SECRET}", id="0x secret"),
        pytest.param(f"loaf_tk_4242_5_{L2_SECRET.upper()}", id="uppercase hex"),
        pytest.param(f"loaf_tk_4242_5_{L2_SECRET[:-1]}", id="79 hex"),
        pytest.param(f"loaf_tk_4242_5_{L2_SECRET}0", id="81 hex"),
        pytest.param(f"loaf_tk_4242_5_{L2_SECRET[:-1]}g", id="not hex"),
        pytest.param(f"loaf_tk_4242_5_{L2_SECRET[:40]} {L2_SECRET[40:]}", id="a space inside"),
        pytest.param(f"loaf_tk_4242_5_{L2_SECRET[:40]}\n{L2_SECRET[40:]}", id="a line inside"),
        pytest.param(f"LOAF_TK_4242_5_{L2_SECRET}", id="uppercase prefix"),
        pytest.param(f"loaf_tk_4242__5_{L2_SECRET}", id="doubled separator"),
        pytest.param(f"loaf_tk_0_5_{L2_SECRET}", id="chain 0"),
        pytest.param(f"loaf_tk_04242_5_{L2_SECRET}", id="chain leading zero"),
        pytest.param(f"loaf_tk_2130706433_5_{L2_SECRET}", id="chain the KoalaBear modulus"),
        pytest.param(f"loaf_tk_12345678901_5_{L2_SECRET}", id="chain of 11 digits"),
        pytest.param(f"loaf_tk_4242_05_{L2_SECRET}", id="account leading zero"),
        pytest.param(f"loaf_tk_4242_-5_{L2_SECRET}", id="account negative"),
        pytest.param(f"loaf_tk_4242_281474976710656_{L2_SECRET}", id="account 2^48"),
        pytest.param("loaf_tk_4242_5_" + "0" * 80, id="secret 0"),
        pytest.param("loaf_tk_4242_5_" + N.to_bytes(40, "little").hex(), id="secret n"),
        pytest.param(f'"{TRADING_KEY}"', id="quoted"),
        pytest.param(f"LOAF_TRADING_KEY={TRADING_KEY}", id="the whole .env line"),
        pytest.param(f"{TRADING_KEY} # mine", id="a comment"),
        pytest.param("loaf_lt_4242_5_1800000000000_" + "ab" * 80, id="a login token"),
    ],
)
def test_anything_else_is_refused_with_one_text(monkeypatch, bad):
    def refused(make: Any) -> None:
        with pytest.raises(loaf.LoafConfigError) as info:
            make()
        assert str(info.value) == BAD_KEY  # names nothing it was given
        assert info.value.__cause__ is None and info.value.__context__ is None

    refused(lambda: LoafClient(bad, base_url=BASE))
    monkeypatch.setenv("LOAF_TRADING_KEY", bad)
    refused(lambda: LoafClient(base_url=BASE))


def test_the_key_and_its_login_tokens_cannot_be_pickled_or_copied():
    # Serializers that would otherwise read the slots (jsonpickle) ask __getstate__ too.
    key = TradingKey(TRADING_KEY)
    for holder, text in ((key, "holds a secret key"), (LoginTokens(key), "holds a trading key")):
        for leak in (pickle.dumps, copy.copy, copy.deepcopy, lambda h: h.__getstate__()):
            with pytest.raises(TypeError, match=text):
                leak(holder)


def test_the_trading_key_is_in_no_traceback_local(monkeypatch):
    # Crash reporters and debuggers record every traceback frame's locals.
    later: list[tuple[type[BaseException], dict[str, Any]]] = [
        (ValueError, {"max_retries": "x"}),
    ]
    monkeypatch.setenv("LOAF_TRADING_KEY", TRADING_KEY)
    for failure, kwargs in later:
        with pytest.raises(failure) as info:
            LoafClient(base_url=BASE, **kwargs)
        assert secret_locals(info.value, L2_SECRET) == []
    monkeypatch.delenv("LOAF_TRADING_KEY")

    # Nor an explicit argument, whichever step of the constructor fails.
    earlier: list[tuple[type[BaseException], dict[str, Any]]] = [
        (loaf.LoafConfigError, {"max_slippage_bps": -1}),
    ]
    for failure, kwargs in earlier + later:
        with pytest.raises(failure) as info:
            LoafClient(TRADING_KEY, base_url=BASE, **kwargs)
        assert secret_locals(info.value, L2_SECRET) == []

    # Nearly the key itself, refused: one hex digit short, 80 hex that are no scalar below K1's
    # group order (the last byte is the most significant), or a chain id or an account out of
    # range around the key's very secret. The parse keeps no match: its string is the key.
    for near_miss, kept in (
        (TRADING_KEY[:-1], L2_SECRET[:-1]),
        (TRADING_KEY[:-2] + "ff", L2_SECRET[:-2]),
        (f"loaf_tk_2130706433_5_{L2_SECRET}", L2_SECRET),
        (f"loaf_tk_4242_281474976710656_{L2_SECRET}", L2_SECRET),
    ):
        monkeypatch.setenv("LOAF_TRADING_KEY", near_miss)
        with pytest.raises(loaf.LoafConfigError) as info:
            LoafClient(base_url=BASE)
        assert secret_locals(info.value, kept) == []
    monkeypatch.delenv("LOAF_TRADING_KEY")

    # The key as bytes (a secret file read in binary mode) is no trading key either.
    for raw in (TRADING_KEY.encode(), bytearray(TRADING_KEY.encode())):
        with pytest.raises(loaf.LoafConfigError, match="is not a trading key") as info:
            LoafClient(raw, base_url=BASE)  # type: ignore[arg-type]
        assert info.value.__context__ is None
        assert secret_locals(info.value, L2_SECRET) == []


def test_interrupted_key_setup_leaves_no_trace(monkeypatch):
    # A Ctrl-C while the first client computes its public key, when K1 builds its table.
    def interrupted() -> Any:
        raise KeyboardInterrupt

    monkeypatch.setattr(k1, "_generator_table", interrupted)
    with pytest.raises(KeyboardInterrupt) as info:
        LoafClient(TRADING_KEY, base_url=BASE)
    assert secret_locals(info.value, L2_SECRET) == []


def test_half_built_objects_still_repr():
    # Crash reporters repr every local of a failed constructor's frame.
    def selves(exc: BaseException) -> list[Any]:
        found = []
        tb = exc.__traceback__
        while tb is not None:
            if tb.tb_frame.f_code.co_name == "__init__":
                found.append(tb.tb_frame.f_locals["self"])
            tb = tb.tb_next
        return found

    with pytest.raises(ValueError) as failed:
        LoafClient(TRADING_KEY, base_url=BASE, max_retries="x")  # type: ignore[arg-type]
    (client,) = selves(failed.value)
    assert repr(client) == (
        f"<LoafClient base_url='{BASE}' authenticated public_key={L2_PUBLIC_KEY} "
        f"v{loaf.__version__}>"
    )
    with pytest.raises(loaf.LoafConfigError) as refused:
        LoafClient("loaf_tk_4242_5_" + "0" * 80, base_url=BASE)
    client, key = selves(refused.value)
    assert repr(client) == f"<LoafClient base_url=None anonymous v{loaf.__version__}>"
    assert repr(key) == "TradingKey(public_key=None, l2_account_index=None)"
    with pytest.raises(loaf.LoafConfigError) as refused:
        loaf.LoafWebSocketClient(ws_url="ws://test/ws", reconnect_delay=0)
    (ws,) = selves(refused.value)
    assert repr(ws) == "<LoafWebSocketClient 'ws://test/ws' idle channels=0>"


# --------------------------------------------------------------------------- #
# Login tokens
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("v", LOGIN_KAT["signatures"], ids=lambda v: v["aux"][:2])
def test_a_login_token_is_the_engines_known_answer(monkeypatch, v):
    # The key of a known answer signs its message, with its random bytes, into the token the
    # exchange reads it from: the fields and then s and e.
    chain_id, account_index, expiry_ms = v["l2ChainId"], v["l2AccountIndex"], v["l2ExpiryMs"]
    key = TradingKey(f"loaf_tk_{chain_id}_{account_index}_{v['sk']}")
    assert key.public_key == "0x" + v["pk"]
    monkeypatch.setattr("loaf._l2.os", SimpleNamespace(urandom=lambda n: bytes.fromhex(v["aux"])))
    token = key.login_token(expiry_ms)
    assert token == f"loaf_lt_{chain_id}_{account_index}_{expiry_ms}_{v['s']}{v['e']}"
    assert LOGIN_TOKEN_RE.fullmatch(token)


def test_a_login_token_lives_10_minutes_and_a_fresh_one_is_signed_at_half_life(l2_clock):
    tokens = LoginTokens(TradingKey(TRADING_KEY))
    now = freeze(l2_clock)
    first, renew_in = tokens.token_and_renewal()
    fields = {"l2ChainId": 4242, "l2AccountIndex": 5, "l2ExpiryMs": now + 600_000}
    assert verify_login(first) == fields
    assert renew_in == 300.0
    freeze(l2_clock, now + 299_999)  # more than half its life left: kept
    assert tokens.token_and_renewal() == (first, 0.001)
    freeze(l2_clock, now + 300_000)  # half of it left: a fresh one
    second, renew_in = tokens.token_and_renewal()
    assert verify_login(second)["l2ExpiryMs"] == now + 300_000 + 600_000
    assert renew_in == 300.0 and tokens.token() == second


@pytest.mark.parametrize(
    "fast_ms, at_last_use, reason",
    [
        (5 * MINUTE_MS, False, None),  # 5 minutes fast, a token just signed: 15 minutes ahead
        (5 * MINUTE_MS + 1, False, TOO_LONG),
        (-5 * MINUTE_MS, True, None),  # 5 slow, a token at its last use: 1 ms to its expiry
        (-5 * MINUTE_MS - 1, True, EXPIRED),
    ],
    ids=["5-min-fast", "more-fast", "5-min-slow", "more-slow"],
)
def test_this_machines_clock_may_be_5_minutes_off_either_way(
    l2_clock, fast_ms, at_last_use, reason
):
    # The exchange takes a login token before its expiry and at most 15 minutes ahead, by its own
    # clock; the SDK's lives 10 minutes by this machine's, and is used until 5 are left. A clock
    # that runs fast matters most to a token just signed, one that runs slow to one at its last
    # use, a millisecond before the SDK signs the next.
    tokens = LoginTokens(TradingKey(TRADING_KEY))
    now = freeze(l2_clock)
    token, renew_in = tokens.token_and_renewal()
    used_at = now + round(renew_in * 1000) - 1 if at_last_use else now
    freeze(l2_clock, used_at)
    assert tokens.token() == token
    assert login_refusal(token, L2_PUBLIC_KEY, used_at - fast_ms) == reason


def test_concurrent_callers_share_one_signature(monkeypatch):
    signed: list[int] = []
    sign = TradingKey.login_token

    def counted(key: TradingKey, expiry_ms: int) -> str:
        signed.append(expiry_ms)
        return sign(key, expiry_ms)

    monkeypatch.setattr(TradingKey, "login_token", counted)
    tokens = LoginTokens(TradingKey(TRADING_KEY))
    start = threading.Barrier(8)
    got: list[str] = []

    def take() -> None:
        start.wait()
        got.append(tokens.token())

    threads = [threading.Thread(target=take) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(got) == 8 and len(set(got)) == 1 and len(signed) == 1


def test_forget_drops_only_the_token_kept():
    tokens = LoginTokens(TradingKey(TRADING_KEY))
    first = tokens.token()
    tokens.forget("loaf_lt_4242_5_1_" + "ab" * 80)  # not the one kept
    assert tokens.token() == first
    tokens.forget(first)
    second = tokens.token()
    assert second != first
    tokens.forget(first)  # refused after a newer one was signed: the newer one stays
    assert tokens.token() == second


def test_requests_share_the_login_token(exchange, trading_client):
    # Signed once, then carried by every request while more than half its life is left.
    exchange.queue("/history/trades", *(httpx.Response(200, json={"trades": []}) for _ in range(3)))
    client = trading_client()
    for _ in range(3):
        client.history.trades()
    first, *rest = bearers(exchange, "/history/trades")
    assert rest == [first, first]
    verify_login(first)


def test_each_attempt_of_a_read_carries_its_own_login_token(
    exchange, trading_client, monkeypatch, l2_clock
):
    # A backoff can outlast the token's half-life: the retry carries a fresh one.
    now = freeze(l2_clock)
    monkeypatch.setattr(
        "loaf.client.time", SimpleNamespace(sleep=lambda seconds: freeze(l2_clock, now + 300_000))
    )
    exchange.queue("/history/orders", httpx.Response(503, json={"error": "busy"}),
                   httpx.Response(200, json={"orders": []}))
    trading_client().history.orders()
    first, second = (verify_login(t)["l2ExpiryMs"] for t in bearers(exchange, "/history/orders"))
    assert (first, second) == (now + 600_000, now + 300_000 + 600_000)


# --------------------------------------------------------------------------- #
# A refused login token: LoafAuthError, saying what to do, and a fresh token next
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "setup, reason, meaning",
    [
        ({"key_in_force": OTHER_KEY}, BAD_SIGNATURE, NOT_IN_FORCE),  # reset in the web app
        ({"key_in_force": None}, UNKNOWN_KEY, NOT_IN_FORCE),  # none in force yet
        ({"key_in_force": "0x" + "0" * 80}, KEY_DISABLED, NOT_IN_FORCE),  # disabled on L1
        ({"trading_key": f"loaf_tk_4242_6_{L2_SECRET}"}, UNKNOWN_ACCOUNT, NOT_IN_FORCE),
        ({"trading_key": f"loaf_tk_4243_5_{L2_SECRET}"}, WRONG_CHAIN, NOT_IN_FORCE),
        ({"clock": 11 * MINUTE_MS}, EXPIRED, CLOCK_OFF),  # this machine's clock 11 min behind
        ({"clock": -6 * MINUTE_MS}, TOO_LONG, CLOCK_OFF),  # or 6 min ahead
    ],
    ids=["reset", "no-key", "disabled", "another-account", "another-chain", "behind", "ahead"],
)
def test_a_refused_login_token_says_what_to_do(
    exchange, trading_client, l2_clock, setup, reason, meaning
):
    now = freeze(l2_clock)
    exchange.key_in_force = setup.get("key_in_force", L2_PUBLIC_KEY)
    if "clock" in setup:
        exchange.now_ms = now + setup["clock"]
    client = trading_client(trading_key=setup.get("trading_key", TRADING_KEY))
    for _ in range(2):
        with pytest.raises(loaf.LoafAuthError) as info:
            client.history.trades()
        error = info.value
        assert error.message == f"{reason} — {meaning}"
        assert (error.status_code, error.body) == (401, {"error": reason})
    # Each refused token is dropped: the next request signs a fresh one.
    first, second = bearers(exchange, "/history/trades")
    assert first != second


def test_after_a_reset_the_next_request_is_refused_though_its_token_was_taken_before(
    exchange, trading_client
):
    # The exchange checks each request's token against the key in force at that moment, so the
    # token it took a moment ago, under the old key, is refused at once.
    exchange.queue("/history/trades", httpx.Response(200, json={"trades": []}))
    client = trading_client()
    client.history.trades()
    exchange.key_in_force = OTHER_KEY  # reset in the web app
    with pytest.raises(loaf.LoafAuthError) as info:
        client.history.trades()
    assert info.value.message == f"{BAD_SIGNATURE} — {NOT_IN_FORCE}"
    taken, refused = bearers(exchange, "/history/trades")
    assert refused == taken


def test_another_401_keeps_its_text_and_drops_the_token(exchange, trading_client):
    # A route that needs a web-app login (requested by hand): nothing about the key to add.
    exchange.queue("/auth/profile", httpx.Response(401, json={"error": PRIVY_ONLY}))
    client = trading_client()
    with pytest.raises(loaf.LoafAuthError) as info:
        client.get("/auth/profile")
    assert info.value.message == PRIVY_ONLY
    client.get("/l2/keys")
    (refused,), (fresh,) = bearers(exchange, "/auth/profile"), bearers(exchange, "/l2/keys")
    assert fresh != refused


def test_a_login_token_is_in_no_error_repr_log_or_traceback_local(
    exchange, trading_client, caplog
):
    # It is a bearer credential: whoever holds it reads the account and cancels its orders until
    # it expires.
    caplog.set_level(logging.DEBUG)
    client = trading_client()
    exchange.key_in_force = OTHER_KEY  # refused: LoafAuthError
    with pytest.raises(loaf.LoafAuthError) as refused:
        client.history.orders()
    exchange.key_in_force = L2_PUBLIC_KEY
    exchange.queue("/history/orders", *(httpx.ConnectError("unreachable") for _ in range(4)))
    with pytest.raises(loaf.LoafConnectionError) as unreachable:
        client.history.orders()
    exchange.queue("/history/orders", httpx.Response(
        200, content=b"{", headers={"content-type": "application/json"}
    ))
    with pytest.raises(loaf.LoafServerError) as garbled:
        client.history.orders()
    sent = bearers(exchange, "/history/orders")
    assert len(set(sent)) == 2  # the refused one, then a fresh one
    for error in (refused.value, unreachable.value, garbled.value):
        assert not any(token in f"{error} {error!r} {error.args}" for token in sent)
        assert secret_locals(error, *sent) == []
    shown = f"{client!r} {client._login_tokens!r} {client.websocket()!r} {caplog.text}"
    assert not any(token in shown for token in sent)
    assert "Bearer" not in caplog.text

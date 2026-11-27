"""The rules a REST base URL and a WebSocket URL must meet at construction."""

from __future__ import annotations

import sys
import warnings
from types import SimpleNamespace
from typing import Any, cast

import pytest

from loaf import LoafClient, LoafConfigError, LoafWebSocketClient
from loaf._urls import shown
from loaf.ws.client import derive_ws_url

from conftest import TRADING_KEY


@pytest.mark.parametrize(
    ("base_url", "ws_url"),
    [
        ("http://localhost/api", "ws://localhost/ws"),
        ("http://localhost:8005/api", "ws://localhost:8005/ws"),
        ("http://127.0.0.1/api", "ws://127.0.0.1/ws"),
        ("http://127.1.2.3:8005/api/", "ws://127.1.2.3:8005/ws"),
        ("http://[::1]/api", "ws://[::1]/ws"),
        ("http://[::1]:8005/api", "ws://[::1]:8005/ws"),
    ],
)
def test_plain_http_to_this_machine_is_taken(base_url: str, ws_url: str) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        client = LoafClient(TRADING_KEY, base_url=base_url)
    assert client.base_url == base_url.rstrip("/")
    assert client.ws_url == ws_url
    assert client.websocket().ws_url == ws_url


@pytest.mark.parametrize(
    "base_url",
    [
        "http://api.example-not-loaf.test/api",
        "http://10.0.0.5:8005/api",
        "http://0.0.0.0/api",
        "http://localhost.example.test/api",
        "http://sub.localhost:8005/api",  # not every resolver keeps it on this machine
        "http://[::2]/api",
    ],
)
@pytest.mark.parametrize("trading_key", [TRADING_KEY, None])
def test_plain_http_elsewhere_is_refused(base_url: str, trading_key: str | None) -> None:
    with pytest.raises(LoafConfigError) as caught:
        LoafClient(trading_key, base_url=base_url)
    message = str(caught.value)
    assert message.startswith(f"base_url is {base_url!r}: plain http:// is taken only")
    assert "(localhost, 127.0.0.0/8, [::1])" in message
    assert "unencrypted" in message and "allow_http=True" in message


def test_allow_http_takes_plain_http_to_another_host() -> None:
    client = LoafClient(TRADING_KEY, base_url="http://sub.localhost:8005/api", allow_http=True)
    assert client.ws_url == "ws://sub.localhost:8005/ws"
    client = LoafClient(TRADING_KEY, base_url="http://dev.internal:8005/api", allow_http=True)
    assert client.base_url == "http://dev.internal:8005/api"
    assert client.ws_url == "ws://dev.internal:8005/ws"
    ws = client.websocket()  # the client's allow_http
    assert ws.ws_url == "ws://dev.internal:8005/ws" and ws._ssl is None
    with pytest.warns(UserWarning):  # another host than base_url's
        assert client.websocket(ws_url="ws://other.internal/ws").ws_url == "ws://other.internal/ws"
    with pytest.raises(LoafConfigError, match="^ws_url is 'ws://dev.internal/ws': plain ws://"):
        client.websocket(ws_url="ws://dev.internal/ws", allow_http=False)


def test_uppercase_scheme_and_host_are_lowercased() -> None:
    client = LoafClient(base_url="HTTPS://Api.Example.Test/api/")
    assert client.base_url == "https://api.example.test/api"
    assert LoafClient(base_url="https://host/V2/api").base_url == "https://host/V2/api"  # path kept
    assert client.ws_url == "wss://api.example.test/ws"
    assert client.websocket()._ssl is not None
    ws = LoafClient(base_url="HTTPS://Host/api", ws_url="WSS://HOST/ws").ws_url
    assert ws == "wss://host/ws"
    assert LoafClient(base_url="HTTP://LOCALHOST:8005/api").ws_url == "ws://localhost:8005/ws"
    assert derive_ws_url("HTTPS://Host/api") == "wss://host/ws"


@pytest.mark.parametrize(
    "base_url", ["host/api", "api.example.test", "localhost:8005/api", "//host/api"]
)
def test_missing_scheme_is_refused(base_url: str) -> None:
    with pytest.raises(LoafConfigError) as caught:
        LoafClient(base_url=base_url)
    assert str(caught.value) == (
        "base_url must start with https:// (or http:// to this machine), such as "
        f"https://host/api, got {base_url!r}"
    )


@pytest.mark.parametrize(
    "base_url", ["ftp://host/api", "ws://localhost/api", "wss://host/api", "file:///api"]
)
def test_unsupported_scheme_is_refused(base_url: str) -> None:
    with pytest.raises(LoafConfigError, match="^base_url must start with https://"):
        LoafClient(base_url=base_url)


@pytest.mark.parametrize("ws_url", ["https://host/ws", "http://localhost/ws", "host/ws"])
def test_ws_url_needs_a_websocket_scheme(ws_url: str) -> None:
    expected = "^ws_url must start with wss:// \\(or ws:// to this machine\\), such as wss://host"
    with pytest.raises(LoafConfigError, match=expected):
        LoafClient(ws_url=ws_url)
    with pytest.raises(LoafConfigError, match=expected):
        LoafWebSocketClient(ws_url=ws_url)


@pytest.mark.parametrize("base_url", ["https:///api", "https://:443/api", "https:host/api"])
def test_missing_host_is_refused(base_url: str) -> None:
    with pytest.raises(LoafConfigError, match=f"^base_url has no host: {base_url!r}$"):
        LoafClient(base_url=base_url)


@pytest.mark.parametrize(
    "base_url", ["https://host:http/api", "https://host:70000/api", "https://[::1/api"]
)
def test_malformed_authority_is_refused(base_url: str) -> None:
    with pytest.raises(LoafConfigError, match="^base_url is not a valid URL: "):
        LoafClient(base_url=base_url)


@pytest.mark.parametrize(
    ("base_url", "shown"),
    [
        ("https://alice:s3cret-pw@api.example.test/api", "https://***@api.example.test/api"),
        ("http://alice:s3cret-pw@localhost/api", "http://***@localhost/api"),
        ("https://alice@api.example.test/api", "https://***@api.example.test/api"),
        ("https://:s3cret-pw@api.example.test/api", "https://***@api.example.test/api"),
        ("https://alice:s3c@ret-pw@api.example.test/api", "https://***@api.example.test/api"),
        ("https://alice:s3cret-pw@:443/api", "https://***@:443/api"),
    ],
)
def test_credentials_in_a_url_are_refused(base_url: str, shown: str) -> None:
    with pytest.raises(LoafConfigError) as caught:
        LoafClient(base_url=base_url)
    assert str(caught.value) == (
        f"base_url carries credentials: {shown!r}. The SDK takes no user name or password in a "
        "URL: your trading key authenticates every request. Drop them."
    )


def test_credentials_in_a_url_are_refused_from_every_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = "wss://alice:s3cret-pw@feed.test/ws"
    for make, source in (
        (lambda: LoafClient(allow_http=True, ws_url=ws), "ws_url"),
        (lambda: LoafWebSocketClient(ws_url=ws), "ws_url"),
        (lambda: LoafWebSocketClient(LoafClient(), ws_url=ws), "ws_url"),
    ):
        with pytest.raises(LoafConfigError, match=f"^{source} carries credentials: "):
            make()
    monkeypatch.setenv("LOAF_WS_URL", ws)
    with pytest.raises(LoafConfigError, match="^\\$LOAF_WS_URL carries credentials: 'wss://\\*"):
        LoafClient()
    monkeypatch.delenv("LOAF_WS_URL")
    monkeypatch.setenv("LOAF_API_BASE_URL", "https://alice:s3cret-pw@api.test/api")
    with pytest.raises(LoafConfigError, match="^\\$LOAF_API_BASE_URL carries credentials: "):
        LoafClient()


@pytest.mark.parametrize(
    "base_url",
    [
        "http://alice:s3cret-pw@api.example.test/api",  # refused: credentials
        "ftp://alice:s3cret-pw@api.example.test/api",  # refused: scheme
        "alice:s3cret-pw@api.example.test/api",  # refused: no scheme
        "https://alice:s3cret-pw@host:port/api",  # refused: port
        "https://alice:s3cret/pw@api.example.test/api",  # a password with a /: refused: port
        "https://alice:s3cret?pw@api.example.test/api",
        "https://alice:s3cret#pw@api.example.test/api",
        "https://alice:s3cret-pw@[::1/api",  # does not parse
    ],
)
def test_credentials_in_a_url_are_not_echoed(base_url: str) -> None:
    with pytest.raises(LoafConfigError) as caught:
        LoafClient(base_url=base_url)
    message = str(caught.value)
    assert "s3cret" not in message and "pw" not in message and "alice" not in message
    assert "***@" in message
    # Nor in an exception chained to it (the ValueError urllib raised for the port).
    error = caught.value
    assert error.__cause__ is None and (error.__context__ is None or error.__suppress_context__)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://alice:pa/ss@host/api", "https://***@host/api"),
        ("https://alice:pa#ss@host/api", "https://***@host/api"),
        ("https://a@b@host/api", "https://***@host/api"),
        ("alice:pw@host/api", "***@host/api"),
        ("https://alice:pw@[::1/api", "https://***@[::1/api"),
        ("https://host/api", "https://host/api"),
        ("  https://host/api  ", "https://host/api"),
    ],
)
def test_shown_hides_everything_up_to_the_last_at(url: str, expected: str) -> None:
    assert shown(url) == expected


def test_credentials_in_a_ws_url_are_not_echoed() -> None:
    for make in (
        lambda: LoafClient(ws_url="ws://alice:s3cret-pw@remote.example.test/ws"),
        lambda: LoafWebSocketClient(ws_url="ws://alice:s3cret-pw@remote.example.test/ws"),
    ):
        with pytest.raises(LoafConfigError) as caught:
            make()
        assert "s3cret-pw" not in str(caught.value) and "alice" not in str(caught.value)


def test_a_chain_mismatch_shows_the_base_url_without_credentials() -> None:
    client = LoafClient(TRADING_KEY, base_url="https://api.test/api")
    client.base_url = "https://alice:s3cret-pw@api.test/api"  # never taken, so set by hand
    key = client.orders._credentials()
    with pytest.raises(LoafConfigError) as caught:
        client.orders._check_chain(cast(Any, SimpleNamespace(l2_chain_id=1)), key, "OPRA")
    message = str(caught.value)
    assert "the one at https://***@api.test/api " in message and "s3cret" not in message


def test_ws_url_on_another_host_warns() -> None:
    with pytest.warns(UserWarning) as caught:
        client = LoafClient(TRADING_KEY, base_url="https://api.test/api", ws_url="wss://ws.test/ws")
    assert len(caught) == 1
    assert str(caught[0].message) == (
        "ws_url is on host 'ws.test', not base_url's host 'api.test': a client with a trading "
        "key also sends its login token to 'ws.test'."
    )
    assert caught[0].filename == __file__  # points at the caller's line
    assert client.ws_url == "wss://ws.test/ws"


def test_the_other_host_warning_names_the_callers_line() -> None:
    client = LoafClient(TRADING_KEY, base_url="https://api.test/api")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        line = sys._getframe().f_lineno + 1
        client.websocket(ws_url="wss://feed.test/ws")
        LoafWebSocketClient(client, ws_url="wss://feed.test/ws")
        LoafClient(TRADING_KEY, base_url="https://api.test/api", ws_url="wss://feed.test/ws")
    assert [(w.filename, w.lineno) for w in caught] == [
        (__file__, line), (__file__, line + 1), (__file__, line + 2)
    ]


def test_ws_url_on_the_same_host_or_derived_does_not_warn() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        LoafClient(TRADING_KEY, base_url="https://api.test/api")
        LoafClient(TRADING_KEY, base_url="https://api.test/api", ws_url="wss://API.test:8443/ws")
        LoafClient(base_url="http://localhost:8005/api", ws_url="ws://localhost:8006/ws")
        client = LoafClient(TRADING_KEY, base_url="https://api.test/api")
        client.websocket()
        client.websocket(ws_url="wss://api.test/other")
        LoafWebSocketClient(ws_url="wss://elsewhere.test/ws")  # no client: no base_url


def test_environment_variables_follow_the_same_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOAF_API_BASE_URL", "http://api.example-not-loaf.test/api")
    with pytest.raises(LoafConfigError, match="^\\$LOAF_API_BASE_URL is 'http://api.example"):
        LoafClient()
    # An argument wins over the variable, and is what an error names.
    with pytest.raises(LoafConfigError, match="^base_url is 'http://other.test/api'"):
        LoafClient(base_url="http://other.test/api")
    assert LoafClient(allow_http=True).base_url == "http://api.example-not-loaf.test/api"

    monkeypatch.setenv("LOAF_API_BASE_URL", "HTTPS://Api.Test/api/")
    assert LoafClient().base_url == "https://api.test/api"
    monkeypatch.setenv("LOAF_API_BASE_URL", "api.test/api")
    with pytest.raises(LoafConfigError, match="^\\$LOAF_API_BASE_URL must start with https://"):
        LoafClient()
    monkeypatch.setenv("LOAF_API_BASE_URL", "https://api.test/api")

    monkeypatch.setenv("LOAF_WS_URL", "ws://ws.example-not-loaf.test/ws")
    with pytest.raises(LoafConfigError, match="^\\$LOAF_WS_URL is 'ws://ws.example-not-loaf"):
        LoafClient()
    monkeypatch.setenv("LOAF_WS_URL", "https://api.test/ws")
    with pytest.raises(LoafConfigError, match="^\\$LOAF_WS_URL must start with wss://"):
        LoafClient()
    monkeypatch.setenv("LOAF_WS_URL", "WSS://Ws.Test/ws")
    with pytest.warns(UserWarning, match="^\\$LOAF_WS_URL is on host 'ws.test', not base_url's"):
        client = LoafClient()
    assert client.ws_url == "wss://ws.test/ws"
    assert client.websocket().ws_url == "wss://ws.test/ws"
    monkeypatch.setenv("LOAF_WS_URL", "wss://api.test/ws")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert LoafClient().ws_url == "wss://api.test/ws"


def test_no_environment_variable_allows_http(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOAF_API_BASE_URL", "http://dev.internal/api")
    for name in ("LOAF_ALLOW_HTTP", "ALLOW_HTTP"):
        monkeypatch.setenv(name, "1")
    with pytest.raises(LoafConfigError, match="plain http://"):
        LoafClient()


@pytest.mark.parametrize(
    "ws_url",
    ["ws://localhost/ws", "ws://127.0.0.1:1/ws", "ws://127.9.9.9:1/ws", "ws://[::1]:8/ws"],
)
def test_websocket_client_alone_takes_ws_to_this_machine(ws_url: str) -> None:
    ws = LoafWebSocketClient(None, ws_url=ws_url)
    assert ws.ws_url == ws_url and ws._ssl is None and ws._login_tokens is None


def test_websocket_client_alone_refuses_ws_elsewhere() -> None:
    with pytest.raises(LoafConfigError, match="^ws_url is 'ws://feed.test/ws': plain ws://"):
        LoafWebSocketClient(ws_url="ws://feed.test/ws")
    with pytest.raises(LoafConfigError, match="^ws_url is 'ws://sub.localhost/ws': plain ws://"):
        LoafWebSocketClient(ws_url="ws://sub.localhost/ws")
    assert LoafWebSocketClient(ws_url="ws://feed.test/ws", allow_http=True).ws_url == (
        "ws://feed.test/ws"
    )
    ws = LoafWebSocketClient(ws_url="WSS://Feed.Test/ws")
    assert ws.ws_url == "wss://feed.test/ws" and ws._ssl is not None
    with pytest.raises(LoafConfigError, match="^ws_url has no host"):
        LoafWebSocketClient(ws_url="wss:///ws")
    with pytest.raises(ValueError, match="ws_url is required"):
        LoafWebSocketClient()


def test_websocket_client_with_a_client_checks_its_own_ws_url() -> None:
    client = LoafClient(TRADING_KEY, base_url="https://api.test/api")
    with pytest.raises(LoafConfigError, match="^ws_url is 'ws://api.test/ws': plain ws://"):
        LoafWebSocketClient(client, ws_url="ws://api.test/ws")
    with pytest.warns(UserWarning, match="^ws_url is on host 'feed.test', not base_url's host"):
        ws = LoafWebSocketClient(client, ws_url="wss://feed.test/ws")
    assert ws.ws_url == "wss://feed.test/ws"
    with pytest.warns(UserWarning, match="^ws_url is on host 'feed.test'"):
        LoafWebSocketClient(client, ws_url="ws://feed.test/ws", allow_http=True)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert LoafWebSocketClient(client).ws_url == "wss://api.test/ws"
        assert LoafWebSocketClient(client, ws_url="wss://API.TEST/ws").ws_url == "wss://api.test/ws"


def test_derive_ws_url_refuses_what_it_cannot_map() -> None:
    assert derive_ws_url("https://host/") == "wss://host/ws"
    assert derive_ws_url("http://localhost:8005") == "ws://localhost:8005/ws"
    for base_url in ("ftp://host/api", "host/api", "https:///api", "https://[::1/api"):
        with pytest.raises(LoafConfigError, match="^base_url must start with https:// or http://"):
            derive_ws_url(base_url)

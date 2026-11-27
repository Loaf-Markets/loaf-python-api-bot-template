"""The rules a REST base URL and a WebSocket URL must meet before any request uses them.

Every authenticated request carries a login token, a bearer credential the exchange takes from
any host for its 10-minute life, and a signed order needs none. So a URL is taken only with a
scheme the SDK speaks and a host, with no credentials (the trading key authenticates every
request), and an unencrypted one (``http://``, ``ws://``) only to this machine unless the caller
passes ``allow_http=True``.
"""

from __future__ import annotations

import ipaddress
import sys
import warnings
from urllib.parse import SplitResult, urlsplit, urlunsplit

from .exceptions import LoafConfigError

#: (plain scheme, encrypted scheme) of each kind of URL.
_REST = ("http", "https")
_WS = ("ws", "wss")
_WS_FOR_REST = {"https": "wss", "http": "ws"}

#: This package's name (``loaf``): the frames of its modules are the SDK's.
_PACKAGE = __name__.rpartition(".")[0]


def is_this_machine(host: str) -> bool:
    """Whether ``host`` (lowercase, without brackets) names this machine: ``localhost`` or a
    loopback address (127.0.0.0/8, ``::1``). A ``*.localhost`` name is not: not every resolver
    keeps it on this machine."""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def shown(url: str) -> str:
    """``url`` for error text: everything between its ``://`` (its start, without one) and its
    last ``@`` replaced by ``***``. A URL parser ends the host at the first ``/``, ``?`` or ``#``,
    but a password typed into a URL can hold one, so every ``@`` counts: no credentials are
    shown, whether or not the string parses."""
    text = url.strip()
    scheme, sep, rest = text.partition("://")
    prefix = scheme + sep
    if not sep:
        prefix, rest = "", text
    if "@" not in rest:
        return prefix + rest
    return prefix + "***@" + rest.rpartition("@")[2]


def _netloc(parts: SplitResult) -> str:
    """The URL's authority with its host (and port) lowercased, any credentials kept as given."""
    userinfo, at, hostport = parts.netloc.rpartition("@")
    return userinfo + at + hostport.lower()


def _checked(url: str, *, source: str, schemes: tuple[str, str], allow_http: bool) -> str:
    plain, secure = schemes
    try:
        parts = urlsplit(url.strip())
    except ValueError:  # an unclosed IPv6 bracket
        raise LoafConfigError(f"{source} is not a valid URL: {shown(url)!r}") from None
    scheme = parts.scheme.lower()
    if scheme not in schemes:
        raise LoafConfigError(
            f"{source} must start with {secure}:// (or {plain}:// to this machine), such as "
            f"{secure}://host{'/api' if secure == 'https' else '/ws'}, got {shown(url)!r}"
        )
    try:
        host = parts.hostname
        _ = parts.port  # raises ValueError on a port that is not a number from 0 to 65535
    except ValueError:
        raise LoafConfigError(f"{source} is not a valid URL: {shown(url)!r}") from None
    if "@" in parts.netloc:
        raise LoafConfigError(
            f"{source} carries credentials: {shown(url)!r}. The SDK takes no user name or "
            "password in a URL: your trading key authenticates every request. Drop them."
        )
    if not host:
        raise LoafConfigError(f"{source} has no host: {shown(url)!r}")
    if scheme == plain and not allow_http and not is_this_machine(host):
        raise LoafConfigError(
            f"{source} is {shown(url)!r}: plain {plain}:// is taken only to this machine "
            f"(localhost, 127.0.0.0/8, [::1]), because to any other host the login tokens and "
            f"signed orders would travel unencrypted. Use {secure}://, or pass allow_http=True "
            "for a dev server on your own network, in a container network or on a *.localhost "
            "name."
        )
    return urlunsplit((scheme, _netloc(parts), parts.path, parts.query, parts.fragment))


def rest_url(url: str, *, source: str, allow_http: bool) -> str:
    """``url`` checked as a REST base URL, its scheme and host lowercased and any trailing
    slash dropped. ``source`` names the argument or environment variable it came from."""
    return _checked(url, source=source, schemes=_REST, allow_http=allow_http).rstrip("/")


def ws_url(url: str, *, source: str, allow_http: bool) -> str:
    """``url`` checked as a WebSocket URL, its scheme and host lowercased. ``source`` names the
    argument or environment variable it came from."""
    return _checked(url, source=source, schemes=_WS, allow_http=allow_http)


def derive_ws_url(base_url: str) -> str:
    """The WebSocket URL of REST base URL ``base_url``: ``https://host/api`` ->
    ``wss://host/ws``, ``http://host:8005/api`` -> ``ws://host:8005/ws``."""
    try:
        parts = urlsplit(base_url.strip())
    except ValueError:  # an unclosed IPv6 bracket
        parts = None
    scheme = None if parts is None else _WS_FOR_REST.get(parts.scheme.lower())
    if parts is None or scheme is None or not parts.netloc:
        raise LoafConfigError(
            f"base_url must start with https:// or http:// and name a host, got "
            f"{shown(base_url)!r}"
        )
    path = parts.path.rstrip("/")
    if path.endswith("/api"):
        path = path[: -len("/api")]
    return urlunsplit((scheme, _netloc(parts), path.rstrip("/") + "/ws", "", ""))


def _in_the_sdk(module: object) -> bool:
    """Whether ``module`` (a frame's ``__name__``) is this package or one of its modules."""
    return isinstance(module, str) and (module == _PACKAGE or module.startswith(_PACKAGE + "."))


def _outside_the_sdk() -> int:
    """The :func:`warnings.warn` ``stacklevel``, from its caller, of the first frame outside
    this package: the caller's own line, however the SDK was entered."""
    level = 1
    frame = sys._getframe(1)  # the function that warns
    while frame is not None and _in_the_sdk(frame.f_globals.get("__name__")):
        frame = frame.f_back
        level += 1
    return level


def warn_other_host(ws: str, base_url: str, *, source: str) -> None:
    """Warn when WebSocket URL ``ws`` (from ``source``) is on another host than ``base_url``:
    a client with a trading key sends its login token there too. The warning names the first
    line outside this package, so it points at the caller's code."""
    ws_host = urlsplit(ws).hostname
    rest_host = urlsplit(base_url).hostname
    if ws_host != rest_host:
        warnings.warn(
            f"{source} is on host {ws_host!r}, not base_url's host {rest_host!r}: a client with a "
            f"trading key also sends its login token to {ws_host!r}.",
            UserWarning,
            stacklevel=_outside_the_sdk(),
        )

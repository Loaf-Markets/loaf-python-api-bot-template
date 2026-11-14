"""The exchange's served decimals, read as exact :class:`~decimal.Decimal` values.

Every price, size and dollar amount the exchange serves of a perp's terms and live state, a
position, a margin pool, a funding payment and a fill (spot fills too), a ``mark_price`` frame's
price and an account's withdrawable USDC, is its actual value: an exact decimal string in one
canonical form, digits with a ``-`` before a negative and a ``.`` before a fraction whose last
digit is not 0, no exponent, no ``+`` and no leading zero but the units' (``"0.25"``), zero
``"0"``. A size has at most its market's ``quantityDecimals`` places (a lot), a price at most
``7 - quantityDecimals`` (an index, a mark, an entry or a liquidation price can fall between
ticks) and a dollar amount at most 6 (US$0.000001): never more than
:data:`~loaf.constants.ORDER_DECIMALS`. The SDK reads each into the Decimal of exactly that value,
and refuses one in any other form, a number among them, so that no value is ever read in another
unit: a read raises :class:`~loaf.exceptions.LoafServerError` naming the field, and the WebSocket
client drops and logs the frame. Only the integers an order signs are scaled, inside the SDK.
"""

from __future__ import annotations

import re
import reprlib
from decimal import Decimal
from typing import Any, Callable, Mapping, MutableMapping

from .constants import ORDER_DECIMALS
from .enums import WSMessageType
from .exceptions import LoafServerError

_DECIMAL_RE = re.compile(r"(?!-0\Z)-?(?:0|[1-9][0-9]*)(?:\.[0-9]*[1-9])?")

# The served decimals of each shape, by field. A perp's live state (``market.live`` and each row of
# a perps_markets_live frame); a held report's price, and a mark_price frame's; a marketList row's
# live figures.
_LIVE = ("indexPrice", "markPrice", "openInterest")
_PRICE = ("price",)
_SELECTOR_LIVE = ("markPrice", "openInterest")
# A perp's terms: its market.l2's, and a market_terms_update frame's l2.
_PERP_TERMS = ("minBase", "minQuote", "quoteLimit")
# A margin pool and its positions, as perps.positions() and perps_account_update serve them.
_POOL = ("collateral", "value", "requirement")
_POSITION = ("base", "entryQuote", "entryPrice", "fundingOwed", "notional", "liquidationPrice")
# A funding payment, and a fill (every market's).
_FUNDING = ("base", "payment")
_FILL = ("quantity", "price", "fee", "closedPnl")
# An account's withdrawable USDC: the summary's, and a perps_account_update frame's.
_ACCOUNT = ("withdrawable",)
# The fields served null where there is no such value (a pool past its range, a position with no
# liquidation price, a fill that reduced nothing, an account unreadable now).
_NULLABLE = frozenset({"value", "requirement", "liquidationPrice", "closedPnl", "withdrawable"})


#: How much of a malformed value its error shows: the start and end of a long string, the first
#: items of a long list or object, three levels of nesting, at most _SHOWN_MAX characters.
_SHOWN = reprlib.Repr()
_SHOWN.maxstring = _SHOWN.maxother = 80
_SHOWN.maxlevel = 3
_SHOWN_MAX = 200


def _shown(value: Any) -> str:
    """A bounded repr of ``value``."""
    text = _SHOWN.repr(value)
    return text if len(text) <= _SHOWN_MAX else text[: _SHOWN_MAX - 1] + "…"


class Malformed(ValueError):
    """A served decimal that is not one, ``field`` naming it in its answer or frame. The message
    shows at most 200 characters of the value."""

    def __init__(self, field: str, value: Any) -> None:
        super().__init__(
            f"a malformed {field}: {_shown(value)} is not an exact decimal (digits, a '-' "
            "before a "
            "negative, no exponent, no leading or trailing zero, '0' for zero, at most "
            f"{ORDER_DECIMALS} decimal places)"
        )


def decimal(value: Any, field: str) -> Decimal:
    """The served decimal ``value`` (``field``'s) as the Decimal of exactly its value, or
    :class:`Malformed`."""
    if not isinstance(value, str) or _DECIMAL_RE.fullmatch(value) is None:
        raise Malformed(field, value)
    point = value.find(".")
    if point >= 0 and len(value) - point - 1 > ORDER_DECIMALS:
        raise Malformed(field, value)
    return Decimal(value)  # from its digits: exact, whatever the decimal context


def read(answer: Any, reader: Callable[[Any], None], what: str) -> Any:
    """``answer``, the body ``what`` answered, its served decimals read in place by ``reader``; a
    malformed one raises :class:`~loaf.exceptions.LoafServerError`, a 200, naming it."""
    try:
        reader(answer)
    except Malformed as exc:
        raise LoafServerError(f"{what} answered {exc}", status_code=200, body=answer) from None
    return answer


def _convert(row: Any, where: str, fields: tuple[str, ...]) -> None:
    """Each of ``fields`` that ``row`` carries (an object, else nothing), as its Decimal in place; a
    ``None`` stays where the field may be null. ``where`` names ``row`` (``""``: the answer)."""
    if not isinstance(row, MutableMapping):
        return
    for name in fields:
        if name in row and not (row[name] is None and name in _NULLABLE):
            row[name] = decimal(row[name], f"{where}.{name}" if where else name)


def _rows(body: Any, key: str) -> list[Any]:
    """``body[key]`` when it is a list, else ``[]``."""
    rows = body.get(key) if isinstance(body, Mapping) else None
    return rows if isinstance(rows, list) else []


def _live(live: Any, where: str) -> None:
    _convert(live, where, _LIVE)
    if isinstance(live, Mapping):
        _convert(live.get("heldPrice"), f"{where}.heldPrice", _PRICE)


def _account(perps: Any, where: str) -> None:
    for i, pool in enumerate(_rows(perps, "pools")):
        here = f"{where}.pools[{i}]"
        _convert(pool, here, _POOL)
        for j, position in enumerate(_rows(pool, "positions")):
            _convert(position, f"{here}.positions[{j}]", _POSITION)


def _each(body: Any, key: str, fields: tuple[str, ...]) -> None:
    for i, row in enumerate(_rows(body, key)):
        _convert(row, f"{key}[{i}]", fields)


def market_detail(detail: Any) -> None:
    """``GET /trade/{ticker}``: its market's live state and a perp's terms, and each
    ``marketList`` row's live figures."""
    market = detail.get("market") if isinstance(detail, Mapping) else None
    if isinstance(market, Mapping):
        _live(market.get("live"), "market.live")
        _convert(market.get("l2"), "market.l2", _PERP_TERMS)
    for i, row in enumerate(_rows(detail, "marketList")):
        if isinstance(row, Mapping):
            _convert(row.get("live"), f"marketList[{i}].live", _SELECTOR_LIVE)


def perps(summary: Any) -> None:
    """``GET /portfolio/summary``'s ``perps``: its pools and their positions."""
    if isinstance(summary, Mapping):
        _account(summary.get("perps"), "perps")


def summary(answer: Any) -> None:
    """``GET /portfolio/summary``, and a ``perps_account_update`` frame: its ``perps`` and its
    ``withdrawable``."""
    perps(answer)
    _convert(answer, "", _ACCOUNT)


def component(answer: Any) -> None:
    """``GET /portfolio/component``: its ``tradeHistory`` and ``fundingHistory`` rows."""
    _each(answer, "tradeHistory", _FILL)
    _each(answer, "fundingHistory", _FUNDING)


def trades(page: Any) -> None:
    """A ``GET /history/trades`` page's rows."""
    _each(page, "trades", _FILL)


def funding(page: Any) -> None:
    """A ``GET /history/funding`` page's rows."""
    _each(page, "funding", _FUNDING)


# A WebSocket frame's readers, by its type.
def _markets_live(message: Mapping[str, Any]) -> None:
    for i, row in enumerate(_rows(message, "markets")):
        _live(row, f"markets[{i}]")


def _terms(message: Mapping[str, Any]) -> None:
    _convert(message.get("l2"), "l2", _PERP_TERMS)


def _mark(message: Mapping[str, Any]) -> None:
    _convert(message, "", _PRICE)


def _funding_payment(message: Mapping[str, Any]) -> None:
    _convert(message.get("funding"), "funding", _FUNDING)


def _trade(message: Mapping[str, Any]) -> None:
    _convert(message.get("trade"), "trade", _FILL)


_FRAMES: dict[str, Callable[[Mapping[str, Any]], None]] = {
    WSMessageType.PERPS_MARKETS_LIVE.value: _markets_live,
    WSMessageType.MARKET_TERMS_UPDATE.value: _terms,
    WSMessageType.MARK_PRICE.value: _mark,
    WSMessageType.PERPS_ACCOUNT_UPDATE.value: summary,
    WSMessageType.FUNDING_PAYMENT.value: _funding_payment,
    WSMessageType.TRADE_NEW.value: _trade,
}


def frame(message: Any) -> None:
    """A WebSocket frame's served decimals, by its ``type``: a ``perps_markets_live`` frame's rows,
    a ``market_terms_update``'s terms, a ``mark_price``'s price, a ``perps_account_update``'s pools
    and withdrawable, a ``funding_payment``'s payment and a ``trade_new``'s fill."""
    kind = message.get("type") if isinstance(message, Mapping) else None
    reader = _FRAMES.get(kind) if isinstance(kind, str) else None
    if reader is not None:
        reader(message)

"""Base class shared by all resource groups."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .._l2 import _ASSET_TICKER_RE, _MARKET_TICKER_RE
from ..exceptions import _client_validation_error

if TYPE_CHECKING:
    import re

    from ..client import LoafClient


class Resource:
    """Holds a back-reference to the owning client."""

    def __init__(self, client: LoafClient) -> None:
        self._client = client


def _check_market_ticker(ticker: Any) -> None:
    _check_ticker(
        ticker,
        _MARKET_TICKER_RE,
        "a market's ticker: 1-10 uppercase letters or digits, -PERP appended for a perp "
        "(e.g. 'MUS', 'H100-PERP')",
    )


def _check_asset_ticker(ticker: Any) -> None:
    _check_ticker(
        ticker, _ASSET_TICKER_RE, "an asset's ticker: 1-10 uppercase letters or digits (e.g. 'MUS')"
    )


def _check_ticker(ticker: Any, pattern: re.Pattern[str], rule: str) -> None:
    if not isinstance(ticker, str) or not pattern.fullmatch(ticker):
        raise _client_validation_error(f"ticker must be {rule}, got {ticker!r}")

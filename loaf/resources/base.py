"""Base class shared by all resource groups."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .._l2 import _TICKER_RE
from ..exceptions import _client_validation_error

if TYPE_CHECKING:
    from ..client import LoafClient


class Resource:
    """Holds a back-reference to the owning client."""

    def __init__(self, client: LoafClient) -> None:
        self._client = client


def _check_ticker(ticker: Any) -> None:
    if not isinstance(ticker, str) or not _TICKER_RE.fullmatch(ticker):
        raise _client_validation_error(
            "ticker must be 1-4 uppercase letters or digits (a property's ticker, e.g. 'OPRA'), "
            f"got {ticker!r}"
        )

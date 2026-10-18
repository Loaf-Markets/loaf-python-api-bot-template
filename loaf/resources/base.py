"""Base class shared by all resource groups."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from ..exceptions import _client_validation_error

if TYPE_CHECKING:
    from ..client import LoafClient

_TICKER_RE = re.compile(r"[A-Z0-9]{1,4}")


def _check_ticker(ticker: Any) -> str:
    """``ticker``, refused locally unless it is a property ticker (1-4 of ``A-Z 0-9``), so
    nothing else is ever put into a URL path (``../``, ``?``, ``#``, a newline)."""
    if not isinstance(ticker, str) or not _TICKER_RE.fullmatch(ticker):
        raise _client_validation_error(
            "ticker must be 1-4 uppercase letters or digits (a property's ticker, e.g. 'OPRA'), "
            f"got {ticker!r}"
        )
    return ticker


class Resource:
    """Holds a back-reference to the owning client."""

    def __init__(self, client: LoafClient) -> None:
        self._client = client

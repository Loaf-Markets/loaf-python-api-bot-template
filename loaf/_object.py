"""A lenient response wrapper that supports both dict and attribute access.

The Loaf API returns JSON objects. Rather than hand-maintain a brittle class
for every shape (which would break whenever the backend adds a field), parsed
responses are wrapped in :class:`LoafObject` — a ``dict`` subclass whose keys
are also reachable as attributes, recursively::

    book = client.market.property("OPRA").orderBook
    book["bids"][0]["price"]   # dict access
    book.bids[0].price         # attribute access — identical

Because it *is* a dict, it round-trips through ``json.dumps`` and unknown/new
fields are preserved automatically. See ``loaf.models`` for typed descriptions
of the documented shapes.
"""

from __future__ import annotations

from typing import Any


def _wrap(value: Any) -> Any:
    if isinstance(value, LoafObject):
        return value
    if isinstance(value, dict):
        return LoafObject(value)
    if isinstance(value, list):
        return [_wrap(item) for item in value]
    return value


class LoafObject(dict):
    """A ``dict`` with recursive attribute access for ergonomic field reads."""

    def __init__(self, data: dict | None = None, **kwargs: Any) -> None:
        super().__init__()
        if data:
            for key, value in dict(data).items():
                super().__setitem__(key, _wrap(value))
        for key, value in kwargs.items():
            super().__setitem__(key, _wrap(value))

    def __setitem__(self, key: Any, value: Any) -> None:
        super().__setitem__(key, _wrap(value))

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(
                f"{type(self).__name__!s} has no field {name!r}. "
                f"Available fields: {', '.join(map(str, self.keys())) or '(none)'}"
            ) from exc

    def __setattr__(self, name: str, value: Any) -> None:
        self[name] = value

    def __delattr__(self, name: str) -> None:
        try:
            del self[name]
        except KeyError as exc:  # pragma: no cover - defensive
            raise AttributeError(name) from exc

    def __repr__(self) -> str:
        return f"LoafObject({dict.__repr__(self)})"


def parse(value: Any) -> Any:
    """Public entry point: wrap a parsed-JSON value in :class:`LoafObject`(s)."""
    return _wrap(value)

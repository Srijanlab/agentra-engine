"""In-process TTL cache for hot DynamoDB reads.

A polling dashboard would otherwise issue the same registry scans several times
a second. Vercel keeps instances warm for minutes under active use, so even a
per-instance cache with a short TTL cuts reads (and cost) by 10-50x.
"""

from __future__ import annotations

import time
from typing import Callable, TypeVar

_T = TypeVar("_T")
_store: dict[str, tuple[float, object]] = {}
DEFAULT_TTL = 15.0


def get_or_set(key: str, producer: Callable[[], _T], ttl: float = DEFAULT_TTL) -> _T:
    now = time.monotonic()
    hit = _store.get(key)
    if hit is not None and now - hit[0] < ttl:
        return hit[1]  # type: ignore[return-value]
    value = producer()
    _store[key] = (now, value)
    return value


def drop(*keys: str) -> None:
    for key in keys:
        _store.pop(key, None)


def clear() -> None:
    _store.clear()

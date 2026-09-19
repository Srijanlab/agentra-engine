"""gh_cache/_entry.py — the value a producer may hand back to `cached()`."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class FetchResult:
    """Wraps a producer's value with the HTTP ETag GitHub returned, if any --
    opportunistic: a producer that has no ETag to offer just returns its value
    directly and skips this wrapper entirely."""

    value: Any
    etag: str | None = None

"""gh_cache/_ttl.py — the configurable fresh-window for the read-through cache."""

from __future__ import annotations

import os

_MIN_TTL = 10.0
_MAX_TTL = 300.0
_FALLBACK_TTL = 45.0


def default_ttl() -> float:
    """AGENTRA_GH_CACHE_TTL_SECONDS, clamped to [10, 300]; 45s if unset or invalid."""
    raw = os.environ.get("AGENTRA_GH_CACHE_TTL_SECONDS")
    try:
        value = float(raw) if raw else _FALLBACK_TTL
    except ValueError:
        value = _FALLBACK_TTL
    return max(_MIN_TTL, min(_MAX_TTL, value))

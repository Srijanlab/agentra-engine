"""server/gh_cache.py — a small Firestore-backed TTL cache so the dashboard's
read endpoints don't hit the GitHub API on every request.

Keyed docs live under the `gh_cache` collection: {value, ts}. On a serverless
host each request may be a cold instance, so an in-process cache barely helps --
Firestore is the shared layer. One Firestore read (free-tier cheap) replaces N
GitHub calls on a hit.
"""

from __future__ import annotations

import time
from typing import Any, Awaitable, Callable

from agentra import registry

_DEFAULT_TTL = 90.0
_local: dict[str, tuple[float, Any]] = {}


async def cached(key: str, producer: Callable[[], Awaitable[Any]], *, ttl: float = _DEFAULT_TTL) -> Any:
    db = registry.firestore_client()
    if db is None:  # local/CLI/tests -- no quota pressure, no cache
        return await producer()

    now = time.time()
    hit = _local.get(key)
    if hit is not None and now - hit[0] < ttl:
        return hit[1]

    if db is not None:
        try:
            snap = db.collection("gh_cache").document(key).get()
            if snap.exists:
                data = snap.to_dict() or {}
                if now - (data.get("ts") or 0) < ttl:
                    _local[key] = (now, data.get("value"))
                    return data.get("value")
        except Exception:
            pass  # cache read failed (quota, offline) -- fall through to a live fetch

    value = await producer()
    _local[key] = (now, value)
    if db is not None:
        try:
            db.collection("gh_cache").document(key).set({"value": value, "ts": now})
        except Exception:
            pass  # best-effort write
    return value


def invalidate(*keys: str) -> None:
    """Drop cache entries -- call after a write that changes what they cover."""
    for key in keys:
        _local.pop(key, None)
    db = registry.firestore_client()
    if db is None:
        return
    for key in keys:
        try:
            db.collection("gh_cache").document(key).delete()
        except Exception:
            pass

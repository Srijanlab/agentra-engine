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


async def cached(key: str, producer: Callable[[], Awaitable[Any]], *, ttl: float = _DEFAULT_TTL) -> Any:
    db = registry.firestore_client()
    if db is None:  # local/CLI -- no cache, just run it
        return await producer()

    doc_ref = db.collection("gh_cache").document(key)
    try:
        snap = doc_ref.get()
        if snap.exists:
            data = snap.to_dict() or {}
            if time.time() - (data.get("ts") or 0) < ttl:
                return data.get("value")
    except Exception:
        pass  # cache read failed -- fall through to a live fetch

    value = await producer()
    try:
        doc_ref.set({"value": value, "ts": time.time()})
    except Exception:
        pass  # best-effort write
    return value


def invalidate(*keys: str) -> None:
    """Drop cache entries -- call after a write that changes what they cover."""
    db = registry.firestore_client()
    if db is None:
        return
    for key in keys:
        try:
            db.collection("gh_cache").document(key).delete()
        except Exception:
            pass

"""server/gh_cache.py — a small DynamoDB-backed TTL cache so the dashboard's
read endpoints don't hit the GitHub API on every request.

Keyed items live in the agentra-gh-cache table: {value, ts, expires_at}. On a
serverless host each request may be a cold instance, so an in-process cache
barely helps -- DynamoDB is the shared layer. One read replaces N GitHub
calls on a hit. expires_at is DynamoDB's native item-TTL attribute (enabled
on the table via CDK) -- expired entries are reclaimed automatically, unlike
under Firestore where nothing ever garbage-collected an old entry.
"""

from __future__ import annotations

import time
from typing import Any, Awaitable, Callable

from agentra import registry

_DEFAULT_TTL = 90.0
_local: dict[str, tuple[float, Any]] = {}


async def cached(key: str, producer: Callable[[], Awaitable[Any]], *, ttl: float = _DEFAULT_TTL) -> Any:
    ddb = registry.dynamodb_resource()
    if ddb is None:  # local/CLI/tests -- no quota pressure, no cache
        return await producer()

    now = time.time()
    hit = _local.get(key)
    if hit is not None and now - hit[0] < ttl:
        return hit[1]

    from agentra.registry import _dynamo

    try:
        item = _dynamo.get_item(_dynamo.table("gh-cache"), {"key": key})
        if item is not None and now - (item.get("ts") or 0) < ttl:
            _local[key] = (now, item.get("value"))
            return item.get("value")
    except Exception:
        pass  # cache read failed (offline, throttled) -- fall through to a live fetch

    value = await producer()
    _local[key] = (now, value)
    try:
        _dynamo.put_item(
            _dynamo.table("gh-cache"), {"key": key, "value": value, "ts": now, "expires_at": int(now + ttl)}
        )
    except Exception:
        pass  # best-effort write
    return value


def invalidate(*keys: str) -> None:
    """Drop cache entries -- call after a write that changes what they cover."""
    for key in keys:
        _local.pop(key, None)
    ddb = registry.dynamodb_resource()
    if ddb is None:
        return
    from agentra.registry import _dynamo

    tbl = _dynamo.table("gh-cache")
    for key in keys:
        try:
            tbl.delete_item(Key={"key": key})
        except Exception:
            pass

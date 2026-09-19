"""server/gh_cache/ — a read-through TTL cache so the dashboard's GitHub-backed
read endpoints (issues/projects/memory reads) stay fast and never block on --
or fail because of -- a live GitHub API call.

Two layers: an in-process dict (first line, free on a warm instance) over a
durable store -- DynamoDB in cloud mode, local JSON under AGENTRA_HOME
otherwise -- so a cold instance, the CLI, or a test still gets a populated
cache instead of a no-op. On a miss or expiry the producer runs; if it raises
for any reason, the last known value is served instead (stale-on-error) so a
GitHub blip never surfaces as a 5xx on the dashboard -- only a key with no
prior cached value at all lets the error propagate. A route's own explicit
error (e.g. the unregistered-app 404 in routes/apps.py) is checked before
`cached()` is ever called, so it is unaffected by this.
"""

from __future__ import annotations

import inspect
import logging
import time
from typing import Any, Awaitable, Callable

from agentra import registry
from agentra.server.gh_cache import _dynamo_store, _inprocess, _local_store
from agentra.server.gh_cache._entry import FetchResult
from agentra.server.gh_cache._ttl import default_ttl

logger = logging.getLogger(__name__)

_local = _inprocess._store  # exposed for tests: the in-process layer's raw dict

__all__ = ["cached", "invalidate", "invalidate_app", "FetchResult", "default_ttl"]


def _durable():
    return _dynamo_store if registry.dynamodb_resource() is not None else _local_store


async def _call_producer(producer: Callable[..., Awaitable[Any]], etag: str | None) -> Any:
    """Call a zero-arg producer as-is; a one-arg producer gets the previous
    etag (or None) so it can send a conditional-request validator on refresh."""
    try:
        accepts_etag = len(inspect.signature(producer).parameters) >= 1
    except (TypeError, ValueError):
        accepts_etag = False
    return await producer(etag) if accepts_etag else await producer()


async def cached(key: str, producer: Callable[..., Awaitable[Any]], *, ttl: float | None = None) -> Any:
    """Serve `key` from cache if fresh; otherwise call `producer` and repopulate
    both cache layers. If `producer` raises, the last known value (however
    stale) is returned instead of propagating the error -- only a genuinely
    empty cache lets the error through."""
    ttl = default_ttl() if ttl is None else ttl
    now = time.time()

    entry = _inprocess.get(key)
    if entry is None:
        entry = _durable().get(key)
        if entry is not None:
            _inprocess.set(key, entry)

    if entry is not None and now < entry.get("expires_at", 0):
        return entry["value"]

    try:
        raw = await _call_producer(producer, entry.get("etag") if entry else None)
    except Exception:
        if entry is not None:
            logger.warning("gh_cache producer failed for %s -- serving stale value", key, exc_info=True)
            return entry["value"]
        raise

    value, etag = (raw.value, raw.etag) if isinstance(raw, FetchResult) else (raw, None)
    new_entry = {"value": value, "ts": now, "expires_at": now + ttl, "etag": etag}
    _inprocess.set(key, new_entry)
    _durable().put(key, new_entry)
    return value


def invalidate(*keys: str) -> None:
    """Drop cache entries (both layers) -- call after a write that changes what they cover."""
    _inprocess.delete(*keys)
    try:
        _durable().delete(*keys)
    except Exception:
        logger.warning("gh_cache durable invalidate failed for %s", keys, exc_info=True)


def invalidate_app(app_name: str) -> None:
    """Every dashboard cache entry covering one app: its own detail/board/review
    views, plus the shared /apps digest -- a single write busts that whole
    digest rather than tracking per-app membership within it."""
    try:
        invalidate(
            f"app_detail:{app_name}",
            f"backlog_board:{app_name}",
            f"ready_to_review:{app_name}",
            "digest_batch",
        )
    except Exception:
        logger.warning("gh_cache invalidate_app failed for %s", app_name, exc_info=True)

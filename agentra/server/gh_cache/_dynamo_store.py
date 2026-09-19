"""gh_cache/_dynamo_store.py — the durable layer in cloud mode: the `gh-cache`
DynamoDB table, keyed by `key`, with `expires_at` as DynamoDB's native item-TTL
attribute so expired entries are reclaimed automatically."""

from __future__ import annotations

from typing import Any


def get(key: str) -> dict | None:
    from agentra.registry import _dynamo

    try:
        item = _dynamo.get_item(_dynamo.table("gh-cache"), {"key": key})
    except Exception:
        return None
    if item is None:
        return None
    item.pop("key", None)
    return item


def put(key: str, entry: dict[str, Any]) -> None:
    from agentra.registry import _dynamo

    try:
        _dynamo.put_item(_dynamo.table("gh-cache"), {"key": key, **entry})
    except Exception:
        pass  # best-effort write


def delete(*keys: str) -> None:
    from agentra.registry import _dynamo

    tbl = _dynamo.table("gh-cache")
    for key in keys:
        try:
            tbl.delete_item(Key={"key": key})
        except Exception:
            pass

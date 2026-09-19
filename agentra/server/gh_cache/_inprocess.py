"""gh_cache/_inprocess.py — the in-process layer: a per-instance dict, free and
first-checked before the durable store (DynamoDB or local JSON)."""

from __future__ import annotations

from typing import Any

_store: dict[str, dict[str, Any]] = {}


def get(key: str) -> dict | None:
    return _store.get(key)


def set(key: str, entry: dict) -> None:
    _store[key] = entry


def delete(*keys: str) -> None:
    for key in keys:
        _store.pop(key, None)


def clear() -> None:
    _store.clear()

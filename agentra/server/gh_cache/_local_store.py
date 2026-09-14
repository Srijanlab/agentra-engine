"""gh_cache/_local_store.py — the durable layer off cloud (no DynamoDB): one
JSON file per key under AGENTRA_HOME/gh-cache/, same {value, ts, expires_at,
etag} shape as the DynamoDB table, so the CLI and local/dev runs get the same
cache semantics instead of a no-op."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import quote

from agentra import registry


def _dir() -> Path:
    return registry.AGENTRA_HOME / "gh-cache"


def _path(key: str) -> Path:
    return _dir() / f"{quote(key, safe='')}.json"


def get(key: str) -> dict | None:
    path = _path(key)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def put(key: str, entry: dict[str, Any]) -> None:
    path = _path(key)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(entry))
    except OSError:
        pass  # best-effort write


def delete(*keys: str) -> None:
    for key in keys:
        try:
            _path(key).unlink(missing_ok=True)
        except OSError:
            pass

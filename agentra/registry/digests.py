"""registry/digests.py — per-app last-posted timestamps for the awaiting-testing Slack digest."""

from __future__ import annotations

import json

from agentra.registry import core


def _path():
    return core.AGENTRA_HOME / "awaiting_digests.json"


def _key(app: str) -> str:
    return f"awaiting_testing_digest:{app}"


def get_last_awaiting_digest_at(app: str) -> float | None:
    """Epoch seconds of the last awaiting-testing digest posted for `app`, or None."""
    if core._ddb is not None:
        from agentra.registry import _dynamo

        item = _dynamo.get_item(_dynamo.table("system"), {"key": _key(app)})
        value = item.get("ts") if item else None
    else:
        path = _path()
        value = json.loads(path.read_text()).get(app) if path.exists() else None
    return float(value) if value is not None else None


def record_awaiting_digest(app: str, ts: float) -> None:
    """Stores the epoch-seconds time an awaiting-testing digest was posted for `app`."""
    if core._ddb is not None:
        from agentra.registry import _dynamo

        _dynamo.put_item(_dynamo.table("system"), {"key": _key(app), "ts": float(ts)})
        return
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    on_disk = json.loads(path.read_text()) if path.exists() else {}
    path.write_text(json.dumps({**on_disk, app: float(ts)}, indent=2))

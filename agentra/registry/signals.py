"""registry/signals.py — durable system event log backing GET /signals."""

from __future__ import annotations

import json
import time

from agentra.registry import core

_CAP = 200


def _path():
    return core.AGENTRA_HOME / "signals.json"


def record_signal(source: str, message: str, ts: float | None = None) -> None:
    """Appends a signal event, keeping only the most recent `_CAP` entries."""
    entry = {"ts": ts if ts is not None else time.time(), "source": source, "message": message}
    if core._ddb is not None:
        from agentra.registry import _dynamo

        item = _dynamo.get_item(_dynamo.table("system"), {"key": "signals"})
        events = ((item or {}).get("events") or [])[-(_CAP - 1):] + [entry]
        _dynamo.put_item(_dynamo.table("system"), {"key": "signals", "events": events})
        return
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    events = json.loads(path.read_text()) if path.exists() else []
    events = events[-(_CAP - 1):] + [entry]
    path.write_text(json.dumps(events, indent=2))


def list_signals(limit: int = 100) -> list[dict]:
    """Most-recent-first signal events, capped at `limit`."""
    if core._ddb is not None:
        from agentra.registry import _dynamo

        item = _dynamo.get_item(_dynamo.table("system"), {"key": "signals"})
        events = (item or {}).get("events") or []
    else:
        path = _path()
        events = json.loads(path.read_text()) if path.exists() else []
    return list(reversed(events))[:limit]

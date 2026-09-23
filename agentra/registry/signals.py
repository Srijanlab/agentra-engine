"""registry/signals.py — durable system event log backing GET /signals."""

from __future__ import annotations

import json
import time

from agentra.registry import core

_CAP = 200
_TRIM_RETRIES = 5
_KEY = {"key": "signals"}


def _path():
    return core.AGENTRA_HOME / "signals.json"


def _append_dynamo(entry: dict) -> None:
    """Atomically appends one event to the shared item, then trims it to the newest `_CAP`."""
    from agentra.registry import _dynamo

    tbl = _dynamo.table("system")
    resp = tbl.update_item(
        Key=_KEY,
        UpdateExpression="SET #e = list_append(if_not_exists(#e, :empty), :new)",
        ExpressionAttributeNames={"#e": "events"},
        ExpressionAttributeValues={":empty": [], ":new": [_dynamo.to_item(entry)]},
        ReturnValues="UPDATED_NEW",
    )
    size = len(resp["Attributes"]["events"])
    for _ in range(_TRIM_RETRIES):
        if size <= _CAP or _trim_dynamo(tbl, size):
            return
        item = tbl.get_item(Key=_KEY, ConsistentRead=True).get("Item") or {}
        size = len(item.get("events") or [])


def _trim_dynamo(tbl, size: int) -> bool:
    """Drops the oldest events beyond `_CAP` if the list still has `size` entries; False on a lost race."""
    from botocore.exceptions import ClientError

    excess = ", ".join(f"#e[{i}]" for i in range(size - _CAP))
    try:
        tbl.update_item(
            Key=_KEY,
            UpdateExpression=f"REMOVE {excess}",
            ConditionExpression="size(#e) = :size",
            ExpressionAttributeNames={"#e": "events"},
            ExpressionAttributeValues={":size": size},
        )
        return True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise


def record_signal(source: str, message: str, ts: float | None = None) -> None:
    """Appends a signal event, keeping only the most recent `_CAP` entries."""
    entry = {"ts": ts if ts is not None else time.time(), "source": source, "message": message}
    if core._ddb is not None:
        _append_dynamo(entry)
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

        item = _dynamo.get_item(_dynamo.table("system"), _KEY)
        events = ((item or {}).get("events") or [])[-_CAP:]
    else:
        path = _path()
        events = json.loads(path.read_text()) if path.exists() else []
    return list(reversed(events))[:limit]

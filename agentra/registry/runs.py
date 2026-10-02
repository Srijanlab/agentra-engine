"""registry/runs.py — durable run records."""

from __future__ import annotations

import functools
import json
import logging
import time
from typing import Any

from agentra.registry import _cache, core

logger = logging.getLogger(__name__)


def record_run(run_key: str, **fields: Any) -> None:
    fields.setdefault("updated_at", time.time())
    _cache.clear()  # runs/loops summaries all shift
    if core._ddb is not None:
        from agentra.registry import _dynamo

        # shard="R" on every write (idempotent) -- the by-recency GSI only
        # projects items carrying it; omitting it on any write path would
        # silently make that run invisible to list_runs(), no error raised.
        _dynamo.merge_update(_dynamo.table("runs"), {"run_key": run_key}, {"shard": "R", **fields})
        return
    runs = _local_runs()
    runs.setdefault(run_key, {}).update(fields)
    _local_save_runs(runs)


def _strip_internal(item: dict | None) -> dict | None:
    if item is not None:
        item.pop("shard", None)
    return item


def get_run(run_key: str) -> dict | None:
    if core._ddb is not None:
        from agentra.registry import _dynamo

        return _cache.get_or_set(
            f"run:{run_key}", lambda: _strip_internal(_dynamo.get_item(_dynamo.table("runs"), {"run_key": run_key})), ttl=6
        )
    return _local_runs().get(run_key)


def _stream_runs(limit: int) -> list[dict]:
    from boto3.dynamodb.conditions import Key

    from agentra.registry import _dynamo

    resp = _dynamo.table("runs").query(
        IndexName="by-recency", KeyConditionExpression=Key("shard").eq("R"), ScanIndexForward=False, Limit=limit
    )
    return [_strip_internal(_dynamo.from_item(i)) for i in resp.get("Items", [])]


def list_runs(limit: int = 50) -> list[dict]:
    if core._ddb is not None:
        return _cache.get_or_set(f"runs:{limit}", lambda: _stream_runs(limit), ttl=8)

    runs = _local_runs()
    ordered = sorted(
        ({"run_key": key, **info} for key, info in runs.items()),
        key=lambda r: r.get("started_at") or 0,  # a malformed record must not break the whole list
        reverse=True,
    )
    return ordered[:limit]


def loop_id_for(objective: str) -> str:
    import hashlib

    return hashlib.sha1(objective.encode("utf-8")).hexdigest()[:10]


def loop_id_for_issue(app: str, issue_number: int | str) -> str:
    """A loop maps 1:1 to a tracked GitHub issue -- every run that works that issue
    (implement, resume-after-human, deploy, verify) shares this id. Falls back to
    loop_id_for(objective) at dispatch time, before the run has picked an issue."""
    import hashlib

    return hashlib.sha1(f"{app}#{issue_number}".encode("utf-8")).hexdigest()[:10]


def last_run_at(app: str, source: str | None = None) -> float | None:
    runs = list_app_runs(app, sources=(source,) if source else None, limit=1)
    return runs[0]["started_at"] if runs else None


def list_app_runs(
    app: str,
    sources: tuple[str, ...] | list[str] | None = None,
    limit: int | None = 50,
    statuses: tuple[str, ...] | list[str] | None = None,
    loop_id: str | None = None,
) -> list[dict]:
    """The app's own runs (optionally filtered), newest first by started_at, at most `limit` (None = all)."""
    if core._ddb is not None:
        return _query_app_runs(app, sources, limit, statuses, loop_id)
    matches = [
        {"run_key": key, **info}
        for key, info in _local_runs().items()
        if info.get("app") == app
        and info.get("started_at") is not None
        and (not sources or info.get("source") in sources)
        and (not statuses or info.get("status") in statuses)
        and (loop_id is None or info.get("loop_id") == loop_id)
    ]
    matches.sort(key=lambda r: r["started_at"], reverse=True)
    return matches if limit is None else matches[:limit]


def _query_app_runs(
    app: str,
    sources: tuple[str, ...] | list[str] | None,
    limit: int | None,
    statuses: tuple[str, ...] | list[str] | None = None,
    loop_id: str | None = None,
) -> list[dict]:
    from boto3.dynamodb.conditions import Attr, Key

    from agentra.registry import _dynamo

    kwargs: dict[str, Any] = {
        "IndexName": "by-app-recency", "KeyConditionExpression": Key("app").eq(app), "ScanIndexForward": False,
    }
    conditions = []
    if sources:
        conditions.append(Attr("source").is_in(list(sources)))
    if statuses:
        conditions.append(Attr("status").is_in(list(statuses)))
    if loop_id is not None:
        conditions.append(Attr("loop_id").eq(loop_id))
    if conditions:
        kwargs["FilterExpression"] = functools.reduce(lambda a, b: a & b, conditions)
    found: list[dict] = []
    while limit is None or len(found) < limit:
        resp = _dynamo.table("runs").query(**kwargs)
        found.extend(_strip_internal(_dynamo.from_item(i)) for i in resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    return found if limit is None else found[:limit]


def _stale_run_candidates() -> list[dict]:
    """Newest global window plus every queued/running run of each registered app, deduped by run_key."""
    candidates = {run["run_key"]: run for run in list_runs(limit=200)}
    for app in core.list_apps():
        for run in list_app_runs(app, statuses=("queued", "running"), limit=None):
            candidates[run["run_key"]] = run
    return list(candidates.values())


def reconcile_stale_runs() -> list[str]:
    now = time.time()
    threshold = core.stale_heartbeat_seconds()
    marked: list[str] = []
    for run in _stale_run_candidates():
        if run.get("status") not in ("queued", "running"):
            continue
        run_key = run["run_key"]
        last_activity = run.get("updated_at") or run.get("started_at") or now
        if now - last_activity > threshold:
            record_run(
                run_key,
                status="failed",
                error=f"orphaned: no activity for over {int(threshold // 60)} minutes -- "
                "the process running this cycle likely died (e.g. an OOM kill or revision rollout)",
            )
            marked.append(run_key)
    reconcile_stale_loops()
    return marked


def reconcile_stale_loops() -> list[str]:
    """Un-stick loops whose last run ended but never rolled up -- an exception in
    the cycle tail, or a container killed mid-cycle, skips brain._finish_loop_rollup
    and leaves the loop at last_run_status="running" forever, so every later cycle
    treats it as still in flight. Reconcile from the run's actual terminal state."""
    from agentra.registry import loops as _loops

    now = time.time()
    threshold = core.stale_heartbeat_seconds()
    fixed: list[str] = []
    for loop in _loops.list_loops_by_status(last_run_statuses=("running", "queued")):
        loop_id, last_key = loop.get("loop_id"), loop.get("last_run_key")
        run = get_run(last_key) if last_key else None
        run_status = (run or {}).get("status")
        if run_status in ("completed", "failed", "blocked", "waiting_for_human"):
            _loops.roll_up_loop(loop_id, last_key, run_status, 0.0)
            fixed.append(loop_id)
        elif (
            now - float(loop.get("updated_at") or 0) > threshold
            and now - float((run or {}).get("updated_at") or 0) > threshold
        ):
            _loops.roll_up_loop(loop_id, last_key or "", "failed", 0.0)
            fixed.append(loop_id)
    return fixed


def list_agent_steps(app: str | None = None, limit: int = 100) -> list[dict]:
    """Agent-turn history for the dashboard's AgentsPanel -- reads Langfuse's own
    observations (see agentra.langfuse_api.list_recent_generations), not a
    registry-owned store: every agent turn already emits this data to Langfuse
    as a side effect of run_agent(), so a separate table here was pure
    duplicate bookkeeping (removed -- there is no local-JSON fallback either,
    since local/CLI mode has no Langfuse credentials to read from and this
    panel simply shows nothing there, same as it showed nothing before without
    a registered app)."""
    from agentra import langfuse_api

    return langfuse_api.list_recent_generations(app=app, limit=limit)


def _local_runs() -> dict[str, dict]:
    if not core._RUNS_PATH.exists():
        return {}
    try:
        return json.loads(core._RUNS_PATH.read_text())
    except (ValueError, OSError):
        return {}


def _local_save_runs(runs: dict[str, dict]) -> None:
    core._RUNS_PATH.parent.mkdir(parents=True, exist_ok=True)
    core._RUNS_PATH.write_text(json.dumps(runs, indent=2))

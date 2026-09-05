"""registry/runs.py — durable run records."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from agentra.registry import _cache, core

logger = logging.getLogger(__name__)


def record_run(run_key: str, **fields: Any) -> None:
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
        key=lambda r: r["started_at"],
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
    if core._ddb is not None:
        from boto3.dynamodb.conditions import Key

        from agentra.registry import _dynamo

        resp = _dynamo.table("runs").query(
            IndexName="by-app-recency", KeyConditionExpression=Key("app").eq(app), ScanIndexForward=False, Limit=100
        )
        matches = [
            r for r in (_strip_internal(_dynamo.from_item(i)) for i in resp.get("Items", []))
            if source is None or r.get("source") == source
        ]
        return max((r["started_at"] for r in matches), default=None)

    matches = [r for r in list_runs(limit=200) if r.get("app") == app and (source is None or r.get("source") == source)]
    if not matches:
        return None
    return max(r["started_at"] for r in matches)


def reconcile_stale_runs() -> list[str]:
    now = time.time()
    marked: list[str] = []
    for run in list_runs(limit=200):
        if run.get("status") not in ("queued", "running"):
            continue
        run_key = run["run_key"]
        last_activity = run.get("updated_at") or run.get("started_at") or now
        if now - last_activity > core.STALE_PROCESSING_SECONDS:
            record_run(
                run_key,
                status="failed",
                error=f"orphaned: no activity for over {core.STALE_PROCESSING_SECONDS // 60} minutes -- "
                "the process running this cycle likely died (e.g. an OOM kill or revision rollout)",
            )
            marked.append(run_key)
    return marked


def list_waiting_for_human(limit: int = 200) -> list[dict]:
    """Runs currently parked in the 'waiting_for_human' state -- backs the dashboard's 'Needs your input' panel."""
    return [r for r in list_runs(limit=limit) if r.get("status") in ("waiting_for_human", "escalated")]


def reconcile_waiting_for_human() -> list[dict]:
    """Human-in-the-loop escalation (GitHub issue #34): a run sitting in 'waiting_for_human' must never silently stay there forever with no further signal -- past core.HUMAN_INPUT_MAX_WAIT_SECONDS since it started waiting, flip it to the distinguishable 'escalated' state so a human looking at the dashboard (or a re-sent Slack message, dispatched by the caller using the human_input context this returns) can tell "still within normal wait" from "this has been sitting here too long."  Pure state transition only, no outbound calls (GitHub/Slack) -- keeps registry/ dependency-free of connectors/, same layering as the rest of this module."""
    now = time.time()
    escalated: list[dict] = []
    for run in list_runs(limit=500):
        if run.get("status") != "waiting_for_human":
            continue
        human_input = run.get("human_input") or {}
        waiting_since = human_input.get("waiting_since")
        if waiting_since is None or now - waiting_since <= core.HUMAN_INPUT_MAX_WAIT_SECONDS:
            continue
        record_run(run["run_key"], status="escalated")
        run["status"] = "escalated"
        escalated.append(run)
    return escalated


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

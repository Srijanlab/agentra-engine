"""registry/loops.py — the loop as a stored entity (one per tracked issue)."""

from __future__ import annotations

import json
import time
from typing import Any

from agentra.registry import _cache, core
from agentra.registry.runs import list_runs, loop_id_for_issue

_LOOPS_LIST_LIMIT = 100
_VALID_KINDS = ("feature", "bug", "objective")
_VALID_STATUSES = ("active", "waiting_for_human", "shipped", "released", "abandoned")


def bind_loop(
    app: str,
    issue_number: int | str,
    *,
    title: str | None = None,
    kind: str = "feature",
    objective: str | None = None,
) -> str:
    """Create (or refresh) the loop doc for `issue_number` and return its id.
    Idempotent. The caller links the run: `record_run(run_key, loop_id=<ret>)`."""
    if kind not in _VALID_KINDS:
        kind = "feature"
    loop_id = loop_id_for_issue(app, issue_number)
    now = time.time()
    existing = _get_loop_doc(loop_id)
    fields: dict[str, Any] = {
        "loop_id": loop_id,
        "app": app,
        "issue_number": str(issue_number),
        "kind": kind,
        "updated_at": now,
        "langfuse_session_id": loop_id,
    }
    if title:
        fields["title"] = title
    if objective:
        fields["objective"] = objective
    if existing is None:
        fields.update(created_at=now, status="active", run_count=0, total_cost_usd=0.0)
    _write_loop(loop_id, fields)
    _retire_objective_placeholders(app, keep=loop_id)
    return loop_id


def _retire_objective_placeholders(app: str, keep: str) -> None:
    """check_backlog binds an objective-keyed placeholder before the cycle picks an
    issue; once bind_loop creates the real issue loop it's a never-rolled-up ghost
    (run_count 0) cluttering the dashboard -- drop it."""
    for loop in list_loops(app=app, limit=_LOOPS_LIST_LIMIT):
        if (
            loop.get("loop_id") != keep
            and loop.get("kind") == "objective"
            and int(loop.get("run_count", 0) or 0) == 0
        ):
            _delete_loop(loop["loop_id"])


def bind_loop_for_run(app: str, objective: str) -> str:
    """Create (or refresh) an objective-keyed loop entry at run-creation time,
    before the cycle has resolved a specific issue number. Returns the loop_id so
    the caller can link it to the run immediately via record_run.

    If an active issue-keyed loop already exists for this app, that loop_id is
    returned and no new doc is written -- ensuring one loop per issue, no duplicates.
    When implement_feature later resolves a tracked issue it calls bind_loop
    (issue-keyed), which creates/refreshes the more-specific doc and updates the
    run's loop_id to that entry.

    Idempotent: a second call for the same app+objective refreshes updated_at only."""
    from agentra.registry.runs import loop_id_for  # local import to avoid circular

    existing_loops = [
        l for l in list_loops(app=app, limit=10)
        if l.get("issue_number") and l.get("status") in ("active", "waiting_for_human")
    ]
    if existing_loops:
        return existing_loops[0]["loop_id"]

    loop_id = loop_id_for(objective)
    now = time.time()
    existing = _get_loop_doc(loop_id)
    fields: dict[str, Any] = {
        "loop_id": loop_id,
        "app": app,
        "kind": "objective",
        "objective": objective,
        "updated_at": now,
        "langfuse_session_id": loop_id,
    }
    if existing is None:
        fields.update(created_at=now, status="active", run_count=0, total_cost_usd=0.0)
    _write_loop(loop_id, fields)
    return loop_id


def roll_up_loop(loop_id: str, run_key: str, run_status: str, cost_usd: float) -> None:
    """Fold a finished run's outcome into its loop's rolling totals."""
    doc = _get_loop_doc(loop_id)
    if doc is None:
        return
    loop_status = "waiting_for_human" if run_status in ("waiting_for_human", "escalated") else doc.get("status", "active")
    _write_loop(loop_id, {
        "run_count": int(doc.get("run_count", 0)) + 1,
        "total_cost_usd": float(doc.get("total_cost_usd", 0.0)) + (cost_usd or 0.0),
        "last_run_key": run_key,
        "last_run_status": run_status,
        "status": loop_status,
        "updated_at": time.time(),
    })


def set_loop_status(loop_id: str, status: str) -> None:
    if status not in _VALID_STATUSES:
        raise ValueError(f"unknown loop status {status!r} (expected one of {_VALID_STATUSES})")
    if _get_loop_doc(loop_id) is None:
        return
    _write_loop(loop_id, {"status": status, "updated_at": time.time()})


def get_loop(loop_id: str) -> dict | None:
    """The loop doc plus its runs, newest run first."""
    doc = _get_loop_doc(loop_id)
    if doc is None:
        return None
    runs = [r for r in list_runs(limit=300) if r.get("loop_id") == loop_id]
    runs.sort(key=lambda r: r.get("started_at", 0), reverse=True)
    return {**doc, "runs": runs}


def list_loops(app: str | None = None, limit: int = _LOOPS_LIST_LIMIT) -> list[dict]:
    """Stored loop summaries, most recently active first. The app-filtered case
    (the common one -- already tuned once against Firestore quota, hence no
    per-run scan) is a real indexed Query, not a fetch-then-filter."""
    if core._ddb is not None:
        if app is not None:
            return _cache.get_or_set(f"loops:{app}:{limit}", lambda: _query_loops_by_app(app, limit), ttl=15)
        return _cache.get_or_set(f"loops:{limit}", lambda: _scan_loops(limit), ttl=15)

    loops = sorted(_local_loops().values(), key=lambda l: l.get("updated_at", 0), reverse=True)[:limit]
    if app is not None:
        loops = [l for l in loops if l.get("app") == app]
    return loops


# --- storage -----------------------------------------------------------------

def _scan_loops(limit: int) -> list[dict]:
    """Unfiltered case only -- small table, cold path, not worth a GSI."""
    from agentra.registry import _dynamo

    items = _dynamo.scan_all(_dynamo.table("loops"))
    items.sort(key=lambda l: l.get("updated_at", 0), reverse=True)
    return items[:limit]


def _query_loops_by_app(app: str, limit: int) -> list[dict]:
    from boto3.dynamodb.conditions import Key

    from agentra.registry import _dynamo

    resp = _dynamo.table("loops").query(
        IndexName="by-app-recency", KeyConditionExpression=Key("app").eq(app), ScanIndexForward=False, Limit=limit
    )
    return [_dynamo.from_item(i) for i in resp.get("Items", [])]


def _get_loop_doc(loop_id: str) -> dict | None:
    if core._ddb is not None:
        from agentra.registry import _dynamo

        return _dynamo.get_item(_dynamo.table("loops"), {"loop_id": loop_id})
    return _local_loops().get(loop_id)


def _write_loop(loop_id: str, fields: dict) -> None:
    _cache.clear()
    if core._ddb is not None:
        from agentra.registry import _dynamo

        _dynamo.merge_update(_dynamo.table("loops"), {"loop_id": loop_id}, fields)
        return
    loops = _local_loops()
    loops.setdefault(loop_id, {}).update(fields)
    core._LOOPS_PATH.parent.mkdir(parents=True, exist_ok=True)
    core._LOOPS_PATH.write_text(json.dumps(loops, indent=2))


def _delete_loop(loop_id: str) -> None:
    _cache.clear()
    if core._ddb is not None:
        from agentra.registry import _dynamo

        _dynamo.table("loops").delete_item(Key={"loop_id": loop_id})
        return
    loops = _local_loops()
    if loops.pop(loop_id, None) is not None:
        core._LOOPS_PATH.write_text(json.dumps(loops, indent=2))


def _local_loops() -> dict[str, dict]:
    if not core._LOOPS_PATH.exists():
        return {}
    try:
        return json.loads(core._LOOPS_PATH.read_text())
    except (ValueError, OSError):
        return {}

"""registry/loops.py — the loop as a stored entity (one per tracked issue)."""

from __future__ import annotations

import json
import time
from typing import Any

from agentra.registry import _cache, core
from agentra.registry.runs import list_runs, loop_id_for_issue, record_run

_LOOPS_LIST_LIMIT = 100
_VALID_KINDS = ("feature", "bug", "objective")
_VALID_STATUSES = ("active", "waiting_for_human", "shipped", "released", "abandoned")


def bind_loop(
    run_key: str,
    app: str,
    issue_number: int | str,
    *,
    title: str | None = None,
    kind: str = "feature",
    objective: str | None = None,
) -> str:
    """Attach a run to the loop for `issue_number`, creating the loop doc on first
    sight. Idempotent -- safe to call every time a run (re)binds its issue."""
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
    record_run(run_key, loop_id=loop_id, issue_number=str(issue_number))
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
    """Stored loop summaries, most recently active first. One indexed query -- no
    per-run scan (that was the free-tier read blowout)."""
    if core._db is not None:
        loops = _cache.get_or_set(f"loops:{limit}", lambda: _stream_loops(limit), ttl=15)
    else:
        loops = sorted(_local_loops().values(), key=lambda l: l.get("updated_at", 0), reverse=True)[:limit]
    if app is not None:
        loops = [l for l in loops if l.get("app") == app]
    return loops


# --- storage -----------------------------------------------------------------

def _stream_loops(limit: int) -> list[dict]:
    from google.cloud import firestore

    docs = (
        core._db.collection("loops")
        .order_by("updated_at", direction=firestore.Query.DESCENDING)
        .limit(limit)
        .stream()
    )
    return [d.to_dict() for d in docs]


def _get_loop_doc(loop_id: str) -> dict | None:
    if core._db is not None:
        snap = core._db.collection("loops").document(loop_id).get()
        return snap.to_dict() if snap.exists else None
    return _local_loops().get(loop_id)


def _write_loop(loop_id: str, fields: dict) -> None:
    _cache.clear()
    if core._db is not None:
        core._db.collection("loops").document(loop_id).set(fields, merge=True)
        return
    loops = _local_loops()
    loops.setdefault(loop_id, {}).update(fields)
    core._LOOPS_PATH.parent.mkdir(parents=True, exist_ok=True)
    core._LOOPS_PATH.write_text(json.dumps(loops, indent=2))


def _local_loops() -> dict[str, dict]:
    if not core._LOOPS_PATH.exists():
        return {}
    return json.loads(core._LOOPS_PATH.read_text())

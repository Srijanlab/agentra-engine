"""server/routes/systems.py — system pause/resume, runs, steps, and signals."""

from __future__ import annotations

import logging
from fastapi import APIRouter, HTTPException

from agentra import registry
from agentra.server.state import _active_runs
from agentra.server.utils import _server_log

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/system/paused")
async def get_system_paused() -> dict:
    record = registry.is_paused()
    if record is None:
        return {"paused": False}
    return {
        "paused": True,
        "reason": record.get("reason"),
        "paused_at": record.get("paused_at"),
    }


@router.post("/system/pause")
async def pause_system(payload: dict | None = None) -> dict:
    reason = (payload or {}).get("reason")
    registry.pause(reason)
    _server_log("pause", f"system paused: reason={reason!r}")
    return {"paused": True}


@router.post("/system/resume")
async def resume_system() -> dict:
    registry.resume()
    _server_log("resume", "system resumed")
    return {"paused": False}


@router.get("/runs")
async def get_runs(limit: int = 50) -> dict:
    registry.reconcile_stale_runs()
    return {"runs": registry.list_runs(limit=limit)}


@router.get("/runs/{run_key}")
async def get_run_status(run_key: str) -> dict:
    run = _active_runs.get(run_key) or registry.get_run(run_key)
    if run is None:
        raise HTTPException(status_code=404, detail=f"run {run_key!r} not found")
    return run


@router.get("/agent-steps")
async def get_agent_steps(app: str | None = None, limit: int = 100) -> dict:
    return {"steps": registry.list_agent_steps(app=app, limit=limit)}


@router.get("/signals")
async def get_signals() -> dict:
    """Consolidated read of every open bug and queue entry for all registered apps."""
    from agentra.memory import Memory

    results: dict[str, dict] = {}
    for app_name in registry.list_apps():
        repo = registry.get_app_repo(app_name)
        if repo is None:
            continue
        mem = Memory(repo)
        results[app_name] = {
            "known_bugs": mem.known_bugs(),
            "feature_queue": mem.feature_queue(),
            "shipped_features": mem.shipped_features(),
        }
    return results


@router.get("/server-logs")
async def get_server_logs(limit: int = 100) -> dict:
    db = registry.firestore_client()
    if db is None:
        return {"logs": []}
    from google.cloud import firestore

    docs = (
        db.collection("server_logs")
        .order_by("ts", direction=firestore.Query.DESCENDING)
        .limit(limit)
        .stream()
    )
    return {"logs": [d.to_dict() for d in docs]}

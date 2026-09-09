"""server/routes/schedule.py — read-only per-app scheduled-cycle visibility."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from agentra import registry
from agentra.registry.scheduler import compute_schedule_status
from agentra.server.state import _active_runs

router = APIRouter()

_CYCLE_RUN_SOURCES = {"scheduled", "on-demand"}


@router.get("/apps/{app_name}/schedule")
async def get_app_schedule(app_name: str) -> dict:
    """Report when Agentra will next run a scheduled cycle for an app, plus its queued/running state."""
    if app_name not in registry.list_apps():
        raise HTTPException(status_code=404, detail=f"app {app_name!r} not registered")
    repo = registry.get_app_repo(app_name)
    if repo is None:
        raise HTTPException(status_code=409, detail=f"local checkout for {app_name!r} is missing and could not be recovered")

    status = compute_schedule_status(app_name, repo)
    return {
        "app": app_name,
        "cadence_hours": status.cadence_hours,
        "last_scheduled_run_at": status.last_scheduled_run_at,
        "next_scheduled_run_at": status.next_scheduled_run_at,
        "due_now": status.due_now,
        "paused": status.paused,
        "queued": status.queued,
        "running_best_effort": _looks_running(app_name),
    }


def _looks_running(app_name: str) -> bool:
    """Best-effort: a cycle run for this app looks active in this process's run cache."""
    return any(
        run.get("app") == app_name
        and run.get("status") == "running"
        and run.get("source") in _CYCLE_RUN_SOURCES
        for run in _active_runs.values()
    )

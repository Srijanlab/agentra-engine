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


@router.get("/system/llm-backend")
async def get_llm_backend() -> dict:
    return {"backend": registry.get_llm_backend()}


@router.post("/system/llm-backend")
async def set_llm_backend(payload: dict | None = None) -> dict:
    backend = (payload or {}).get("backend")
    try:
        registry.set_llm_backend(backend)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _server_log("llm-backend", f"llm backend set to {backend!r}")
    return {"backend": backend}


@router.get("/system/llm-pool")
async def get_llm_pool() -> dict:
    return {**registry.get_llm_rotation(), "health": registry.get_llm_provider_health()}


@router.get("/debug/llm-rotation")
async def debug_llm_rotation() -> dict:
    return registry.get_llm_rotation()


@router.put("/system/llm-pool")
async def set_llm_pool(payload: dict | None = None) -> dict:
    try:
        pool = registry.set_llm_rotation((payload or {}).get("backends"))
    except registry.InvalidLLMPool as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _server_log("llm-pool", f"llm pool set to {pool['backends']!r}")
    return pool


@router.post("/system/llm-pool/next")
async def select_llm_pool_provider() -> dict:
    return registry.select_llm_provider()


@router.post("/system/llm-pool/{provider}/throttle")
async def report_llm_provider_throttled(provider: str, payload: dict | None = None) -> dict:
    try:
        health = registry.report_llm_provider_failure(provider, (payload or {}).get("retry_after_seconds"))
    except registry.InvalidLLMPool as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _server_log("llm-pool", f"llm provider {provider!r} throttled: failures={health['failures']}")
    return health


@router.post("/system/llm-pool/{provider}/success")
async def report_llm_provider_ok(provider: str) -> dict:
    try:
        return registry.report_llm_provider_success(provider)
    except registry.InvalidLLMPool as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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
async def get_signals(limit: int = 100) -> dict:
    """Returns recent, durably persisted system events (signals), most-recent-first."""
    return {"signals": registry.list_signals(limit=limit)}


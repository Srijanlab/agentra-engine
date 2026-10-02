"""server/routes/systems.py — system pause/resume, runs, steps, and signals."""

from __future__ import annotations

import logging
from fastapi import APIRouter, HTTPException, Request

from agentra import registry
from agentra.server.audit import audit_log
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
async def pause_system(request: Request, payload: dict | None = None) -> dict:
    reason = (payload or {}).get("reason")
    registry.pause(reason)
    audit_log(request, "pause", f"system paused: reason={reason!r}")
    return {"paused": True}


@router.post("/system/resume")
async def resume_system(request: Request) -> dict:
    registry.resume()
    audit_log(request, "resume", "system resumed")
    return {"paused": False}


@router.get("/system/llm-backend")
async def get_llm_backend() -> dict:
    return {"backend": registry.get_llm_backend()}


@router.post("/system/llm-backend")
async def set_llm_backend(request: Request, payload: dict | None = None) -> dict:
    backend = (payload or {}).get("backend")
    try:
        registry.set_llm_backend(backend)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    audit_log(request, "llm-backend", f"llm backend set to {backend!r}")
    return {"backend": backend}


@router.get("/system/backend-credentials")
async def get_backend_credentials_status() -> dict:
    """Which backends have a stored API key. Key values are never returned."""
    return {"credentials": registry.get_backend_credential_status()}


@router.post("/system/backend-credentials")
async def set_backend_credential(payload: dict | None = None) -> dict:
    """Store or clear an API key for one backend.
    Body: {"backend": "codex", "api_key": "sk-..."} — pass api_key="" to clear."""
    body = payload or {}
    backend = body.get("backend")
    api_key = body.get("api_key")
    if not backend:
        raise HTTPException(status_code=400, detail="backend is required")
    try:
        registry.set_backend_credentials(backend, api_key or None)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _server_log("backend-credentials", f"credential set for backend={backend!r} cleared={not api_key}")
    return {"credentials": registry.get_backend_credential_status()}


@router.get("/system/agent-backends")
async def get_system_agent_backends() -> dict:
    """Account-level default SDLC-agent → runtime-backend map."""
    return {"backends": registry.get_system_llm_backends()}


@router.post("/system/agent-backends")
async def set_system_agent_backends(payload: dict | None = None) -> dict:
    """Set (or clear) the account-level default SDLC-agent → runtime-backend map.
    Pass {"backends": {}} to reset all agents to claude."""
    backends = (payload or {}).get("backends", {})
    try:
        registry.set_system_llm_backends(backends)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _invalidate_all_app_caches()
    _server_log("agent-backends", f"system agent backends set: {backends!r}")
    return {"backends": registry.get_system_llm_backends()}


def _invalidate_all_app_caches() -> None:
    """A system-level default affects every app's resolved llm_backends, so
    each app's cached detail view (routes/apps.py's `app_detail:{name}` entry)
    must be busted rather than waiting out its TTL."""
    from agentra.server.gh_cache import invalidate_app

    for name in registry.list_apps():
        invalidate_app(name)


@router.get("/system/llm-pool")
async def get_llm_pool() -> dict:
    return {**registry.get_llm_rotation(), "health": registry.get_llm_provider_health()}


@router.get("/debug/llm-rotation")
async def debug_llm_rotation() -> dict:
    return registry.get_llm_rotation()


@router.put("/system/llm-pool")
async def set_llm_pool(request: Request, payload: dict | None = None) -> dict:
    try:
        pool = registry.set_llm_rotation((payload or {}).get("backends"))
    except registry.InvalidLLMPool as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    audit_log(request, "llm-pool", f"llm pool set to {pool['backends']!r}")
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


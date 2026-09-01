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
    """Returns recent system events (signals) from server.log."""
    import re
    from pathlib import Path
    
    signals_path = registry.AGENTRA_HOME / "server.log"
    if not signals_path.exists():
        return {"signals": []}
    
    lines = signals_path.read_text().strip().split("\n")
    signals = []
    
    # Parse each log line: [timestamp] source=X message...
    ts_pattern = re.compile(r'^\[(.*?)\]')
    source_pattern = re.compile(r'source=(\S+)')
    
    for line in reversed(lines[-limit:]):  # Most recent first
        if not line.strip():
            continue
        
        ts_match = ts_pattern.search(line)
        source_match = source_pattern.search(line)
        
        ts = ts_match.group(1) if ts_match else None
        source = source_match.group(1) if source_match else None
        
        # Remove timestamp prefix for message
        message = ts_pattern.sub('', line).strip()
        
        signals.append({
            "ts": ts,
            "source": source,
            "message": message
        })
    
    return {"signals": signals}


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

"""server/routes/loops.py — loop list/detail and the Langfuse trace proxy."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException

from agentra import langfuse_api, registry
from agentra.server.state import _active_runs

router = APIRouter()


@router.get("/loops")
async def get_all_loops(app: str | None = None, limit: int = 100) -> dict:
    return {"loops": await asyncio.to_thread(registry.list_loops, app, limit)}


@router.get("/apps/{name}/loops")
async def get_app_loops(name: str) -> dict:
    if name not in registry.list_apps():
        raise HTTPException(status_code=404, detail=f"app {name!r} not registered")
    return {"loops": await asyncio.to_thread(registry.list_loops, name)}


@router.get("/loops/{loop_id}")
async def get_loop(loop_id: str) -> dict:
    loop = await asyncio.to_thread(registry.get_loop, loop_id)
    if loop is None:
        raise HTTPException(status_code=404, detail=f"loop {loop_id!r} not found")
    return loop


@router.get("/runs/{run_key}/trace")
async def get_run_trace(run_key: str) -> dict:
    """The run's full Langfuse trace (observation tree), fetched server-side so the
    dashboard never sees Langfuse credentials."""
    run = _active_runs.get(run_key) or await asyncio.to_thread(registry.get_run, run_key)
    if run is None:
        raise HTTPException(status_code=404, detail=f"run {run_key!r} not found")
    trace_id = run.get("langfuse_trace_id")
    if not trace_id:
        raise HTTPException(status_code=404, detail="no trace recorded for this run")
    trace = await asyncio.to_thread(langfuse_api.fetch_trace, trace_id)
    if trace is None:
        raise HTTPException(status_code=502, detail="trace not available from Langfuse")
    return trace

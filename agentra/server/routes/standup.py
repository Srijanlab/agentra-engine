"""server/routes/standup.py — standup report history.

Standup *generation* runs Claude with a repo checkout, which the engine (Vercel,
cloud mode) doesn't have -- so `POST .../standup`, `/standup/daily`, and the
`/standup/live` WebSocket are held until they move to agentra-loop. Reading past
reports (`/standup/latest`) stays here.
"""

from __future__ import annotations

import logging
from fastapi import APIRouter, HTTPException

from agentra import chat_store, registry

logger = logging.getLogger(__name__)

router = APIRouter()

_STANDUP_HELD = "standup generation is temporarily disabled -- it is moving to agentra-loop (where the repo checkout is)."


@router.get("/apps/{app_name}/standup/latest")
async def get_latest_standup(app_name: str) -> dict:
    if app_name not in registry.list_apps():
        raise HTTPException(status_code=404, detail=f"app {app_name!r} not registered")
    latest = chat_store.latest_standup(app_name)
    if latest is None:
        return {"app": app_name, "standup": None}
    return {"app": app_name, "standup": latest}


@router.post("/apps/{app_name}/standup")
async def trigger_app_standup(app_name: str) -> dict:
    raise HTTPException(status_code=503, detail=_STANDUP_HELD)


@router.post("/standup/daily")
async def trigger_daily_standup() -> dict:
    raise HTTPException(status_code=503, detail=_STANDUP_HELD)

"""server/routes/standup.py — standup generation and live WebSocket updates."""

from __future__ import annotations

import datetime as dt
import logging
import re
from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

from agentra import chat_store, registry
from agentra.memory import Memory
from agentra.standup import run_daily_standup, run_standup
from agentra.server.utils import (
    AGENT_CHAT_SYSTEM_PROMPTS,
    _app_branch,
    _chat_agent_label,
    _paused_response,
    _server_log,
)

logger = logging.getLogger(__name__)

router = APIRouter()

_STANDUP_MENTION_PATTERN = re.compile(r"@(\w+)")


def _route_standup_message(text: str) -> str:
    from agentra.standup import AGENT_LABELS

    match = _STANDUP_MENTION_PATTERN.search(text)
    if not match:
        return "orchestrator"
    mentioned = match.group(1).lower()
    for agent_id, label in AGENT_LABELS.items():
        if mentioned == agent_id or mentioned == label.split()[0].lower():
            return agent_id
    return "orchestrator"


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
    if registry.is_paused():
        return _paused_response("standup")

    repo = registry.get_app_repo(app_name)
    if repo is None:
        raise HTTPException(status_code=404, detail=f"app {app_name!r} not registered")

    report = await run_standup(repo, app_name)
    error = registry.persist_agentra_dir(repo, _app_branch(app_name), f"agentra: daily standup for {app_name!r}")
    if error:
        _server_log("standup", f"app={app_name!r} standup saved locally but failed to push: {error}")
    _server_log("standup", f"app={app_name!r} generated")
    return {"app": app_name, "report": report}


@router.post("/standup/daily")
async def trigger_daily_standup() -> dict:
    if registry.is_paused():
        return _paused_response("standup")

    apps = registry.list_apps()
    if not apps:
        _server_log("standup", "no apps registered -- no-op")
        return {"triggered": False, "reason": "no apps registered"}

    reports = await run_daily_standup(apps)
    _server_log("standup", f"daily standup generated for {list(reports.keys())}")
    return {"triggered": True, "reports": reports}


@router.websocket("/apps/{app_name}/standup/live")
async def standup_live_channel(websocket: WebSocket, app_name: str) -> None:
    await websocket.accept()
    repo = registry.get_app_repo(app_name)
    if repo is None:
        await websocket.close(code=1008, reason=f"app {app_name!r} not registered")
        return

    mem = Memory(repo)
    date_str = dt.datetime.now(dt.timezone.utc).date().isoformat()

    existing = chat_store.get_standup_channel_messages(app_name, date_str)
    if existing:
        # Reconnect same day: replay the exact recorded transcript
        # (opening burst plus any interactive exchange since) -- unchanged.
        for msg in existing:
            await websocket.send_json({"type": "message", **msg})
    else:
        from agentra.standup import get_or_generate_standup_updates

        # Shared with the REST endpoints: reuse today's already-generated
        # report-store updates verbatim (no second LLM call) if either of
        # them beat this connection to it; otherwise generate once here
        # and persist to the report store too, so *they* can reuse it.
        updates, was_fresh = await get_or_generate_standup_updates(repo, app_name, mem, date_str)
        for agent_id, text in updates.items():
            msg = chat_store.record_standup_channel_message(app_name, date_str, agent_id, text)
            payload = {"type": "message", **msg}
            if was_fresh:
                payload["fresh"] = True
            await websocket.send_json(payload)

    await websocket.send_json({"type": "burst_complete"})

    try:
        while True:
            data = await websocket.receive_json()
            human_text = (data.get("message") or "").strip()
            if not human_text:
                continue

            human_msg = chat_store.record_standup_channel_message(app_name, date_str, "human", human_text)
            await websocket.send_json({"type": "message", **human_msg})

            agent_id = _route_standup_message(human_text)
            resume_session_id = chat_store.get_standup_agent_session_id(app_name, date_str, agent_id)
            system_prompt = AGENT_CHAT_SYSTEM_PROMPTS.get(agent_id, AGENT_CHAT_SYSTEM_PROMPTS["custom"])

            from agentra.agents.base import stream_chat_turn

            await websocket.send_json({"type": "start", "sender": agent_id})
            full_text = ""
            session_id: str | None = None
            async for event in stream_chat_turn(
                prompt=human_text,
                system_prompt=system_prompt,
                cwd=repo,
                allowed_tools=["Read", "Grep", "Glob"],
                max_turns=10,
                agent_label=_chat_agent_label(agent_id),
                resume=resume_session_id,
            ):
                if event["type"] == "delta":
                    full_text += event["text"]
                    await websocket.send_json({"type": "delta", "sender": agent_id, "text": event["text"]})
                else:
                    full_text = event["text"] or full_text
                    session_id = event["session_id"]

            if session_id:
                chat_store.set_standup_agent_session_id(app_name, date_str, agent_id, session_id)
            agent_msg = chat_store.record_standup_channel_message(app_name, date_str, agent_id, full_text)
            await websocket.send_json({"type": "message", "replacing_stream": True, **agent_msg})
    except WebSocketDisconnect:
        pass

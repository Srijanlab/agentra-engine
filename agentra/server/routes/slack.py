"""server/routes/slack.py — inbound Slack events: a human's reply in a
HUMAN_INPUT_REQUIRED thread resumes the blocked run (GitHub issue #68)."""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, HTTPException, Request

from agentra import registry
from agentra.connectors import slack
from agentra.server.routes.human_input import dispatch_human_answer
from agentra.server.utils import _server_log

logger = logging.getLogger(__name__)

router = APIRouter()


def _resume_from_slack_reply(mapping: dict, text: str) -> None:
    app_name = mapping.get("app")
    issue_number = mapping.get("issue_number")
    if not app_name or issue_number is None:
        return
    repo = registry.get_app_repo(app_name)
    if repo is None:
        _server_log("slack", f"app={app_name!r} not registered -- ignoring thread reply for issue #{issue_number}")
        return
    try:
        dispatch_human_answer(app_name, repo, int(issue_number), text, source="slack")
    except ValueError as exc:
        _server_log("slack", f"app={app_name!r} issue=#{issue_number} -- reply ignored: {exc}")


@router.post("/slack/events")
async def slack_events(request: Request) -> dict:
    body = await request.body()
    if not slack.verify_signature(
        request.headers.get("X-Slack-Request-Timestamp", ""),
        body,
        request.headers.get("X-Slack-Signature", ""),
    ):
        raise HTTPException(status_code=403, detail="bad Slack signature")

    payload = json.loads(body or b"{}")
    if payload.get("type") == "url_verification":
        return {"challenge": payload.get("challenge")}

    event = payload.get("event") or {}
    # A genuine human reply in a thread: type "message", threaded, not from a
    # bot, and not an edit/join/other subtype.
    if (
        event.get("type") == "message"
        and not event.get("bot_id")
        and event.get("subtype") is None
        and event.get("thread_ts")
        and (event.get("text") or "").strip()
    ):
        mapping = registry.resolve_slack_thread(event["thread_ts"])
        if mapping:
            _resume_from_slack_reply(mapping, event["text"].strip())

    return {"ok": True}

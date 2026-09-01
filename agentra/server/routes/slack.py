"""server/routes/slack.py — inbound Slack events. A reply in a
HUMAN_INPUT_REQUIRED thread resumes the blocked run (GitHub issue #68); a DM or
@mention anywhere else goes to the conversational agentra assistant."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import OrderedDict

from fastapi import APIRouter, HTTPException, Request

from agentra import registry
from agentra.connectors import slack
from agentra.server.routes.human_input import dispatch_human_answer
from agentra.server.utils import _server_log

logger = logging.getLogger(__name__)

router = APIRouter()

_MENTION_RE = re.compile(r"<@[A-Z0-9]+>")
_seen_event_ids: OrderedDict[str, None] = OrderedDict()
_SEEN_MAX = 512


def _already_handled(event_id: str | None) -> bool:
    """Slack re-delivers an event if we don't 200 within 3s; the assistant turn
    takes longer than that, so dedupe by event_id before dispatching work."""
    if not event_id:
        return False
    if event_id in _seen_event_ids:
        return True
    _seen_event_ids[event_id] = None
    if len(_seen_event_ids) > _SEEN_MAX:
        _seen_event_ids.popitem(last=False)
    return False


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


async def _run_assistant(text: str, *, channel: str, thread_ts: str, thread_key: str) -> None:
    from agentra.agents.slack_assistant import answer

    try:
        reply = await answer(text, thread_key=thread_key)
    except Exception as exc:
        logger.warning("slack assistant turn failed", exc_info=True)
        reply = f"Sorry — that turn failed: {exc}"
    slack._post_message(reply, channel=channel, thread_ts=thread_ts)


def _dispatch_assistant(event: dict) -> None:
    text = _MENTION_RE.sub("", event.get("text") or "").strip()
    channel = event.get("channel")
    if not text or not channel:
        return
    # Keep every round of one Slack conversation on the same agent session.
    thread_ts = event.get("thread_ts") or event.get("ts")
    thread_key = f"{channel}:{thread_ts}"
    _server_log("slack", f"assistant turn channel={channel} thread={thread_ts}")
    asyncio.create_task(
        _run_assistant(text, channel=channel, thread_ts=thread_ts, thread_key=thread_key)
    )


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

    if _already_handled(payload.get("event_id")):
        return {"ok": True}

    event = payload.get("event") or {}
    etype = event.get("type")
    is_bot = bool(event.get("bot_id"))

    # An @mention of the agentra app, or a direct message to it: conversational assistant.
    if not is_bot and (
        etype == "app_mention"
        or (etype == "message" and event.get("channel_type") == "im" and event.get("subtype") is None)
    ):
        _dispatch_assistant(event)
        return {"ok": True}

    # A genuine human reply in a thread: type "message", threaded, not from a
    # bot, and not an edit/join/other subtype. Only acts on a thread that maps
    # to a known HUMAN_INPUT_REQUIRED escalation.
    if (
        etype == "message"
        and not is_bot
        and event.get("subtype") is None
        and event.get("thread_ts")
        and (event.get("text") or "").strip()
    ):
        mapping = registry.resolve_slack_thread(event["thread_ts"])
        if mapping:
            _resume_from_slack_reply(mapping, event["text"].strip())

    return {"ok": True}

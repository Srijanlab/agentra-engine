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
from agentra.connectors.slack_allowlist import allowlist_configured, is_allowed
from agentra.server.routes.human_input import dispatch_human_answer
from agentra.server.utils import _server_log

logger = logging.getLogger(__name__)

router = APIRouter()

_MENTION_RE = re.compile(r"<@[A-Z0-9]+>")
_seen_event_ids: OrderedDict[str, None] = OrderedDict()
_SEEN_MAX = 512

_FRIENDLY_ERROR = "Sorry — I couldn't finish that turn. Try rephrasing, or check the dashboard."
_DENY_ASSISTANT = "Sorry, you're not on agentra's Slack allowlist."
_DENY_HUMAN_INPUT = (
    "Sorry, you're not on agentra's Slack allowlist. You can still answer via the agentra "
    "dashboard's 'Needs your input' panel or by commenting on the GitHub issue."
)

_warned_no_allowlist = False


def _warn_open_access_once() -> None:
    """Emit a single per-process warning that the Slack assistant has no allowlist."""
    global _warned_no_allowlist
    if not _warned_no_allowlist:
        _warned_no_allowlist = True
        _server_log(
            "slack",
            "Slack assistant running without an allowlist (SLACK_ALLOWED_USERS unset) -- open to all senders",
        )


def _authorized(user: str | None, *, channel: str | None, thread_ts: str | None, deny_text: str) -> bool:
    """True if this sender may dispatch work; otherwise post `deny_text` and return False."""
    if not allowlist_configured():
        _warn_open_access_once()
        return True
    if is_allowed(user):
        return True
    _server_log("slack", f"denied unlisted Slack sender user={user!r} channel={channel}")
    if channel:
        slack._post_message(deny_text, channel=channel, thread_ts=thread_ts)
    return False


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


async def _run_assistant(
    text: str, *, channel: str, thread_ts: str, thread_key: str, slack_user_id: str | None = None
) -> None:
    from agentra.agents.slack_assistant import answer

    try:
        reply = await answer(text, thread_key=thread_key, slack_user_id=slack_user_id)
    except Exception as exc:
        logger.warning("slack assistant turn failed", exc_info=True)
        _server_log("slack", f"assistant turn raised thread={thread_key}: {exc}")
        reply = _FRIENDLY_ERROR
    slack._post_message(reply, channel=channel, thread_ts=thread_ts)


def _dispatch_assistant(event: dict) -> None:
    text = _MENTION_RE.sub("", event.get("text") or "").strip()
    channel = event.get("channel")
    if not text or not channel:
        return
    # Keep every round of one Slack conversation on the same agent session.
    thread_ts = event.get("thread_ts") or event.get("ts")
    user = event.get("user")
    if not _authorized(user, channel=channel, thread_ts=thread_ts, deny_text=_DENY_ASSISTANT):
        return
    thread_key = f"{channel}:{thread_ts}"
    _server_log("slack", f"assistant turn user={user} channel={channel} thread={thread_ts}")
    asyncio.create_task(
        _run_assistant(text, channel=channel, thread_ts=thread_ts, thread_key=thread_key, slack_user_id=user)
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
            if not _authorized(
                event.get("user"),
                channel=event.get("channel"),
                thread_ts=event["thread_ts"],
                deny_text=_DENY_HUMAN_INPUT,
            ):
                return {"ok": True}
            _server_log(
                "slack",
                f"human-input resume user={event.get('user')} channel={event.get('channel')} "
                f"thread={event['thread_ts']}",
            )
            _resume_from_slack_reply(mapping, event["text"].strip())

    return {"ok": True}

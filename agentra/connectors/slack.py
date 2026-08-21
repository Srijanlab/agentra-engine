"""Outbound-only Slack notifications for human-in-the-loop escalation (GitHub issue #34)."""

from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger(__name__)

SLACK_API_POST_MESSAGE = "https://slack.com/api/chat.postMessage"

_BOT_TOKEN_ENV = "SLACK_BOT_TOKEN"
_CHANNEL_ENV = "SLACK_HUMAN_INPUT_CHANNEL"


def is_configured() -> bool:
    return bool(os.environ.get(_BOT_TOKEN_ENV)) and bool(os.environ.get(_CHANNEL_ENV))


def _post_message(text: str) -> bool:
    """Raw chat.postMessage call."""
    token = os.environ.get(_BOT_TOKEN_ENV)
    channel = os.environ.get(_CHANNEL_ENV)
    if not token or not channel:
        return False
    try:
        resp = httpx.post(
            SLACK_API_POST_MESSAGE,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"},
            json={"channel": channel, "text": text, "unfurl_links": False},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            logger.warning("slack.chat.postMessage rejected: %s", data.get("error"))
            return False
        return True
    except Exception:
        logger.warning("slack.chat.postMessage failed", exc_info=True)
        return False


def _format_human_input_message(
    *,
    app: str,
    run_id: str,
    question: str,
    issue_url: str | None,
    dashboard_url: str | None,
    branch: str | None,
    session_id: str | None,
    escalated: bool,
) -> str:
    header = (
        f":rotating_light: *{app}* run `{run_id}` has been waiting on a human answer past its "
        "configured timeout -- re-surfacing this:"
        if escalated
        else f":raised_hand: *{app}* needs a human decision to continue run `{run_id}`:"
    )
    lines = [header, "", question.strip()]
    links = []
    if issue_url:
        links.append(f"<{issue_url}|GitHub issue>")
    if dashboard_url:
        links.append(f"<{dashboard_url}|dashboard run view>")
    if links:
        lines.append("")
        lines.append(" · ".join(links))
    if branch:
        lines.append(f"_branch: `{branch}`_" + (f" _session: `{session_id}`_" if session_id else ""))
    lines.append("")
    lines.append(
        "Reply by answering on the GitHub issue (as a comment) or via the dashboard's "
        "'Needs your input' panel -- this run will resume from exactly where it left off."
    )
    return "\n".join(lines)


def notify_human_input_required(
    *,
    app: str,
    run_id: str,
    question: str,
    issue_url: str | None = None,
    dashboard_url: str | None = None,
    branch: str | None = None,
    session_id: str | None = None,
    escalated: bool = False,
) -> bool:
    """Posts a HUMAN_INPUT_REQUIRED notification to the configured Slack channel."""
    if not is_configured():
        return False
    text = _format_human_input_message(
        app=app, run_id=run_id, question=question, issue_url=issue_url, dashboard_url=dashboard_url,
        branch=branch, session_id=session_id, escalated=escalated,
    )
    return _post_message(text)

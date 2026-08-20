"""Outbound-only Slack notifications for human-in-the-loop escalation
(GitHub issue #34).

Scope, deliberately: this module can only ever *send* a message. There is
no inbound Slack Events API receiver here and none should be added in this
pass -- per the finalized spec for issue #34, a new unauthenticated (or
even signature-verified) public HTTP endpoint is a distinct security
surface (replay protection, signature verification, request routing to the
right run) that deserves its own review, not a rider on this feature. A
human's answer to a Slack notification is expected to come back through
one of the two channels server/routes/human_input.py already wires up
instead: the dashboard's "Needs your input" panel, or a plain comment on
the needs_human GitHub issue (polled -- see
memory.find_unanswered_human_input_comment and
server/routes/triggers.py's scheduled reconciliation). See this repo's
.agentra/memory/architecture/design.md for the follow-up note.

Configuration: SLACK_BOT_TOKEN (a `xoxb-...` bot token) and
SLACK_HUMAN_INPUT_CHANNEL (a channel ID, e.g. "C0123456789", or a
"#channel-name" the bot has been invited to) as plain environment
variables -- same pattern as GITHUB_APP_PRIVATE_KEY (connectors/
github_app.py's module docstring): a deployment sources these from GCP
Secret Manager into the container's env at deploy time (Terraform), this
module just reads os.environ and never touches Secret Manager itself.
Either unset means this deployment hasn't configured Slack -- every
function below silently no-ops (returns False, never raises) rather than
surfacing a "Slack failed" error as a run failure, per the finalized
spec's explicit acceptance criterion that a HUMAN_INPUT_REQUIRED event
must complete cleanly (GitHub issue filed, run visible in the dashboard)
whether or not Slack is configured.
"""

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
    """Raw chat.postMessage call. Returns True on a real Slack-API-level
    success (Slack returns HTTP 200 with a JSON body even for most
    failures, e.g. a bad token or a channel the bot isn't in -- `ok: false`
    -- so an HTTP-level try/except alone isn't enough to know whether the
    message actually landed). Never raises -- every failure mode (not
    configured, network error, bad token, bot not in channel, ...) is
    logged and swallowed, since Slack is a best-effort notification
    channel, never something a run's success should depend on."""
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
    """Posts a HUMAN_INPUT_REQUIRED notification to the configured Slack
    channel. Returns True if it was actually sent, False if Slack isn't
    configured for this deployment or the send failed -- callers must
    treat both the same way (log-and-move-on, never fail the run over
    this), per this module's docstring."""
    if not is_configured():
        return False
    text = _format_human_input_message(
        app=app, run_id=run_id, question=question, issue_url=issue_url, dashboard_url=dashboard_url,
        branch=branch, session_id=session_id, escalated=escalated,
    )
    return _post_message(text)

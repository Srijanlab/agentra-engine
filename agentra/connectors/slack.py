"""Outbound-only Slack notifications: human-in-the-loop escalation (GitHub issue #34) and shipped-to-pre-prod delivery confirmations."""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time

import httpx

logger = logging.getLogger(__name__)

SLACK_API_POST_MESSAGE = "https://slack.com/api/chat.postMessage"

_BOT_TOKEN_ENV = "SLACK_BOT_TOKEN"
_CHANNEL_ENV = "SLACK_HUMAN_INPUT_CHANNEL"
_SIGNING_SECRET_ENV = "SLACK_SIGNING_SECRET"


def is_configured() -> bool:
    """Bot token is the hard requirement -- a channel can come from a per-app override
    (registry.get_slack_channel) instead of the global env var, so that alone isn't required."""
    return bool(os.environ.get(_BOT_TOKEN_ENV))


def _post_message(text: str, channel: str | None = None, thread_ts: str | None = None) -> dict | None:
    """Raw chat.postMessage call. `channel` overrides the global SLACK_HUMAN_INPUT_CHANNEL env
    var when given (a per-app channel, see registry.get_slack_channel). Returns the Slack API
    response dict (which carries `ts`, the thread anchor) on success, else None."""
    token = os.environ.get(_BOT_TOKEN_ENV)
    channel = channel or os.environ.get(_CHANNEL_ENV)
    if not token or not channel:
        return None
    payload = {"channel": channel, "text": text, "unfurl_links": False}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    try:
        resp = httpx.post(
            SLACK_API_POST_MESSAGE,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"},
            json=payload,
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            logger.warning("slack.chat.postMessage rejected: %s", data.get("error"))
            return None
        return data
    except Exception:
        logger.warning("slack.chat.postMessage failed", exc_info=True)
        return None


def verify_signature(timestamp: str, body: bytes, signature: str) -> bool:
    """Slack request signing (https://api.slack.com/authentication/verifying-requests-from-slack).
    Returns False -- never raises -- on any missing piece, a stale timestamp (>5 min), or a
    mismatch, so the inbound-events route can 403 uniformly."""
    secret = os.environ.get(_SIGNING_SECRET_ENV)
    if not secret or not timestamp or not signature:
        return False
    try:
        if abs(time.time() - int(timestamp)) > 300:
            return False
    except ValueError:
        return False
    expected = "v0=" + hmac.new(
        secret.encode(), b"v0:" + timestamp.encode() + b":" + body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


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
        f":rotating_light: *{app}* — still waiting (past timeout)"
        if escalated
        else f":raised_hand: *{app}* needs a decision"
    )
    lines = [header, "", question.strip(), ""]
    links = []
    if issue_url:
        links.append(f"<{issue_url}|issue>")
    if dashboard_url:
        links.append(f"<{dashboard_url}|run>")
    meta = " · ".join(links)
    if branch:
        meta = f"{meta} · `{branch}`" if meta else f"`{branch}`"
    if meta:
        lines.append(f"_{meta}_")
    lines.append("Reply in this thread to continue.")
    return "\n".join(lines)


def _format_shipped_message(
    *,
    app: str,
    feature_title: str,
    issue_url: str | None,
    verification_result: str,
) -> str:
    header = f":rocket: *{app}* shipped to pre-prod: *{feature_title}*"
    lines = [header]
    if issue_url:
        lines.append(f"<{issue_url}|GitHub issue>")
    lines.append(verification_result.strip())
    return "\n".join(lines)


def notify_shipped(
    *,
    app: str,
    feature_title: str,
    issue_url: str | None = None,
    verification_result: str,
    channel: str | None = None,
) -> bool:
    """Posts a 'shipped to pre-prod' notification, once pre-prod delivery is actually confirmed
    (trivial merge success, or verify_pre_prod pass) -- never at the earlier status:shipped
    GitHub label stamp. `channel` is the app's own Slack channel (registry.get_slack_channel),
    falling back to the global SLACK_HUMAN_INPUT_CHANNEL env var when unset. No-ops (returns
    False) when Slack isn't configured or no channel resolves, and never raises (mirrors
    notify_human_input_required's fail-open behavior)."""
    if not is_configured():
        return False
    text = _format_shipped_message(
        app=app, feature_title=feature_title, issue_url=issue_url, verification_result=verification_result,
    )
    return _post_message(text, channel=channel) is not None


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
    channel: str | None = None,
    thread_ts: str | None = None,
) -> str | None:
    """Posts a HUMAN_INPUT_REQUIRED notification. `channel` is the app's own Slack channel
    (registry.get_slack_channel), falling back to the global SLACK_HUMAN_INPUT_CHANNEL env var
    when unset. `thread_ts` continues an existing conversation (GitHub issue #68's two-way
    loop -- a follow-up question lands in the same thread as the first). Returns the root
    message's `ts` (the thread anchor) or None if nothing was posted."""
    if not is_configured():
        return None
    text = _format_human_input_message(
        app=app, run_id=run_id, question=question, issue_url=issue_url, dashboard_url=dashboard_url,
        branch=branch, session_id=session_id, escalated=escalated,
    )
    resp = _post_message(text, channel=channel, thread_ts=thread_ts)
    if not resp:
        return None
    # For a threaded reply Slack returns the reply's own ts; keep the original
    # thread anchor so every round of this conversation maps to the same run.
    return thread_ts or resp.get("ts")

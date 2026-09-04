"""Parsing and evaluation of the optional SLACK_ALLOWED_USERS sender allowlist."""

from __future__ import annotations

import os

_ENV_VAR = "SLACK_ALLOWED_USERS"


def _allowed_ids() -> set[str]:
    raw = os.environ.get(_ENV_VAR, "") or ""
    return {entry.strip() for entry in raw.split(",") if entry.strip()}


def allowlist_configured() -> bool:
    """True when SLACK_ALLOWED_USERS names at least one Slack user id."""
    return bool(_allowed_ids())


def is_allowed(user_id: str | None) -> bool:
    """True only for an exact, case-sensitive match of user_id against the allowlist (fails closed on a missing id)."""
    if not user_id:
        return False
    return user_id in _allowed_ids()

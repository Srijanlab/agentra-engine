"""server/audit.py — attributes operator actions to the caller in the signals feed."""

from __future__ import annotations

from fastapi import Request

from agentra.server.utils import _server_log


def actor_for(request: Request) -> str:
    """Resolves the caller label: user email, fixed verify-token actor, `loop` for /internal, else `anonymous`."""
    state = request.state
    email = getattr(state, "user_email", None)
    if email:
        return email
    actor = getattr(state, "actor", None)
    if actor:
        return actor
    return "loop" if request.url.path.startswith("/internal/") else "anonymous"


def audit_log(request: Request, channel: str, message: str) -> None:
    """Logs a signal event carrying the request's actor."""
    _server_log(channel, message, actor=actor_for(request))

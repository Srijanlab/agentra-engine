"""Shared-secret checks for the self-authenticating /trigger/* endpoints."""

from __future__ import annotations

import base64
import hmac
import os

from fastapi import Header, HTTPException


def _firebase_configured() -> bool:
    return bool(os.environ.get("FIREBASE_PROJECT_ID"))


def _not_configured(what: str) -> HTTPException:
    return HTTPException(status_code=503, detail=f"{what} is not configured")


def _bearer_token(authorization: str | None) -> str:
    if authorization and authorization[:7].lower() == "bearer ":
        return authorization[7:].strip()
    return ""


def require_bearer(authorization: str | None, env_vars: tuple[str, ...]) -> None:
    """Require a bearer matching any configured secret in env_vars; fail closed under Firebase."""
    secrets = [s for s in (os.environ.get(v) for v in env_vars) if s]
    if not secrets:
        if _firebase_configured():
            raise _not_configured(" / ".join(env_vars))
        return
    token = _bearer_token(authorization)
    matches = [hmac.compare_digest(token.encode(), s.encode()) for s in secrets]
    if not token or not any(matches):
        raise HTTPException(status_code=401, detail="invalid bearer token")


def verify_tick_auth(authorization: str | None = Header(default=None)) -> None:
    require_bearer(authorization, ("AGENTRA_INTERNAL_TOKEN", "CRON_SECRET"))


def verify_queue_auth(authorization: str | None = Header(default=None)) -> None:
    require_bearer(authorization, ("AGENTRA_INTERNAL_TOKEN",))


def verify_alarm_auth(authorization: str | None = Header(default=None)) -> None:
    expected = os.environ.get("ALARM_WEBHOOK_PASSWORD")
    if not expected:
        if _firebase_configured():
            raise _not_configured("ALARM_WEBHOOK_PASSWORD")
        return
    if authorization is None or not authorization.startswith("Basic "):
        raise HTTPException(status_code=401, detail="missing Basic auth")
    try:
        decoded = base64.b64decode(authorization.removeprefix("Basic ")).decode("utf-8")
        _username, _, password = decoded.partition(":")
    except Exception:
        raise HTTPException(status_code=401, detail="malformed Basic auth")
    if not hmac.compare_digest(password.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="invalid credentials")

"""server/queue_auth.py — credential check for POST /trigger/queue (internal bearer or Pub/Sub OIDC)."""

from __future__ import annotations

import hmac
import logging
import os

from fastapi import Header, Request
from fastapi.responses import JSONResponse

from agentra.server.verify_token import unauthenticated_body

logger = logging.getLogger("agentra.server.queue_auth")

_QUEUE_AUTH_HINT = (
    "POST /trigger/queue accepts 'Authorization: Bearer <AGENTRA_INTERNAL_TOKEN>' or a Pub/Sub push OIDC token. "
    "Pub/Sub push deployments must set BOTH AGENTRA_PUBSUB_AUDIENCE and AGENTRA_PUBSUB_SERVICE_ACCOUNT_EMAIL "
    "(the email must match the verified service-account email claim)."
)


class QueueAuthError(Exception):
    """Raised when POST /trigger/queue credentials are missing or invalid."""


def queue_auth_body() -> dict:
    """401 body for /trigger/queue: the shared shape with a static, config-independent hint."""
    return {**unauthenticated_body(), "hint": _QUEUE_AUTH_HINT}


async def queue_auth_error_handler(request: Request, exc: QueueAuthError) -> JSONResponse:
    """Render QueueAuthError as the 401 body."""
    return JSONResponse(queue_auth_body(), status_code=401)


def _bearer(authorization: str | None) -> str:
    prefix = "bearer "
    if (authorization or "")[: len(prefix)].lower() == prefix:
        return authorization[len(prefix):].strip()
    return ""


def _internal_token_ok(token: str) -> bool:
    expected = os.environ.get("AGENTRA_INTERNAL_TOKEN")
    return bool(expected) and hmac.compare_digest(token.encode(), expected.encode())


def _oidc_ok(token: str) -> bool:
    audience = os.environ.get("AGENTRA_PUBSUB_AUDIENCE")
    if not audience:
        return False
    expected_email = os.environ.get("AGENTRA_PUBSUB_SERVICE_ACCOUNT_EMAIL")
    if not expected_email:
        logger.warning(
            "rejected pub/sub oidc token: AGENTRA_PUBSUB_SERVICE_ACCOUNT_EMAIL is required when AGENTRA_PUBSUB_AUDIENCE is set"
        )
        return False
    try:
        from google.auth.transport import requests as g_requests
        from google.oauth2 import id_token

        claims = id_token.verify_oauth2_token(token, g_requests.Request(), audience=audience)
    except Exception as exc:
        logger.info("rejected pub/sub oidc token: %s", exc)
        return False
    if claims.get("email") != expected_email or claims.get("email_verified") is not True:
        logger.warning("rejected pub/sub oidc token: email claim does not match a verified AGENTRA_PUBSUB_SERVICE_ACCOUNT_EMAIL")
        return False
    return True


def verify_queue_auth(authorization: str | None = Header(default=None)) -> None:
    """Reject the request with 401 unless it carries the internal token or a valid Pub/Sub OIDC token."""
    token = _bearer(authorization)
    if token and (_internal_token_ok(token) or _oidc_ok(token)):
        return
    raise QueueAuthError()

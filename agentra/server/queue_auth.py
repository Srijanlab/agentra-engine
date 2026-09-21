"""server/queue_auth.py — credential check for POST /trigger/queue (internal bearer or Pub/Sub OIDC)."""

from __future__ import annotations

import hmac
import logging
import os

from fastapi import Header, HTTPException

logger = logging.getLogger("agentra.server.queue_auth")


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
    try:
        from google.auth.transport import requests as g_requests
        from google.oauth2 import id_token

        claims = id_token.verify_oauth2_token(token, g_requests.Request(), audience=audience)
    except Exception as exc:
        logger.info("rejected pub/sub oidc token: %s", exc)
        return False
    expected_email = os.environ.get("AGENTRA_PUBSUB_SERVICE_ACCOUNT_EMAIL")
    if not expected_email:
        return True
    return claims.get("email") == expected_email and claims.get("email_verified") is True


def verify_queue_auth(authorization: str | None = Header(default=None)) -> None:
    """Reject the request with 401 unless it carries the internal token or a valid Pub/Sub OIDC token."""
    token = _bearer(authorization)
    if token and (_internal_token_ok(token) or _oidc_ok(token)):
        return
    raise HTTPException(status_code=401, detail="authentication required")

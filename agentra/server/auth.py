"""server/auth.py — Google (Firebase) sign-in gate for the dashboard API."""

from __future__ import annotations

import logging
import os

from fastapi import Request
from fastapi.responses import JSONResponse

logger = logging.getLogger("agentra.server.auth")

# Paths reachable without a signed-in user. Everything else needs a valid
# Firebase ID token whose email is in AGENTRA_ALLOWED_EMAILS.
_PUBLIC_PREFIXES = (
    "/health",
    "/healthz",
    "/favicon",
    "/assets/",
    "/slack/",            # Slack signs its own requests
    "/internal/",         # own bearer token (AGENTRA_INTERNAL_TOKEN)
    "/trigger/alarm",     # own Basic-auth password
    "/trigger/queue",     # internal enqueue path (loop / SQS)
    "/connectors/github/callback",  # GitHub OAuth redirect, no bearer possible
    "/debug/",            # temporary diagnostics
)
_PUBLIC_EXACT = {"", "/"}

_ISSUER_PREFIX = "https://securetoken.google.com/"


def _allowed_emails() -> set[str]:
    raw = os.environ.get("AGENTRA_ALLOWED_EMAILS", "")
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def _token_from(request: Request) -> str | None:
    header = request.headers.get("authorization") or ""
    if header[:7].lower() == "bearer ":
        return header[7:].strip()
    # EventSource cannot set headers -> accept the ID token as a query param.
    return request.query_params.get("access_token")


def _verify(token: str, project: str) -> dict | None:
    try:
        from google.auth.transport import requests as g_requests
        from google.oauth2 import id_token

        claims = id_token.verify_firebase_token(token, g_requests.Request(), audience=project)
    except Exception as exc:  # expired / malformed / wrong audience
        logger.info("rejected id token: %s", exc)
        return None
    return claims if claims.get("iss") == f"{_ISSUER_PREFIX}{project}" else None


async def auth_middleware(request: Request, call_next):
    path = request.url.path
    project = os.environ.get("FIREBASE_PROJECT_ID")
    if (
        request.method == "OPTIONS"
        or not project  # unconfigured (local dev / self-hosted) -> stay open
        or path in _PUBLIC_EXACT
        or path.startswith(_PUBLIC_PREFIXES)
    ):
        return await call_next(request)

    token = _token_from(request)
    claims = _verify(token, project) if token else None
    if claims is None:
        return JSONResponse({"detail": "authentication required"}, status_code=401)

    email = (claims.get("email") or "").lower()
    allowed = _allowed_emails()
    if allowed and email not in allowed:
        return JSONResponse({"detail": f"{email or 'this account'} is not authorized"}, status_code=403)

    request.state.user_email = email
    return await call_next(request)


CORS_ORIGIN_REGEX = os.environ.get(
    "AGENTRA_WEB_ORIGIN_REGEX",
    r"^(https://([a-z0-9-]+\.)?srijanlab\.com"
    r"|https://[a-z0-9-]+(--[a-z0-9-]+)?\.(web\.app|firebaseapp\.com)"
    r"|http://localhost:5173)$",
)

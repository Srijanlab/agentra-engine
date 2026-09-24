"""server/auth.py — Google (Firebase) sign-in gate for the dashboard API."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass

from fastapi import Request
from fastapi.responses import JSONResponse

from agentra.server.verify_token import check_verify_token, unauthenticated_body

logger = logging.getLogger("agentra.server.auth")

# Paths reachable without a signed-in user. Everything else needs a valid
# Firebase ID token whose email is in AGENTRA_ALLOWED_EMAILS.
_PUBLIC_PREFIXES = (
    "/health",
    "/healthz",
    "/favicon",
    "/internal/",         # own bearer token (AGENTRA_INTERNAL_TOKEN)
    "/trigger/alarm",     # own Basic-auth password
    "/trigger/queue",     # own bearer token or Pub/Sub OIDC (server/queue_auth.py)
    "/trigger/cron",      # own bearer token (AGENTRA_TICK_TOKEN / CRON_SECRET; internal token only as fallback)
    "/trigger/deploy-complete",  # own bearer token (AGENTRA_INTERNAL_TOKEN / CRON_SECRET)
    "/connectors/github/callback",  # GitHub OAuth redirect, no bearer possible
)
_PUBLIC_EXACT = {"", "/"}

_ISSUER_PREFIX = "https://securetoken.google.com/"

_QUERY_TOKEN_PATH = re.compile(r"^/runs/[^/]+/logs$")


def _allowed_emails() -> set[str]:
    raw = os.environ.get("AGENTRA_ALLOWED_EMAILS", "")
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def _firebase_project() -> str:
    return (os.environ.get("FIREBASE_PROJECT_ID") or "").strip()


def _cloud_configured() -> bool:
    """True when DynamoDB is live or its table prefix is set (init never raises, so creds can fail silently)."""
    try:
        from agentra import registry

        if registry.cloud_mode():
            return True
    except Exception:
        pass
    return bool((os.environ.get("AGENTRA_DYNAMODB_TABLE_PREFIX") or "").strip())


@dataclass(frozen=True)
class AuthStatus:
    """Snapshot of the auth gate's configuration; carries booleans only, never secret values."""

    cloud_mode: bool
    firebase_configured: bool
    allowlist_configured: bool

    @property
    def missing(self) -> list[str]:
        if not self.cloud_mode:
            return []
        out = [] if self.firebase_configured else ["FIREBASE_PROJECT_ID"]
        return out + ([] if self.allowlist_configured else ["AGENTRA_ALLOWED_EMAILS"])

    @property
    def mode(self) -> str:
        if self.missing:
            return "misconfigured"
        return "enforced" if self.firebase_configured else "open"

    @property
    def problems(self) -> list[str]:
        if self.missing:
            return [f"{m} is {'not set' if m == 'FIREBASE_PROJECT_ID' else 'empty'}" for m in self.missing]
        if not self.firebase_configured:
            return ["FIREBASE_PROJECT_ID is not set"]
        return [] if self.allowlist_configured else ["AGENTRA_ALLOWED_EMAILS is empty"]

    def as_dict(self) -> dict:
        return {
            "mode": self.mode,
            "cloud_mode": self.cloud_mode,
            "firebase_configured": self.firebase_configured,
            "allowlist_configured": self.allowlist_configured,
            "problems": self.problems,
        }


def auth_status() -> AuthStatus:
    """Compute the current auth mode from env and registry state; never raises."""
    try:
        return AuthStatus(_cloud_configured(), bool(_firebase_project()), bool(_allowed_emails()))
    except Exception:
        return AuthStatus(True, False, False)


def _warn_pubsub_email_missing() -> None:
    if os.environ.get("AGENTRA_PUBSUB_AUDIENCE") and not os.environ.get("AGENTRA_PUBSUB_SERVICE_ACCOUNT_EMAIL"):
        logger.warning(
            "AGENTRA_PUBSUB_AUDIENCE is set but AGENTRA_PUBSUB_SERVICE_ACCOUNT_EMAIL is not: "
            "Pub/Sub OIDC requests to /trigger/queue will be rejected until the email is set"
        )


def log_startup_warnings() -> None:
    """Warn once at startup when the API is unauthenticated or admits any Firebase account."""
    _warn_pubsub_email_missing()
    status = auth_status()
    if status.mode == "open":
        logger.warning("API is unauthenticated: neither DynamoDB nor FIREBASE_PROJECT_ID is configured")
    elif status.mode == "misconfigured":
        logger.warning("auth misconfigured, gated routes return 503: %s", "; ".join(status.problems))
    elif not status.allowlist_configured:
        logger.warning("AGENTRA_ALLOWED_EMAILS is empty: any Firebase-authenticated account is admitted")


def _token_from(request: Request) -> str | None:
    """Return the bearer token, falling back to ?access_token= only on the SSE logs path."""
    header = request.headers.get("authorization") or ""
    if header[:7].lower() == "bearer ":
        return header[7:].strip()
    if request.method == "GET" and _QUERY_TOKEN_PATH.match(request.url.path):
        return request.query_params.get("access_token")
    return None


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
    if request.method == "OPTIONS":
        return await call_next(request)
    verify = check_verify_token(request)
    if isinstance(verify, JSONResponse):
        return verify
    if verify is True:
        request.state.actor = "verify-token"
    if path in _PUBLIC_EXACT or path.startswith(_PUBLIC_PREFIXES) or verify is True:
        return await call_next(request)

    status = auth_status()
    if status.missing:
        return JSONResponse(
            {
                "detail": f"auth misconfigured: set {' and '.join(status.missing)}",
                "error": "auth_misconfigured",
                "missing": status.missing,
            },
            status_code=503,
        )
    if not status.firebase_configured:
        return await call_next(request)

    token = _token_from(request)
    claims = _verify(token, _firebase_project()) if token else None
    if claims is None:
        return JSONResponse(unauthenticated_body(), status_code=401)

    if claims.get("email_verified") is not True:
        return JSONResponse({"detail": "email not verified", "error": "email_not_verified"}, status_code=403)

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

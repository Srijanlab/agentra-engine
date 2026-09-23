"""Scoped read-only verification token for pre-prod black-box testing (env AGENTRA_VERIFY_TOKEN)."""

from __future__ import annotations

import hmac
import os
import re

from fastapi import Request
from fastapi.responses import JSONResponse

VERIFY_HEADER = "x-agentra-verify-token"

_ELIGIBLE = re.compile(r"^/(apps|apps/[^/]+/schedule|runs/[^/]+)$")

_AUTH_HINT = (
    "Provide a Firebase ID token via 'Authorization: Bearer <token>', or, for "
    "eligible read-only GET endpoints, the 'X-Agentra-Verify-Token' header."
)


def unauthenticated_body(detail: str = "authentication required") -> dict:
    """Shared 401 body shape: a detail string plus a non-sensitive error code and auth-flow hint."""
    return {"detail": detail, "error": "authentication_required", "hint": _AUTH_HINT}


def _is_production() -> bool:
    return any(
        (os.environ.get(var) or "").strip().lower() == "production" for var in ("VERCEL_ENV", "AGENTRA_ENVIRONMENT")
    )


def check_verify_token(request: Request) -> JSONResponse | bool | None:
    """None: no verify header or feature disabled (use the normal gate); True: authorized; JSONResponse: rejected."""
    supplied = request.headers.get(VERIFY_HEADER)
    if supplied is None:
        return None
    if _is_production():
        return JSONResponse({"detail": "verification token is not accepted in production"}, status_code=403)
    expected = os.environ.get("AGENTRA_VERIFY_TOKEN") or ""
    if not expected:
        return None
    eligible = request.method == "GET" and bool(_ELIGIBLE.match(request.url.path))
    if supplied and eligible and hmac.compare_digest(supplied.encode(), expected.encode()):
        return True
    return JSONResponse(unauthenticated_body(), status_code=401)

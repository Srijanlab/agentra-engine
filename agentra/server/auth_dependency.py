from fastapi import Request, Depends, HTTPException
import re
from .verify_token import _ELIGIBLE
from .auth import _cloud_configured, _allowed_emails, _firebase_project

# Dependency to enforce authentication on app routes
async def require_authentication(request: Request) -> None:
    """Ensure the request is authenticated either by Firebase ID token or a
    pre‑prod verification token for eligible GET endpoints.
    The Firebase auth gate has run in the auth middleware and, if valid,
    populated request.state.user_email.
    If no user_email is present, we fall back to the verification token
    used by the front‑end in pre‑production environments.
    """
    if getattr(request.state, "user_email", None):
        return
    # No Firebase user, try a verification token for a GET to an eligible path
    header = request.headers.get("x-agentra-verify-token")
    if not header:
        raise HTTPException(status_code=401, detail="authentication required")
    if request.method != "GET":
        raise HTTPException(status_code=401, detail="authentication required")
    if not _ELIGIBLE.match(request.url.path):
        raise HTTPException(status_code=401, detail="authentication required")
    expected = getattr(request.state, "verify_token", None)
    if not expected:
        expected = (lambda: None)()
    if not _ELIGIBLE.match(request.url.path):
        raise HTTPException(status_code=401, detail="authentication required")
    # Use verify_token logic to confirm the header matches the env
    import hmac
    if not hmac.compare_digest(header, (lambda: None)()):
        raise HTTPException(status_code=401, detail="authentication required")
    return

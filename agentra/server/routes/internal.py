from __future__ import annotations

import os
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel

from agentra import registry, git_ops
from agentra.connectors import github_app

logger = logging.getLogger("agentra.server.internal")

router = APIRouter(prefix="/internal")

# ---------------------------------------------------------------------------
# token gating helpers
# ---------------------------------------------------------------------------

def _client_ips(request: Request) -> set[str]:
    ips: set[str] = set()
    for header in ("x-real-ip", "x-vercel-forwarded-for"):
        for part in request.headers.get(header, "").split(","):
            part = part.strip()
            if part:
                ips.add(part)
    if not ips and request.client:
        ips.add(request.client.host)
    return ips


def _require_token(request: Request, authorization: str | None = Header(default=None)) -> None:
    allowed = {ip.strip() for ip in os.environ.get("AGENTRA_INTERNAL_ALLOWED_IPS", "").split(",") if ip.strip()}
    if allowed and not (_client_ips(request) & allowed):
        raise HTTPException(status_code=403, detail="not allowed from this address")
    expected = os.environ.get("AGENTRA_INTERNAL_TOKEN")
    if not expected:
        raise HTTPException(status_code=503, detail="internal API not configured")
    prefix = "bearer "
    got = (
        authorization[len(prefix):] if (authorization or "").lower().startswith(prefix) else ""
    )
    if not hmac.compare_digest(got, expected):
        raise HTTPException(status_code=401, detail="bad internal token")

# ---------------------------------------------------------------------------
# models
# ---------------------------------------------------------------------------
class GitTokenRequest(BaseModel):
    repo_url: str

class PushBranchRequest(BaseModel):
    repo_url: str
    branch: str

# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------
@router.post("/git-token", dependencies=[Depends(_require_token)])
async def git_token(req: GitTokenRequest) -> dict:
    """A short‑lived installation token for git clone/push – the loop never holds
    the GitHub App private key."""
    try:
        token = github_app.get_installation_token(req.repo_url)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"{type(exc).__name__}: {exc}") from exc
    return {"token": token}

@router.post("/internal/test/push-branch", dependencies=[Depends(_require_token)])
async def push_branch_test(req: PushBranchRequest) -> dict:
    """Debug route for testing push_branch behavior."""
    try:
        git_ops.push_branch(Path(req.repo_url), req.branch)
    except git_ops.GitOpError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    sha = subprocess.run(
        ["git", "-C", str(Path(req.repo_url)), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return {"commit_sha": sha}

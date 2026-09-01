"""server/routes/connectors.py — GitHub App integration and metadata views."""

from __future__ import annotations

import logging
import os
from fastapi import APIRouter, HTTPException
from fastapi.responses import RedirectResponse

from agentra import registry
from agentra.connectors import github_app

logger = logging.getLogger(__name__)

router = APIRouter()

DEV_MODE = os.environ.get("AGENTRA_DEV_MODE") == "1"
if DEV_MODE:
    from agentra.connectors import github_fake
    github_fake.install(persist_path=registry.AGENTRA_HOME / "dev_github_fake.json")

_DEV_INSTALLATION = {"account": "dev-local", "type": "User", "repository_selection": "all"}
_DEV_REPOS = [
    {
        "full_name": "dev-local/example-app",
        "clone_url": "https://github.com/dev-local/example-app.git",
        "default_branch": "main",
        "private": False,
        "account": "dev-local",
    }
]


@router.get("/connectors/github")
async def github_connector_status() -> dict:
    if DEV_MODE and not github_app.is_configured():
        return {"configured": True, "installations": [_DEV_INSTALLATION], "error": None, "install_url": None}
    if not github_app.is_configured():
        token = os.environ.get("GITHUB_TOKEN")
        apps = registry.list_apps()
        if token or apps:
            accounts = set()
            for app_info in apps.values():
                url = app_info.get("repo_url", "")
                owner_repo = github_app.owner_repo_from_url(url)
                if owner_repo:
                    accounts.add(owner_repo.split("/")[0])
            installations = [
                {"account": acc, "type": "Account", "repository_selection": "selected"}
                for acc in sorted(accounts)
            ]
            if not installations and token:
                installations = [{"account": "github-token", "type": "Token", "repository_selection": "all"}]
            return {
                "configured": True,
                "installations": installations,
                "error": None,
                "install_url": None,
            }
        return {"configured": False, "installations": [], "error": None, "install_url": None}
    try:
        installations = github_app.list_installations()
        return {
            "configured": True,
            "installations": installations,
            "error": None,
            "install_url": github_app.install_url(),
        }
    except github_app.GitHubAppError as exc:
        return {"configured": True, "installations": [], "error": str(exc), "install_url": github_app.install_url()}


@router.get("/connectors/github/callback")
async def github_connector_callback() -> RedirectResponse:
    return RedirectResponse(url="/")


@router.get("/connectors/github/repos")
async def github_connector_repos() -> dict:
    if DEV_MODE and not github_app.is_configured():
        return {"repos": _DEV_REPOS}
    if not github_app.is_configured():
        apps = registry.list_apps()
        repos = []
        for name, info in apps.items():
            url = info.get("repo_url", "")
            owner_repo = github_app.owner_repo_from_url(url) or name
            repos.append({
                "full_name": owner_repo,
                "clone_url": url,
                "default_branch": info.get("branch", "main"),
                "private": True,
                "account": owner_repo.split("/")[0] if "/" in owner_repo else "github-token",
            })
        return {"repos": repos}
    try:
        return {"repos": github_app.list_repos()}
    except github_app.GitHubAppError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

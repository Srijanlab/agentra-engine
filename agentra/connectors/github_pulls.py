"""GitHub Pull Requests REST API, authenticated via the installation token from
github_app.py -- used by deployment.py's "external" promotion strategy: for a code
repo whose deploy agentra can't drive directly (no Vercel/Firebase/self-hosted-VM
target, e.g. agentra-loop's own push-triggered GitHub Actions workflow), promotion
IS opening and merging a pre_prod_branch -> prod_branch PR, nothing more -- the merge
itself (after any required CI checks) is what triggers that repo's deploy."""

from __future__ import annotations

import logging

import httpx

from agentra.connectors.github_app import GITHUB_API, get_installation_token, owner_repo_from_url

logger = logging.getLogger(__name__)


class GitHubPullsError(Exception):
    """The repo isn't a github.com HTTPS URL, or the Pulls API call failed unexpectedly."""


def _headers(repo_url: str) -> dict[str, str]:
    token = get_installation_token(repo_url)
    return {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}


def _owner_repo_or_raise(repo_url: str) -> str:
    owner_repo = owner_repo_from_url(repo_url)
    if owner_repo is None:
        raise GitHubPullsError(f"not a github.com HTTPS URL: {repo_url!r}")
    return owner_repo


def _find_open_pr(repo_url: str, owner_repo: str, head: str, base: str) -> dict | None:
    owner = owner_repo.split("/", 1)[0]
    resp = httpx.get(
        f"{GITHUB_API}/repos/{owner_repo}/pulls",
        headers=_headers(repo_url),
        params={"head": f"{owner}:{head}", "base": base, "state": "open"},
        timeout=15,
    )
    resp.raise_for_status()
    prs = resp.json()
    return prs[0] if prs else None


def open_or_merge_promotion_pr(repo_url: str, head: str, base: str, *, title: str) -> str:
    """Opens a head -> base PR if none is already open, then merges it. Returns a short
    human-readable status string; raises GitHubPullsError / httpx.HTTPStatusError on an
    unexpected failure (a 405 not-mergeable-yet response is not an error -- it's reported
    back as a status string so the caller can just relay it)."""
    owner_repo = _owner_repo_or_raise(repo_url)
    pr = _find_open_pr(repo_url, owner_repo, head, base)
    if pr is None:
        create_resp = httpx.post(
            f"{GITHUB_API}/repos/{owner_repo}/pulls",
            headers=_headers(repo_url),
            json={"title": title, "head": head, "base": base},
            timeout=15,
        )
        if create_resp.status_code == 422 and "No commits between" in create_resp.text:
            return f"Nothing to promote -- {base!r} is already up to date with {head!r}."
        create_resp.raise_for_status()
        pr = create_resp.json()

    number = pr["number"]
    merge_resp = httpx.put(
        f"{GITHUB_API}/repos/{owner_repo}/pulls/{number}/merge",
        headers=_headers(repo_url),
        json={"merge_method": "merge"},
        timeout=30,
    )
    if merge_resp.status_code == 405:
        return (
            f"PR #{number} ({pr.get('html_url')}) is open but not mergeable yet "
            "(checks pending, or a conflict) -- not merged this cycle."
        )
    merge_resp.raise_for_status()
    return f"Merged PR #{number}: {head!r} -> {base!r}."

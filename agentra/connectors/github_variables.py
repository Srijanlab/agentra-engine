"""GitHub Actions repository Variables -- plain-text config, unlike
Secrets (which are write-only and can't be read back), authenticated via
the same installation-token minting github_app.py already does for git
operations and github_issues.py's backlog sync.

Used as the authoritative store for environments.yaml/objective.yaml
config when the target repo has a github.com remote and the App has the
"Variables" repository permission, falling back to the local YAML mirror
otherwise -- same hybrid pattern as connectors/github_issues.py.
"""

from __future__ import annotations

import httpx

from agentra.connectors.github_app import GITHUB_API, get_installation_token, owner_repo_from_url


class GitHubVariablesError(Exception):
    """The repo isn't a github.com HTTPS URL, or the Variables API call
    itself failed (e.g. a 4xx/5xx GitHub returned)."""


def _owner_repo_or_raise(repo_url: str) -> str:
    owner_repo = owner_repo_from_url(repo_url)
    if owner_repo is None:
        raise GitHubVariablesError(f"not a github.com HTTPS URL: {repo_url!r}")
    return owner_repo


def _headers(repo_url: str) -> dict[str, str]:
    token = get_installation_token(repo_url)
    return {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}


def list_variables(repo_url: str) -> dict[str, str]:
    """Every repo-level Actions variable, name -> value, in one paginated
    sweep -- callers that need several named variables (environments.py's
    ~11 AGENTRA_* fields) should call this once and look up locally rather
    than making one HTTP round-trip per variable."""
    owner_repo = _owner_repo_or_raise(repo_url)
    headers = _headers(repo_url)
    variables: dict[str, str] = {}
    page = 1
    while True:
        resp = httpx.get(
            f"{GITHUB_API}/repos/{owner_repo}/actions/variables",
            headers=headers,
            params={"per_page": 100, "page": page},
            timeout=15,
        )
        resp.raise_for_status()
        page_variables = resp.json().get("variables", [])
        for v in page_variables:
            variables[v["name"]] = v["value"]
        if len(page_variables) < 100:
            break
        page += 1
    return variables


def set_variable(repo_url: str, name: str, value: str) -> None:
    """Creates the variable if it doesn't exist yet, updates it otherwise
    -- GitHub's API has no upsert endpoint, so this tries PATCH (update)
    first and falls back to POST (create) on a 404."""
    owner_repo = _owner_repo_or_raise(repo_url)
    headers = _headers(repo_url)
    resp = httpx.patch(
        f"{GITHUB_API}/repos/{owner_repo}/actions/variables/{name}",
        headers=headers,
        json={"name": name, "value": value},
        timeout=15,
    )
    if resp.status_code == 404:
        resp = httpx.post(
            f"{GITHUB_API}/repos/{owner_repo}/actions/variables",
            headers=headers,
            json={"name": name, "value": value},
            timeout=15,
        )
    resp.raise_for_status()

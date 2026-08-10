"""GitHub App-based repo access.

Mints short-lived (1h) installation access tokens on demand instead of
relying on one static, manually-scoped PAT. The original TASK-014
GITHUB_TOKEN was a fine-grained PAT scoped to a handful of repos at
creation time -- confirmed live, registering ContentAutomationPlatform
through the dashboard (TASK-016) 403'd outright because that org was never
in its scope. A GitHub App scales to any number of orgs/repos the App gets
installed on, without hand-editing a token's repo list every time.

Two GCP-sourced env vars configure this: GITHUB_APP_ID (numeric) and
GITHUB_APP_PRIVATE_KEY (PEM, from the App's settings page -> "Generate a
private key"). Absent either, every function here raises
GitHubAppNotConfigured -- git_ops.py catches that and falls back to the
static GITHUB_TOKEN/GIT_ASKPASS path, so a deployment without the App
configured keeps working exactly as it did before this module existed.
"""

from __future__ import annotations

import calendar
import os
import re
import time

import httpx
import jwt

GITHUB_API = "https://api.github.com"

# installations rarely change; a cache miss just re-fetches from the API,
# so staleness here is cheap to recover from, not a correctness risk.
_installation_id_cache: dict[str, int] = {}
_token_cache: dict[int, tuple[str, float]] = {}
_slug_cache: str | None = None


class GitHubAppNotConfigured(Exception):
    """GITHUB_APP_ID/GITHUB_APP_PRIVATE_KEY aren't set -- caller should
    fall back to the static-token path, not treat this as a real error."""


class GitHubAppError(Exception):
    """The App is configured but the actual GitHub API call failed --
    e.g. not installed on the requested repo, a malformed private key."""


def _app_credentials() -> tuple[str, str]:
    app_id = os.environ.get("GITHUB_APP_ID")
    private_key = os.environ.get("GITHUB_APP_PRIVATE_KEY")
    if not app_id or not private_key:
        raise GitHubAppNotConfigured("GITHUB_APP_ID/GITHUB_APP_PRIVATE_KEY not set")
    return app_id, private_key


def _app_jwt() -> str:
    app_id, private_key = _app_credentials()
    now = int(time.time())
    payload = {
        "iat": now - 60,  # tolerate clock drift, per GitHub's own guidance
        "exp": now + 9 * 60,  # GitHub allows at most 10 minutes
        "iss": app_id,
    }
    try:
        return jwt.encode(payload, private_key, algorithm="RS256")
    except (ValueError, jwt.InvalidKeyError) as exc:
        raise GitHubAppError(f"malformed GITHUB_APP_PRIVATE_KEY: {exc}") from exc


def _github_get(path: str) -> httpx.Response:
    resp = httpx.get(
        f"{GITHUB_API}{path}",
        headers={"Authorization": f"Bearer {_app_jwt()}", "Accept": "application/vnd.github+json"},
        timeout=15,
    )
    return resp


def owner_repo_from_url(repo_url: str) -> str | None:
    """'https://github.com/OWNER/REPO.git' -> 'OWNER/REPO'; None for
    anything not a github.com HTTPS URL (SSH URLs, other hosts) -- those
    fall straight back to the static-token path in git_ops.py, since the
    App can't help with them regardless of whether it's configured."""
    m = re.match(r"^https://github\.com/([^/]+)/([^/]+?)(\.git)?/?$", repo_url)
    return f"{m.group(1)}/{m.group(2)}" if m else None


def _get_installation_id(owner_repo: str) -> int:
    if owner_repo in _installation_id_cache:
        return _installation_id_cache[owner_repo]
    resp = _github_get(f"/repos/{owner_repo}/installation")
    if resp.status_code == 404:
        raise GitHubAppError(f"agentra's GitHub App is not installed on {owner_repo}")
    resp.raise_for_status()
    installation_id = resp.json()["id"]
    _installation_id_cache[owner_repo] = installation_id
    return installation_id


def _mint_token_for_installation(installation_id: int) -> str:
    """Shared by get_installation_token (per-repo) and list_repos
    (per-installation) -- both ultimately need the same 1h access token for
    a given installation, minted fresh or reused from cache if still valid
    for at least another 2 minutes."""
    cached = _token_cache.get(installation_id)
    if cached and cached[1] - time.time() > 120:
        return cached[0]

    resp = httpx.post(
        f"{GITHUB_API}/app/installations/{installation_id}/access_tokens",
        headers={"Authorization": f"Bearer {_app_jwt()}", "Accept": "application/vnd.github+json"},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    token = data["token"]
    # expires_at is UTC ("...Z") -- calendar.timegm, not time.mktime, which
    # would silently reinterpret it in the local timezone.
    expires_at = calendar.timegm(time.strptime(data["expires_at"], "%Y-%m-%dT%H:%M:%SZ"))
    _token_cache[installation_id] = (token, expires_at)
    return token


def get_installation_token(repo_url: str) -> str:
    """The one entry point git_ops.py needs: an installation access token
    good for git operations against `repo_url`. Raises
    GitHubAppNotConfigured if the App isn't set up, or GitHubAppError if
    it's configured but not installed on this particular repo."""
    owner_repo = owner_repo_from_url(repo_url)
    if owner_repo is None:
        raise GitHubAppError(f"not a github.com HTTPS URL: {repo_url!r}")
    installation_id = _get_installation_id(owner_repo)
    return _mint_token_for_installation(installation_id)


def _raw_installations() -> list[dict]:
    resp = _github_get("/app/installations")
    resp.raise_for_status()
    return resp.json()


def list_installations() -> list[dict]:
    """For the dashboard's connector status panel -- every account
    (user/org) the App is installed on, and how broad that install is."""
    return [
        {
            "account": inst["account"]["login"],
            "type": inst["account"]["type"],
            "repository_selection": inst["repository_selection"],
        }
        for inst in _raw_installations()
    ]


def list_repos() -> list[dict]:
    """Every repo reachable across every installation of the App -- for the
    dashboard's "Register App" repo picker, so registering a repo is a
    click instead of finding and pasting its clone URL by hand."""
    repos: list[dict] = []
    for inst in _raw_installations():
        token = _mint_token_for_installation(inst["id"])
        resp = httpx.get(
            f"{GITHUB_API}/installation/repositories",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            params={"per_page": 100},
            timeout=15,
        )
        resp.raise_for_status()
        for repo in resp.json()["repositories"]:
            repos.append(
                {
                    "full_name": repo["full_name"],
                    "clone_url": repo["clone_url"],
                    "default_branch": repo["default_branch"],
                    "private": repo["private"],
                    "account": inst["account"]["login"],
                }
            )
    return repos


def install_url() -> str | None:
    """Public 'install this App' page -- the dashboard links here so
    connecting a new org/account is a click from agentra's own UI instead
    of hunting through GitHub's App settings pages. Only works for
    accounts other than the App's own owner if the App's "Where can this
    GitHub App be installed?" setting is "Any account" -- still "Only on
    this account" leaves this link functional only for the owner's own
    orgs, a GitHub-side setting this code can't change for you. None if
    the App isn't configured at all, or the slug lookup fails (e.g. a bad
    key) -- not worth raising over, the rest of the status payload still
    reports the real problem."""
    global _slug_cache
    if _slug_cache is not None:
        return f"https://github.com/apps/{_slug_cache}/installations/new"
    if not is_configured():
        return None
    try:
        resp = _github_get("/app")
        resp.raise_for_status()
        _slug_cache = resp.json()["slug"]
    except (httpx.HTTPError, GitHubAppError, KeyError):
        return None
    return f"https://github.com/apps/{_slug_cache}/installations/new"


def is_configured() -> bool:
    try:
        _app_credentials()
        return True
    except GitHubAppNotConfigured:
        return False

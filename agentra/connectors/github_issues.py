"""GitHub Issues REST API, authenticated via the same installation-token
minting github_app.py already does for git operations -- no new credential
plumbing, just a new use of the token git_ops.py already mints for pushes.

GitHub Issues is the sole backlog store now (see memory.py's known_bugs()/
feature_queue()/shipped_features() -- no local .agentra/*.json mirror). A
shipped feature is a closed issue labeled "enhancement" too: record_shipped()
either closes an existing feature_queue issue or opens-and-immediately-closes
a fresh one, so "what's pending" and "what shipped" are just open vs. closed
issues under the same label, with no separate ledger to keep in sync.

Requires the GitHub App to actually be installed with Issues read/write
permission on the target repo (confirmed granted, per the user) -- raises
GitHubAppNotConfigured/GitHubAppError (from github_app.py) the same way
git operations do if it isn't.
"""

from __future__ import annotations

import httpx

from agentra.connectors.github_app import GITHUB_API, get_installation_token, owner_repo_from_url


class GitHubIssuesError(Exception):
    """The repo isn't a github.com HTTPS URL, or the Issues API call itself
    failed (e.g. a 4xx/5xx GitHub returned)."""


def _owner_repo_or_raise(repo_url: str) -> str:
    owner_repo = owner_repo_from_url(repo_url)
    if owner_repo is None:
        raise GitHubIssuesError(f"not a github.com HTTPS URL: {repo_url!r}")
    return owner_repo


def _headers(repo_url: str) -> dict[str, str]:
    token = get_installation_token(repo_url)
    return {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}


def create_issue(repo_url: str, title: str, body: str, labels: list[str] | None = None) -> dict:
    """Returns the created issue's JSON (number, html_url, etc.) -- callers
    that need to track it back to a known_bug/feature_queue entry should
    store `number` as that entry's external_id, the existing dedup hook
    point (memory.py's known_bugs/feature_queue already key on external_id).

    Label names that don't already exist on the target repo are silently
    dropped by GitHub's API rather than erroring -- Phase 2 wiring should
    either create "agentra:bug"/"agentra:feature" labels once per repo
    (POST /repos/{owner_repo}/labels) or tolerate unlabeled issues."""
    owner_repo = _owner_repo_or_raise(repo_url)
    resp = httpx.post(
        f"{GITHUB_API}/repos/{owner_repo}/issues",
        headers=_headers(repo_url),
        json={"title": title, "body": body, "labels": labels or []},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def list_open_issues(repo_url: str, labels: list[str] | None = None) -> list[dict]:
    """Open issues only, optionally filtered to any of `labels` (GitHub's
    `labels` query param is an OR of exact label names). GitHub's issues
    endpoint also returns pull requests (a PR is an issue internally) --
    filtered out here since callers only ever want real issues."""
    owner_repo = _owner_repo_or_raise(repo_url)
    params: dict[str, str | int] = {"state": "open", "per_page": 100}
    if labels:
        params["labels"] = ",".join(labels)
    resp = httpx.get(
        f"{GITHUB_API}/repos/{owner_repo}/issues",
        headers=_headers(repo_url),
        params=params,
        timeout=15,
    )
    resp.raise_for_status()
    return [issue for issue in resp.json() if "pull_request" not in issue]


def close_issue(
    repo_url: str, issue_number: int, comment: str | None = None, body_suffix: str | None = None
) -> None:
    """Optionally posts `comment` (e.g. "resolved by <feature name>,
    shipped in <commit_sha>") before closing, so the issue's own history
    records why/how -- same audit-trail intent as memory.py's old
    known_bugs.json entries, just visible on GitHub instead of only in
    .agentra/.

    `body_suffix`, if given, is appended to the issue's body (after an
    extra GET to fetch the current body) before closing -- unlike a
    comment, the body is returned inline by the issues-list endpoint, so
    memory.py's shipped_features() can read structured fields (run_id,
    commit_sha) back from a closed issue with the same list call it uses
    to find it, no per-issue follow-up request needed."""
    owner_repo = _owner_repo_or_raise(repo_url)
    headers = _headers(repo_url)
    if comment:
        resp = httpx.post(
            f"{GITHUB_API}/repos/{owner_repo}/issues/{issue_number}/comments",
            headers=headers,
            json={"body": comment},
            timeout=15,
        )
        resp.raise_for_status()
    patch_json: dict = {"state": "closed"}
    if body_suffix:
        get_resp = httpx.get(
            f"{GITHUB_API}/repos/{owner_repo}/issues/{issue_number}", headers=headers, timeout=15
        )
        get_resp.raise_for_status()
        current_body = get_resp.json().get("body") or ""
        patch_json["body"] = current_body.rstrip() + "\n\n" + body_suffix
    resp = httpx.patch(
        f"{GITHUB_API}/repos/{owner_repo}/issues/{issue_number}",
        headers=headers,
        json=patch_json,
        timeout=15,
    )
    resp.raise_for_status()


def list_closed_issues(repo_url: str, labels: list[str] | None = None, limit: int = 30) -> list[dict]:
    """Closed issues only, newest-closed first, optionally filtered to any of
    `labels`. Used by memory.py's shipped_features() -- a shipped feature is
    a closed 'enhancement'-labeled issue, so this is that ledger's only read
    path, no local mirror involved."""
    owner_repo = _owner_repo_or_raise(repo_url)
    params: dict[str, str | int] = {
        "state": "closed",
        "sort": "updated",
        "direction": "desc",
        "per_page": limit,
    }
    if labels:
        params["labels"] = ",".join(labels)
    resp = httpx.get(
        f"{GITHUB_API}/repos/{owner_repo}/issues",
        headers=_headers(repo_url),
        params=params,
        timeout=15,
    )
    resp.raise_for_status()
    return [issue for issue in resp.json() if "pull_request" not in issue]

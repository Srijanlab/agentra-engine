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

import logging

import httpx

from agentra.connectors.github_app import GITHUB_API, get_installation_token, owner_repo_from_url

logger = logging.getLogger(__name__)


class GitHubIssuesError(Exception):
    """The repo isn't a github.com HTTPS URL, or the Issues API call itself
    failed (e.g. a 4xx/5xx GitHub returned), or (create_sub_issue's own
    GraphQL call) a GraphQL-level `errors` array came back."""


def _owner_repo_or_raise(repo_url: str) -> str:
    owner_repo = owner_repo_from_url(repo_url)
    if owner_repo is None:
        raise GitHubIssuesError(f"not a github.com HTTPS URL: {repo_url!r}")
    return owner_repo


def _headers(repo_url: str) -> dict[str, str]:
    token = get_installation_token(repo_url)
    return {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}


def get_issue(repo_url: str, issue_number: int) -> dict | None:
    """Single issue's REST JSON, or None if it doesn't exist. Used to look
    up a parent issue's title when recording a sub-feature (memory.py's
    record_shipped, sub_feature_of) -- the sub-feature's Project board is
    the parent's, titled after the parent, so a fresh board needs the
    parent's actual title, not the sub-feature's own."""
    owner_repo = _owner_repo_or_raise(repo_url)
    resp = httpx.get(
        f"{GITHUB_API}/repos/{owner_repo}/issues/{issue_number}", headers=_headers(repo_url), timeout=15
    )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


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


def _graphql(repo_url: str, query: str, variables: dict) -> dict:
    """GitHub's REST API has no sub-issue endpoint -- addSubIssue only
    exists in GraphQL v4. Every other function in this module stays REST
    (simpler, and it's all GitHub's REST Issues API already covers); this
    is the one GraphQL escape hatch, kept local to create_sub_issue rather
    than importing connectors/github_projects.py's own copy, so this
    module has no dependency on that one."""
    token = get_installation_token(repo_url)
    resp = httpx.post(
        f"{GITHUB_API}/graphql",
        headers={"Authorization": f"Bearer {token}"},
        json={"query": query, "variables": variables},
        timeout=15,
    )
    resp.raise_for_status()
    body = resp.json()
    if body.get("errors"):
        raise GitHubIssuesError(f"GraphQL errors: {body['errors']}")
    return body["data"]


def create_sub_issue(
    repo_url: str, parent_issue_number: int, title: str, body: str, labels: list[str] | None = None
) -> dict:
    """Creates a new issue and links it as a GitHub-native sub-issue of
    parent_issue_number (GraphQL addSubIssue, confirmed live against a
    real repo) -- GitHub then tracks a real "sub-issues progress" count on
    the parent, visible on its Project card too if it's on one, instead of
    agentra having to maintain that relationship itself.

    The link is best-effort, same as every other secondary relationship in
    this codebase (e.g. record_shipped's Project sync): the sub-issue
    itself -- the primary, valuable artifact -- is always created and
    returned even if linking it under the parent fails (logged, not
    raised)."""
    sub_issue = create_issue(repo_url, title, body, labels=labels)
    try:
        owner_repo = _owner_repo_or_raise(repo_url)
        parent_resp = httpx.get(
            f"{GITHUB_API}/repos/{owner_repo}/issues/{parent_issue_number}", headers=_headers(repo_url), timeout=15
        )
        parent_resp.raise_for_status()
        parent_node_id = parent_resp.json()["node_id"]
        _graphql(
            repo_url,
            """
            mutation($issueId: ID!, $subIssueId: ID!) {
              addSubIssue(input: {issueId: $issueId, subIssueId: $subIssueId}) {
                subIssue { id }
              }
            }
            """,
            {"issueId": parent_node_id, "subIssueId": sub_issue["node_id"]},
        )
    except Exception:
        logger.warning(
            "create_sub_issue: created issue #%s but failed to link it under #%s on %s",
            sub_issue["number"], parent_issue_number, repo_url, exc_info=True,
        )
    return sub_issue


def list_in_progress_features(repo_url: str, labels: list[str] | None = None) -> list[dict]:
    """Open issues that already have at least one sub-issue -- a
    multi-part feature the Orchestrator started splitting up but hasn't
    yet signaled complete (memory.py's record_shipped only closes the
    parent once more_parts_expected=False on a later sub_feature_of
    call). A plain not-yet-started feature_queue entry has zero
    sub-issues, so this and list_open_issues never overlap. Each entry
    carries sub_issues_total/sub_issues_completed (GitHub's own
    subIssuesSummary) so a caller can tell "half done" from "just
    started" without a follow-up call per issue."""
    owner, name = _owner_repo_or_raise(repo_url).split("/", 1)
    data = _graphql(
        repo_url,
        """
        query($owner: String!, $name: String!, $labels: [String!]) {
          repository(owner: $owner, name: $name) {
            issues(states: OPEN, first: 50, labels: $labels) {
              nodes {
                number
                title
                body
                url
                subIssuesSummary { total completed }
              }
            }
          }
        }
        """,
        {"owner": owner, "name": name, "labels": labels or None},
    )
    return [
        {
            "number": i["number"],
            "title": i["title"],
            "body": i.get("body"),
            "html_url": i.get("url"),
            "sub_issues_total": i["subIssuesSummary"]["total"],
            "sub_issues_completed": i["subIssuesSummary"]["completed"],
        }
        for i in data["repository"]["issues"]["nodes"]
        if i["subIssuesSummary"]["total"] > 0
    ]


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

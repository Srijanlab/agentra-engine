"""GitHub Issues REST API, authenticated via the installation token
from github_app.py — no new credential plumbing, just a new use of
the token git_ops.py already mints for pushes.

GitHub Issues is the sole backlog store (memory.py's known_bugs()/
feature_queue()/shipped_features() — no local .agentra/*.json mirror).
A shipped feature stays OPEN, stamped "status:shipped" (mark_shipped in
github_issue_lifecycle.py) — only actual production promotion closes it
(memory.py's mark_status_done). So an issue's open/closed state maps
1:1 onto the dashboard's Ready to Review (open, status:shipped) vs
Release to Production (closed) split.

Lifecycle write operations (close_issue, mark_shipped, record_in_progress_branch,
record_spec, etc.) live in github_issue_lifecycle.py to keep this module
read-heavy. Re-exported here for backward-compatible imports.
"""

from __future__ import annotations

import json
import logging
import re

import httpx

from agentra.connectors.github_app import GITHUB_API, get_installation_token, owner_repo_from_url

logger = logging.getLogger(__name__)


class GitHubIssuesError(Exception):
    """The repo isn't a github.com HTTPS URL, or the Issues API call failed
    (4xx/5xx from GitHub), or a GraphQL-level errors array came back."""


def _owner_repo_or_raise(repo_url: str) -> str:
    owner_repo = owner_repo_from_url(repo_url)
    if owner_repo is None:
        raise GitHubIssuesError(f"not a github.com HTTPS URL: {repo_url!r}")
    return owner_repo


def _headers(repo_url: str) -> dict[str, str]:
    token = get_installation_token(repo_url)
    return {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}


def _graphql(repo_url: str, query: str, variables: dict) -> dict:
    """GitHub's REST API has no sub-issue endpoint — addSubIssue and
    subIssuesSummary only exist in GraphQL v4. Every other function stays
    REST; this is the shared escape hatch."""
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


def get_issue(repo_url: str, issue_number: int) -> dict | None:
    """Single issue's REST JSON, or None if it doesn't exist."""
    owner_repo = _owner_repo_or_raise(repo_url)
    resp = httpx.get(
        f"{GITHUB_API}/repos/{owner_repo}/issues/{issue_number}", headers=_headers(repo_url), timeout=15
    )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


def create_issue(repo_url: str, title: str, body: str, labels: list[str] | None = None) -> dict:
    """Returns the created issue's JSON (number, html_url, etc.).

    Label names that don't already exist on the target repo are silently
    dropped by GitHub's API rather than erroring — ensure_labels() at
    app registration prevents this in practice."""
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
    """Open issues only, optionally filtered by labels (AND semantics).
    Filters out pull requests, which GitHub's issues endpoint also returns."""
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


def list_closed_issues(repo_url: str, labels: list[str] | None = None, limit: int = 5) -> list[dict]:
    """Closed issues only, newest-closed first. Used by memory.py's
    shipped_features() — no local mirror involved."""
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


def create_sub_issue(
    repo_url: str, parent_issue_number: int, title: str, body: str, labels: list[str] | None = None
) -> dict:
    """Creates a new issue and links it as a GitHub-native sub-issue of
    parent_issue_number (GraphQL addSubIssue). GitHub then tracks a real
    sub-issues progress count on the parent.

    The link is best-effort: the sub-issue itself is always created and
    returned even if linking it under the parent fails."""
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


def fetch_app_digest_batch(repo_urls: list[str], closed_limit: int = 5) -> dict[str, dict]:
    """Single GraphQL call fetching open bugs, open features, and most-recent
    closed issues for every repo in repo_urls simultaneously.

    Returns a dict keyed by repo_url:
      {
        "open_bugs":     [{"number", "title", "body", "html_url", "labels"}, ...],
        "open_features": [{"number", "title", "body", "html_url", "labels"}, ...],
        "closed_features": [...],   # newest closed_limit items, feature-labeled
        "closed_bugs":   [...],     # newest closed_limit items, bug-labeled
      }

    Falls back to an empty dict for any repo that has no github.com remote or
    whose token can't be fetched.  If the whole batch call fails the caller
    should degrade gracefully rather than explode.

    Alias naming: GraphQL field aliases can't contain slashes or dots, so each
    repo is addressed by a stable index (r0, r1, …) and the result is mapped
    back to repo_urls by position.
    """
    if not repo_urls:
        return {}

    # All repos must share the same installation token — they do in practice
    # (they all live under the same GitHub App installation). Use the first
    # url's token for the single request.
    first_url = repo_urls[0]
    try:
        token = get_installation_token(first_url)
    except Exception:
        logger.warning("fetch_app_digest_batch: could not get installation token for %s", first_url)
        return {}

    # Build one big aliased query.  Each repo contributes four aliased fields.
    fragments: list[str] = []
    owner_repos: list[str | None] = []
    for idx, url in enumerate(repo_urls):
        or_ = owner_repo_from_url(url)
        owner_repos.append(or_)
        if or_ is None:
            # Not a github.com URL — pad with empty aliases so indices stay aligned
            fragments.append(f"r{idx}_ob: __typename r{idx}_of: __typename r{idx}_cf: __typename r{idx}_cb: __typename")
            continue
        owner, name = or_.split("/", 1)
        fragments.append(f"""
        r{idx}_ob: repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) {{
          issues(states: OPEN, first: 100, labels: ["bug", "agentra"], orderBy: {{field: UPDATED_AT, direction: DESC}}) {{
            nodes {{ number title body url labels(first: 20) {{ nodes {{ name }} }} }}
          }}
        }}
        r{idx}_of: repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) {{
          issues(states: OPEN, first: 100, labels: ["feature", "agentra"], orderBy: {{field: UPDATED_AT, direction: DESC}}) {{
            nodes {{ number title body url labels(first: 20) {{ nodes {{ name }} }} }}
          }}
        }}
        r{idx}_cf: repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) {{
          issues(states: CLOSED, first: {closed_limit}, labels: ["feature", "agentra"], orderBy: {{field: UPDATED_AT, direction: DESC}}) {{
            nodes {{ number title body url closedAt labels(first: 20) {{ nodes {{ name }} }} }}
          }}
        }}
        r{idx}_cb: repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) {{
          issues(states: CLOSED, first: {closed_limit}, labels: ["bug", "agentra"], orderBy: {{field: UPDATED_AT, direction: DESC}}) {{
            nodes {{ number title body url closedAt labels(first: 20) {{ nodes {{ name }} }} }}
          }}
        }}
        """)

    query = "{ " + "\n".join(fragments) + " }"
    try:
        resp = httpx.post(
            f"{GITHUB_API}/graphql",
            headers={"Authorization": f"Bearer {token}"},
            json={"query": query},
            timeout=20,
        )
        resp.raise_for_status()
        body = resp.json()
        if body.get("errors"):
            logger.warning("fetch_app_digest_batch: GraphQL errors: %s", body["errors"])
            return {}
        data = body["data"]
    except Exception:
        logger.warning("fetch_app_digest_batch: request failed", exc_info=True)
        return {}

    def _normalise_node(node: dict) -> dict:
        return {
            "number": node["number"],
            "title": node["title"],
            "body": node.get("body"),
            "html_url": node.get("url"),
            "closed_at": node.get("closedAt"),
            "labels": [l["name"] for l in node.get("labels", {}).get("nodes", [])],
            "state": "closed" if node.get("closedAt") else "open",
        }

    result: dict[str, dict] = {}
    for idx, url in enumerate(repo_urls):
        if owner_repos[idx] is None:
            continue
        ob = data.get(f"r{idx}_ob") or {}
        of_ = data.get(f"r{idx}_of") or {}
        cf = data.get(f"r{idx}_cf") or {}
        cb = data.get(f"r{idx}_cb") or {}
        result[url] = {
            "open_bugs":      [_normalise_node(n) for n in (ob.get("issues") or {}).get("nodes", [])],
            "open_features":  [_normalise_node(n) for n in (of_.get("issues") or {}).get("nodes", [])],
            "closed_features":[_normalise_node(n) for n in (cf.get("issues") or {}).get("nodes", [])],
            "closed_bugs":    [_normalise_node(n) for n in (cb.get("issues") or {}).get("nodes", [])],
        }
    return result


def list_in_progress_features(repo_url: str, labels: list[str] | None = None) -> list[dict]:
    """Open issues that already have at least one sub-issue. Each entry
    carries sub_issues_total/sub_issues_completed from GitHub's own
    subIssuesSummary. Excludes issues labeled 'status:shipped'."""
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
                labels(first: 20) { nodes { name } }
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
        and "status:shipped" not in {l["name"] for l in i["labels"]["nodes"]}
    ]


_LABEL_DEFINITIONS: dict[str, tuple[str, str]] = {
    "agentra": ("5319e7", "Created by agentra, or for agentra to work on -- everything agentra's dashboard/backlog reads is filtered to this"),
    "bug": ("d73a4a", "Something isn't working"),
    "feature": ("a2eeef", "A whole feature -- may have multiple 'story' sub-issues"),
    "story": ("c5def5", "One part of a multi-part feature"),
    "need_human": ("fbca04", "Needs a human decision or action -- agentra should not attempt this"),
    "blocking_agentra": ("b60205", "Blocks agentra's own further progress until a human resolves it"),
    "status:in-progress": ("fef2c0", "Real work has started -- see In-Progress-Branch comment for where"),
    "status:shipped": ("bfd4f2", "Implemented and committed, still open awaiting production promotion"),
    "status:done": ("0e8a16", "Actually deployed to production"),
}


def ensure_labels(repo_url: str) -> None:
    """Creates whichever of _LABEL_DEFINITIONS don't already exist on the
    target repo. Idempotent, called once at app registration. Best-effort:
    a failure (e.g. App lacks repo admin permission) just falls back to
    today's behavior, not a registration failure."""
    owner_repo = _owner_repo_or_raise(repo_url)
    headers = _headers(repo_url)
    resp = httpx.get(f"{GITHUB_API}/repos/{owner_repo}/labels", headers=headers, params={"per_page": 100}, timeout=15)
    resp.raise_for_status()
    existing = {lbl["name"] for lbl in resp.json()}
    for name, (color, description) in _LABEL_DEFINITIONS.items():
        if name in existing:
            continue
        create_resp = httpx.post(
            f"{GITHUB_API}/repos/{owner_repo}/labels",
            headers=headers,
            json={"name": name, "color": color, "description": description},
            timeout=15,
        )
        if create_resp.status_code != 422:  # 422 = already exists from a concurrent create
            create_resp.raise_for_status()


def add_labels(repo_url: str, issue_number: int, labels: list[str]) -> None:
    """Adds labels to an existing issue without touching existing ones.
    GitHub's add-labels endpoint is additive, not a replace."""
    owner_repo = _owner_repo_or_raise(repo_url)
    httpx.post(
        f"{GITHUB_API}/repos/{owner_repo}/issues/{issue_number}/labels",
        headers=_headers(repo_url),
        json={"labels": labels},
        timeout=15,
    ).raise_for_status()


def add_comment(repo_url: str, issue_number: int, comment: str) -> None:
    """Posts a comment without changing the issue's state."""
    owner_repo = _owner_repo_or_raise(repo_url)
    httpx.post(
        f"{GITHUB_API}/repos/{owner_repo}/issues/{issue_number}/comments",
        headers=_headers(repo_url),
        json={"body": comment},
        timeout=15,
    ).raise_for_status()


def list_comments(repo_url: str, issue_number: int) -> list[dict]:
    """Oldest-first (GitHub's own default order)."""
    owner_repo = _owner_repo_or_raise(repo_url)
    resp = httpx.get(
        f"{GITHUB_API}/repos/{owner_repo}/issues/{issue_number}/comments",
        headers=_headers(repo_url),
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


# Backward-compatible re-exports from github_issue_lifecycle.py
from agentra.connectors.github_issue_lifecycle import (  # noqa: E402
    close_issue,
    get_in_progress_branch,
    get_in_progress_run_id,
    get_in_progress_session_id,
    get_spec,
    mark_shipped,
    record_commit,
    record_in_progress_branch,
    record_spec,
)

__all__ = [
    "fetch_app_digest_batch",
    "GitHubIssuesError",
    "get_issue",
    "create_issue",
    "list_open_issues",
    "list_closed_issues",
    "create_sub_issue",
    "list_in_progress_features",
    "ensure_labels",
    "add_labels",
    "add_comment",
    "list_comments",
    "close_issue",
    "mark_shipped",
    "record_in_progress_branch",
    "get_in_progress_branch",
    "get_in_progress_run_id",
    "get_in_progress_session_id",
    "record_commit",
    "record_spec",
    "get_spec",
]

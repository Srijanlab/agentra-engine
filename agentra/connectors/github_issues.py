"""GitHub Issues REST API, authenticated via the installation token from github_app.py — no new credential plumbing, just a new use of the token git_ops.py already mints for pushes."""

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


def issue_html_url(repo_url: str, issue_number: int) -> str | None:
    """The browsable https://github.com/OWNER/REPO/issues/N URL for an issue, or None if repo_url isn't a github.com HTTPS remote."""
    owner_repo = owner_repo_from_url(repo_url)
    if owner_repo is None:
        return None
    return f"https://github.com/{owner_repo}/issues/{issue_number}"


def _headers(repo_url: str) -> dict[str, str]:
    token = get_installation_token(repo_url)
    return {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}


def _graphql(repo_url: str, query: str, variables: dict) -> dict:
    """GitHub's REST API has no sub-issue endpoint — addSubIssue and subIssuesSummary only exist in GraphQL v4."""
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
    """Returns the created issue's JSON (number, html_url, etc.)."""
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
    """Creates a new issue and links it as a GitHub-native sub-issue of parent_issue_number (GraphQL addSubIssue)."""
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


def add_issue_as_sub_issue(repo_url: str, parent_issue_number: int, sub_issue_number: int) -> None:
    """Links an already-existing issue as a GitHub-native sub-issue of parent_issue_number (GraphQL addSubIssue) --
    for retroactively organizing issues filed independently, unlike create_sub_issue which files a new one."""
    owner_repo = _owner_repo_or_raise(repo_url)
    parent_resp = httpx.get(
        f"{GITHUB_API}/repos/{owner_repo}/issues/{parent_issue_number}", headers=_headers(repo_url), timeout=15
    )
    parent_resp.raise_for_status()
    sub_resp = httpx.get(
        f"{GITHUB_API}/repos/{owner_repo}/issues/{sub_issue_number}", headers=_headers(repo_url), timeout=15
    )
    sub_resp.raise_for_status()
    _graphql(
        repo_url,
        """
        mutation($issueId: ID!, $subIssueId: ID!) {
          addSubIssue(input: {issueId: $issueId, subIssueId: $subIssueId}) {
            subIssue { id }
          }
        }
        """,
        {"issueId": parent_resp.json()["node_id"], "subIssueId": sub_resp.json()["node_id"]},
    )


def fetch_app_digest_batch(repo_urls: list[str], closed_limit: int = 5) -> dict[str, dict]:
    """Single GraphQL call fetching open bugs, open features, and most-recent closed issues for every repo in repo_urls simultaneously."""
    if not repo_urls:
        return {}

    # Group repos by installation token — repos from different GitHub App
    from collections import defaultdict
    token_to_urls: dict[str, list[str]] = defaultdict(list)
    for url in repo_urls:
        if owner_repo_from_url(url) is None:
            continue  # not a github.com URL, skip
        try:
            token = get_installation_token(url)
            token_to_urls[token].append(url)
        except Exception:
            logger.warning("fetch_app_digest_batch: could not get installation token for %s, skipping", url)

    if not token_to_urls:
        return {}

    merged: dict[str, dict] = {}
    for token, urls in token_to_urls.items():
        merged.update(_fetch_batch_for_token(token, urls, closed_limit))
    return merged


def _fetch_batch_for_token(token: str, repo_urls: list[str], closed_limit: int) -> dict[str, dict]:

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
            # GraphQL returns partial results alongside errors — log the
            failed = {e.get("path", [None])[0] for e in body["errors"] if e.get("path")}
            logger.warning("fetch_app_digest_batch: partial GraphQL errors for fields %s", sorted(failed))
        data = body.get("data") or {}
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
    """Open issues that already have at least one sub-issue."""
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
    "agentra": ("5319e7", "Required on every tracked issue -- the backlog GraphQL filter looks for this label"),
    "bug": ("d73a4a", "Something isn't working"),
    "feature": ("a2eeef", "A whole feature -- may have multiple 'story' sub-issues"),
    "story": ("c5def5", "One part of a multi-part feature"),
    "discovery": ("0052cc", "Self-originated by Discovery when the backlog was empty, not customer/dashboard-submitted"),
    "need_human": ("fbca04", "Needs a human decision or action -- agentra should not attempt this"),
    "blocking_agentra": ("b60205", "Blocks agentra's own further progress until a human resolves it"),
    "status:in-progress": ("fef2c0", "Real work has started -- see In-Progress-Branch comment for where"),
    "status:code_complete": ("c2e0c6", "Coding done, pushed to its remote feature branch -- awaiting merge to pre-prod/beta"),
    "status:shipped": ("bfd4f2", "Merged into pre-prod/beta -- awaiting live verification (verify_pre_prod)"),
    "status:tested": ("1d76db", "Live-verified against pre-prod (verify_pre_prod passed) -- awaiting production promotion"),
    "status:done": ("0e8a16", "Actually deployed to production"),
}


def ensure_labels(repo_url: str) -> None:
    """Creates whichever of _LABEL_DEFINITIONS don't already exist on the target repo."""
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
        if create_resp.status_code == 422 and any(
            e.get("code") == "already_exists" for e in create_resp.json().get("errors", [])
        ):
            continue  # a concurrent create won the race
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


def remove_label(repo_url: str, issue_number: int, label: str) -> None:
    """Removes a single label from an issue."""
    owner_repo = _owner_repo_or_raise(repo_url)
    resp = httpx.delete(
        f"{GITHUB_API}/repos/{owner_repo}/issues/{issue_number}/labels/{label}",
        headers=_headers(repo_url),
        timeout=15,
    )
    if resp.status_code != 404:
        resp.raise_for_status()


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
    find_unanswered_human_input_comment,
    get_human_input_context,
    get_in_progress_branch,
    get_in_progress_run_id,
    list_run_ids_for_issue,
    get_in_progress_session_id,
    get_spec,
    mark_code_complete,
    mark_shipped,
    mark_shipped_to_preprod,
    mark_tested,
    escalate_existing_issue,
    find_tracking_issue_for_branch,
    record_commit,
    record_human_answer,
    record_human_input_context,
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
    "remove_label",
    "add_comment",
    "list_comments",
    "issue_html_url",
    "close_issue",
    "mark_shipped",
    "mark_code_complete",
    "mark_shipped_to_preprod",
    "mark_tested",
    "record_in_progress_branch",
    "get_in_progress_branch",
    "get_in_progress_run_id",
    "list_run_ids_for_issue",
    "get_in_progress_session_id",
    "record_commit",
    "record_human_input_context",
    "get_human_input_context",
    "record_human_answer",
    "find_unanswered_human_input_comment",
    "record_spec",
    "get_spec",
    "escalate_existing_issue",
    "find_tracking_issue_for_branch",
]

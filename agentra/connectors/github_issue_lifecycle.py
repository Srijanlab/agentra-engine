"""Issue lifecycle write operations: in-progress branch tracking, spec storage,
commit linking, and shipped/closed status transitions.

Kept separate from github_issues.py's CRUD layer so that layer stays
read-heavy and this one owns every mutation that changes an issue's state
beyond its initial creation.
"""

from __future__ import annotations

import json
import re

from agentra.connectors.github_issues import add_comment, add_labels, list_comments


_IN_PROGRESS_BRANCH_RE = re.compile(r"^In-Progress-Branch: (\S+)$", re.MULTILINE)
_IN_PROGRESS_RUN_ID_RE = re.compile(r"^Run-ID: (\S+)$", re.MULTILINE)
_IN_PROGRESS_SESSION_ID_RE = re.compile(r"^Session-ID: (\S+)$", re.MULTILINE)

_SPEC_MARKER = "Spec (agentra):"
_SPEC_JSON_RE = re.compile(r"```json\s*\n(.*?)\n```", re.DOTALL)


def record_in_progress_branch(
    repo_url: str, issue_number: int, branch: str, run_id: str | None = None, session_id: str | None = None
) -> None:
    """Marks branch (and run_id for traceability) as where an interrupted
    implement_feature call's work lives. Posted as a plain comment so
    get_in_progress_branch can read the most recent one back without ever
    touching the issue body, avoiding conflicts with record_shipped's own
    body_suffix stamping on the same issue.

    Without this, an interrupted cycle's code changes exist only on the
    VM's local disk — gone the moment REPOS_ROOT gets re-cloned on
    redeploy. Confirmed live: GitHub issue #13's fix was lost exactly
    this way when run_local_tests failed and the cycle never reached
    deploy_pre_prod.

    session_id: the Claude session this issue's build is using, so a later
    resumed cycle (get_in_progress_session_id) continues the same
    conversation instead of cold-starting a new one for this issue."""
    body = f"In-Progress-Branch: {branch}"
    if run_id:
        body += f"\nRun-ID: {run_id}"
    if session_id:
        body += f"\nSession-ID: {session_id}"
    add_comment(repo_url, issue_number, body)


def get_in_progress_branch(repo_url: str, issue_number: int) -> str | None:
    """The most recently recorded in-progress branch for this issue, or None.
    Walks comments newest-first (GitHub returns oldest-first by default)."""
    comments = list_comments(repo_url, issue_number)
    for comment in reversed(comments):
        match = _IN_PROGRESS_BRANCH_RE.search(comment.get("body") or "")
        if match:
            return match.group(1)
    return None


def get_in_progress_run_id(repo_url: str, issue_number: int) -> str | None:
    """The run_id alongside the most recent in-progress-branch marker, or None.
    A marker recorded before this field existed has no run_id line."""
    comments = list_comments(repo_url, issue_number)
    for comment in reversed(comments):
        body = comment.get("body") or ""
        if not _IN_PROGRESS_BRANCH_RE.search(body):
            continue
        match = _IN_PROGRESS_RUN_ID_RE.search(body)
        if match:
            return match.group(1)
        return None  # marker exists but has no run_id
    return None


def get_in_progress_session_id(repo_url: str, issue_number: int) -> str | None:
    """The Claude session_id alongside the most recent in-progress-branch
    marker, or None. Same read pattern as get_in_progress_run_id -- a marker
    recorded before this field existed (or a turn that never produced a
    session_id) simply has no Session-ID line."""
    comments = list_comments(repo_url, issue_number)
    for comment in reversed(comments):
        body = comment.get("body") or ""
        if not _IN_PROGRESS_BRANCH_RE.search(body):
            continue
        match = _IN_PROGRESS_SESSION_ID_RE.search(body)
        return match.group(1) if match else None
    return None


def record_commit(repo_url: str, issue_number: int, commit_sha: str) -> None:
    """Links commit_sha on the tracking issue as a plain comment. GitHub
    auto-links a bare 40-char sha to its commit in the same repo.

    A tracking issue's work can span more than one commit (multi-part
    features, resumed calls, self-heal fix-ups) — record_shipped's body
    stamp only captures the last one, so this is the only place the full
    commit history for an issue is visible."""
    add_comment(repo_url, issue_number, f"Commit: {commit_sha}")


def record_spec(repo_url: str, issue_number: int, spec: dict) -> None:
    """Persists Requirements Agent's finalized spec as a comment. Uses the
    same post/read-most-recent pattern as record_in_progress_branch so a
    resumed cycle can reuse the spec without regenerating it, and Testing
    Agent's pre-prod pass has real acceptance_criteria to verify against
    without codebase access."""
    body = f"{_SPEC_MARKER}\n\n```json\n{json.dumps(spec, indent=2)}\n```"
    add_comment(repo_url, issue_number, body)


def get_spec(repo_url: str, issue_number: int) -> dict | None:
    """Most recently recorded spec, or None. A corrupt stored comment is
    treated the same as 'no spec yet' rather than raising, so it can't
    block a new spec from being generated."""
    comments = list_comments(repo_url, issue_number)
    for comment in reversed(comments):
        body = comment.get("body") or ""
        if not body.startswith(_SPEC_MARKER):
            continue
        match = _SPEC_JSON_RE.search(body)
        if not match:
            continue
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
    return None


def close_issue(
    repo_url: str, issue_number: int, comment: str | None = None, body_suffix: str | None = None
) -> None:
    """Optionally posts comment then closes. body_suffix, if given, is
    appended to the issue body before closing — unlike a comment, the body
    is returned inline by the issues-list endpoint, so memory.py's
    shipped_features() can read structured fields (run_id, commit_sha)
    from a closed issue with the same list call, no per-issue follow-up."""
    import httpx
    from agentra.connectors.github_issues import _headers, _owner_repo_or_raise
    from agentra.connectors.github_app import GITHUB_API

    owner_repo = _owner_repo_or_raise(repo_url)
    headers = _headers(repo_url)
    if comment:
        httpx.post(
            f"{GITHUB_API}/repos/{owner_repo}/issues/{issue_number}/comments",
            headers=headers,
            json={"body": comment},
            timeout=15,
        ).raise_for_status()
    patch_json: dict = {"state": "closed"}
    if body_suffix:
        get_resp = httpx.get(
            f"{GITHUB_API}/repos/{owner_repo}/issues/{issue_number}", headers=headers, timeout=15
        )
        get_resp.raise_for_status()
        current_body = get_resp.json().get("body") or ""
        patch_json["body"] = current_body.rstrip() + "\n\n" + body_suffix
    httpx.patch(
        f"{GITHUB_API}/repos/{owner_repo}/issues/{issue_number}",
        headers=headers,
        json=patch_json,
        timeout=15,
    ).raise_for_status()


def mark_shipped(repo_url: str, issue_number: int, comment: str | None = None, body_suffix: str | None = None) -> None:
    """Like close_issue but leaves the issue OPEN and stamps 'status:shipped'.
    A shipped feature stays open through the 'Ready to Review' stage; only
    actual production promotion closes it (memory.py's mark_status_done,
    called from server.py's _record_production_release). This keeps an
    issue's open/closed state 1:1 with the dashboard's Ready to Review /
    Release to Production split."""
    import httpx
    from agentra.connectors.github_issues import _headers, _owner_repo_or_raise
    from agentra.connectors.github_app import GITHUB_API

    owner_repo = _owner_repo_or_raise(repo_url)
    headers = _headers(repo_url)
    if comment:
        httpx.post(
            f"{GITHUB_API}/repos/{owner_repo}/issues/{issue_number}/comments",
            headers=headers,
            json={"body": comment},
            timeout=15,
        ).raise_for_status()
    if body_suffix:
        get_resp = httpx.get(
            f"{GITHUB_API}/repos/{owner_repo}/issues/{issue_number}", headers=headers, timeout=15
        )
        get_resp.raise_for_status()
        current_body = get_resp.json().get("body") or ""
        httpx.patch(
            f"{GITHUB_API}/repos/{owner_repo}/issues/{issue_number}",
            headers=headers,
            json={"body": current_body.rstrip() + "\n\n" + body_suffix},
            timeout=15,
        ).raise_for_status()
    add_labels(repo_url, issue_number, ["status:shipped"])

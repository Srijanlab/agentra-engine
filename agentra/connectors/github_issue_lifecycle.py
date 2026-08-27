"""Issue lifecycle write operations: in-progress branch tracking, spec storage, commit linking, and shipped/closed status transitions."""

from __future__ import annotations

import json
import re

from agentra.connectors.github_issues import add_comment, add_labels, list_comments, list_open_issues


_IN_PROGRESS_BRANCH_RE = re.compile(r"^In-Progress-Branch: (\S+)$", re.MULTILINE)
_IN_PROGRESS_RUN_ID_RE = re.compile(r"^Run-ID: (\S+)$", re.MULTILINE)
_IN_PROGRESS_SESSION_ID_RE = re.compile(r"^Session-ID: (\S+)$", re.MULTILINE)

_SPEC_MARKER = "Spec (agentra):"
_SPEC_JSON_RE = re.compile(r"```json\s*\n(.*?)\n```", re.DOTALL)

# Human-in-the-loop escalation (GitHub issue #34): resume-correlation data
_HUMAN_INPUT_MARKER = "Human-Input-Required (agentra):"
_HUMAN_INPUT_APP_RE = re.compile(r"^App: (\S+)$", re.MULTILINE)
_HUMAN_INPUT_RUN_ID_RE = re.compile(r"^Run-ID: (\S+)$", re.MULTILINE)
_HUMAN_INPUT_BRANCH_RE = re.compile(r"^Branch: (\S+)$", re.MULTILINE)
_HUMAN_INPUT_SESSION_ID_RE = re.compile(r"^Session-ID: (\S+)$", re.MULTILINE)
_HUMAN_INPUT_TRACKING_ISSUE_RE = re.compile(r"^Tracking-Issue: (\d+)$", re.MULTILINE)
_HUMAN_INPUT_QUESTION_RE = re.compile(r"^Question: (.*)$", re.MULTILINE)


def record_in_progress_branch(
    repo_url: str, issue_number: int, branch: str, run_id: str | None = None, session_id: str | None = None
) -> None:
    """Marks branch (and run_id for traceability) as where an interrupted implement_feature call's work lives."""
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


def find_tracking_issue_for_branch(repo_url: str, branch: str, agentra_label: str) -> int | None:
    """The open issue (bug or feature) already tracking `branch` via its own In-Progress-Branch
    marker, or None. GitHub issue #97 (and #85/#89 before it): record_code_complete's
    more_parts_expected path created a brand-new parent tracking issue every time it was called
    without an explicit resolves_id, even when the branch being committed was already tracked by
    an existing issue -- confirmed live (issue #92's real work landed under a new #97 instead).
    This is the structural check (branch identity, not fuzzy text similarity) that lets
    record_code_complete reuse the real tracking issue instead. Bounded by the number of
    currently-open agentra-managed issues, checked only on this comparatively rare path."""
    for issue in list_open_issues(repo_url, labels=[agentra_label]):
        if get_in_progress_branch(repo_url, issue["number"]) == branch:
            return issue["number"]
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
    """The Claude session_id alongside the most recent in-progress-branch marker, or None."""
    comments = list_comments(repo_url, issue_number)
    for comment in reversed(comments):
        body = comment.get("body") or ""
        if not _IN_PROGRESS_BRANCH_RE.search(body):
            continue
        match = _IN_PROGRESS_SESSION_ID_RE.search(body)
        return match.group(1) if match else None
    return None


def record_commit(repo_url: str, issue_number: int, commit_sha: str) -> None:
    """Links commit_sha on the tracking issue as a plain comment."""
    add_comment(repo_url, issue_number, f"Commit: {commit_sha}")


def record_spec(repo_url: str, issue_number: int, spec: dict) -> None:
    """Persists Requirements Agent's finalized spec as a comment."""
    body = f"{_SPEC_MARKER}\n\n```json\n{json.dumps(spec, indent=2)}\n```"
    add_comment(repo_url, issue_number, body)


def get_spec(repo_url: str, issue_number: int) -> dict | None:
    """Most recently recorded spec, or None."""
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


def record_human_input_context(
    repo_url: str,
    issue_number: int,
    *,
    app: str,
    run_id: str,
    question: str,
    branch: str | None = None,
    session_id: str | None = None,
    tracking_issue: int | None = None,
) -> None:
    """Stamps the resume-correlation data a HUMAN_INPUT_REQUIRED escalation needs to resume later -- app id, run id, branch, Claude session_id, the ORIGINAL tracking issue (implement_feature's resolves_id/sub_feature_of, distinct from this needs_human issue itself -- None for an escalation with no separate tracking issue, e.g."""
    question_line = " ".join(question.split())  # flatten to one line for the regex reader below
    body = f"{_HUMAN_INPUT_MARKER}\nApp: {app}\nRun-ID: {run_id}\n"
    if branch:
        body += f"Branch: {branch}\n"
    if session_id:
        body += f"Session-ID: {session_id}\n"
    if tracking_issue is not None:
        body += f"Tracking-Issue: {tracking_issue}\n"
    body += f"Question: {question_line}"
    add_comment(repo_url, issue_number, body)


def get_human_input_context(repo_url: str, issue_number: int) -> dict | None:
    """The most recently stamped resume-correlation data for a needs_human issue, or None if it was never stamped (e.g."""
    comments = list_comments(repo_url, issue_number)
    for comment in reversed(comments):
        body = comment.get("body") or ""
        if not body.startswith(_HUMAN_INPUT_MARKER):
            continue
        app_m = _HUMAN_INPUT_APP_RE.search(body)
        run_id_m = _HUMAN_INPUT_RUN_ID_RE.search(body)
        branch_m = _HUMAN_INPUT_BRANCH_RE.search(body)
        session_id_m = _HUMAN_INPUT_SESSION_ID_RE.search(body)
        tracking_issue_m = _HUMAN_INPUT_TRACKING_ISSUE_RE.search(body)
        question_m = _HUMAN_INPUT_QUESTION_RE.search(body)
        return {
            "app": app_m.group(1) if app_m else None,
            "run_id": run_id_m.group(1) if run_id_m else None,
            "branch": branch_m.group(1) if branch_m else None,
            "session_id": session_id_m.group(1) if session_id_m else None,
            "tracking_issue": int(tracking_issue_m.group(1)) if tracking_issue_m else None,
            "question": question_m.group(1) if question_m else None,
        }
    return None


# Every marker this module posts as a plain comment, in one place, so
_INTERNAL_COMMENT_PREFIXES = (
    "In-Progress-Branch:",
    _SPEC_MARKER,
    "Commit:",
    _HUMAN_INPUT_MARKER,
    "Answered:",
)


def find_unanswered_human_input_comment(repo_url: str, issue_number: int) -> str | None:
    """Polling-based half of the GitHub-issue-comment answer channel -- no inbound webhook (see connectors/slack.py's module docstring and design.md for why Slack-reply-driven resume is deferred to its own security review)."""
    comments = list_comments(repo_url, issue_number)
    marker_seen = False
    for comment in comments:  # oldest-first: the marker must come before its answer
        body = comment.get("body") or ""
        if body.startswith(_HUMAN_INPUT_MARKER):
            marker_seen = True
            continue
        if not marker_seen:
            continue
        if any(body.startswith(prefix) for prefix in _INTERNAL_COMMENT_PREFIXES):
            continue
        if not body.strip():
            continue
        return body.strip()
    return None


def record_human_answer(repo_url: str, issue_number: int, answer: str, resumed_run_key: str | None = None) -> None:
    """Comments the human's answer onto the needs_human issue -- called once a dashboard answer submission (or a GitHub-comment-driven resume via find_unanswered_human_input_comment) has been accepted."""
    body = f"Answered: {answer}"
    if resumed_run_key:
        body += f"\n\nResuming as run {resumed_run_key}."
    add_comment(repo_url, issue_number, body)


def escalate_existing_issue(repo_url: str, issue_number: int, run_id: str, full_diagnosis: str, labels: list[str]) -> None:
    """Escalates directly on the issue already tracking this work -- comments the blocking question and adds `labels` (typically just the needs_human label) -- instead of filing a separate needs_human issue for work that already has a home (confirmed live: issues #79, #80, #81, same interrupted item, three separate escalation issues, when the question should have just landed on the tracking issue itself)."""
    add_comment(repo_url, issue_number, f"Blocked, needs human input (run {run_id}):\n\n{full_diagnosis}")
    add_labels(repo_url, issue_number, labels)


def close_issue(
    repo_url: str, issue_number: int, comment: str | None = None, body_suffix: str | None = None
) -> None:
    """Optionally posts comment then closes."""
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
    """Like close_issue but leaves the issue OPEN and stamps 'status:shipped'."""
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


def mark_code_complete(repo_url: str, issue_number: int, comment: str | None = None, body_suffix: str | None = None) -> None:
    """First stage of the shipping pipeline: coding is done and pushed to its remote feature branch, not yet merged anywhere. Leaves the issue OPEN. Clears status:in-progress -- add_labels alone is additive and would otherwise leave both labels on the issue simultaneously (confirmed live on issue #78 itself)."""
    import httpx
    from agentra.connectors.github_issues import _headers, _owner_repo_or_raise, remove_label
    from agentra.connectors.github_app import GITHUB_API

    remove_label(repo_url, issue_number, "status:in-progress")

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
    add_labels(repo_url, issue_number, ["status:code_complete"])


def mark_shipped_to_preprod(repo_url: str, issue_number: int, comment: str | None = None) -> None:
    """Second stage: the code-complete branch has been merged into pre-prod/beta. Transitions status:code_complete -> status:shipped."""
    from agentra.connectors.github_issues import remove_label

    if comment:
        add_comment(repo_url, issue_number, comment)
    remove_label(repo_url, issue_number, "status:code_complete")
    add_labels(repo_url, issue_number, ["status:shipped"])


def mark_tested(repo_url: str, issue_number: int, comment: str | None = None) -> None:
    """Third stage: verify_pre_prod passed against the live pre-prod deployment. Transitions status:shipped -> status:tested."""
    from agentra.connectors.github_issues import remove_label

    if comment:
        add_comment(repo_url, issue_number, comment)
    remove_label(repo_url, issue_number, "status:shipped")
    add_labels(repo_url, issue_number, ["status:tested"])

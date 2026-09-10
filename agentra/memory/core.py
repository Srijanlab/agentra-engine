"""memory/core.py — module-level helpers, label constants, and converters
shared by the Memory mixins. No class definitions here."""

from __future__ import annotations

import datetime as dt
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

CATEGORIES = ("architecture",)

# The per-repo spec files (docs/agentra-spec.md). Live at .agentra/<name>.md --
# NOT under .agentra/memory/ -- and are agent-maintained, one set per code repo.
SPEC_FILES = ("architecture", "design", "testing")

_SPEC_HEADER_RE = re.compile(
    r"\A<!-- owner: [^>\n]+ -->\n<!-- source-sha: [0-9a-f]* -->\n", re.MULTILINE
)


def spec_header(owner: str, sha: str | None) -> str:
    """The two-line provenance header every non-JSON spec file carries: who owns
    it (`human`, `agent:codebase`, `agent:testing`, `generated`) and the commit
    the content was last built from."""
    return f"<!-- owner: {owner} -->\n<!-- source-sha: {sha or ''} -->\n"


def strip_spec_header(text: str) -> str:
    """The spec body with its provenance header removed -- fed to the Codebase
    Agent for a delta update so it edits prose, not the header."""
    return _SPEC_HEADER_RE.sub("", text, count=1)

_SAFETY_DETAIL_LIMIT = 200

_AGENTRA_LABEL = "agentra"
_BUG_LABEL = "bug"
_FEATURE_LABEL = "feature"
_STORY_LABEL = "story"
# Marks a feature-request issue as self-originated by discover_opportunities
_DISCOVERY_LABEL = "discovery"
_NEED_HUMAN_LABEL = "need_human"
_BLOCKING_AGENTRA_LABEL = "blocking_agentra"
_STATUS_IN_PROGRESS_LABEL = "status:in-progress"
_STATUS_CODE_COMPLETE_LABEL = "status:code_complete"
# GitHub issue #38: the "merged to pre-prod, awaiting live verification" stage was
# renamed status:shipped -> status:awaiting-testing. Reads still recognise the old
# name (_STATUS_AWAITING_TESTING_LABELS / _at_awaiting_testing_stage); writes emit
# the new label and strip the legacy one.
_STATUS_AWAITING_TESTING_LABEL = "status:awaiting-testing"
_LEGACY_STATUS_SHIPPED_LABEL = "status:shipped"
_STATUS_AWAITING_TESTING_LABELS = frozenset({_STATUS_AWAITING_TESTING_LABEL, _LEGACY_STATUS_SHIPPED_LABEL})
# Back-compat alias -- existing imports keep working, now pointing at the new name.
_STATUS_SHIPPED_LABEL = _STATUS_AWAITING_TESTING_LABEL
_STATUS_TESTED_LABEL = "status:tested"
_STATUS_DONE_LABEL = "status:done"
# Forward-progress labels -- an issue carrying any of these is no longer
# "not started" backlog, regardless of which stage it's at.
_STATUS_PROGRESS_LABELS = (
    _STATUS_CODE_COMPLETE_LABEL, _STATUS_AWAITING_TESTING_LABEL, _LEGACY_STATUS_SHIPPED_LABEL,
    _STATUS_TESTED_LABEL, _STATUS_DONE_LABEL,
)

# GitHub issue #38: the dashboard's pipeline columns, in order. Each stage:
# key, display name, backing GitHub status label (None for the implicit backlog).
_PIPELINE_STAGES = (
    ("queue", "Backlog", None),
    ("in-progress", "In Progress", _STATUS_IN_PROGRESS_LABEL),
    ("code_complete", "Code Complete", _STATUS_CODE_COMPLETE_LABEL),
    ("awaiting-testing", "Awaiting Testing", _STATUS_AWAITING_TESTING_LABEL),
    ("tested", "Ready to Review", _STATUS_TESTED_LABEL),
    ("done", "In Production", _STATUS_DONE_LABEL),
)
_OBJECTIVE_VARIABLE = "AGENTRA_OBJECTIVE"

# A shipped feature/bug fix has run_id/commit_sha stamped into the issue body
_SHIPPED_RUN_ID_RE = re.compile(r"^Shipped-Run-ID: (.+)$", re.MULTILINE)
_SHIPPED_COMMIT_RE = re.compile(r"^Shipped-Commit: (.+)$", re.MULTILINE)
_SHIPPED_SESSION_ID_RE = re.compile(r"^Shipped-Session-ID: (.+)$", re.MULTILINE)

# A dashboard submission's title/description split: the short title lives on
_DESCRIPTION_RE = re.compile(r"^Description: (.+)$", re.MULTILINE)

# Failure triage: a permanent failure (real defect) becomes a GitHub Issue so
_TRANSIENT_FAILURE_PATTERNS = [
    re.compile(r"Reached maximum number of turns \(\d+\)"),
    re.compile(r"rate.?limit", re.IGNORECASE),
    re.compile(r"usage limit", re.IGNORECASE),
    re.compile(r"session limit", re.IGNORECASE),
    # GitHub issue #60: the Claude Code CLI's actual weekly-quota wording
    # ("You've hit your weekly limit · resets ...") doesn't contain the
    # literal phrase "usage limit" -- catch that phrasing and its sibling
    # (5-hour/session) quota messages too.
    re.compile(r"weekly limit", re.IGNORECASE),
    re.compile(r"hit your .{0,30}limit", re.IGNORECASE),
    re.compile(r"overloaded", re.IGNORECASE),
    re.compile(r"returned an error result: success"),
    # GitHub issue #18: a transient 5xx from the Claude Code / inference
    # gateway ("API Error: 500 Internal Server Error", Bad Gateway, etc.)
    # is server-side and should be retried, never filed as a code defect.
    re.compile(r"API Error:\s*5\d\d", re.IGNORECASE),
    re.compile(r"\b5\d\d\s+Internal Server Error", re.IGNORECASE),
    re.compile(r"server-side issue.{0,20}temporary", re.IGNORECASE),
    re.compile(r"Bad Gateway", re.IGNORECASE),
    re.compile(r"Service Unavailable", re.IGNORECASE),
    re.compile(r"Gateway Timeout", re.IGNORECASE),
]

# Auth/permission failures: no amount of different code fixes these — the fix
_UNFIXABLE_BY_AGENTRA_PATTERNS = [
    re.compile(r"unauthorized", re.IGNORECASE),
    re.compile(r"permission denied", re.IGNORECASE),
    re.compile(r"access.{0,20}not granted", re.IGNORECASE),
    re.compile(r"write access.{0,20}not granted", re.IGNORECASE),
]

# Claude Code CLI login/credential failures (GitHub issue #42): the CLI
_LOGIN_REQUIRED_PATTERNS = [
    re.compile(r"not logged in", re.IGNORECASE),
    # Deliberately not anchored to "please run /login" verbatim -- confirmed
    re.compile(r"run\s*`?/login", re.IGNORECASE),
]

_GITIGNORE_CONTENT = """\
# Generated by agentra — do not edit manually.

logs/
test_artifacts/
"""


def is_transient_failure(text: str) -> bool:
    return any(p.search(text) for p in _TRANSIENT_FAILURE_PATTERNS)


def is_login_required_failure(text: str) -> bool:
    """True for the Claude Code CLI's own "not authenticated at all" failure..."""
    return any(p.search(text) for p in _LOGIN_REQUIRED_PATTERNS)


def cannot_be_fixed_by_agentra(text: str) -> bool:
    return is_login_required_failure(text) or any(p.search(text) for p in _UNFIXABLE_BY_AGENTRA_PATTERNS)


def format_safety_denial_line(tool_name: str, pattern: str, detail: str, limit: int = _SAFETY_DETAIL_LIMIT) -> str:
    """Module-level (not just a Memory method) so agents/safety.py's..."""
    truncated = detail if len(detail) <= limit else detail[: limit - 3] + "..."
    return f"[safety] denied tool={tool_name} pattern={pattern!r} detail={truncated!r}"


def _label_names(issue: dict) -> set[str]:
    return {lbl["name"] if isinstance(lbl, dict) else lbl for lbl in issue.get("labels", [])}


def _at_awaiting_testing_stage(labels) -> bool:
    """True when an issue carries the awaiting-testing label under either its
    current (status:awaiting-testing) or legacy (status:shipped) name."""
    return bool(_STATUS_AWAITING_TESTING_LABELS.intersection(labels))


def pipeline_stages() -> list[dict]:
    """The dashboard's pipeline columns in order -- key, display name, backing
    GitHub status label (None for the implicit backlog), legacy label aliases,
    and position. GitHub issue #38 adds 'awaiting-testing' between code-complete
    and in-production."""
    return [
        {
            "key": key,
            "display": display,
            "label": label,
            "legacy_labels": [_LEGACY_STATUS_SHIPPED_LABEL] if key == "awaiting-testing" else [],
            "position": position,
        }
        for position, (key, display, label) in enumerate(_PIPELINE_STAGES)
    ]


def _issue_description(issue: dict) -> str:
    match = _DESCRIPTION_RE.search(issue.get("body") or "")
    return match.group(1) if match else issue["title"]


def _target_repo_label(labels: set[str]) -> str | None:
    """A multi-repo app's issues may carry a repo:<name> label (applied by
    discover_opportunities' auto-filing, or by a human filing an issue) naming which
    code repo the item belongs to -- implement_feature's target_repo argument defaults
    to this when the caller doesn't pass one explicitly."""
    for label in labels:
        if label.startswith("repo:"):
            return label[len("repo:"):]
    return None


def _github_bug_to_dict(issue: dict) -> dict:
    # run_id set to the same value as external_id (not None): discovery.py's
    issue_number = str(issue["number"])
    labels = _label_names(issue)
    return {
        "run_id": issue_number,
        "severity": "medium",
        "diagnosis": issue["title"],
        "description": _issue_description(issue),
        "proposed_fix": issue.get("body") or "",
        "source": "github",
        "external_id": issue_number,
        "html_url": issue.get("html_url"),
        "needs_human": _NEED_HUMAN_LABEL in labels,
        "blocking_agentra": _BLOCKING_AGENTRA_LABEL in labels,
        "target_repo": _target_repo_label(labels),
    }


def _github_closed_bug_to_dict(issue: dict) -> dict:
    # Separate from _github_bug_to_dict (open bugs) rather than adding closed_at
    issue_number = str(issue["number"])
    labels = _label_names(issue)
    return {
        "run_id": issue_number,
        "severity": "medium",
        "diagnosis": issue["title"],
        "description": _issue_description(issue),
        "proposed_fix": issue.get("body") or "",
        "source": "github",
        "external_id": issue_number,
        "html_url": issue.get("html_url"),
        "closed_at": issue.get("closed_at"),
        "status_done": _STATUS_DONE_LABEL in labels,
    }


def _github_feature_to_dict(issue: dict) -> dict:
    return {
        "description": issue["title"],
        "detail": _issue_description(issue),
        "source": "github",
        "external_id": str(issue["number"]),
        "html_url": issue.get("html_url"),
        "target_repo": _target_repo_label(_label_names(issue)),
    }


def _github_shipped_to_dict(issue: dict) -> dict:
    body = issue.get("body") or ""
    run_id_m = _SHIPPED_RUN_ID_RE.search(body)
    commit_m = _SHIPPED_COMMIT_RE.search(body)
    session_id_m = _SHIPPED_SESSION_ID_RE.search(body)
    labels = _label_names(issue)
    return {
        "feature": issue["title"],
        "commit_sha": commit_m.group(1) if commit_m else None,
        "run_id": run_id_m.group(1) if run_id_m else None,
        "session_id": session_id_m.group(1) if session_id_m else None,
        "ts": issue.get("closed_at"),
        "updated_at": issue.get("updated_at"),
        "external_id": str(issue["number"]),
        "html_url": issue.get("html_url"),
        "status_done": _STATUS_DONE_LABEL in labels,
    }


def _ensure_gitignore(root: Path) -> None:
    """Write .agentra/.gitignore if it is missing or doesn't ignore both..."""
    gitignore = root / ".gitignore"
    if gitignore.exists():
        content = gitignore.read_text()
        if "logs/" in content and "test_artifacts/" in content:
            return
    gitignore.write_text(_GITIGNORE_CONTENT)

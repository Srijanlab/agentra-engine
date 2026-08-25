"""GitHub issue #60 regression lock-in: "autonomous-cycle failed during an
autonomous cycle" (root cause: the Claude Code CLI subprocess hit its weekly
usage limit, exit code 1) kept resurfacing in the backlog even though the
underlying condition was already correctly classified as a transient/quota
failure by is_transient_failure(). Unlike is_transient_failure's early-return
in record_failure() (which stops a *new* occurrence from ever being filed),
nothing closed out a bug that had already been filed before -- so it just sat
open forever, needs_human=false/blocking_agentra=false, getting picked up by
future cycles as if it were real work to do.

Mirrors tests/test_issue42_auth_failure_regression.py's rigor: exercises the
real create -> reconcile -> close round trip against the fake (in-memory)
GitHub backend, not just that the right method gets *called*.
"""

import subprocess
from pathlib import Path

from agentra.connectors import github_fake, github_issues
from agentra.memory import Memory
from agentra.memory.core import is_transient_failure

_WEEKLY_LIMIT_TEXT = (
    "autonomous cycle raised: Claude Code returned an error result: "
    "You've hit your weekly limit · resets Aug 24, 10pm (UTC) (exit code: 1)"
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _make_repo(tmp_path: Path, name: str = "myapp") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("hello\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial commit")
    _git(repo, "remote", "add", "origin", f"https://github.com/acme/{name}.git")
    return repo


def test_weekly_usage_limit_text_is_recognized_as_transient():
    """The exact wording the live Claude Code CLI produces for a weekly-quota
    failure ("You've hit your weekly limit") must be recognized -- it doesn't
    contain the literal phrase "usage limit"."""
    assert is_transient_failure(_WEEKLY_LIMIT_TEXT) is True


def test_a_new_occurrence_of_the_weekly_limit_failure_is_not_filed_as_a_bug(tmp_path, monkeypatch):
    """record_failure()'s existing transient early-return should mean a fresh
    occurrence never becomes an open bug in the first place."""
    github_fake.install(monkeypatch=monkeypatch)
    repo = _make_repo(tmp_path)
    mem = Memory(repo)

    mem.record_failure("run1", "autonomous-cycle", _WEEKLY_LIMIT_TEXT)

    assert mem.known_bugs() == []


def test_clear_resolved_transient_bugs_actually_closes_the_real_issue(tmp_path, monkeypatch):
    """The reconciliation sweep: a bug already open (e.g. filed before this
    phrasing was recognized) whose diagnosis matches an already-handled
    transient/quota condition must be reliably closed on the real (fake) issue
    -- not left with only a non-terminal status:shipped label -- with an
    explanatory comment, and must show up as resolved rather than vanishing."""
    github_fake.install(monkeypatch=monkeypatch)
    repo = _make_repo(tmp_path)
    mem = Memory(repo)
    repo_url = mem._repo_url()

    # Simulate a bug filed before the "weekly limit" pattern was recognized
    # (i.e. bypassing record_failure's now-early-return), exactly like #60.
    issue = github_issues.create_issue(
        repo_url,
        "autonomous-cycle failed during an autonomous cycle",
        f"Description: autonomous-cycle failed during an autonomous cycle\n\n"
        f"Severity: high\nSource: autonomous-failure\n\nProposed fix:\n{_WEEKLY_LIMIT_TEXT}",
        labels=["bug", "agentra"],
    )
    issue_number = issue["number"]

    assert len(mem.known_bugs()) == 1

    cleared = mem.clear_resolved_transient_bugs("run2")

    assert cleared == [str(issue_number)]

    # Gone from the live open/known-bugs backlog...
    assert mem.known_bugs() == []

    # ...the real GitHub issue actually reached a closed state (not just
    # filtered out of a local view) ...
    real_issue = github_issues.get_issue(repo_url, issue_number)
    assert real_issue["state"] == "closed"

    # ...with an explanatory comment referencing the existing transient/quota
    # detection...
    comments = github_issues.list_comments(repo_url, issue_number)
    assert any("transient" in (c.get("body") or "").lower() for c in comments)

    # ...and it shows up as resolved, not vanished without a trace.
    closed = mem.closed_bugs()
    assert len(closed) == 1
    assert closed[0]["external_id"] == str(issue_number)


def test_a_repeat_occurrence_after_reconciliation_does_not_reopen_or_refile(tmp_path, monkeypatch):
    """Durability: once resolved via the sweep, a later occurrence of the same
    transient condition must not re-open, re-file, or otherwise resurface the
    bug in the backlog."""
    github_fake.install(monkeypatch=monkeypatch)
    repo = _make_repo(tmp_path)
    mem = Memory(repo)
    repo_url = mem._repo_url()

    issue = github_issues.create_issue(
        repo_url,
        "autonomous-cycle failed during an autonomous cycle",
        f"Description: autonomous-cycle failed during an autonomous cycle\n\n"
        f"Proposed fix:\n{_WEEKLY_LIMIT_TEXT}",
        labels=["bug", "agentra"],
    )
    mem.clear_resolved_transient_bugs("run2")
    assert mem.known_bugs() == []

    # A later cycle hits the identical transient condition again.
    mem.record_failure("run3", "autonomous-cycle", _WEEKLY_LIMIT_TEXT)

    assert mem.known_bugs() == []
    real_issue = github_issues.get_issue(repo_url, issue["number"])
    assert real_issue["state"] == "closed"


def test_clear_resolved_transient_bugs_does_not_touch_an_unrelated_open_bug(tmp_path, monkeypatch):
    """Must not indiscriminately close genuine, non-transient known bugs."""
    github_fake.install(monkeypatch=monkeypatch)
    repo = _make_repo(tmp_path)
    mem = Memory(repo)
    repo_url = mem._repo_url()

    transient_issue = github_issues.create_issue(
        repo_url,
        "autonomous-cycle failed during an autonomous cycle",
        f"Proposed fix:\n{_WEEKLY_LIMIT_TEXT}",
        labels=["bug", "agentra"],
    )
    ordinary_issue = github_issues.create_issue(
        repo_url,
        "run_local_tests failed during an autonomous cycle",
        "Proposed fix:\nTypeError: cannot read property 'x' of undefined at line 42",
        labels=["bug", "agentra"],
    )

    cleared = mem.clear_resolved_transient_bugs("run2")

    assert cleared == [str(transient_issue["number"])]
    remaining = mem.known_bugs()
    assert len(remaining) == 1
    assert remaining[0]["external_id"] == str(ordinary_issue["number"])
    real_ordinary = github_issues.get_issue(repo_url, ordinary_issue["number"])
    assert real_ordinary["state"] == "open"


def test_clear_resolved_transient_bugs_is_a_noop_with_no_matching_bugs(tmp_path, monkeypatch):
    github_fake.install(monkeypatch=monkeypatch)
    repo = _make_repo(tmp_path)
    mem = Memory(repo)

    assert mem.clear_resolved_transient_bugs("run1") == []

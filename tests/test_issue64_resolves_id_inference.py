"""GitHub issue #64 regression lock-in: issue #61 was filed as a duplicate of
#60 (rather than closing #60 directly) because implement_feature's brain-side
tool call mentioned "(GitHub issue #60)" only in the free-text feature_brief,
never in the resolves_id/resolves_origin arguments record_code_complete actually
reads -- and record_code_complete's own text-similarity safety net could never
catch it, since record_failure stamps every bug with the same generic
diagnosis regardless of the real error. Two independent fixes:

1. _infer_resolves_from_brief: a best-effort fallback that scans the brief
   for a "#<number>" reference matching an open known bug or feature-queue
   item, used only when the caller didn't pass resolves_id explicitly.
2. record_failure's diagnosis now carries the real error's first line, so
   any future similarity-based matching against it has real signal.

This file exercises (1) directly, and end-to-end via record_code_complete against
the fake GitHub backend, proving the #60/#61 scenario now resolves the
original bug instead of filing a duplicate.
"""

import subprocess
from pathlib import Path

from agentra.agents.brain.tools import _infer_resolves_from_brief
from agentra.connectors import github_fake
from agentra.memory import Memory


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


def test_infer_resolves_from_brief_matches_a_known_bug_referenced_in_prose(tmp_path, monkeypatch):
    github_fake.install(monkeypatch=monkeypatch)
    repo = _make_repo(tmp_path)
    mem = Memory(repo)
    repo_url = mem._repo_url()

    from agentra.connectors import github_issues

    bug = github_issues.create_issue(
        repo_url, "autonomous-cycle failed during an autonomous cycle: weekly limit", "body", labels=["bug", "agentra"]
    )

    brief = (
        "Detect Claude Code CLI weekly/usage-limit errors as a distinct transient "
        f"quota condition (GitHub issue #{bug['number']}), analogous to #42 auth-failure detection"
    )

    result = _infer_resolves_from_brief(mem, brief)

    assert result == (str(bug["number"]), "known_bug")


def test_infer_resolves_from_brief_matches_a_feature_queue_item_referenced_in_prose(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    mem = Memory(repo)
    # Isolated from known_bugs()/list_open_issues's own label-matching semantics
    # (pre-existing, unrelated OR-based quirk) -- this test is only about
    # _infer_resolves_from_brief's own precedence and number-matching logic.
    monkeypatch.setattr(mem, "known_bugs", lambda: [])
    monkeypatch.setattr(mem, "feature_queue", lambda: [{"external_id": "7", "description": "Add dark mode"}])

    brief = "Implement dark mode toggle in settings (resolves issue #7)"

    result = _infer_resolves_from_brief(mem, brief)

    assert result == ("7", "feature_queue")


def test_infer_resolves_from_brief_returns_none_when_no_number_matches_open_backlog(tmp_path, monkeypatch):
    github_fake.install(monkeypatch=monkeypatch)
    repo = _make_repo(tmp_path)
    mem = Memory(repo)

    # References a number, but nothing open matches it (e.g. a random PR
    # number, or an issue that's already closed) -- must not false-positive.
    result = _infer_resolves_from_brief(mem, "See discussion in #9999 for context")

    assert result is None


def test_end_to_end_resolving_a_bug_referenced_only_in_prose_does_not_file_a_duplicate(tmp_path, monkeypatch):
    """The actual #60/#61 scenario: a fix brief mentions the bug's issue
    number only in prose. Inferring resolves_id/resolves_origin from that and
    passing it into record_code_complete(known_bug_issue=...) must resolve the
    original bug, not create a new tracking issue for the same work."""
    github_fake.install(monkeypatch=monkeypatch)
    repo = _make_repo(tmp_path)
    mem = Memory(repo)
    repo_url = mem._repo_url()

    from agentra.connectors import github_issues

    bug = github_issues.create_issue(
        repo_url, "autonomous-cycle failed during an autonomous cycle: weekly limit", "body", labels=["bug", "agentra"]
    )
    bug_number = bug["number"]

    brief = f"Fix backlog sync for weekly-limit failures (GitHub issue #{bug_number})"
    resolves_id, resolves_origin = _infer_resolves_from_brief(mem, brief)

    assert resolves_id == str(bug_number)

    shipped = mem.record_code_complete(
        brief, commit_sha="deadbeef", run_id="run1",
        known_bug_issue=resolves_id if resolves_origin == "known_bug" else None,
    )

    # No new issue was filed -- the existing bug is what gets referenced.
    assert shipped["issue_number"] == bug_number
    assert len(github_issues.list_open_issues(repo_url, labels=["agentra"])) == 1

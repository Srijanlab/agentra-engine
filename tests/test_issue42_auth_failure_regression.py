"""GitHub issue #42 regression lock-in: "Claude Code returned an error
result: Not logged in · Please run /login (exit code: 1)" kept resurfacing
in the backlog across multiple prior "shipped" fixes (7c98f61, 1fd6b86,
dd29215). Unlike the other test_*_auth_failure*.py files, which each cover
one layer with Memory/registry methods monkeypatched away, this file
exercises the real create -> dedupe -> comment -> mark_shipped round trip
against connectors/github_fake.py's in-memory GitHub backend -- i.e. it
tests the actual "close/dedupe the issue" plumbing end to end, not just
that the right method gets *called*. Per the resumed brief: "a bug that
keeps reappearing suggests the close-issue-on-fix logic itself may be
broken, not the underlying auth handling" -- this file is the check for
that class of regression specifically.

Covers, against a real (fake) GitHub Issues round trip:
  1. The failure is classified distinctly as an environmental/credential
     issue (bug + needs_human + blocking_agentra labels), not a generic
     code bug agentra might otherwise try to "fix" with more code.
  2. Exactly one Slack escalation fires, with the literal actionable
     message, on first occurrence.
  3. A second occurrence of the *same* failure signature does NOT file a
     duplicate GitHub issue and does NOT re-notify Slack -- it comments on
     the existing open issue instead.
  4. Once a later run proves Claude Code is authenticated again,
     Memory.clear_resolved_auth_bugs actually flips the real issue's state
     (status:shipped) so it stops showing up in known_bugs()/
     blocking_bugs() and starts showing up in closed_bugs() -- the
     "properly closed once a fix ships" half of this regression.
"""

import subprocess
from pathlib import Path

from agentra.connectors import github_fake, github_issues, slack
from agentra.memory import Memory

_LOGIN_ERROR_TEXT = "Claude Code returned an error result: Not logged in · Please run /login (exit code: 1)"


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


def _slack_capture(monkeypatch):
    calls = []
    monkeypatch.setattr(slack, "notify_human_input_required", lambda **k: calls.append(k) or True)
    return calls


def test_first_occurrence_is_classified_distinctly_and_escalated_exactly_once(tmp_path, monkeypatch):
    github_fake.install(monkeypatch=monkeypatch)
    slack_calls = _slack_capture(monkeypatch)
    repo = _make_repo(tmp_path)
    mem = Memory(repo)

    mem.record_failure("run1", "understand_codebase", _LOGIN_ERROR_TEXT)

    bugs = mem.known_bugs()
    assert len(bugs) == 1
    # Distinctly classified as an environmental/credential issue, not a
    # generic code bug agentra might otherwise try to "fix" with more code.
    assert bugs[0]["needs_human"] is True
    assert bugs[0]["blocking_agentra"] is True

    # Escalated via the existing Slack human-in-the-loop channel, exactly
    # once, with the actionable message -- not left to silently fail or
    # get buried as a generic "agent turn raised: ..." note.
    assert len(slack_calls) == 1
    assert slack_calls[0]["question"] == "Claude Code session is not authenticated on this runner -- run /login and re-trigger."


def test_a_repeat_occurrence_does_not_file_a_duplicate_or_re_notify(tmp_path, monkeypatch):
    """The exact regression this issue kept re-shipping fixes for: a bug
    that "keeps reappearing" implies either duplicate filing or endless
    re-notification -- assert neither happens for a second, independent
    occurrence of the identical failure signature (as would happen on a
    second scheduled trigger while a runner is still not logged in)."""
    github_fake.install(monkeypatch=monkeypatch)
    slack_calls = _slack_capture(monkeypatch)
    repo = _make_repo(tmp_path)
    mem = Memory(repo)

    mem.record_failure("run1", "understand_codebase", _LOGIN_ERROR_TEXT)
    mem.record_failure("run2", "understand_codebase", _LOGIN_ERROR_TEXT)

    # Still exactly one open bug -- the second occurrence commented on the
    # existing issue rather than filing a fresh duplicate.
    bugs = mem.known_bugs()
    assert len(bugs) == 1

    # No second Slack ping for an already-reported, still-open failure.
    assert len(slack_calls) == 1

    # The GitHub issue itself recorded both occurrences (not silently
    # dropped) via a comment on the one true issue.
    repo_url = mem._repo_url()
    comments = github_issues.list_comments(repo_url, int(bugs[0]["external_id"]))
    assert any("run2" in (c.get("body") or "") for c in comments)


def test_a_different_ordinary_bug_is_not_deduped_against_the_auth_failure(tmp_path, monkeypatch):
    """Dedup must be specific to the actual failure signature, not so broad
    it swallows unrelated bugs into the same tracking issue."""
    github_fake.install(monkeypatch=monkeypatch)
    _slack_capture(monkeypatch)
    repo = _make_repo(tmp_path)
    mem = Memory(repo)

    mem.record_failure("run1", "understand_codebase", _LOGIN_ERROR_TEXT)
    mem.record_failure("run2", "run_local_tests", "TypeError: cannot read property 'x' of undefined at line 42")

    bugs = mem.known_bugs()
    assert len(bugs) == 2
    # The ordinary bug must NOT be flagged needs_human/blocking_agentra --
    # only the auth failure is an environmental/credential issue.
    ordinary = next(b for b in bugs if "TypeError" in b["proposed_fix"])
    assert ordinary["needs_human"] is False
    assert ordinary["blocking_agentra"] is False


def test_clear_resolved_auth_bugs_actually_closes_the_real_issue_out_of_the_backlog(tmp_path, monkeypatch):
    """The other half of "properly closed/deduplicated once a fix ships":
    once a later run proves re-authentication worked,
    clear_resolved_auth_bugs must make the real GitHub issue actually stop
    showing up as an open/blocking bug -- not just call some mock and
    assume the underlying label/state transition happened."""
    github_fake.install(monkeypatch=monkeypatch)
    _slack_capture(monkeypatch)
    repo = _make_repo(tmp_path)
    mem = Memory(repo)

    mem.record_failure("run1", "understand_codebase", _LOGIN_ERROR_TEXT)
    assert len(mem.blocking_bugs()) == 1

    cleared = mem.clear_resolved_auth_bugs("run2")

    assert len(cleared) == 1
    # Gone from the open/blocking backlog...
    assert mem.known_bugs() == []
    assert mem.blocking_bugs() == []
    # ...and now shows up as resolved, not vanished without a trace.
    closed = mem.closed_bugs()
    assert len(closed) == 1
    assert closed[0]["external_id"] == cleared[0]


def test_clear_resolved_auth_bugs_does_not_touch_an_unrelated_open_bug(tmp_path, monkeypatch):
    github_fake.install(monkeypatch=monkeypatch)
    _slack_capture(monkeypatch)
    repo = _make_repo(tmp_path)
    mem = Memory(repo)

    mem.record_failure("run1", "understand_codebase", _LOGIN_ERROR_TEXT)
    mem.record_failure("run2", "deploy_pre_prod", "403 Write access to repository not granted")

    cleared = mem.clear_resolved_auth_bugs("run3")

    assert len(cleared) == 1
    remaining = mem.known_bugs()
    assert len(remaining) == 1
    assert "403" in remaining[0]["proposed_fix"]

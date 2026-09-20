"""GitHub issues #46/#47: an auth failure or a generic non-auth "unfixable"
failure must get the SAME escalation wiring every other human-input block
gets -- a Slack notify with a real thread mapping, recorded human-input
context, and the loop marked waiting_for_human -- not just a Slack ping with
none of that state, which left the block invisible on the dashboard's "Needs
your input" tab and any thread reply silently dropped (resolve_slack_thread
had no mapping to resolve).
"""

import subprocess
from pathlib import Path

import pytest

from agentra import registry
from agentra.connectors import github_fake, slack
from agentra.memory import Memory

_LOGIN_ERROR_TEXT = "Claude Code returned an error result: Not logged in · Please run /login (exit code: 1)"
_UNFIXABLE_TEXT = "403 Write access to repository not granted -- push failed"


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


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    home = tmp_path / "agentra_home"
    for attr, sub in [
        ("AGENTRA_HOME", ""), ("APPS_PATH", "apps.json"), ("PAUSE_PATH", "paused.json"),
        ("_RUNS_PATH", "runs.json"), ("_LOOPS_PATH", "loops.json"),
    ]:
        monkeypatch.setattr(registry, attr, home / sub if sub else home)


def _slack_capture(monkeypatch):
    calls = []
    monkeypatch.setattr(slack, "notify_human_input_required", lambda **k: calls.append(k) or "1234.5678")
    return calls


def test_an_auth_failure_is_fully_escalated_not_just_slack_pinged(tmp_path, monkeypatch):
    """GitHub issue #46: the old behavior only posted a Slack message inviting a
    reply -- no human_input_context, no thread mapping, no waiting_for_human loop
    state, so the reply had nothing to resolve to and the dashboard showed nothing."""
    github_fake.install(monkeypatch=monkeypatch)
    slack_calls = _slack_capture(monkeypatch)
    repo = _make_repo(tmp_path)
    mem = Memory(repo)

    mem.record_failure("run1", "understand_codebase", _LOGIN_ERROR_TEXT)

    assert len(slack_calls) == 1
    issue_number = int(mem.known_bugs()[0]["external_id"])

    # Human-input context recorded -- the GitHub issue carries resume-correlation state.
    assert mem.get_human_input_context(issue_number) is not None

    # A thread mapping exists so a reply in that Slack thread would actually resolve.
    assert registry.slack_thread_for("myapp", issue_number) == "1234.5678"

    # The loop shows up on the dashboard's "Needs your input" tab.
    waiting = registry.list_waiting_for_human()
    assert any(loop["issue_number"] == str(issue_number) for loop in waiting)


def test_a_generic_unfixable_failure_is_now_escalated_the_same_way(tmp_path, monkeypatch):
    """GitHub issue #47: previously this hard-blocked future cycles via
    blocking_bugs() while being invisible on both Slack and the dashboard."""
    github_fake.install(monkeypatch=monkeypatch)
    slack_calls = _slack_capture(monkeypatch)
    repo = _make_repo(tmp_path)
    mem = Memory(repo)

    mem.record_failure("run1", "deploy_pre_prod", _UNFIXABLE_TEXT)

    assert len(mem.blocking_bugs()) == 1
    assert len(slack_calls) == 1
    issue_number = int(mem.known_bugs()[0]["external_id"])

    assert mem.get_human_input_context(issue_number) is not None
    assert registry.slack_thread_for("myapp", issue_number) == "1234.5678"
    waiting = registry.list_waiting_for_human()
    assert any(loop["issue_number"] == str(issue_number) for loop in waiting)


def test_a_repeat_unfixable_failure_does_not_re_escalate(tmp_path, monkeypatch):
    github_fake.install(monkeypatch=monkeypatch)
    slack_calls = _slack_capture(monkeypatch)
    repo = _make_repo(tmp_path)
    mem = Memory(repo)

    mem.record_failure("run1", "deploy_pre_prod", _UNFIXABLE_TEXT)
    mem.record_failure("run2", "deploy_pre_prod", _UNFIXABLE_TEXT)

    assert len(mem.known_bugs()) == 1  # commented on the same issue, not duplicated
    assert len(slack_calls) == 1  # no second escalation for the same still-open failure


def test_a_transient_failure_is_never_escalated(tmp_path, monkeypatch):
    github_fake.install(monkeypatch=monkeypatch)
    slack_calls = _slack_capture(monkeypatch)
    repo = _make_repo(tmp_path)
    mem = Memory(repo)

    mem.record_failure("run1", "run_local_tests", "rate limit exceeded, please retry")

    assert mem.known_bugs() == []
    assert slack_calls == []
    assert registry.list_waiting_for_human() == []


def test_an_ordinary_fixable_failure_is_never_escalated(tmp_path, monkeypatch):
    """A plain code bug (not unfixable, not an auth failure) must still just be
    filed as a normal backlog bug -- no Slack ping, no waiting_for_human state."""
    github_fake.install(monkeypatch=monkeypatch)
    slack_calls = _slack_capture(monkeypatch)
    repo = _make_repo(tmp_path)
    mem = Memory(repo)

    mem.record_failure("run1", "run_local_tests", "TypeError: cannot read property 'x' of undefined at line 42")

    bugs = mem.known_bugs()
    assert len(bugs) == 1
    assert bugs[0]["needs_human"] is False
    assert slack_calls == []
    assert registry.list_waiting_for_human() == []

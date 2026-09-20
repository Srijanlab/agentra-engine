"""GitHub issue #55: a run that ends carrying a HUMAN_INPUT_REQUIRED error note
must always raise a real human gate (Slack post + need_human label + loop
state) -- confirmed live 2026-09-20, three loops died silently on this gap."""

import asyncio
import subprocess
from pathlib import Path

import pytest

from agentra import registry
from agentra.connectors import github_fake, github_issues, slack
from agentra.memory import Memory
from agentra.server import human_gate
from agentra.server.routes import triggers

_REPO_URL = "https://github.com/acme/myapp.git"


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
    _git(repo, "remote", "add", "origin", _REPO_URL)
    return repo


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    home = tmp_path / "agentra_home"
    for attr, sub in [
        ("AGENTRA_HOME", ""), ("APPS_PATH", "apps.json"), ("PAUSE_PATH", "paused.json"),
        ("_RUNS_PATH", "runs.json"), ("_LOOPS_PATH", "loops.json"),
    ]:
        monkeypatch.setattr(registry, attr, home / sub if sub else home)
    monkeypatch.setattr(registry.core, "_SLACK_THREADS_PATH", home / "slack_threads.json")


def _slack_capture(monkeypatch):
    calls = []
    monkeypatch.setattr(slack, "notify_human_input_required", lambda **k: calls.append(k) or "1234.5678")
    return calls


def _setup(tmp_path, monkeypatch) -> Memory:
    github_fake.install(monkeypatch=monkeypatch)
    repo = _make_repo(tmp_path)
    registry.register_app("myapp", repo_path=str(repo))
    return Memory(repo)


def _labels(issue_number: int) -> set[str]:
    return set(github_issues.get_issue(_REPO_URL, issue_number)["labels"])


def test_a_silent_human_input_required_run_raises_a_full_gate(tmp_path, monkeypatch):
    mem = _setup(tmp_path, monkeypatch)
    slack_calls = _slack_capture(monkeypatch)

    issue_number = mem.record_known_bug("run1", "high", "Divide work across providers", "tbd", title="Feature work")
    loop_id = registry.bind_loop("myapp", issue_number, kind="feature")
    registry.record_run(
        "run1", app="myapp", loop_id=loop_id, status="completed",
        error="HUMAN_INPUT_REQUIRED: OAuth or magic links?",
    )

    gate = human_gate.maybe_raise("run1")

    assert gate == {
        "app": "myapp", "run_key": "run1", "issue_number": issue_number,
        "label_set": True, "slack_posted": True,
    }
    assert len(slack_calls) == 1
    assert "OAuth or magic links?" in slack_calls[0]["question"]
    assert f"issue #{issue_number}" in slack_calls[0]["question"]

    waiting = registry.list_waiting_for_human()
    assert any(
        l["loop_id"] == loop_id and l["human_input"]["question"] == "OAuth or magic links?" for l in waiting
    )
    assert registry.slack_thread_for("myapp", issue_number) == "1234.5678"
    assert "need_human" in _labels(issue_number)


def test_a_run_without_a_tracking_issue_files_one(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    _slack_capture(monkeypatch)

    registry.record_run("run1", app="myapp", status="failed", error="HUMAN_INPUT_REQUIRED: fix the import error")

    gate = human_gate.maybe_raise("run1")

    assert gate is not None
    assert gate["label_set"] is True
    assert "need_human" in _labels(gate["issue_number"])


def test_a_run_with_no_token_in_its_error_raises_nothing(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    slack_calls = _slack_capture(monkeypatch)

    registry.record_run("run1", app="myapp", status="failed", error="TypeError: boom")

    assert human_gate.maybe_raise("run1") is None
    assert slack_calls == []
    assert registry.list_waiting_for_human() == []


def test_a_still_running_status_raises_nothing(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    slack_calls = _slack_capture(monkeypatch)

    registry.record_run("run1", app="myapp", status="running", error="HUMAN_INPUT_REQUIRED: pick one")

    assert human_gate.maybe_raise("run1") is None
    assert slack_calls == []


def test_reporting_the_same_run_twice_never_double_posts(tmp_path, monkeypatch):
    mem = _setup(tmp_path, monkeypatch)
    slack_calls = _slack_capture(monkeypatch)

    issue_number = mem.record_known_bug("run1", "high", "thing", "tbd")
    loop_id = registry.bind_loop("myapp", issue_number, kind="bug")
    registry.record_run("run1", app="myapp", loop_id=loop_id, status="completed", error="HUMAN_INPUT_REQUIRED: pick one")

    first = human_gate.maybe_raise("run1")
    second = human_gate.maybe_raise("run1")  # e.g. the cron backstop re-scanning the same run

    assert first is not None and first["slack_posted"] is True
    assert second is None
    assert len(slack_calls) == 1


def test_a_later_different_run_after_the_human_answers_raises_a_fresh_gate(tmp_path, monkeypatch):
    mem = _setup(tmp_path, monkeypatch)
    slack_calls = _slack_capture(monkeypatch)

    issue_number = mem.record_known_bug("run1", "high", "thing", "tbd")
    loop_id = registry.bind_loop("myapp", issue_number, kind="bug")
    registry.record_run("run1", app="myapp", loop_id=loop_id, status="completed", error="HUMAN_INPUT_REQUIRED: pick one")
    human_gate.maybe_raise("run1")
    assert len(slack_calls) == 1

    mem.record_human_answer(issue_number, "use OAuth")  # removes need_human

    registry.record_run("run2", app="myapp", loop_id=loop_id, status="completed", error="HUMAN_INPUT_REQUIRED: pick two")
    gate = human_gate.maybe_raise("run2")

    assert gate is not None and gate["slack_posted"] is True
    assert len(slack_calls) == 2


def test_slack_failure_is_retried_by_the_next_sweep_not_the_label(tmp_path, monkeypatch):
    mem = _setup(tmp_path, monkeypatch)

    issue_number = mem.record_known_bug("run1", "high", "thing", "tbd")
    loop_id = registry.bind_loop("myapp", issue_number, kind="bug")
    registry.record_run("run1", app="myapp", loop_id=loop_id, status="completed", error="HUMAN_INPUT_REQUIRED: pick one")

    monkeypatch.setattr(slack, "notify_human_input_required", lambda **k: None)  # simulate Slack down
    first = human_gate.maybe_raise("run1")
    assert first["label_set"] is True
    assert first["slack_posted"] is False

    calls = _slack_capture(monkeypatch)  # Slack recovers
    gates = human_gate.sweep_recent_runs()

    assert len(calls) == 1
    assert any(g["run_key"] == "run1" and g["slack_posted"] for g in gates)
    assert registry.slack_thread_for("myapp", issue_number) == "1234.5678"


def test_cron_tick_reports_human_gates_key(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    _slack_capture(monkeypatch)
    monkeypatch.setattr(registry, "list_apps", lambda: {})  # no apps due for a scheduled cycle

    result = asyncio.run(triggers._tick())

    assert "human_gates" in result
    assert result["human_gates"] == []

"""server/routes/human_input.py -- the dashboard-answer half of GitHub
issue #34's human-in-the-loop resume (the GitHub-issue-comment half is
covered by tests/test_human_input_reconciliation.py).

No real LLM call: run_autonomous_cycle is monkeypatched throughout.
"""

import asyncio
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentra import environments, registry, server
from agentra.agents.brain import AutonomousCycleReport
from agentra.connectors import github_fake
from agentra.memory import Memory
from agentra.server.routes import human_input


def _close_background_coro(coro):
    coro.close()
    return None


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _register_tmp_app(tmp_path: Path, name: str = "myapp") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("hello\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial commit")
    repo_url = f"https://github.com/acme/{name}.git"
    _git(repo, "remote", "add", "origin", repo_url)
    Memory(repo).set_objective("Ship useful dashboard improvements.")
    registry.register_app(name, str(repo), repo_url=repo_url, branch="main")
    return repo


def _isolate_registry(tmp_path, monkeypatch):
    home = tmp_path / "agentra_home"
    monkeypatch.setattr(registry, "_db", None)
    monkeypatch.setattr(registry, "AGENTRA_HOME", home)
    monkeypatch.setattr(registry, "APPS_PATH", home / "apps.json")
    monkeypatch.setattr(registry, "INBOX_ROOT", home / "inbox")
    monkeypatch.setattr(registry, "PAUSE_PATH", home / "paused.json")
    monkeypatch.setattr(registry, "_RUNS_PATH", home / "runs.json")
    monkeypatch.setattr(registry, "_AGENT_STEPS_PATH", home / "agent_steps.jsonl")
    server._active_runs.clear()
    server._app_locks.clear()
    monkeypatch.setattr(server.asyncio, "create_task", _close_background_coro)
    github_fake.install(monkeypatch=monkeypatch)


def _escalate(repo: Path, *, tracking_issue: int = 17, branch: str = "dev/abc-add-login", session_id: str = "sess-abc123") -> int:
    """Files a needs_human issue with resume-correlation context stamped on
    it -- the state _escalate_to_human (agents/brain/tools.py) leaves
    behind, reproduced directly here so these tests don't need a full
    brain cycle just to set up their fixture."""
    mem = Memory(repo)
    issue_number = mem.record_known_bug(
        "run1", "medium", "Two auth providers are equally valid.",
        "Requires a human decision.", source="implementation-agent-human-input-required",
        needs_human=True, title="Human input required: Add login",
    )
    mem.record_human_input_context(
        issue_number, app=repo.name, run_id="run1", question="Should we use OAuth or magic links?",
        branch=branch, session_id=session_id, tracking_issue=tracking_issue,
    )
    return issue_number


# -- dispatch_human_answer (shared by both answer channels) -----------------


def test_dispatch_human_answer_raises_for_an_issue_with_no_recorded_context(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    repo = _register_tmp_app(tmp_path)

    with pytest.raises(ValueError):
        human_input.dispatch_human_answer("myapp", repo, 999, "some answer", source="human-input")


def test_dispatch_human_answer_records_the_answer_and_removes_needs_human_label(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    repo = _register_tmp_app(tmp_path)
    issue_number = _escalate(repo)

    result = human_input.dispatch_human_answer("myapp", repo, issue_number, "Use OAuth.", source="human-input")

    assert "run_key" in result
    assert result["branch"] == "dev/abc-add-login"
    assert result["session_id"] == "sess-abc123"

    repo_url = f"https://github.com/acme/myapp.git"
    from agentra.connectors import github_issues

    issue = github_issues.get_issue(repo_url, issue_number)
    assert "need_human" not in issue["labels"]
    assert any("Answered: Use OAuth." in c for c in issue.get("comments", []))


def test_dispatch_human_answer_flips_the_waiting_run_to_answered(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    repo = _register_tmp_app(tmp_path)
    issue_number = _escalate(repo)
    registry.record_run(
        "orig-run", app="myapp", status="waiting_for_human", started_at=time.time(),
        human_input={"issue_number": issue_number, "question": "q", "waiting_since": time.time()},
    )

    human_input.dispatch_human_answer("myapp", repo, issue_number, "Use OAuth.", source="human-input")

    assert registry.get_run("orig-run")["status"] == "answered"
    waiting_now = [r["run_key"] for r in registry.list_waiting_for_human()]
    assert "orig-run" not in waiting_now


# -- POST /apps/{app}/human-input --------------------------------------------


def test_submit_human_input_returns_404_for_unregistered_app(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)

    response = TestClient(server.app).post("/apps/nope/human-input", json={"issue_number": 1, "answer": "x"})

    assert response.status_code == 404


def test_submit_human_input_returns_404_for_an_issue_never_escalated(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    _register_tmp_app(tmp_path)

    response = TestClient(server.app).post("/apps/myapp/human-input", json={"issue_number": 999, "answer": "x"})

    assert response.status_code == 404


def test_submit_human_input_accepts_and_dispatches_a_resume(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    repo = _register_tmp_app(tmp_path)
    issue_number = _escalate(repo)

    response = TestClient(server.app).post(
        "/apps/myapp/human-input", json={"issue_number": issue_number, "answer": "Use OAuth."}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] is True
    assert body["branch"] == "dev/abc-add-login"
    recorded = registry.get_run(body["run_key"])
    assert recorded["status"] == "queued"
    assert recorded["app"] == "myapp"


def test_submit_human_input_respects_system_pause(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    repo = _register_tmp_app(tmp_path)
    issue_number = _escalate(repo)
    registry.pause()

    response = TestClient(server.app).post(
        "/apps/myapp/human-input", json={"issue_number": issue_number, "answer": "Use OAuth."}
    )

    assert response.status_code == 200
    assert response.json()["triggered"] is False


# -- GET /needs-human ---------------------------------------------------------


def test_list_needs_human_returns_waiting_and_escalated_runs(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    registry.record_run("r1", app="myapp", status="waiting_for_human", started_at=time.time(), human_input={"question": "q"})
    registry.record_run("r2", app="myapp", status="completed", started_at=time.time())

    response = TestClient(server.app).get("/needs-human")

    assert response.status_code == 200
    keys = {r["run_key"] for r in response.json()["runs"]}
    assert keys == {"r1"}


# -- end-to-end: the resumed cycle reuses the original branch/session -------


def test_resume_dispatches_run_autonomous_cycle_with_the_original_branch_and_session(tmp_path, monkeypatch):
    """The core acceptance criterion: resuming reuses feature_branch +
    session_id rather than starting fresh. Runs _run_human_resume_background
    for real (not swallowed by create_task) with run_autonomous_cycle
    monkeypatched to capture its kwargs, same pattern as
    test_server_triggers.py's promote end-to-end test."""
    _isolate_registry(tmp_path, monkeypatch)
    repo = _register_tmp_app(tmp_path)
    issue_number = _escalate(repo, tracking_issue=17, branch="dev/abc-add-login", session_id="sess-abc123")

    captured = {}

    async def fake_run_autonomous_cycle(repo_arg, objective, env, **kwargs):
        captured.update(kwargs)
        return AutonomousCycleReport(run_id=kwargs["run_id"], actions=["did stuff"], final_message="ok", cost_usd=0.01)

    monkeypatch.setattr(human_input, "run_autonomous_cycle", fake_run_autonomous_cycle)

    context = Memory(repo).get_human_input_context(issue_number)
    asyncio.run(human_input._run_human_resume_background("new-run-key", "myapp", repo, context, "Use OAuth."))

    assert captured["human_answer"] == "Use OAuth."
    assert captured["human_answer_issue"] == 17
    assert "dev/abc-add-login" in captured["feature"]
    assert "17" in captured["feature"]
    assert registry.get_run("new-run-key")["status"] == "completed"

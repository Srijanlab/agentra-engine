"""server/routes/human_input.py -- the dashboard-answer half of GitHub
issue #34's human-in-the-loop resume. The engine records the answer and
enqueues a `human_resume` job; the loop runs the resume.
"""

import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentra import registry, server
from agentra.connectors import github_fake
from agentra.memory import Memory
from agentra.server.routes import human_input


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


def _isolate(tmp_path, monkeypatch):
    home = tmp_path / "agentra_home"
    monkeypatch.setattr(registry, "_ddb", None)
    monkeypatch.setattr(registry, "AGENTRA_HOME", home)
    monkeypatch.setattr(registry, "APPS_PATH", home / "apps.json")
    monkeypatch.setattr(registry, "INBOX_ROOT", home / "inbox")
    monkeypatch.setattr(registry, "PAUSE_PATH", home / "paused.json")
    monkeypatch.setattr(registry, "_RUNS_PATH", home / "runs.json")
    monkeypatch.setattr(registry, "_LOOPS_PATH", home / "loops.json")
    monkeypatch.setattr(registry, "_JOBS_PATH", home / "jobs.json")
    monkeypatch.setattr(registry, "_AGENT_STEPS_PATH", home / "agent_steps.jsonl")
    server._active_runs.clear()
    server._app_locks.clear()
    github_fake.install(monkeypatch=monkeypatch)


def _escalate(repo: Path, *, tracking_issue: int | None = None) -> int:
    mem = Memory(repo)
    issue_number = mem.record_known_bug(
        "run1", "medium", "Two auth providers are equally valid.",
        "Requires a human decision.", source="implementation-agent-human-input-required",
        needs_human=True, title="Human input required: Add login",
    )
    mem.record_human_input_context(
        issue_number, app=repo.name, run_id="run1", question="OAuth or magic links?",
        branch="dev/abc-add-login", session_id="sess-abc123",
        tracking_issue=tracking_issue if tracking_issue is not None else issue_number,
    )
    return issue_number


def test_dispatch_human_answer_raises_for_an_issue_with_no_context(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    repo = _register_tmp_app(tmp_path)
    with pytest.raises(ValueError):
        human_input.dispatch_human_answer("myapp", repo, 999, "some answer", source="human-input")


def test_dispatch_human_answer_records_the_answer_and_enqueues_a_resume(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    repo = _register_tmp_app(tmp_path)
    issue_number = _escalate(repo, tracking_issue=17)

    out = human_input.dispatch_human_answer("myapp", repo, issue_number, "Use OAuth.", source="human-input")

    assert out["run_key"] and out["job_id"]
    assert not Memory(repo).human_input_pending(issue_number)  # label removed
    [job] = registry.list_jobs()
    assert job["kind"] == "human_resume"
    assert job["payload"]["issue_number"] == issue_number
    assert job["payload"]["answer"] == "Use OAuth."
    assert job["payload"]["context"]["branch"] == "dev/abc-add-login"


def test_a_second_answer_after_the_first_is_a_noop_ack(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    repo = _register_tmp_app(tmp_path)
    issue_number = _escalate(repo)
    human_input.dispatch_human_answer("myapp", repo, issue_number, "OAuth", source="human-input")

    again = human_input.dispatch_human_answer("myapp", repo, issue_number, "magic links", source="slack")
    assert again["already_answered"] is True
    assert len(registry.list_jobs()) == 1


def test_submit_human_input_endpoint(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    repo = _register_tmp_app(tmp_path)
    issue_number = _escalate(repo)
    client = TestClient(server.app)

    assert client.post("/apps/nope/human-input", json={"issue_number": 1, "answer": "x"}).status_code == 404
    assert client.post("/apps/myapp/human-input", json={"issue_number": 999, "answer": "x"}).status_code == 404

    ok = client.post("/apps/myapp/human-input", json={"issue_number": issue_number, "answer": "Use OAuth."})
    assert ok.status_code == 200 and ok.json()["accepted"] is True
    assert registry.list_jobs()[0]["kind"] == "human_resume"


def test_submit_human_input_respects_system_pause(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    repo = _register_tmp_app(tmp_path)
    issue_number = _escalate(repo)
    registry.pause("maintenance")

    body = TestClient(server.app).post(
        "/apps/myapp/human-input", json={"issue_number": issue_number, "answer": "x"}
    ).json()
    assert body["triggered"] is False
    assert registry.list_jobs() == []


def test_list_needs_human(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    repo = _register_tmp_app(tmp_path)
    issue_number = _escalate(repo, tracking_issue=17)
    loop_id = registry.bind_loop("myapp", 17)
    registry.set_loop_human_input(loop_id, {"question": "OAuth or magic links?", "issue_number": issue_number})
    registry.set_loop_status(loop_id, "waiting_for_human")

    runs = TestClient(server.app).get("/needs-human").json()["runs"]
    assert any(r["human_input"].get("question") == "OAuth or magic links?" for r in runs)

"""Tests for server/routes/review.py: the dashboard's Backlog board and Ready to Review tabs."""

import json
import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from agentra import registry, server
from agentra.connectors import github_fake, github_issues
from agentra.memory import Memory


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


def test_backlog_board_buckets_not_started_in_progress_and_code_complete(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    repo = _register_tmp_app(tmp_path)
    repo_url = Memory(repo)._repo_url()
    mem = Memory(repo)

    fresh_bug = github_issues.create_issue(repo_url, "A fresh bug", "body", labels=["bug", "agentra"])
    fresh_feature = github_issues.create_issue(repo_url, "A fresh feature", "body", labels=["feature", "agentra"])

    in_progress_bug = github_issues.create_issue(repo_url, "An in-progress bug", "body", labels=["bug", "agentra"])
    mem.record_in_progress_branch(in_progress_bug["number"], "dev/in-progress-branch")

    code_complete_bug = github_issues.create_issue(repo_url, "A code-complete bug", "body", labels=["bug", "agentra"])
    github_issues.mark_code_complete(repo_url, code_complete_bug["number"])

    client = TestClient(server.app)
    response = client.get("/apps/myapp/backlog-board")
    assert response.status_code == 200
    body = response.json()

    not_started_bug_titles = {b["diagnosis"] for b in body["not_started"]["bugs"]}
    assert "A fresh bug" in not_started_bug_titles
    assert "An in-progress bug" not in not_started_bug_titles  # elevated out of not_started
    assert "A code-complete bug" not in not_started_bug_titles

    not_started_feature_titles = {f["description"] for f in body["not_started"]["features"]}
    assert "A fresh feature" in not_started_feature_titles

    in_progress_titles = {i["diagnosis"] for i in body["in_progress"]["single_part"]}
    assert "An in-progress bug" in in_progress_titles

    code_complete_titles = {i["diagnosis"] for i in body["code_complete"]}
    assert "A code-complete bug" in code_complete_titles


def test_ready_to_review_attaches_test_report_when_one_exists(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    repo = _register_tmp_app(tmp_path)
    repo_url = Memory(repo)._repo_url()
    mem = Memory(repo)

    tested_issue = github_issues.create_issue(repo_url, "A tested feature", "body", labels=["feature", "agentra"])
    mem.record_in_progress_branch(tested_issue["number"], "dev/tested-branch", run_id="run-abc")
    github_issues.mark_code_complete(repo_url, tested_issue["number"])
    github_issues.mark_shipped_to_preprod(repo_url, tested_issue["number"])
    github_issues.mark_tested(repo_url, tested_issue["number"])

    from agentra.agents.testing import report_path

    path = report_path(repo, "run-abc")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"status": "pass", "test_cases": [{"criterion": "x", "result": "pass", "evidence": "y"}]}))

    no_report_issue = github_issues.create_issue(repo_url, "Tested with no report", "body", labels=["feature", "agentra"])
    mem.record_in_progress_branch(no_report_issue["number"], "dev/no-report-branch", run_id="run-missing")
    github_issues.mark_code_complete(repo_url, no_report_issue["number"])
    github_issues.mark_shipped_to_preprod(repo_url, no_report_issue["number"])
    github_issues.mark_tested(repo_url, no_report_issue["number"])

    client = TestClient(server.app)
    response = client.get("/apps/myapp/ready-to-review")
    assert response.status_code == 200
    items = {i["description"]: i for i in response.json()["items"]}

    assert items["A tested feature"]["test_report"]["status"] == "pass"
    assert items["A tested feature"]["test_report"]["test_cases"][0]["criterion"] == "x"
    assert items["Tested with no report"]["test_report"] is None

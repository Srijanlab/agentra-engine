"""POST /apps with repos=[...] (multi-repo app registration), the digest/detail
routes' back-compat coordination-repo view, and DELETE /apps/{name}."""

import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from agentra import registry, server
from agentra.connectors import github_fake


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _init_origin(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    (path / "README.md").write_text(f"{path.name}\n")
    _git(path, "add", "README.md")
    _git(path, "commit", "-m", "initial")
    return path


def _isolate_registry(tmp_path, monkeypatch):
    home = tmp_path / "agentra_home"
    monkeypatch.setattr(registry, "_db", None)
    monkeypatch.setattr(registry, "AGENTRA_HOME", home)
    monkeypatch.setattr(registry, "APPS_PATH", home / "apps.json")
    monkeypatch.setattr(registry, "INBOX_ROOT", home / "inbox")
    monkeypatch.setattr(registry, "PAUSE_PATH", home / "paused.json")
    monkeypatch.setattr(registry, "REPOS_ROOT", tmp_path / "repos")
    monkeypatch.setattr(registry, "_RUNS_PATH", home / "runs.json")
    monkeypatch.setattr(registry, "_LOOPS_PATH", home / "loops.json")
    monkeypatch.setattr(registry, "_AGENT_STEPS_PATH", home / "agent_steps.jsonl")
    server._active_runs.clear()
    server._app_locks.clear()
    github_fake.install(monkeypatch=monkeypatch)


def _register_multi_repo(client: TestClient, tmp_path: Path) -> dict:
    coord = _init_origin(tmp_path / "coord-origin")
    engine = _init_origin(tmp_path / "engine-origin")
    ui = _init_origin(tmp_path / "ui-origin")
    resp = client.post("/apps", json={
        "name": "agentra",
        "objective": "Improve agentra itself.",
        "schedule_hours": 24,
        "repos": [
            {"name": "backlog", "repo_url": str(coord), "branch": "main", "role": "coordination"},
            {"name": "engine", "repo_url": str(engine), "branch": "main", "role": "code"},
            {"name": "ui", "repo_url": str(ui), "branch": "main", "role": "code"},
        ],
    })
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_register_multi_repo_app_clones_every_repo(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    client = TestClient(server.app)

    body = _register_multi_repo(client, tmp_path)

    assert body["registered"] is True
    repos = registry.get_app_repos("agentra")
    assert set(repos) == {"backlog", "engine", "ui"}
    assert all(spec.path.is_dir() for spec in repos.values())
    assert repos["backlog"].role == "coordination"


def test_register_multi_repo_app_sets_objective_on_coordination_repo(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    client = TestClient(server.app)

    _register_multi_repo(client, tmp_path)

    from agentra.memory import Memory

    coord = registry.get_coordination_repo("agentra")
    assert Memory(coord.path).get_objective() == "Improve agentra itself."


def test_list_apps_does_not_crash_for_a_multi_repo_app(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    client = TestClient(server.app)
    _register_multi_repo(client, tmp_path)

    resp = client.get("/apps")

    assert resp.status_code == 200
    assert "agentra" in resp.json()["apps"]
    assert resp.json()["apps"]["agentra"]["objective"] == "Improve agentra itself."


def test_get_app_detail_returns_repos_for_a_multi_repo_app(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    client = TestClient(server.app)
    _register_multi_repo(client, tmp_path)

    resp = client.get("/apps/agentra")

    assert resp.status_code == 200
    detail = resp.json()
    assert {r["name"] for r in detail["repos"]} == {"backlog", "engine", "ui"}
    assert detail["repo_url"] is not None  # sourced from the coordination repo


def test_register_multi_repo_app_requires_a_coordination_repo(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    client = TestClient(server.app)
    engine = _init_origin(tmp_path / "engine-origin")

    resp = client.post("/apps", json={
        "name": "agentra",
        "repos": [{"name": "engine", "repo_url": str(engine), "branch": "main", "role": "code"}],
    })

    assert resp.status_code == 400


def test_delete_app_removes_it(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    client = TestClient(server.app)
    _register_multi_repo(client, tmp_path)

    resp = client.delete("/apps/agentra")

    assert resp.status_code == 200
    assert resp.json() == {"removed": True, "name": "agentra"}
    assert "agentra" not in registry.list_apps()


def test_delete_unknown_app_is_404(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    client = TestClient(server.app)

    resp = client.delete("/apps/nope")

    assert resp.status_code == 404


def test_dispatch_once_does_not_crash_for_a_multi_repo_app(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    client = TestClient(server.app)
    _register_multi_repo(client, tmp_path)

    summary = registry.dispatch_once()

    assert summary.errors == []


def test_daily_standup_does_not_crash_for_a_multi_repo_app(tmp_path, monkeypatch):
    import asyncio

    from agentra import standup

    _isolate_registry(tmp_path, monkeypatch)
    client = TestClient(server.app)
    _register_multi_repo(client, tmp_path)

    reports = asyncio.run(standup.run_daily_standup(registry.list_apps()))

    assert "agentra" in reports


def test_slack_assistant_finds_agentra_by_app_name(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    client = TestClient(server.app)
    _register_multi_repo(client, tmp_path)

    from agentra.agents import slack_assistant

    repo = slack_assistant._agentra_repo()

    assert repo == registry.get_coordination_repo("agentra").path

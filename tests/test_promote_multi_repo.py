"""Multi-repo Phase 3: the dashboard's Promote button (POST /apps/{name}/promote)
and orchestrator.run_promote/_record_production_release must resolve which CODE
repo to promote for a multi-repo app -- the coordination repo (issue bookkeeping,
released.json ledger) and the code repo (deploy strategy dispatch, its own
prod_branch/env) are different repos, unlike a legacy single-repo app where
they're the same.
"""

import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from agentra import environments, registry, server
from agentra.connectors import github_fake
from agentra.memory import Memory
from agentra.server.routes import triggers



def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


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
    monkeypatch.setattr(registry, "_ddb", None)
    monkeypatch.setattr(registry, "AGENTRA_HOME", home)
    monkeypatch.setattr(registry, "APPS_PATH", home / "apps.json")
    monkeypatch.setattr(registry, "INBOX_ROOT", home / "inbox")
    monkeypatch.setattr(registry, "PAUSE_PATH", home / "paused.json")
    monkeypatch.setattr(registry, "_RUNS_PATH", home / "runs.json")
    monkeypatch.setattr(registry, "_LOOPS_PATH", home / "loops.json")
    monkeypatch.setattr(registry, "_JOBS_PATH", home / "jobs.json")
    monkeypatch.setattr(registry, "_AGENT_STEPS_PATH", home / "agent_steps.jsonl")
    monkeypatch.setattr(registry, "REPOS_ROOT", tmp_path / "repos")
    server._active_runs.clear()
    server._app_locks.clear()
    github_fake.install(monkeypatch=monkeypatch)


def _register_multi_repo_app(tmp_path: Path) -> dict[str, Path]:
    coord_origin = _init_origin(tmp_path / "coord-origin")
    engine_origin = _init_origin(tmp_path / "engine-origin")
    ui_origin = _init_origin(tmp_path / "ui-origin")
    registry.register_app(
        "agentra",
        repos=[
            {"name": "backlog", "repo_url": str(coord_origin), "branch": "main", "role": "coordination"},
            {"name": "engine", "repo_url": str(engine_origin), "branch": "main", "role": "code"},
            {"name": "ui", "repo_url": str(ui_origin), "branch": "main", "role": "code"},
        ],
    )
    Memory(registry.get_coordination_repo("agentra").path).set_objective("Ship agentra.")
    return registry.get_app_repos("agentra")


# -- route-level validation -----------------------------------------------------------


def test_promote_without_target_repo_enqueues_an_auto_promote(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    _register_multi_repo_app(tmp_path)

    response = TestClient(server.app).post("/apps/agentra/promote")

    assert response.status_code == 200
    assert response.json()["triggered"] is True
    [job] = registry.list_jobs()
    # issue #7: no explicit pick -> the loop auto-resolves which code repos to promote
    assert job["kind"] == "promote" and job["payload"]["target_repos"] is None


def test_promote_rejects_an_unknown_target_repo(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    _register_multi_repo_app(tmp_path)

    response = TestClient(server.app).post("/apps/agentra/promote", json={"target_repo": "nope"})

    assert response.status_code == 400
    assert "nope" in response.json()["detail"]


def test_promote_dispatches_with_the_named_target_repo(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    _register_multi_repo_app(tmp_path)

    response = TestClient(server.app).post("/apps/agentra/promote", json={"target_repo": "engine"})

    assert response.status_code == 200
    assert response.json()["triggered"] is True
    [job] = registry.list_jobs()
    assert job["kind"] == "promote" and job["payload"]["target_repos"] == ["engine"]


def test_promote_legacy_single_repo_app_needs_no_target_repo(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    repo = tmp_path / "myapp"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("hello\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")
    _git(repo, "remote", "add", "origin", "https://github.com/acme/myapp.git")
    Memory(repo).set_objective("Ship things.")
    registry.register_app("myapp", str(repo), repo_url="https://github.com/acme/myapp.git", branch="main")

    response = TestClient(server.app).post("/apps/myapp/promote")

    assert response.status_code == 200
    assert response.json()["triggered"] is True

"""Multi-repo Phase 3: the dashboard's Promote button (POST /apps/{name}/promote)
and orchestrator.run_promote/_record_production_release must resolve which CODE
repo to promote for a multi-repo app -- the coordination repo (issue bookkeeping,
released.json ledger) and the code repo (deploy strategy dispatch, its own
prod_branch/env) are different repos, unlike a legacy single-repo app where
they're the same.
"""

import asyncio
import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from agentra import environments, registry, server
from agentra.agents import deployment
from agentra.agents.base import AgentResult
from agentra.connectors import github_fake
from agentra.memory import Memory
from agentra.server.routes import triggers


def _close_background_coro(coro):
    coro.close()
    return None


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
    monkeypatch.setattr(registry, "_db", None)
    monkeypatch.setattr(registry, "AGENTRA_HOME", home)
    monkeypatch.setattr(registry, "APPS_PATH", home / "apps.json")
    monkeypatch.setattr(registry, "INBOX_ROOT", home / "inbox")
    monkeypatch.setattr(registry, "PAUSE_PATH", home / "paused.json")
    monkeypatch.setattr(registry, "_RUNS_PATH", home / "runs.json")
    monkeypatch.setattr(registry, "_AGENT_STEPS_PATH", home / "agent_steps.jsonl")
    monkeypatch.setattr(registry, "REPOS_ROOT", tmp_path / "repos")
    server._active_runs.clear()
    server._app_locks.clear()
    monkeypatch.setattr(server.asyncio, "create_task", _close_background_coro)
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


def test_promote_requires_target_repo_when_ambiguous(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    _register_multi_repo_app(tmp_path)

    response = TestClient(server.app).post("/apps/agentra/promote")

    assert response.status_code == 400
    assert "target_repo" in response.json()["detail"]


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


# -- run_promote/_record_production_release: coordination vs code repo split ---------


def test_run_promote_deploys_the_code_repo_not_the_coordination_repo(tmp_path, monkeypatch):
    coord_repo = tmp_path / "coord"
    coord_repo.mkdir()
    _git(coord_repo, "init", "-b", "main")
    _git(coord_repo, "config", "user.email", "test@example.com")
    _git(coord_repo, "config", "user.name", "Test")
    (coord_repo / "f.txt").write_text("x\n")
    _git(coord_repo, "add", ".")
    _git(coord_repo, "commit", "-m", "initial")

    code_repo = tmp_path / "code"
    code_repo.mkdir()
    _git(code_repo, "init", "-b", "main")
    _git(code_repo, "config", "user.email", "test@example.com")
    _git(code_repo, "config", "user.name", "Test")
    (code_repo / "f.txt").write_text("x\n")
    _git(code_repo, "add", ".")
    _git(code_repo, "commit", "-m", "initial")
    environments.save(code_repo, environments.EnvironmentConfig(deploy_strategy="vercel_firebase"))

    captured = {}

    async def fake_strategy(repo, env, run_id, session_id):
        captured["repo"] = repo
        return AgentResult(ok=True, text="deployed", json_data={"status": "deployed"}, cost_usd=0.0, turns=0)

    monkeypatch.setattr(deployment, "PROD_STRATEGIES", {"vercel_firebase": fake_strategy})

    from agentra.orchestrator import run_promote

    result = asyncio.run(run_promote(coord_repo, run_id="run1", code_repo=code_repo))

    assert result["ok"] is True
    assert captured["repo"] == code_repo
    assert captured["repo"] != coord_repo


def test_record_production_release_reads_mem_from_coordination_and_env_from_code_repo(tmp_path, monkeypatch):
    coord_repo = tmp_path / "coord"
    coord_repo.mkdir()
    code_repo = tmp_path / "code"
    code_repo.mkdir()
    environments.save(code_repo, environments.EnvironmentConfig(prod_branch="main"))

    monkeypatch.setattr(Memory, "pending_promotion_features", lambda self: [])
    monkeypatch.setattr(Memory, "shipped_features", lambda self: [])
    monkeypatch.setattr(Memory, "closed_bugs", lambda self: [])
    captured_mem_repo = {}

    real_init = Memory.__init__

    def _tracking_init(self, repo, *a, **k):
        captured_mem_repo["repo"] = repo
        return real_init(self, repo, *a, **k)

    monkeypatch.setattr(Memory, "__init__", _tracking_init)
    monkeypatch.setattr(triggers, "_branch_head_sha", lambda repo, branch: None)

    released = triggers._record_production_release(coord_repo, "run1", code_repo=code_repo)

    assert released == []
    assert captured_mem_repo["repo"] == coord_repo

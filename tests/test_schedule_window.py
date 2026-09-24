"""Schedule-due computation must not lose an app's runs or open cycle jobs to fixed-size windows."""

import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentra import environments, registry, server
from agentra.connectors import github_fake
from agentra.memory import Memory
from agentra.registry.scheduler import compute_schedule_status


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


@pytest.fixture
def env(tmp_path, monkeypatch):
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
    monkeypatch.delenv("AGENTRA_CONTINUOUS_GAP_SECONDS", raising=False)
    server._active_runs.clear()
    server._app_locks.clear()
    github_fake.install(monkeypatch=monkeypatch)
    return tmp_path


def _configure(repo: Path, **fields) -> None:
    cfg = environments.load(repo) or environments.EnvironmentConfig()
    for key, value in fields.items():
        setattr(cfg, key, value)
    environments.save(repo, cfg)


def _flood(prefix: str, count: int, app: str, source: str, start: float) -> None:
    for i in range(count):
        registry.record_run(f"{prefix}{i}", app=app, source=source, status="completed", started_at=start + i)


def test_last_run_at_survives_100_newer_non_scheduled_runs(env):
    now = time.time()
    registry.record_run("old", app="myapp", source="scheduled", status="completed", started_at=now - 5000)
    _flood("od", 60, "myapp", "on-demand", now - 1000)
    _flood("pr", 60, "myapp", "promote", now - 900)
    registry.record_run("nostart", app="myapp", source="scheduled", status="completed")

    assert registry.last_run_at("myapp", source="scheduled") == now - 5000
    assert registry.last_run_at("nobody", source="scheduled") is None


def test_last_run_at_survives_200_newer_runs_of_other_apps(env):
    now = time.time()
    registry.record_run("mine", app="myapp", source="scheduled", status="completed", started_at=now - 5000)
    _flood("x", 250, "other", "scheduled", now - 1000)

    assert registry.last_run_at("myapp", source="scheduled") == now - 5000
    assert [r["run_key"] for r in registry.list_app_runs("myapp")] == ["mine"]


def test_fixed_cadence_not_due_despite_non_scheduled_flood(env):
    repo = _register_tmp_app(env)
    _configure(repo, schedule_hours=6.0, schedule_continuous=False)
    now = time.time()
    registry.record_run("sched", app="myapp", source="scheduled", status="completed", started_at=now - 3600)
    _flood("od", 120, "myapp", "on-demand", now - 3000)

    body = TestClient(server.app).get("/apps/myapp/schedule").json()
    assert body["last_scheduled_run_at"] == pytest.approx(now - 3600, abs=1)
    assert body["due_now"] is False
    assert body["next_scheduled_run_at"] == pytest.approx(now - 3600 + 6 * 3600, abs=1)


def test_continuous_ignores_flood_of_other_apps_runs(env):
    repo = _register_tmp_app(env)
    _configure(repo, schedule_continuous=True)
    now = time.time()
    registry.record_run("mine", app="myapp", source="on-demand", status="completed", started_at=now - 100, updated_at=now - 10)
    _flood("x", 250, "other", "scheduled", now - 50)

    status = compute_schedule_status("myapp", repo)
    assert status.due_now is False
    assert TestClient(server.app).get("/apps/myapp/schedule").json()["due_now"] is False


def test_open_cycle_job_not_hidden_by_50_newer_jobs_of_other_apps(env):
    repo = _register_tmp_app(env)
    _configure(repo, schedule_continuous=True)
    registry.enqueue_job("cycle", {"app": "myapp", "run_key": "r0"})
    for i in range(60):
        registry.enqueue_job("cycle", {"app": f"other{i}", "run_key": f"o{i}"}, dedup_key=f"cycle:other{i}")

    status = compute_schedule_status("myapp", repo)
    assert status.cycle_job_pending and status.queued and status.cycle_in_flight
    assert status.due_now is False

    claimed = registry.claim_next_job()
    assert claimed["payload"]["app"] == "myapp"
    for i in range(60):
        registry.enqueue_job("cycle", {"app": f"more{i}", "run_key": f"m{i}"}, dedup_key=f"cycle:more{i}")
    assert compute_schedule_status("myapp", repo).cycle_job_claimed is True


def test_enqueue_cycle_dedup_not_hidden_by_50_newer_jobs(env):
    _register_tmp_app(env)
    registry.enqueue_job("cycle", {"app": "myapp", "run_key": "r0"})
    for i in range(60):
        registry.enqueue_job("cycle", {"app": f"other{i}", "run_key": f"o{i}"}, dedup_key=f"cycle:other{i}")

    body = TestClient(server.app).post("/apps/myapp/run").json()
    assert body["triggered"] is False
    assert body["reason"] == "a cycle for this app is already queued"


def test_list_jobs_default_limit_unchanged(env):
    for i in range(60):
        registry.enqueue_job("cycle", {"app": f"a{i}"}, dedup_key=f"cycle:a{i}")
    assert len(registry.list_jobs(status="pending")) == 50
    assert len(registry.list_jobs(status="pending", limit=None)) == 60

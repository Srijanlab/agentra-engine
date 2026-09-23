"""Continuous scheduling mode, cycle-job dedup, and the schedule_continuous config round-trip."""

import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentra import environments, registry, server
from agentra.connectors import github_fake, github_variables
from agentra.memory import Memory
from agentra.registry.scheduler import compute_schedule_status
from agentra.registry.scheduler.status import continuous_gap_seconds


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


def _client() -> TestClient:
    return TestClient(server.app)


def _configure(repo: Path, **fields) -> None:
    cfg = environments.load(repo) or environments.EnvironmentConfig()
    for key, value in fields.items():
        setattr(cfg, key, value)
    environments.save(repo, cfg)


def _cycle_run(key: str, status: str, *, started_at: float, updated_at: float | None = None, source="scheduled") -> None:
    fields = {"app": "myapp", "source": source, "status": status, "started_at": started_at}
    if updated_at is not None:
        fields["updated_at"] = updated_at
    registry.record_run(key, **fields)


def _schedule() -> dict:
    return _client().get("/apps/myapp/schedule").json()


def test_gap_seconds_default_and_invalid_fallback(monkeypatch):
    monkeypatch.delenv("AGENTRA_CONTINUOUS_GAP_SECONDS", raising=False)
    assert continuous_gap_seconds() == 60
    monkeypatch.setenv("AGENTRA_CONTINUOUS_GAP_SECONDS", "-5")
    assert continuous_gap_seconds() == 60
    monkeypatch.setenv("AGENTRA_CONTINUOUS_GAP_SECONDS", "abc")
    assert continuous_gap_seconds() == 60
    monkeypatch.setenv("AGENTRA_CONTINUOUS_GAP_SECONDS", "5")
    assert continuous_gap_seconds() == 5


def test_continuous_never_ran_is_due_immediately_even_with_zero_hours(env):
    repo = _register_tmp_app(env)
    _configure(repo, schedule_hours=0.0, schedule_continuous=True)
    body = _schedule()
    assert body["continuous"] is True and body["gap_seconds"] == 60
    assert body["due_now"] is True
    tick = _client().get("/trigger/cron").json()["apps"]["myapp"]
    assert tick["triggered"] is True and tick["run_key"] and tick["job_id"]


def test_zero_hours_without_continuous_stays_disabled(env):
    repo = _register_tmp_app(env)
    _configure(repo, schedule_hours=0.0, schedule_continuous=False)
    body = _schedule()
    assert body["continuous"] is False and body["due_now"] is False
    assert _client().get("/trigger/cron").json()["apps"]["myapp"]["triggered"] is False
    assert registry.list_jobs() == []


def test_continuous_in_flight_run_is_not_due(env):
    repo = _register_tmp_app(env)
    _configure(repo, schedule_continuous=True)
    _cycle_run("r1", "running", started_at=time.time() - 7200, updated_at=time.time() - 7200, source="on-demand")
    body = _schedule()
    assert body["due_now"] is False and body["next_scheduled_run_at"] is None
    assert compute_schedule_status("myapp", repo).due_in_seconds > 0
    assert _client().get("/trigger/cron").json()["apps"]["myapp"]["triggered"] is False
    assert registry.list_jobs() == []


@pytest.mark.parametrize("state", ["pending", "claimed"])
def test_continuous_open_cycle_job_is_not_due(env, state):
    repo = _register_tmp_app(env)
    _configure(repo, schedule_continuous=True)
    _cycle_run("r0", "completed", started_at=time.time() - 9999, updated_at=time.time() - 9999)
    registry.enqueue_job("cycle", {"app": "myapp", "run_key": "r0"})
    if state == "claimed":
        registry.claim_next_job()
    body = _schedule()
    assert body["due_now"] is False and body["next_scheduled_run_at"] is None
    assert _client().get("/trigger/cron").json()["apps"]["myapp"]["triggered"] is False
    assert len(registry.list_jobs()) == 1


def test_continuous_gap_not_elapsed_then_elapsed(env):
    repo = _register_tmp_app(env)
    _configure(repo, schedule_continuous=True)
    now = time.time()
    _cycle_run("r1", "completed", started_at=now - 100, updated_at=now - 10)
    body = _schedule()
    assert body["due_now"] is False
    assert body["next_scheduled_run_at"] == pytest.approx(now - 10 + 60, abs=2)
    tick = _client().get("/trigger/cron").json()["apps"]["myapp"]
    assert tick["triggered"] is False and "not due" in tick["reason"]

    registry.record_run("r1", updated_at=now - 120)
    assert _schedule()["due_now"] is True
    assert _client().get("/trigger/cron").json()["apps"]["myapp"]["triggered"] is True


def test_continuous_paused_is_never_due(env):
    repo = _register_tmp_app(env)
    _configure(repo, schedule_continuous=True)
    registry.pause("maintenance")
    body = _schedule()
    assert body["paused"] is True and body["due_now"] is False
    assert _client().get("/trigger/cron").json().get("triggered", False) is False
    assert registry.list_jobs() == []


def test_non_continuous_cadence_is_unchanged(env):
    repo = _register_tmp_app(env)
    _configure(repo, schedule_hours=24.0)
    last = time.time() - 3600
    registry.record_run("r1", app="myapp", source="scheduled", status="completed", started_at=last)
    status = compute_schedule_status("myapp", repo)
    assert status.continuous is False
    assert status.next_due_at == pytest.approx(last + 24 * 3600, abs=2)
    assert status.due_now is False


def test_cycle_dedup_ignores_other_job_kinds_and_sees_claimed_jobs(env):
    _register_tmp_app(env)
    registry.enqueue_job("promote", {"app": "myapp"})
    first = _client().post("/apps/myapp/run").json()
    assert first["triggered"] is True

    registry.claim_next_job()  # oldest is the promote job
    registry.claim_next_job()  # then the cycle job -> both claimed, none pending
    assert registry.list_jobs(status="pending") == []
    second = _client().post("/apps/myapp/run").json()
    assert second["triggered"] is False
    assert len([j for j in registry.list_jobs() if j["kind"] == "cycle"]) == 1


def test_schedule_continuous_env_var_round_trip(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "remote", "add", "origin", "https://github.com/acme/repo.git")
    store: dict[str, str] = {}
    monkeypatch.setattr(github_variables, "set_variable", lambda url, name, value: store.update({name: value}))
    monkeypatch.setattr(github_variables, "list_variables", lambda url: dict(store))

    environments.save(repo, environments.EnvironmentConfig(schedule_continuous=True))
    assert store["AGENTRA_SCHEDULE_CONTINUOUS"] == "true"
    assert environments.load(repo).schedule_continuous is True
    environments.save(repo, environments.EnvironmentConfig(schedule_continuous=False))
    assert environments.load(repo).schedule_continuous is False


def test_patch_and_get_schedule_continuous(env):
    _register_tmp_app(env)
    client = _client()
    assert client.get("/apps/myapp").json()["schedule_continuous"] is False
    resp = client.patch("/apps/myapp", json={"schedule_continuous": True})
    assert resp.status_code == 200 and resp.json()["updated"] is True
    assert client.get("/apps/myapp").json()["schedule_continuous"] is True
    assert client.get("/apps").json()["apps"]["myapp"]["schedule_continuous"] is True
    client.patch("/apps/myapp", json={"schedule_continuous": False})
    assert client.get("/apps/myapp").json()["schedule_continuous"] is False
    assert client.get("/apps").json()["apps"]["myapp"]["schedule_continuous"] is False

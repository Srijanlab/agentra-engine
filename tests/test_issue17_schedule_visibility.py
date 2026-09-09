"""Issue #17 (engine): shared schedule-due helper + read-only GET /apps/{app}/schedule."""

import subprocess
import time
from pathlib import Path

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


def _client() -> TestClient:
    return TestClient(server.app)


def _set_schedule_hours(repo: Path, hours: float) -> None:
    env = environments.load(repo) or environments.EnvironmentConfig()
    env.schedule_hours = hours
    environments.save(repo, env)


def _record_scheduled_run(app: str, started_at: float) -> None:
    registry.record_run(
        f"r{started_at:.0f}", app=app, source="scheduled", status="completed", started_at=started_at
    )


# --- helper --------------------------------------------------------------------

def test_helper_not_yet_due(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    repo = _register_tmp_app(tmp_path)
    _set_schedule_hours(repo, 24.0)
    last = time.time() - 3600
    _record_scheduled_run("myapp", last)

    status = compute_schedule_status("myapp", repo)
    assert status.cadence_hours == 24.0
    assert status.due_in_seconds > 0
    assert status.due_now is False
    assert abs(status.next_due_at - (last + 24 * 3600)) < 2
    assert abs(status.next_scheduled_run_at - (last + 24 * 3600)) < 2


def test_helper_overdue(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    repo = _register_tmp_app(tmp_path)
    _set_schedule_hours(repo, 24.0)
    _record_scheduled_run("myapp", time.time() - 100_000)

    status = compute_schedule_status("myapp", repo)
    assert status.due_in_seconds < 0
    assert status.due_now is True


def test_helper_due_soon_and_never_run(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    repo = _register_tmp_app(tmp_path)
    _set_schedule_hours(repo, 24.0)

    never = compute_schedule_status("myapp", repo)
    assert never.last_scheduled_run_at is None
    assert never.next_due_at is None
    assert never.due_in_seconds is None
    assert never.due_now is True
    assert never.next_scheduled_run_at is None

    _record_scheduled_run("myapp", time.time() - (24 * 3600 - 5))
    soon = compute_schedule_status("myapp", repo)
    assert 0 < soon.due_in_seconds <= 5
    assert soon.due_now is False


# --- endpoint -----------------------------------------------------------------

def test_endpoint_shape_and_types(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    repo = _register_tmp_app(tmp_path)
    _set_schedule_hours(repo, 12.0)
    last = time.time() - 3600
    _record_scheduled_run("myapp", last)

    resp = _client().get("/apps/myapp/schedule")
    assert resp.status_code == 200
    body = resp.json()
    for key in ("app", "cadence_hours", "last_scheduled_run_at", "next_scheduled_run_at",
                "due_now", "paused", "queued", "running_best_effort"):
        assert key in body
    assert body["app"] == "myapp"
    assert isinstance(body["cadence_hours"], (int, float)) and body["cadence_hours"] > 0
    assert body["cadence_hours"] == _client().get("/apps/myapp").json()["schedule_hours"]
    assert isinstance(body["last_scheduled_run_at"], (int, float))
    assert abs(body["next_scheduled_run_at"] - (last + 12 * 3600)) < 2
    assert body["due_now"] is False
    assert body["paused"] is False
    assert body["queued"] is False
    assert body["running_best_effort"] is False


def test_endpoint_404_for_unregistered_app(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    assert _client().get("/apps/ghost/schedule").status_code == 404


def test_endpoint_paused_nulls_next_run(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    repo = _register_tmp_app(tmp_path)
    _set_schedule_hours(repo, 24.0)
    _record_scheduled_run("myapp", time.time() - 3600)

    registry.pause("maintenance")
    body = _client().get("/apps/myapp/schedule").json()
    assert body["paused"] is True
    assert body["next_scheduled_run_at"] is None

    registry.resume()
    body = _client().get("/apps/myapp/schedule").json()
    assert body["paused"] is False
    assert body["next_scheduled_run_at"] is not None


def test_endpoint_queued_flag_tracks_pending_cycle_job(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _register_tmp_app(tmp_path)

    assert _client().get("/apps/myapp/schedule").json()["queued"] is False

    assert _client().post("/apps/myapp/run").json()["triggered"] is True
    assert _client().get("/apps/myapp/schedule").json()["queued"] is True


def test_endpoint_is_read_only(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _register_tmp_app(tmp_path)

    for _ in range(3):
        assert _client().get("/apps/myapp/schedule").json()["queued"] is False
    assert registry.list_jobs() == []
    assert _client().post("/apps/myapp/schedule").status_code in (404, 405)


# --- regression: cron enqueue decisions unchanged ----------------------------

def test_cron_tick_enqueue_decisions_unchanged(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    due = _register_tmp_app(tmp_path, "due")
    notdue = _register_tmp_app(tmp_path, "notdue")
    _set_schedule_hours(due, 24.0)
    _set_schedule_hours(notdue, 24.0)
    _record_scheduled_run("due", time.time() - 100_000)
    _record_scheduled_run("notdue", time.time() - 3600)

    body = _client().post("/trigger/scheduled", json={}).json()["apps"]
    assert body["due"]["triggered"] is True
    assert body["notdue"]["triggered"] is False
    assert "not due" in body["notdue"]["reason"]
    assert {j["payload"]["app"] for j in registry.list_jobs()} == {"due"}

    # A second tick enqueues nothing more and the endpoint still shows queued.
    again = _client().post("/trigger/scheduled", json={}).json()["apps"]
    assert again["due"]["triggered"] is False
    assert len(registry.list_jobs()) == 1
    assert _client().get("/apps/due/schedule").json()["queued"] is True


def test_cron_already_queued_short_circuits(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _register_tmp_app(tmp_path, "app1")
    registry.enqueue_job(
        "cycle", {"app": "app1", "run_key": "seed", "objective": "x"}, dedup_key="cycle:app1"
    )

    result = _client().post("/trigger/scheduled", json={"app": "app1"}).json()
    assert result["triggered"] is False
    assert "already queued" in result["reason"]
    assert len(registry.list_jobs()) == 1

    on_demand = _client().post("/apps/app1/run").json()
    assert on_demand["triggered"] is False
    assert "already queued" in on_demand["reason"]
    assert len(registry.list_jobs()) == 1
    assert _client().get("/apps/app1/schedule").json()["queued"] is True

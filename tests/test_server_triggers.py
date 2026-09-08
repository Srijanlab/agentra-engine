"""The engine's trigger endpoints record a run and enqueue a job for the loop
to claim -- the engine never executes a cycle / promotion / prod-debug itself.
"""

import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from agentra import registry, server
from agentra.connectors import github_fake
from agentra.memory import Memory


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


def test_on_demand_run_enqueues_a_cycle_job(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _register_tmp_app(tmp_path)

    body = _client().post("/apps/myapp/run").json()
    assert body["queued"] is True and body["run_key"]

    [job] = registry.list_jobs()
    assert job["kind"] == "cycle"
    assert job["payload"]["app"] == "myapp"
    assert job["payload"]["run_key"] == body["run_key"]
    assert registry.get_run(body["run_key"])["status"] == "queued"


def test_a_second_run_while_one_is_queued_is_a_noop(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _register_tmp_app(tmp_path)
    first = _client().post("/apps/myapp/run").json()

    second = _client().post("/apps/myapp/run").json()
    assert second["triggered"] is False
    assert len(registry.list_jobs()) == 1
    assert first["run_key"]


def test_scheduled_trigger_respects_per_app_schedule_hours(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    repo = _register_tmp_app(tmp_path)
    from agentra import environments
    env = environments.load(repo) or environments.EnvironmentConfig()
    env.schedule_hours = 24.0
    environments.save(repo, env)
    registry.record_run("prev", app="myapp", source="scheduled", status="completed", started_at=__import__("time").time())

    body = _client().post("/trigger/scheduled", json={"app": "myapp"}).json()
    assert body["triggered"] is False
    assert "not due" in body["reason"]
    assert registry.list_jobs() == []


def test_scheduled_no_app_fans_out_to_every_registered_app(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _register_tmp_app(tmp_path, "one")
    _register_tmp_app(tmp_path, "two")

    body = _client().post("/trigger/scheduled", json={}).json()
    assert set(body["apps"]) == {"one", "two"}
    assert {j["payload"]["app"] for j in registry.list_jobs()} == {"one", "two"}


def test_cron_endpoint_requires_the_secret_when_set(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _register_tmp_app(tmp_path)
    monkeypatch.setenv("CRON_SECRET", "s3cr3t")

    assert _client().get("/trigger/cron").status_code == 401
    ok = _client().get("/trigger/cron", headers={"Authorization": "Bearer s3cr3t"})
    assert ok.status_code == 200
    assert {j["payload"]["app"] for j in registry.list_jobs()} == {"myapp"}


def test_promote_enqueues_a_promote_job(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _register_tmp_app(tmp_path)

    body = _client().post("/apps/myapp/promote").json()
    assert body["queued"] is True
    [job] = registry.list_jobs()
    assert job["kind"] == "promote"
    assert job["payload"]["app"] == "myapp"
    # a second promote dedups on the still-open job
    _client().post("/apps/myapp/promote")
    assert len(registry.list_jobs()) == 1


def test_alarm_enqueues_a_prod_debug_job_and_respects_the_alarm_toggle(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    repo = _register_tmp_app(tmp_path)

    body = _client().post("/trigger/alarm", json={"app": "myapp", "symptom": "500s"}).json()
    assert body["queued"] is True
    assert registry.list_jobs()[0]["kind"] == "prod_debug"

    from agentra import environments
    env = environments.load(repo) or environments.EnvironmentConfig()
    env.alarm_enabled = False
    environments.save(repo, env)
    off = _client().post("/trigger/alarm", json={"app": "myapp", "symptom": "500s"}).json()
    assert off["triggered"] is False


def test_paused_system_enqueues_nothing(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _register_tmp_app(tmp_path)
    registry.pause("maintenance")

    assert _client().post("/apps/myapp/run").json()["triggered"] is False
    assert _client().post("/apps/myapp/promote").json()["triggered"] is False
    assert registry.list_jobs() == []

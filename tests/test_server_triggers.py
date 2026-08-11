import time
from pathlib import Path

from fastapi.testclient import TestClient

from agentra import environments, registry, server
from agentra.memory import Memory


def _close_background_coro(coro):
    coro.close()
    return None


def _register_tmp_app(tmp_path: Path, name: str = "myapp") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    Memory(repo).set_objective("Ship useful dashboard improvements.")
    registry.register_app(name, str(repo), repo_url="https://github.com/acme/myapp.git", branch="main")
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


def test_scheduled_trigger_respects_per_app_schedule_hours(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    repo = _register_tmp_app(tmp_path)
    environments.save(repo, environments.EnvironmentConfig(schedule_hours=24))
    registry.record_run(
        "recent",
        app="myapp",
        source="scheduled",
        status="completed",
        started_at=time.time(),
        objective="Ship useful dashboard improvements.",
        loop_id=registry.loop_id_for("Ship useful dashboard improvements."),
    )

    response = TestClient(server.app).post("/trigger/scheduled", json={"app": "myapp"})

    assert response.status_code == 200
    assert response.json() == {
        "triggered": False,
        "reason": "not due yet per this app's configured schedule",
    }


def test_on_demand_run_bypasses_schedule_gate(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    repo = _register_tmp_app(tmp_path)
    environments.save(repo, environments.EnvironmentConfig(schedule_hours=24))
    registry.record_run(
        "recent",
        app="myapp",
        source="scheduled",
        status="completed",
        started_at=time.time(),
        objective="Ship useful dashboard improvements.",
        loop_id=registry.loop_id_for("Ship useful dashboard improvements."),
    )

    response = TestClient(server.app).post("/apps/myapp/run")

    assert response.status_code == 200
    assert response.json()["triggered"] is True
    assert registry.get_run(response.json()["run_key"])["source"] == "on-demand"


def test_alarm_trigger_respects_per_app_alarm_enabled(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    repo = _register_tmp_app(tmp_path)
    environments.save(repo, environments.EnvironmentConfig(alarm_enabled=False))

    response = TestClient(server.app).post("/trigger/alarm", json={"app": "myapp", "symptom": "500s"})

    assert response.status_code == 200
    assert response.json() == {
        "triggered": False,
        "reason": "alarms disabled for this app",
    }


def test_dashboard_feature_request_submission_reaches_app_backlog(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    repo = _register_tmp_app(tmp_path)

    response = TestClient(server.app).post(
        "/apps/myapp/feature-requests",
        json={"description": "Let admins export the shipped list."},
    )

    assert response.status_code == 200
    assert response.json()["submitted"] is True
    queue = Memory(repo).feature_queue()
    assert len(queue) == 1
    assert queue[0]["description"] == "Let admins export the shipped list."
    assert queue[0]["source"] == "customer"


def test_dashboard_bug_submission_reaches_bug_backlog(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    repo = _register_tmp_app(tmp_path)

    response = TestClient(server.app).post(
        "/apps/myapp/backlog",
        json={"type": "bug", "severity": "high", "description": "Export crashes on an empty result set."},
    )

    assert response.status_code == 200
    assert response.json()["submitted"] is True
    bugs = Memory(repo).known_bugs()
    assert len(bugs) == 1
    assert bugs[0]["diagnosis"] == "Export crashes on an empty result set."
    assert bugs[0]["severity"] == "high"
    assert bugs[0]["source"] == "customer"


def test_promote_endpoint_records_human_approved_run(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    _register_tmp_app(tmp_path)

    response = TestClient(server.app).post("/apps/myapp/promote")

    assert response.status_code == 200
    body = response.json()
    assert body["triggered"] is True
    recorded = registry.get_run(body["run_key"])
    assert recorded["source"] == "promote"
    assert recorded["status"] == "queued"


def test_promote_background_records_released_features(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    repo = _register_tmp_app(tmp_path)
    environments.save(repo, environments.EnvironmentConfig(prod_branch="production"))
    mem = Memory(repo)
    mem.record_shipped("Ready to ship dashboard", commit_sha="abc1234")
    mem.record_shipped("Already released feature", commit_sha="def5678")
    mem.record_released("Already released feature", release_run_id="prior-run", commit_sha="beef999")

    calls = []
    monkeypatch.setattr(server, "_branch_head_sha", lambda _repo, branch: "feedc0de" if branch == "production" else None)
    monkeypatch.setattr(server.deployment, "persist_audit_trail", lambda _repo, branch: calls.append(branch) or None)

    released = server._record_production_release(repo, "promote-run")

    assert released == ["Ready to ship dashboard"]
    assert calls == ["production"]

    ledger = Memory(repo).released_features()
    assert [item["feature"] for item in ledger] == ["Already released feature", "Ready to ship dashboard"]
    assert ledger[1]["release_run_id"] == "promote-run"
    assert ledger[1]["commit_sha"] == "feedc0de"

    response = TestClient(server.app).get("/apps/myapp")
    assert response.status_code == 200
    body = response.json()
    assert body["released_count"] == 2
    assert body["released"][1]["release_run_id"] == "promote-run"


def test_run_logs_endpoint_streams_existing_lines(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    repo = _register_tmp_app(tmp_path)
    run_key = "runlog1"
    registry.record_run(
        run_key,
        app="myapp",
        source="scheduled",
        status="completed",
        started_at=time.time(),
        objective="Ship useful dashboard improvements.",
        loop_id=registry.loop_id_for("Ship useful dashboard improvements."),
    )
    mem = Memory(repo)
    mem.log(run_key, "implementation agent: starting on dedicated branch 'feature/foo'")
    mem.log(run_key, "testing agent: ok=True turns=3 cost=$0.1234")

    response = TestClient(server.app).get(f"/runs/{run_key}/logs")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "implementation agent: starting" in response.text
    assert "testing agent: ok=True" in response.text
    assert "event: done" in response.text

"""GitHub #113: /healthz is a pure alias of /health."""

from fastapi.testclient import TestClient

from agentra import registry, server
from agentra.connectors import github_fake


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
    github_fake.install(monkeypatch=monkeypatch)


def test_healthz_is_a_pure_alias_of_health(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    client = TestClient(server.app)

    health = client.get("/health")
    healthz = client.get("/healthz")

    assert health.status_code == healthz.status_code == 200
    assert health.headers["content-type"] == healthz.headers["content-type"] == "application/json"
    assert health.json() == healthz.json()
    body = healthz.json()
    assert body["status"] == "ok"
    assert isinstance(body["apps_registered"], int)
    assert set(body) == {"status", "apps_registered"}


def test_health_body_unchanged(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    client = TestClient(server.app)
    assert client.get("/health").json() == {"status": "ok", "apps_registered": 0}

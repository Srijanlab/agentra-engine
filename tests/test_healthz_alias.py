"""GitHub #113: /healthz is a pure alias of /health."""

from fastapi.testclient import TestClient

from agentra import registry, server
from agentra.connectors import github_fake


def _isolate_registry(tmp_path, monkeypatch):
    home = tmp_path / "agentra_home"
    monkeypatch.setattr(registry, "_ddb", None)
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
    assert set(body) == {"status", "apps_registered", "commit", "auth", "verify_token_enabled", "verify_token_status",
                        "internal_token_configured"}


def test_health_body_unchanged(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    monkeypatch.delenv("VERCEL_GIT_COMMIT_SHA", raising=False)
    monkeypatch.delenv("AGENTRA_BUILD_SHA", raising=False)
    client = TestClient(server.app)
    monkeypatch.delenv("FIREBASE_PROJECT_ID", raising=False)
    monkeypatch.delenv("AGENTRA_ALLOWED_EMAILS", raising=False)
    monkeypatch.delenv("AGENTRA_VERIFY_TOKEN", raising=False)
    monkeypatch.delenv("AGENTRA_INTERNAL_TOKEN", raising=False)
    assert client.get("/health").json() == {
        "status": "ok",
        "apps_registered": 0,
        "commit": "",
        "verify_token_enabled": False,
        "verify_token_status": "not_configured",
        "internal_token_configured": False,
        "auth": {
            "mode": "open",
            "cloud_mode": False,
            "firebase_configured": False,
            "allowlist_configured": False,
            "problems": ["FIREBASE_PROJECT_ID is not set"],
        },
    }


def test_health_reports_the_deployed_commit(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    monkeypatch.setenv("VERCEL_GIT_COMMIT_SHA", "abc1234def")
    client = TestClient(server.app)
    assert client.get("/health").json()["commit"] == "abc1234def"

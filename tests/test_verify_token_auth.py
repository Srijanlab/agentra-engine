"""AGENTRA_VERIFY_TOKEN: scoped read-only pre-prod verification path in the auth gate."""

import pytest
from fastapi.testclient import TestClient

from agentra import registry, server

TOKEN = "verify-secret"
HDR = {"X-Agentra-Verify-Token": TOKEN}


@pytest.fixture
def client(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(registry, "_ddb", None)
    monkeypatch.setattr(registry, "AGENTRA_HOME", home)
    monkeypatch.setattr(registry, "APPS_PATH", home / "apps.json")
    monkeypatch.setattr(registry, "PAUSE_PATH", home / "paused.json")
    monkeypatch.setattr(registry, "_RUNS_PATH", home / "runs.json")
    monkeypatch.setattr(registry, "_LOOPS_PATH", home / "loops.json")
    monkeypatch.setattr(registry, "_JOBS_PATH", home / "jobs.json")
    monkeypatch.setenv("FIREBASE_PROJECT_ID", "agentra-test")
    monkeypatch.setenv("AGENTRA_ALLOWED_EMAILS", "a@example.com")
    monkeypatch.setenv("AGENTRA_VERIFY_TOKEN", TOKEN)
    monkeypatch.setenv("AGENTRA_INTERNAL_TOKEN", "internal")
    for var in ("VERCEL_ENV", "AGENTRA_ENVIRONMENT", "AGENTRA_TICK_TOKEN", "CRON_SECRET"):
        monkeypatch.delenv(var, raising=False)
    registry.record_run("rk1", app="demo", status="completed")
    return TestClient(server.app)


def test_verify_token_reads_loop_context(client):
    loop_id = registry.bind_loop("demo", 1, title="t")
    r = client.get(f"/loops/{loop_id}/context", headers=HDR)
    assert r.status_code == 200
    assert set(r.json()) == {"objective", "current_step", "state", "decisions", "findings", "refs", "last_outcome", "updated_at"}
    assert client.get("/loops/does-not-exist/context", headers=HDR).status_code == 404
    assert client.get(f"/loops/{loop_id}/context").status_code == 401
    assert client.get(f"/loops/{loop_id}/context", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 401


def test_correct_token_reads_allowed_routes(client):
    assert client.get("/apps", headers=HDR).status_code == 200
    assert client.get("/runs/rk1", headers=HDR).json()["app"] == "demo"
    missing = client.get("/runs/nope", headers=HDR)
    assert missing.status_code == 404 and missing.json() == {"detail": "run 'nope' not found"}
    assert client.get("/apps/demo/schedule", headers=HDR).status_code != 401


@pytest.mark.parametrize("value", ["wrong", ""])
@pytest.mark.parametrize("path", ["/apps", "/apps/demo/schedule", "/runs/rk1"])
def test_wrong_or_empty_token_is_401(client, path, value):
    r = client.get(path, headers={"X-Agentra-Verify-Token": value})
    assert r.status_code == 401
    body = r.json()
    assert body["error"] == "authentication_required"
    assert "hint" in body
    assert TOKEN not in r.text
    assert "a@example.com" not in r.text


def test_token_unset_fails_closed(client, monkeypatch):
    monkeypatch.delenv("AGENTRA_VERIFY_TOKEN")
    for path in ("/apps", "/apps/demo/schedule", "/runs/rk1"):
        assert client.get(path, headers=HDR).status_code == 401
    monkeypatch.setenv("AGENTRA_VERIFY_TOKEN", "")
    assert client.get("/apps", headers={"X-Agentra-Verify-Token": ""}).status_code == 401


def test_authorization_header_does_not_carry_the_verify_token(client):
    assert client.get("/apps", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 401


@pytest.mark.parametrize("path", [
    "/apps/demo", "/runs", "/runs/rk1/logs", "/runs/rk1/screenshot", "/runs/rk1/trace",
    "/loops", "/loops/demo-1", "/loops/demo-1/pipeline", "/internal/loops/demo-1/context", "/needs-human", "/system/llm-pool",
])
def test_token_is_route_scoped(client, path):
    assert client.get(path, headers=HDR).status_code == 401


@pytest.mark.parametrize("method,path", [
    ("post", "/apps"), ("delete", "/apps/demo"), ("post", "/apps/demo/run"),
    ("post", "/apps/demo/promote"), ("post", "/trigger/scheduled"), ("put", "/apps"),
    ("patch", "/runs/rk1"), ("delete", "/runs/rk1"), ("post", "/apps/demo/schedule"),
])
def test_token_is_read_only(client, method, path):
    assert getattr(client, method)(path, headers=HDR).status_code == 401
    assert registry.get_run("rk1")["status"] == "completed"


def test_token_never_authorizes_cron_or_internal(client, monkeypatch):
    monkeypatch.setenv("AGENTRA_TICK_TOKEN", "tick")
    assert client.get("/trigger/cron", headers={**HDR, "Authorization": f"Bearer {TOKEN}"}).status_code == 401
    assert client.get("/trigger/cron", headers=HDR).status_code == 401
    rpc = client.post(
        "/internal/rpc", json={"target": "registry", "method": "list_apps"},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert rpc.status_code == 401


@pytest.mark.parametrize("env", ["VERCEL_ENV", "AGENTRA_ENVIRONMENT"])
def test_production_rejects_the_header(client, monkeypatch, env):
    monkeypatch.setenv(env, "production")
    assert client.get("/apps", headers=HDR).status_code == 403
    assert client.get("/apps", headers={"X-Agentra-Verify-Token": "anything"}).status_code == 403
    monkeypatch.delenv("AGENTRA_VERIFY_TOKEN")
    assert client.get("/apps", headers=HDR).status_code == 403
    assert client.get("/apps").status_code == 401


SCHEMA_PATHS = ["/openapi.json", "/docs", "/redoc"]


def test_correct_token_reads_openapi_schema(client):
    r = client.get("/openapi.json", headers=HDR)
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body["openapi"], str)
    assert {"/apps", "/health", "/runs/{run_key}"} <= set(body["paths"])


def test_correct_token_reads_docs_page(client):
    assert client.get("/docs").status_code == 401
    r = client.get("/docs", headers=HDR)
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_correct_token_does_not_read_redoc_page(client):
    assert client.get("/redoc").status_code == 401
    r = client.get("/redoc", headers=HDR)
    assert r.status_code == 401
    assert r.json()["error"] == "authentication_required"
    assert "text/html" not in r.headers["content-type"]


@pytest.mark.parametrize("path", SCHEMA_PATHS)
def test_schema_paths_without_token_are_401(client, path):
    r = client.get(path)
    assert r.status_code == 401
    body = r.json()
    assert body["error"] == "authentication_required"
    assert "hint" in body and "paths" not in body


@pytest.mark.parametrize("value", ["wrong", ""])
@pytest.mark.parametrize("path", SCHEMA_PATHS)
def test_schema_paths_wrong_or_empty_token_are_401(client, path, value):
    r = client.get(path, headers={"X-Agentra-Verify-Token": value})
    assert r.status_code == 401
    assert r.json()["error"] == "authentication_required"
    assert TOKEN not in r.text
    assert "a@example.com" not in r.text


@pytest.mark.parametrize("method", ["post", "put", "delete", "patch"])
@pytest.mark.parametrize("path", SCHEMA_PATHS)
def test_schema_paths_reject_non_get_with_token(client, method, path):
    assert getattr(client, method)(path, headers=HDR).status_code == 401


def test_schema_paths_reject_bearer_verify_token(client):
    assert client.get("/openapi.json", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 401


def test_token_does_not_reach_other_doc_routes(client):
    assert client.get("/docs/oauth2-redirect", headers=HDR).status_code == 401


@pytest.mark.parametrize("env", ["VERCEL_ENV", "AGENTRA_ENVIRONMENT"])
@pytest.mark.parametrize("path", SCHEMA_PATHS)
def test_production_rejects_token_on_schema_paths(client, monkeypatch, env, path):
    monkeypatch.setenv(env, "production")
    assert client.get(path, headers=HDR).status_code == 403
    assert client.get(path).status_code == 401


def test_health_stays_public(client):
    assert client.get("/health").status_code == 200
    assert client.get("/healthz").status_code == 200


def test_preview_deployment_allows_the_token(client, monkeypatch):
    monkeypatch.setenv("VERCEL_ENV", "preview")
    assert client.get("/apps", headers=HDR).status_code == 200


ELIGIBLE_PATHS = ["/apps", "/apps/demo/schedule", "/runs/rk1", "/openapi.json", "/docs"]


@pytest.mark.parametrize("path", ELIGIBLE_PATHS)
def test_whitespace_padded_env_token_still_authorizes(client, monkeypatch, path):
    monkeypatch.setenv("AGENTRA_VERIFY_TOKEN", f"  {TOKEN}\n")
    assert client.get(path, headers=HDR).status_code != 401


def test_whitespace_padded_header_value_is_trimmed(client):
    r = client.get("/apps", headers={"X-Agentra-Verify-Token": f" {TOKEN} "})
    assert r.status_code == 200


@pytest.mark.parametrize("blank", ["   ", "\n", " \t "])
def test_whitespace_only_env_token_fails_closed(client, monkeypatch, blank):
    monkeypatch.setenv("AGENTRA_VERIFY_TOKEN", blank)
    assert client.get("/apps", headers=HDR).status_code == 401
    assert client.get("/apps", headers={"X-Agentra-Verify-Token": blank.strip() or " "}).status_code == 401
    assert client.get("/health").json()["verify_token_enabled"] is False


@pytest.mark.parametrize("path", ["/health", "/healthz"])
def test_health_reports_verify_token_enabled(client, monkeypatch, path):
    body = client.get(path).json()
    assert body["verify_token_enabled"] is True
    assert {"status", "apps_registered", "commit", "auth"} <= set(body)
    assert set(body["auth"]) == {"mode", "cloud_mode", "firebase_configured", "allowlist_configured", "problems"}
    assert TOKEN not in client.get(path).text and "a@example.com" not in client.get(path).text
    monkeypatch.delenv("AGENTRA_VERIFY_TOKEN")
    assert client.get(path).json()["verify_token_enabled"] is False
    monkeypatch.setenv("AGENTRA_VERIFY_TOKEN", TOKEN)
    monkeypatch.setenv("VERCEL_ENV", "production")
    assert client.get(path).json()["verify_token_enabled"] is False


def test_health_and_healthz_match(client):
    assert client.get("/health").json() == client.get("/healthz").json()


@pytest.mark.parametrize("path", ["/health", "/healthz"])
def test_health_verify_token_status_values(client, monkeypatch, path):
    body = client.get(path).json()
    assert (body["verify_token_status"], body["verify_token_enabled"]) == ("enabled", True)
    for blank in (None, "", "   "):
        if blank is None:
            monkeypatch.delenv("AGENTRA_VERIFY_TOKEN")
        else:
            monkeypatch.setenv("AGENTRA_VERIFY_TOKEN", blank)
        body = client.get(path).json()
        assert (body["verify_token_status"], body["verify_token_enabled"]) == ("not_configured", False)
    monkeypatch.setenv("AGENTRA_VERIFY_TOKEN", TOKEN)
    for env in ("VERCEL_ENV", "AGENTRA_ENVIRONMENT"):
        monkeypatch.setenv(env, "production")
        body = client.get(path).json()
        assert (body["verify_token_status"], body["verify_token_enabled"]) == ("disabled_in_production", False)
        monkeypatch.delenv(env)


def test_health_verify_token_status_in_degraded_branch(client, monkeypatch):
    def boom():
        raise RuntimeError("down")

    monkeypatch.setattr(registry, "list_apps", boom)
    for path in ("/health", "/healthz"):
        body = client.get(path).json()
        assert body["status"] == "degraded"
        assert body["verify_token_status"] == "enabled" and body["verify_token_enabled"] is True
        assert TOKEN not in str(body)


def test_health_status_leaks_no_token_material(client, monkeypatch):
    text = client.get("/health").text
    assert TOKEN not in text and "a@example.com" not in text
    assert str(len(TOKEN)) not in client.get('/health').json()['verify_token_status']
    monkeypatch.delenv("AGENTRA_VERIFY_TOKEN")
    assert TOKEN not in client.get("/healthz").text


@pytest.mark.parametrize("path", SCHEMA_PATHS)
def test_schema_paths_ignore_query_and_authorization_token(client, path):
    r = client.get(path, params={"verify_token": TOKEN}, headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 401
    assert r.json()["error"] == "authentication_required" and "paths" not in r.text
    assert client.get(f"{path}?verify_token={TOKEN}").status_code == 401

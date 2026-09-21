"""Auth hardening of /trigger/queue, /trigger/cron, /trigger/alarm, admin routes and /debug/*."""

import base64
import json
import logging

import pytest
from fastapi.testclient import TestClient

from agentra import registry, server
from agentra.server import auth
from tests.test_server_triggers import _isolate, _register_tmp_app

SECRET_ENVS = ("AGENTRA_INTERNAL_TOKEN", "CRON_SECRET", "ALARM_WEBHOOK_PASSWORD", "AGENTRA_ALLOWED_EMAILS", "FIREBASE_PROJECT_ID")


@pytest.fixture
def env(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    for var in SECRET_ENVS:
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


@pytest.fixture
def client(env):
    return TestClient(server.app)


def _envelope(app="myapp"):
    data = {"app": app, "type": "feature", "description": "add a thing"}
    return {"message": {"data": base64.b64encode(json.dumps(data).encode()).decode()}}


def _basic(password):
    return {"Authorization": "Basic " + base64.b64encode(f"u:{password}".encode()).decode()}


def _submitted(monkeypatch):
    calls = []
    monkeypatch.setattr(registry, "submit_request", lambda **kw: calls.append(kw) or "req-1")
    return calls


def _sign_in(monkeypatch, email="allowed@example.com"):
    monkeypatch.setenv("FIREBASE_PROJECT_ID", "agentra-prod")
    monkeypatch.setattr(auth, "_verify", lambda tok, proj: {"email": email} if tok == "good" else None)


GOOD = {"Authorization": "Bearer good"}


# /trigger/queue

def test_queue_with_token_rejects_missing_wrong_and_empty_bearer(client, env, tmp_path):
    env.setenv("AGENTRA_INTERNAL_TOKEN", "tok")
    calls = _submitted(env)
    dispatched = []
    env.setattr(registry, "dispatch_once", lambda: dispatched.append(1))
    assert client.post("/trigger/queue", json=_envelope()).status_code == 401
    for header in ("Bearer wrong-token", "Bearer ", "Bearer", "tok"):
        assert client.post("/trigger/queue", json=_envelope(), headers={"Authorization": header}).status_code == 401
    assert calls == [] and dispatched == []


def test_queue_with_correct_token_processes(client, env):
    env.setenv("AGENTRA_INTERNAL_TOKEN", "tok")
    _submitted(env)
    r = client.post("/trigger/queue", json=_envelope(), headers={"Authorization": "Bearer tok"})
    assert r.status_code == 200
    body = r.json()
    assert body["processed"] is True and body["request_id"] == "req-1" and "dispatch" in body
    bad = client.post("/trigger/queue", json={}, headers={"Authorization": "Bearer tok"})
    assert bad.json()["processed"] is False


def test_queue_firebase_without_token_is_503_and_processes_nothing(client, env):
    env.setenv("FIREBASE_PROJECT_ID", "agentra-prod")
    calls = _submitted(env)
    assert client.post("/trigger/queue", json=_envelope()).status_code == 503
    assert calls == []


def test_queue_firebase_with_token_still_401_unauthenticated(client, env):
    env.setenv("FIREBASE_PROJECT_ID", "agentra-prod")
    env.setenv("AGENTRA_INTERNAL_TOKEN", "tok")
    calls = _submitted(env)
    assert client.post("/trigger/queue", json=_envelope()).status_code == 401
    assert calls == []
    assert client.post("/trigger/queue", json=_envelope(), headers={"Authorization": "Bearer tok"}).status_code == 200


def test_queue_local_dev_stays_open(client, env):
    _submitted(env)
    r = client.post("/trigger/queue", json=_envelope())
    assert r.status_code == 200 and r.json()["processed"] is True


# /trigger/cron

def test_cron_firebase_without_any_secret_is_503_and_no_tick(client, env):
    env.setenv("FIREBASE_PROJECT_ID", "agentra-prod")
    ticks = []
    env.setattr(registry, "reconcile_stale_runs", lambda: ticks.append(1))
    assert client.get("/trigger/cron").status_code == 503
    assert ticks == []


def test_cron_with_secrets_accepts_either_token_only(client, env):
    env.setenv("FIREBASE_PROJECT_ID", "agentra-prod")
    env.setenv("AGENTRA_INTERNAL_TOKEN", "internal")
    env.setenv("CRON_SECRET", "cron")
    assert client.get("/trigger/cron").status_code == 401
    assert client.get("/trigger/cron", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get("/trigger/cron", headers={"Authorization": "Bearer "}).status_code == 401
    for tok in ("internal", "cron"):
        r = client.get("/trigger/cron", headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 200 and "apps" in r.json()


def test_cron_cron_secret_alone_is_enough_under_firebase(client, env):
    env.setenv("FIREBASE_PROJECT_ID", "agentra-prod")
    env.setenv("CRON_SECRET", "cron")
    assert client.get("/trigger/cron", headers={"Authorization": "Bearer cron"}).status_code == 200


def test_cron_local_dev_stays_open(client):
    r = client.get("/trigger/cron")
    assert r.status_code == 200 and "apps" in r.json()


# /trigger/alarm

def test_alarm_firebase_without_password_is_503_and_enqueues_nothing(client, env, tmp_path):
    _register_tmp_app(tmp_path)
    env.setenv("FIREBASE_PROJECT_ID", "agentra-prod")
    assert client.post("/trigger/alarm", json={"app": "myapp"}).status_code == 503
    assert registry.list_jobs() == []


def test_alarm_with_password_enforces_basic_auth(client, env, tmp_path):
    _register_tmp_app(tmp_path)
    env.setenv("FIREBASE_PROJECT_ID", "agentra-prod")
    env.setenv("ALARM_WEBHOOK_PASSWORD", "pw")
    assert client.post("/trigger/alarm", json={"app": "myapp"}).status_code == 401
    assert client.post("/trigger/alarm", json={"app": "myapp"}, headers=_basic("bad")).status_code == 401
    assert registry.list_jobs() == []
    r = client.post("/trigger/alarm", json={"app": "myapp"}, headers=_basic("pw"))
    assert r.status_code == 200 and r.json()["triggered"] is True


def test_alarm_local_dev_stays_open(client, env, tmp_path):
    _register_tmp_app(tmp_path)
    assert client.post("/trigger/alarm", json={"app": "myapp"}).json()["triggered"] is True


def test_alarm_password_enforced_without_firebase(client, env, tmp_path):
    _register_tmp_app(tmp_path)
    env.setenv("ALARM_WEBHOOK_PASSWORD", "pw")
    assert client.post("/trigger/alarm", json={"app": "myapp"}).status_code == 401


# admin routes / allowlist

@pytest.mark.parametrize("method,path", [
    ("post", "/system/pause"), ("post", "/system/resume"), ("get", "/system/llm-pool"),
    ("put", "/system/llm-pool"), ("post", "/system/llm-backend"),
])
@pytest.mark.parametrize("allowlist", [None, ""])
def test_empty_allowlist_denies_every_admin_route(client, env, caplog, method, path, allowlist):
    _sign_in(env)
    if allowlist is not None:
        env.setenv("AGENTRA_ALLOWED_EMAILS", allowlist)
    with caplog.at_level(logging.WARNING, logger="agentra.server.auth"):
        assert getattr(client, method)(path, headers=GOOD, json={}).status_code == 403
    assert any(r.levelno == logging.WARNING and "AGENTRA_ALLOWED_EMAILS" in r.getMessage() for r in caplog.records)
    assert registry.is_paused() is False


def test_pause_with_allowlist(client, env):
    _sign_in(env, "Allowed@Example.com")
    env.setenv("AGENTRA_ALLOWED_EMAILS", "allowed@example.com")
    assert client.post("/system/pause").status_code == 401
    assert client.post("/system/pause", headers={"Authorization": "Bearer bad"}).status_code == 401
    assert client.post("/system/pause", headers=GOOD).json() == {"paused": True}
    _sign_in(env, "other@example.com")
    assert client.post("/system/resume", headers=GOOD).status_code == 403
    assert registry.is_paused() is True


def test_local_dev_admin_routes_open(client):
    assert client.post("/system/pause").status_code == 200


# /debug/*

@pytest.mark.parametrize("path,key", [("/debug/llm-rotation", "backends"), ("/debug/dynamodb", "db_connected")])
def test_debug_routes_are_gated(client, env, path, key):
    assert key in client.get(path).json()
    _sign_in(env)
    env.setenv("AGENTRA_ALLOWED_EMAILS", "allowed@example.com")
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"Authorization": "Bearer bad"}).status_code == 401
    r = client.get(path, headers=GOOD)
    assert r.status_code == 200 and key in r.json()
    _sign_in(env, "other@example.com")
    assert client.get(path, headers=GOOD).status_code == 403


def test_debug_rotation_stays_read_only_behind_auth(client, env):
    _sign_in(env)
    env.setenv("AGENTRA_ALLOWED_EMAILS", "allowed@example.com")
    for method in (client.post, client.put, client.delete):
        assert method("/debug/llm-rotation").status_code == 401
        assert method("/debug/llm-rotation", headers=GOOD).status_code == 405


def test_public_paths_unaffected(client, env):
    env.setenv("FIREBASE_PROJECT_ID", "agentra-prod")
    assert client.get("/health").status_code == 200
    assert client.get("/healthz").status_code == 200
    assert client.post("/internal/rpc", json={}).status_code in (401, 503)

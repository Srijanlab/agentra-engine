"""Every mutating operator route tags its signal event with the caller's email."""

import pytest
from fastapi import Request

from agentra import registry
from agentra.server import audit, auth
from test_apps_multi_repo import _init_origin, _isolate_registry
from test_human_input_route import _escalate
from test_server_triggers import _client, _isolate, _register_tmp_app

EMAIL = "op@example.com"
HEADERS = {"Authorization": "Bearer t"}


@pytest.fixture
def authed(monkeypatch):
    monkeypatch.setenv("FIREBASE_PROJECT_ID", "proj")
    monkeypatch.setenv("AGENTRA_ALLOWED_EMAILS", "")
    monkeypatch.setattr(auth, "_verify", lambda token, project: {"email": "Op@Example.com", "email_verified": True})


def _latest(client, source):
    signals = client.get("/signals", headers=HEADERS).json()["signals"]
    return next(s for s in signals if s["source"] == source)


def _scope(path):
    return Request({"type": "http", "path": path, "headers": [], "method": "POST"})


def test_actor_for_variants():
    req = _scope("/system/pause")
    assert audit.actor_for(req) == "anonymous"
    assert audit.actor_for(_scope("/internal/rpc")) == "loop"
    req.state.actor = "verify-token"
    assert audit.actor_for(req) == "verify-token"
    req.state.user_email = EMAIL
    assert audit.actor_for(req) == EMAIL


def test_auth_gate_open_records_anonymous(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    client = _client()
    assert client.post("/system/pause", json={"reason": "x"}).status_code == 200
    assert _latest(client, "pause")["actor"] == "anonymous"


def test_system_routes_carry_actor(tmp_path, monkeypatch, authed):
    _isolate(tmp_path, monkeypatch)
    client = _client()

    assert client.post("/system/pause", json={"reason": "audit-test"}, headers=HEADERS).status_code == 200
    ev = _latest(client, "pause")
    assert ev["actor"] == EMAIL and "audit-test" in ev["message"]

    assert client.post("/system/resume", headers=HEADERS).status_code == 200
    assert _latest(client, "resume")["actor"] == EMAIL

    assert client.post("/system/llm-backend", json={"backend": "bogus"}, headers=HEADERS).status_code == 400
    assert not any(s["source"] == "llm-backend" for s in client.get("/signals", headers=HEADERS).json()["signals"])
    assert client.post("/system/llm-backend", json={"backend": "claude"}, headers=HEADERS).status_code == 200
    assert _latest(client, "llm-backend")["actor"] == EMAIL

    assert client.put("/system/llm-pool", json={"backends": ["claude"]}, headers=HEADERS).status_code == 200
    assert _latest(client, "llm-pool")["actor"] == EMAIL


def test_run_and_promote_carry_actor_including_paused(tmp_path, monkeypatch, authed):
    _isolate(tmp_path, monkeypatch)
    _register_tmp_app(tmp_path)
    client = _client()

    assert client.post("/apps/myapp/run", headers=HEADERS).json()["queued"] is True
    assert _latest(client, "on-demand")["actor"] == EMAIL
    assert client.post("/apps/myapp/promote", headers=HEADERS).status_code == 200
    assert _latest(client, "promote")["actor"] == EMAIL

    client.post("/system/pause", json={}, headers=HEADERS)
    assert client.post("/apps/myapp/run", headers=HEADERS).json()["triggered"] is False
    assert _latest(client, "on-demand")["message"].startswith("system is paused")
    assert _latest(client, "on-demand")["actor"] == EMAIL
    assert client.post("/apps/myapp/promote", headers=HEADERS).json()["triggered"] is False
    assert _latest(client, "promote")["message"].startswith("system is paused")
    assert _latest(client, "promote")["actor"] == EMAIL


def test_human_input_carries_actor_including_paused(tmp_path, monkeypatch, authed):
    _isolate(tmp_path, monkeypatch)
    repo = _register_tmp_app(tmp_path)
    issue = _escalate(repo)
    client = _client()

    body = {"issue_number": issue, "answer": "Use OAuth."}
    assert client.post("/apps/myapp/human-input", json=body, headers=HEADERS).status_code == 200
    assert _latest(client, "human-input")["actor"] == EMAIL

    client.post("/system/pause", json={}, headers=HEADERS)
    assert client.post("/apps/myapp/human-input", json=body, headers=HEADERS).status_code == 409
    ev = _latest(client, "human-input")
    assert "paused" in ev["message"] and ev["actor"] == EMAIL


def test_app_register_update_delete_carry_actor(tmp_path, monkeypatch, authed):
    _isolate_registry(tmp_path, monkeypatch)
    origin = _init_origin(tmp_path / "origin")
    client = _client()
    secret = "top secret objective text"

    resp = client.post(
        "/apps", json={"name": "shiny", "repo_url": str(origin), "branch": "main", "objective": secret}, headers=HEADERS
    )
    assert resp.status_code == 200, resp.text
    ev = _latest(client, "register")
    assert ev["actor"] == EMAIL and "shiny" in ev["message"]
    assert "objective set" in ev["message"] and secret not in ev["message"]

    assert client.patch("/apps/shiny", json={"schedule_hours": 12}, headers=HEADERS).status_code == 200
    ev = _latest(client, "update")
    assert ev["actor"] == EMAIL and "objective updated" not in ev["message"]

    assert client.patch("/apps/shiny", json={"objective": secret}, headers=HEADERS).status_code == 200
    ev = _latest(client, "update")
    assert ev["actor"] == EMAIL and "objective updated" in ev["message"] and secret not in ev["message"]

    before = len(client.get("/signals", headers=HEADERS).json()["signals"])
    assert client.delete("/apps/ghost", headers=HEADERS).status_code == 404
    assert len(client.get("/signals", headers=HEADERS).json()["signals"]) == before

    assert client.delete("/apps/shiny", headers=HEADERS).status_code == 200
    ev = _latest(client, "register")
    assert ev["actor"] == EMAIL and "removed" in ev["message"] and "shiny" in ev["message"]


def test_multi_repo_register_carries_actor(tmp_path, monkeypatch, authed):
    _isolate_registry(tmp_path, monkeypatch)
    client = _client()
    resp = client.post("/apps", json={
        "name": "multi",
        "objective": "obj",
        "repos": [
            {"name": "backlog", "repo_url": str(_init_origin(tmp_path / "c")), "branch": "main", "role": "coordination"},
            {"name": "engine", "repo_url": str(_init_origin(tmp_path / "e")), "branch": "main", "role": "code"},
        ],
    }, headers=HEADERS)
    assert resp.status_code == 200, resp.text
    ev = _latest(client, "register")
    assert ev["actor"] == EMAIL and "objective set" in ev["message"]


def test_non_request_events_have_null_actor(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    registry.record_signal("scheduled", "tick")
    signals = _client().get("/signals").json()["signals"]
    assert signals[0]["actor"] is None
    assert all({"ts", "source", "message", "actor"} <= s.keys() for s in signals)

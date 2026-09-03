"""server/routes/internal.py — the token-gated RPC the loop uses."""

import pytest
from fastapi.testclient import TestClient

from agentra import registry, server
from agentra.registry import core
from agentra.server.routes import internal

TOKEN = "test-internal-token"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTRA_INTERNAL_TOKEN", TOKEN)
    monkeypatch.delenv("FIREBASE_PROJECT_ID", raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(registry, "_db", None)
    monkeypatch.setattr(registry, "AGENTRA_HOME", home)
    monkeypatch.setattr(registry, "APPS_PATH", home / "apps.json")
    monkeypatch.setattr(core, "_llm_backend_cache", None)
    monkeypatch.setattr(registry, "_LLM_BACKEND_PATH", home / "llm_backend.json")
    return TestClient(server.app)


def _rpc(client, body, token=TOKEN):
    return client.post("/internal/rpc", json=body, headers={"Authorization": f"Bearer {token}"})


def test_missing_token_401(client):
    assert client.post("/internal/rpc", json={"target": "registry", "method": "list_apps"}).status_code == 401


def test_wrong_token_401(client):
    assert _rpc(client, {"target": "registry", "method": "list_apps"}, token="nope").status_code == 401


def test_ip_allowlist_blocks_other_addresses(client, monkeypatch):
    monkeypatch.setenv("AGENTRA_INTERNAL_ALLOWED_IPS", "203.0.113.7, 203.0.113.8")
    r = client.post("/internal/rpc", json={"target": "registry", "method": "list_apps"},
                    headers={"Authorization": f"Bearer {TOKEN}", "X-Real-IP": "198.51.100.1"})
    assert r.status_code == 403


def test_ip_allowlist_ignores_spoofable_x_forwarded_for(client, monkeypatch):
    monkeypatch.setenv("AGENTRA_INTERNAL_ALLOWED_IPS", "203.0.113.7")
    r = client.post("/internal/rpc", json={"target": "registry", "method": "list_apps"},
                    headers={"Authorization": f"Bearer {TOKEN}",
                             "X-Forwarded-For": "203.0.113.7", "X-Real-IP": "198.51.100.1"})
    assert r.status_code == 403


def test_ip_allowlist_permits_a_listed_address(client, monkeypatch):
    monkeypatch.setenv("AGENTRA_INTERNAL_ALLOWED_IPS", "203.0.113.7")
    r = client.post("/internal/rpc", json={"target": "registry", "method": "list_apps"},
                    headers={"Authorization": f"Bearer {TOKEN}", "X-Vercel-Forwarded-For": "203.0.113.7"})
    assert r.status_code == 200


def test_registry_roundtrip(client):
    assert _rpc(client, {"target": "registry", "method": "list_apps"}).json() == {"result": {}}
    r = _rpc(client, {"target": "registry", "method": "register_app",
                      "args": ["demo", "/tmp/demo"], "kwargs": {"repo_url": "https://github.com/x/demo"}})
    assert r.status_code == 200
    assert "demo" in _rpc(client, {"target": "registry", "method": "list_apps"}).json()["result"]


def test_unexposed_method_403(client):
    assert _rpc(client, {"target": "registry", "method": "firestore_client"}).status_code == 403
    assert _rpc(client, {"target": "memory", "method": "write", "repo_url": "https://github.com/x/y"}).status_code == 403


def test_memory_requires_repo_url(client):
    assert _rpc(client, {"target": "memory", "method": "known_bugs"}).status_code == 400


def test_llm_backend_via_rpc(client):
    assert _rpc(client, {"target": "registry", "method": "get_llm_backend"}).json()["result"] == "claude"
    _rpc(client, {"target": "registry", "method": "set_llm_backend", "args": ["nim"]})
    assert _rpc(client, {"target": "registry", "method": "get_llm_backend"}).json()["result"] == "nim"


def test_git_token_requires_token(client):
    assert client.post("/internal/git-token", json={"repo_url": "https://github.com/x/y"}).status_code == 401


def test_git_token_calls_github_app(client, monkeypatch):
    monkeypatch.setattr(internal.github_app, "get_installation_token", lambda url: f"tok-for-{url}")
    r = client.post("/internal/git-token", json={"repo_url": "https://github.com/x/y"},
                    headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.json() == {"token": "tok-for-https://github.com/x/y"}


def test_run_log_writes_to_firestore(client, monkeypatch):
    written = {}

    class _Doc:
        def set(self, data):
            written.update(data)

    class _Col:
        def document(self, key):
            written["key"] = key
            return _Doc()

    class _DB:
        def collection(self, name):
            written["collection"] = name
            return _Col()

    monkeypatch.setattr(internal.registry, "firestore_client", lambda: _DB())
    r = client.post("/internal/runs/run-9/log", json={"lines": ["a", "b"]},
                    headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.json() == {"ok": True, "lines": 2}
    assert written["collection"] == "run_logs" and written["key"] == "run-9"
    assert written["lines"] == ["a", "b"]

"""server/routes/systems.py: the dashboard's Model Backend toggle (Claude vs NIM)."""

from fastapi.testclient import TestClient

from agentra import registry, server
from agentra.registry import core


def _isolate_registry(tmp_path, monkeypatch):
    home = tmp_path / "agentra_home"
    home.mkdir()
    monkeypatch.setattr(registry, "_db", None)
    monkeypatch.setattr(registry, "AGENTRA_HOME", home)
    monkeypatch.setattr(registry, "_LLM_BACKEND_PATH", home / "llm_backend.json")
    monkeypatch.setattr(core, "_llm_backend_cache", None)


def test_get_defaults_to_claude(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    client = TestClient(server.app)
    assert client.get("/system/llm-backend").json() == {"backend": "claude"}


def test_post_then_get_round_trips(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    client = TestClient(server.app)
    assert client.post("/system/llm-backend", json={"backend": "nim"}).status_code == 200
    assert client.get("/system/llm-backend").json() == {"backend": "nim"}


def test_post_rejects_unknown_backend(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    client = TestClient(server.app)
    resp = client.post("/system/llm-backend", json={"backend": "gpt5"})
    assert resp.status_code == 400

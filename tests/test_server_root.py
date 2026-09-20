"""Tests for the GET / route: API-only health payload versus the built dashboard."""

import pytest
from fastapi.testclient import TestClient

from agentra import server


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("FIREBASE_PROJECT_ID", raising=False)
    monkeypatch.delenv("VERCEL_GIT_COMMIT_SHA", raising=False)
    monkeypatch.delenv("AGENTRA_BUILD_SHA", raising=False)
    return TestClient(server.app)


def test_root_without_dist_is_clean_health_payload(client, tmp_path, monkeypatch):
    monkeypatch.setattr(server, "WEB_DIST", tmp_path / "empty")
    monkeypatch.setenv("AGENTRA_BUILD_SHA", "abc123")
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.json() == {"status": "ok", "service": "agentra-engine", "commit": "abc123"}
    assert "error" not in resp.json() and "hint" not in resp.json()
    assert "dashboard not built" not in resp.text


def test_root_commit_prefers_vercel_sha(client, tmp_path, monkeypatch):
    monkeypatch.setattr(server, "WEB_DIST", tmp_path / "empty")
    monkeypatch.setenv("AGENTRA_BUILD_SHA", "build")
    monkeypatch.setenv("VERCEL_GIT_COMMIT_SHA", "vercel")
    assert client.get("/").json()["commit"] == "vercel"


def test_root_commit_falls_back_to_empty(client, tmp_path, monkeypatch):
    monkeypatch.setattr(server, "WEB_DIST", tmp_path / "empty")
    assert client.get("/").json()["commit"] == ""


def test_root_serves_index_html_when_dist_exists(client, tmp_path, monkeypatch):
    (tmp_path / "index.html").write_text("<html>dash</html>")
    monkeypatch.setattr(server, "WEB_DIST", tmp_path)
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert resp.text == "<html>dash</html>"


def test_web_dist_env_dir_is_served(tmp_path, monkeypatch):
    (tmp_path / "index.html").write_text("<html>env</html>")
    monkeypatch.setenv("AGENTRA_WEB_DIST", str(tmp_path))
    import importlib

    import agentra.server as mod

    try:
        reloaded = importlib.reload(mod)
        resp = TestClient(reloaded.app).get("/")
        assert resp.text == "<html>env</html>"
    finally:
        monkeypatch.delenv("AGENTRA_WEB_DIST")
        importlib.reload(mod)


def test_root_public_but_other_routes_gated_with_firebase(client, tmp_path, monkeypatch):
    monkeypatch.setattr(server, "WEB_DIST", tmp_path / "empty")
    monkeypatch.setenv("FIREBASE_PROJECT_ID", "agentra-prod")
    assert client.get("/").status_code == 200
    assert client.get("/apps").status_code == 401

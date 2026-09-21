"""Tests for the GET / route: API-service descriptor or dashboard redirect."""

import pytest
from fastapi.testclient import TestClient

from agentra import server


@pytest.fixture
def client(monkeypatch):
    for var in ("FIREBASE_PROJECT_ID", "VERCEL_GIT_COMMIT_SHA", "AGENTRA_BUILD_SHA", "AGENTRA_DASHBOARD_URL"):
        monkeypatch.delenv(var, raising=False)
    return TestClient(server.app, follow_redirects=False)


def test_root_is_api_service_payload(client, monkeypatch):
    monkeypatch.setenv("AGENTRA_BUILD_SHA", "abc123")
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.json() == {"service": "agentra-engine", "status": "ok", "commit": "abc123", "health": "/health"}
    assert "error" not in resp.json() and "hint" not in resp.json()
    assert "dashboard not built" not in resp.text


def test_root_commit_prefers_vercel_sha(client, monkeypatch):
    monkeypatch.setenv("AGENTRA_BUILD_SHA", "build")
    monkeypatch.setenv("VERCEL_GIT_COMMIT_SHA", "vercel")
    assert client.get("/").json()["commit"] == "vercel"
    assert client.get("/health").json()["commit"] == "vercel"


def test_root_commit_falls_back_to_empty(client):
    assert client.get("/").json()["commit"] == ""


def test_root_redirects_to_configured_dashboard(client, monkeypatch):
    monkeypatch.setenv("AGENTRA_DASHBOARD_URL", "https://dash.example.com/app")
    resp = client.get("/")
    assert resp.status_code == 307
    assert resp.headers["location"] == "https://dash.example.com/app"


@pytest.mark.parametrize("value", ["", "   ", "javascript:alert(1)", "dash.example.com", "ftp://x.example.com"])
def test_root_ignores_invalid_dashboard_url(client, monkeypatch, value):
    monkeypatch.setenv("AGENTRA_DASHBOARD_URL", value)
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.json()["service"] == "agentra-engine"


def test_root_public_but_other_routes_gated_with_firebase(client, monkeypatch):
    monkeypatch.setenv("FIREBASE_PROJECT_ID", "agentra-prod")
    assert client.get("/").status_code == 200
    assert client.get("/apps").status_code == 401


def test_assets_path_is_404(client):
    assert client.get("/assets/anything").status_code == 404

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


_COMMIT_PATHS = ("/", "/health", "/healthz")


def _commits(client):
    return [client.get(p).json()["commit"] for p in _COMMIT_PATHS]


@pytest.mark.parametrize(
    ("vercel", "build", "expected"),
    [("v1", None, "v1"), (None, "b1", "b1"), ("v1", "b1", "v1"), (None, None, ""), ("  v1\n", None, "v1")],
)
def test_all_endpoints_report_same_commit(client, monkeypatch, vercel, build, expected):
    if vercel is not None:
        monkeypatch.setenv("VERCEL_GIT_COMMIT_SHA", vercel)
    if build is not None:
        monkeypatch.setenv("AGENTRA_BUILD_SHA", build)
    assert _commits(client) == [expected] * 3


def test_degraded_health_reports_same_commit_as_root(client, monkeypatch):
    monkeypatch.setenv("VERCEL_GIT_COMMIT_SHA", "deadbeef")

    def boom():
        raise RuntimeError("backend down")

    monkeypatch.setattr(server.registry, "list_apps", boom)
    body = client.get("/health").json()
    assert body["status"] == "degraded"
    assert body["commit"] == client.get("/").json()["commit"] == "deadbeef"


def test_commit_is_stable_after_env_changes_until_cache_reset(client, monkeypatch):
    from agentra.server.build_info import reset_build_commit_cache

    monkeypatch.setenv("VERCEL_GIT_COMMIT_SHA", "first")
    assert _commits(client) == ["first"] * 3
    monkeypatch.setenv("VERCEL_GIT_COMMIT_SHA", "second")
    monkeypatch.setenv("AGENTRA_BUILD_SHA", "other")
    assert _commits(client) == ["first"] * 3
    reset_build_commit_cache()
    assert _commits(client) == ["second"] * 3


def test_repeated_calls_return_same_commit(client, monkeypatch):
    monkeypatch.setenv("AGENTRA_BUILD_SHA", "abc123")
    seen = set()
    for _ in range(10):
        seen.update(_commits(client))
    assert seen == {"abc123"}

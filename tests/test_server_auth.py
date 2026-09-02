"""server/auth.py — the Firebase sign-in gate on the dashboard API."""

from fastapi.testclient import TestClient

from agentra import server
from agentra.server import auth


def test_open_when_firebase_project_unset(monkeypatch):
    monkeypatch.delenv("FIREBASE_PROJECT_ID", raising=False)
    client = TestClient(server.app)
    assert client.get("/health").status_code == 200
    assert client.get("/agents/metadata").status_code == 200


def test_public_paths_bypass_gate(monkeypatch):
    monkeypatch.setenv("FIREBASE_PROJECT_ID", "agentra-prod")
    client = TestClient(server.app)
    assert client.get("/health").status_code == 200
    assert client.get("/healthz").status_code == 200


def test_protected_path_401_without_token(monkeypatch):
    monkeypatch.setenv("FIREBASE_PROJECT_ID", "agentra-prod")
    client = TestClient(server.app)
    assert client.get("/agents/metadata").status_code == 401


def test_valid_token_wrong_email_403(monkeypatch):
    monkeypatch.setenv("FIREBASE_PROJECT_ID", "agentra-prod")
    monkeypatch.setenv("AGENTRA_ALLOWED_EMAILS", "allowed@example.com")
    monkeypatch.setattr(auth, "_verify", lambda tok, proj: {"email": "intruder@evil.com"})
    client = TestClient(server.app)
    r = client.get("/agents/metadata", headers={"Authorization": "Bearer x"})
    assert r.status_code == 403


def test_valid_token_allowed_email_passes(monkeypatch):
    monkeypatch.setenv("FIREBASE_PROJECT_ID", "agentra-prod")
    monkeypatch.setenv("AGENTRA_ALLOWED_EMAILS", "allowed@example.com")
    monkeypatch.setattr(auth, "_verify", lambda tok, proj: {"email": "Allowed@example.com"})
    client = TestClient(server.app)
    assert client.get("/agents/metadata", headers={"Authorization": "Bearer x"}).status_code == 200


def test_token_via_query_param_for_eventsource(monkeypatch):
    monkeypatch.setenv("FIREBASE_PROJECT_ID", "agentra-prod")
    monkeypatch.setenv("AGENTRA_ALLOWED_EMAILS", "allowed@example.com")
    monkeypatch.setattr(auth, "_verify", lambda tok, proj: {"email": "allowed@example.com"} if tok == "good" else None)
    client = TestClient(server.app)
    assert client.get("/agents/metadata?access_token=good").status_code == 200
    assert client.get("/agents/metadata?access_token=bad").status_code == 401

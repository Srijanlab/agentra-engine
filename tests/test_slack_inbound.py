"""GitHub issue #68: a human's reply in the Slack HUMAN_INPUT_REQUIRED thread
resumes the blocked run, same as a dashboard answer or a GitHub-issue comment."""

import hashlib
import hmac
import json
import time

from fastapi.testclient import TestClient

from agentra import registry, server
from agentra.connectors import slack
from agentra.server.routes import slack as slack_route

_SECRET = "test-signing-secret"


def _signed(body: dict) -> tuple[str, dict]:
    raw = json.dumps(body)
    ts = str(int(time.time()))
    sig = "v0=" + hmac.new(_SECRET.encode(), f"v0:{ts}:{raw}".encode(), hashlib.sha256).hexdigest()
    return raw, {"X-Slack-Request-Timestamp": ts, "X-Slack-Signature": sig}


def test_verify_signature_roundtrip(monkeypatch):
    monkeypatch.setenv("SLACK_SIGNING_SECRET", _SECRET)
    raw, headers = _signed({"hello": "world"})
    assert slack.verify_signature(headers["X-Slack-Request-Timestamp"], raw.encode(), headers["X-Slack-Signature"])
    assert not slack.verify_signature(headers["X-Slack-Request-Timestamp"], raw.encode(), "v0=deadbeef")
    assert not slack.verify_signature("1", raw.encode(), headers["X-Slack-Signature"])  # stale timestamp


def test_bad_signature_is_rejected(monkeypatch):
    monkeypatch.setenv("SLACK_SIGNING_SECRET", _SECRET)
    resp = TestClient(server.app).post("/slack/events", content=b"{}", headers={"X-Slack-Signature": "v0=x", "X-Slack-Request-Timestamp": str(int(time.time()))})
    assert resp.status_code == 403


def test_url_verification_challenge(monkeypatch):
    monkeypatch.setenv("SLACK_SIGNING_SECRET", _SECRET)
    raw, headers = _signed({"type": "url_verification", "challenge": "abc123"})
    resp = TestClient(server.app).post("/slack/events", content=raw, headers=headers)
    assert resp.status_code == 200 and resp.json() == {"challenge": "abc123"}


def test_thread_reply_dispatches_a_human_answer(monkeypatch):
    monkeypatch.setenv("SLACK_SIGNING_SECRET", _SECRET)
    monkeypatch.setattr(registry, "resolve_slack_thread", lambda ts: {"app": "myapp", "issue_number": 7} if ts == "1700000000.1" else None)
    monkeypatch.setattr(slack_route.registry, "get_app_repo", lambda name: "/tmp/myapp")
    calls = []
    monkeypatch.setattr(slack_route, "dispatch_human_answer", lambda *a, **k: calls.append((a, k)))

    raw, headers = _signed({
        "type": "event_callback",
        "event": {"type": "message", "thread_ts": "1700000000.1", "text": "  use option B  ", "user": "U1"},
    })
    resp = TestClient(server.app).post("/slack/events", content=raw, headers=headers)

    assert resp.status_code == 200
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[0] == "myapp" and args[2] == 7 and args[3] == "use option B"
    assert kwargs["source"] == "slack"


def test_bot_message_and_unknown_thread_are_ignored(monkeypatch):
    monkeypatch.setenv("SLACK_SIGNING_SECRET", _SECRET)
    monkeypatch.setattr(registry, "resolve_slack_thread", lambda ts: None)
    calls = []
    monkeypatch.setattr(slack_route, "dispatch_human_answer", lambda *a, **k: calls.append(a))

    for event in (
        {"type": "message", "thread_ts": "x", "text": "hi", "bot_id": "B1"},           # from a bot
        {"type": "message", "thread_ts": "unknown", "text": "hi", "user": "U1"},        # unmapped thread
        {"type": "message", "text": "not threaded", "user": "U1"},                      # top-level message
    ):
        raw, headers = _signed({"type": "event_callback", "event": event})
        resp = TestClient(server.app).post("/slack/events", content=raw, headers=headers)
        assert resp.status_code == 200
    assert calls == []


def test_record_and_resolve_slack_thread_roundtrip(tmp_path, monkeypatch):
    home = tmp_path / "agentra_home"
    home.mkdir()
    monkeypatch.setattr(registry, "_db", None)
    monkeypatch.setattr(registry, "AGENTRA_HOME", home)
    monkeypatch.setattr(registry, "_SLACK_THREADS_PATH", home / "slack_threads.json")

    registry.record_slack_thread("111.222", app="myapp", issue_number=42)
    assert registry.resolve_slack_thread("111.222") == {"app": "myapp", "issue_number": 42}
    assert registry.resolve_slack_thread("999.999") is None

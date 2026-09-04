"""SLACK_ALLOWED_USERS sender allowlist for the inbound Slack endpoint (issue #132)."""

import asyncio
import hashlib
import hmac
import json
import time

from fastapi.testclient import TestClient

from agentra import registry, server
from agentra.agents import slack_assistant
from agentra.agents.base import AgentResult
from agentra.connectors import slack
from agentra.connectors import slack_allowlist
from agentra.server.routes import slack as slack_route

_SECRET = "test-signing-secret"


def _signed(body: dict) -> tuple[str, dict]:
    raw = json.dumps(body)
    ts = str(int(time.time()))
    sig = "v0=" + hmac.new(_SECRET.encode(), f"v0:{ts}:{raw}".encode(), hashlib.sha256).hexdigest()
    return raw, {"X-Slack-Request-Timestamp": ts, "X-Slack-Signature": sig}


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("SLACK_SIGNING_SECRET", _SECRET)
    monkeypatch.setattr(registry, "_db", None)
    monkeypatch.setattr(registry, "AGENTRA_HOME", tmp_path / "home")
    monkeypatch.setattr(slack_assistant, "_agentra_repo", lambda: tmp_path)
    slack_route._seen_event_ids.clear()
    slack_route._warned_no_allowlist = False


def _capture_tasks(monkeypatch) -> list:
    tasks: list = []
    monkeypatch.setattr(slack_route.asyncio, "create_task", lambda coro: tasks.append(coro) or coro)
    return tasks


def _capture_posts(monkeypatch) -> list:
    posted: list = []
    monkeypatch.setattr(slack, "_post_message", lambda text, channel=None, thread_ts=None: posted.append((text, channel, thread_ts)))
    return posted


def _dm_event(user: str | None = "U1") -> dict:
    event = {"type": "message", "channel_type": "im", "text": "status?", "channel": "D1", "ts": "1.1"}
    if user is not None:
        event["user"] = user
    return {"type": "event_callback", "event_id": f"Ev-{time.time()}", "event": event}


def test_helper_parsing(monkeypatch):
    monkeypatch.setenv("SLACK_ALLOWED_USERS", " U0ABC123 , ,U0DEF456 ")
    assert slack_allowlist.allowlist_configured()
    assert slack_allowlist.is_allowed("U0ABC123")
    assert slack_allowlist.is_allowed("U0DEF456")
    assert not slack_allowlist.is_allowed("u0abc123")
    assert not slack_allowlist.is_allowed(None)
    monkeypatch.setenv("SLACK_ALLOWED_USERS", "   ")
    assert not slack_allowlist.allowlist_configured()


def test_listed_user_dispatches(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setenv("SLACK_ALLOWED_USERS", "U1,U2")
    _capture_posts(monkeypatch)
    tasks = _capture_tasks(monkeypatch)

    raw, headers = _signed(_dm_event("U1"))
    resp = TestClient(server.app).post("/slack/events", content=raw, headers=headers)
    assert resp.status_code == 200 and resp.json() == {"ok": True}
    assert len(tasks) == 1
    for coro in tasks:
        coro.close()


def test_unlisted_user_denied(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setenv("SLACK_ALLOWED_USERS", "U1,U2")
    posted = _capture_posts(monkeypatch)
    tasks = _capture_tasks(monkeypatch)

    raw, headers = _signed(_dm_event("U99"))
    resp = TestClient(server.app).post("/slack/events", content=raw, headers=headers)
    assert resp.status_code == 200
    assert tasks == []
    assert posted == [("Sorry, you're not on agentra's Slack allowlist.", "D1", "1.1")]


def test_human_input_thread_reply_from_unlisted_user_not_resumed(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setenv("SLACK_ALLOWED_USERS", "U1")
    monkeypatch.setattr(registry, "resolve_slack_thread", lambda ts: {"app": "myapp", "issue_number": 7})
    calls = []
    monkeypatch.setattr(slack_route, "dispatch_human_answer", lambda *a, **k: calls.append((a, k)))
    posted = _capture_posts(monkeypatch)

    raw, headers = _signed({
        "type": "event_callback",
        "event": {"type": "message", "thread_ts": "1700000000.1", "channel": "C7", "text": "use option B", "user": "U99"},
    })
    resp = TestClient(server.app).post("/slack/events", content=raw, headers=headers)
    assert resp.status_code == 200
    assert calls == []
    assert len(posted) == 1
    text, channel, thread_ts = posted[0]
    assert text.startswith("Sorry, you're not on agentra's Slack allowlist.")
    assert "dashboard" in text and "GitHub issue" in text
    assert (channel, thread_ts) == ("C7", "1700000000.1")


def test_allowlist_unset_dispatches_and_warns_once(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.delenv("SLACK_ALLOWED_USERS", raising=False)
    _capture_posts(monkeypatch)
    tasks = _capture_tasks(monkeypatch)
    logs = []
    monkeypatch.setattr(slack_route, "_server_log", lambda ch, msg: logs.append((ch, msg)))

    client = TestClient(server.app)
    for _ in range(2):
        raw, headers = _signed(_dm_event("U1"))
        assert client.post("/slack/events", content=raw, headers=headers).status_code == 200
    for coro in tasks:
        coro.close()

    assert len(tasks) == 2
    warnings = [m for _, m in logs if "without an allowlist" in m]
    assert len(warnings) == 1
    assert any("user=U1" in m for _, m in logs)


def test_missing_user_fails_closed(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setenv("SLACK_ALLOWED_USERS", "U1")
    posted = _capture_posts(monkeypatch)
    tasks = _capture_tasks(monkeypatch)

    raw, headers = _signed(_dm_event(user=None))
    resp = TestClient(server.app).post("/slack/events", content=raw, headers=headers)
    assert resp.status_code == 200
    assert tasks == []
    assert posted == [("Sorry, you're not on agentra's Slack allowlist.", "D1", "1.1")]


def test_answer_propagates_slack_user_id(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    seen = {}

    async def fake_run_agent(**kw):
        seen.update(kw)
        return AgentResult(ok=True, text="ok", json_data=None, cost_usd=0.0, turns=1, session_id="s")

    monkeypatch.setattr(slack_assistant, "run_agent", fake_run_agent)
    reply = asyncio.run(slack_assistant.answer("hi", thread_key="C1:t1", slack_user_id="U0ABC123"))
    assert reply == "ok"
    assert "You are talking to Slack user U0ABC123." in seen["system_prompt"]


def test_failed_turn_returns_friendly_text(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)

    async def fake_run_agent(**kw):
        return AgentResult(ok=False, text="Reached maximum number of turns (24)", json_data=None, cost_usd=0.0, turns=24, session_id="s")

    monkeypatch.setattr(slack_assistant, "run_agent", fake_run_agent)
    reply = asyncio.run(slack_assistant.answer("hi", thread_key="C1:t1"))
    assert reply.startswith("Sorry — I couldn't finish that turn")


def test_manifest_is_valid(tmp_path):
    from pathlib import Path

    manifest = json.loads(Path("docs/slack-app-manifest.json").read_text())
    events = manifest["settings"]["event_subscriptions"]["bot_events"]
    assert set(events) == {"app_mention", "message.im", "message.channels", "message.groups"}
    scopes = manifest["oauth_config"]["scopes"]["bot"]
    assert set(scopes) == {
        "app_mentions:read", "im:history", "im:read", "chat:write", "channels:history", "groups:history",
    }
    assert manifest["settings"]["event_subscriptions"]["request_url"] == "https://agentra.srijanlab.com/slack/events"

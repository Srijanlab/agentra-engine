"""The conversational agentra assistant reachable via Slack DM / @mention
(distinct from the #68 human-input escalation threads)."""

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


def _capture_tasks(monkeypatch) -> list:
    tasks: list = []
    monkeypatch.setattr(slack_route.asyncio, "create_task", lambda coro: tasks.append(coro) or coro)
    return tasks


def test_app_mention_dispatches_the_assistant_and_posts_the_reply(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    captured = {}

    async def fake_run_agent(**kw):
        captured.update(kw)
        return AgentResult(ok=True, text="3 apps registered.", json_data=None, cost_usd=0.0, turns=1, session_id="sess-1")

    monkeypatch.setattr(slack_assistant, "run_agent", fake_run_agent)
    posted = []
    monkeypatch.setattr(slack, "_post_message", lambda text, channel=None, thread_ts=None: posted.append((text, channel, thread_ts)))
    tasks = _capture_tasks(monkeypatch)

    raw, headers = _signed({
        "type": "event_callback",
        "event_id": "Ev1",
        "event": {"type": "app_mention", "text": "<@U0BOT> how many apps?", "channel": "C1", "ts": "111.1", "user": "U1"},
    })
    resp = TestClient(server.app).post("/slack/events", content=raw, headers=headers)
    assert resp.status_code == 200

    asyncio.run(tasks[0])
    assert captured["prompt"] == "how many apps?"  # mention stripped
    assert "Bash" in captured["allowed_tools"]
    assert posted == [("3 apps registered.", "C1", "111.1")]


def test_direct_message_dispatches_the_assistant(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)

    async def fake_run_agent(**kw):
        return AgentResult(ok=True, text="paused.", json_data=None, cost_usd=0.0, turns=1, session_id="s2")

    monkeypatch.setattr(slack_assistant, "run_agent", fake_run_agent)
    posted = []
    monkeypatch.setattr(slack, "_post_message", lambda text, channel=None, thread_ts=None: posted.append((text, channel, thread_ts)))
    tasks = _capture_tasks(monkeypatch)

    raw, headers = _signed({
        "type": "event_callback",
        "event_id": "Ev2",
        "event": {"type": "message", "channel_type": "im", "text": "pause the system", "channel": "D1", "ts": "222.2", "user": "U1"},
    })
    resp = TestClient(server.app).post("/slack/events", content=raw, headers=headers)
    assert resp.status_code == 200
    asyncio.run(tasks[0])
    assert posted == [("paused.", "D1", "222.2")]


def test_duplicate_event_id_is_processed_once(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(slack, "_post_message", lambda *a, **k: None)
    tasks = _capture_tasks(monkeypatch)

    raw, headers = _signed({
        "type": "event_callback",
        "event_id": "EvDup",
        "event": {"type": "app_mention", "text": "<@U0BOT> hi", "channel": "C1", "ts": "1.1", "user": "U1"},
    })
    client = TestClient(server.app)
    client.post("/slack/events", content=raw, headers=headers)
    client.post("/slack/events", content=raw, headers=headers)
    assert len(tasks) == 1
    for coro in tasks:
        coro.close()


def test_bot_mention_is_ignored(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    tasks = _capture_tasks(monkeypatch)

    raw, headers = _signed({
        "type": "event_callback",
        "event_id": "EvBot",
        "event": {"type": "app_mention", "text": "<@U0BOT> hi", "channel": "C1", "ts": "1.1", "bot_id": "B9"},
    })
    resp = TestClient(server.app).post("/slack/events", content=raw, headers=headers)
    assert resp.status_code == 200
    assert tasks == []


def test_answer_resumes_the_threads_prior_session(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from agentra import chat_store

    chat_store.set_agent_session_id("agentra", "slack-assistant:C1:t9", "prev-sess")
    seen = {}

    async def fake_run_agent(**kw):
        seen["resume"] = kw.get("resume")
        return AgentResult(ok=True, text="ok", json_data=None, cost_usd=0.0, turns=1, session_id="new-sess")

    monkeypatch.setattr(slack_assistant, "run_agent", fake_run_agent)

    reply = asyncio.run(slack_assistant.answer("status?", thread_key="C1:t9"))
    assert reply == "ok"
    assert seen["resume"] == "prev-sess"
    assert chat_store.get_agent_session_id("agentra", "slack-assistant:C1:t9") == "new-sess"
    msgs = chat_store.get_agent_chat_messages("agentra", "slack-assistant:C1:t9")
    assert [m["sender"] for m in msgs] == ["human", "agent"]

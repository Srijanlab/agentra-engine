import time
import json
import asyncio
import datetime as dt
from pathlib import Path
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from agentra import registry, server, standup
from agentra.memory import Memory


def _isolate_registry(tmp_path, monkeypatch):
    home = tmp_path / "agentra_home"
    monkeypatch.setattr(registry, "_db", None)
    monkeypatch.setattr(registry, "AGENTRA_HOME", home)
    monkeypatch.setattr(registry, "APPS_PATH", home / "apps.json")
    monkeypatch.setattr(registry, "INBOX_ROOT", home / "inbox")
    monkeypatch.setattr(registry, "PAUSE_PATH", home / "paused.json")
    monkeypatch.setattr(registry, "_RUNS_PATH", home / "runs.json")
    monkeypatch.setattr(registry, "_AGENT_STEPS_PATH", home / "agent_steps.jsonl")
    server._active_runs.clear()
    server._app_locks.clear()


def _register_tmp_app(tmp_path: Path, name: str = "myapp") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    Memory(repo).set_objective("Ship useful dashboard improvements.")
    registry.register_app(name, str(repo), repo_url="https://github.com/acme/myapp.git", branch="main")
    return repo


def test_memory_chats_and_work_updates(tmp_path):
    repo = tmp_path / "mytestrepo"
    repo.mkdir()
    mem = Memory(repo)

    # Test Chat Messages
    assert mem.get_agent_chat_messages("codebase") == []
    mem.record_agent_chat_message("codebase", "human", "hello agent")
    mem.record_agent_chat_message("codebase", "agent", "hello human")

    msgs = mem.get_agent_chat_messages("codebase")
    assert len(msgs) == 2
    assert msgs[0]["sender"] == "human"
    assert msgs[0]["text"] == "hello agent"
    assert msgs[1]["sender"] == "agent"
    assert msgs[1]["text"] == "hello human"

    # Test Work Updates
    assert mem.get_work_updates() == []
    mem.record_work_update("implementation", "Implemented voice chat feature")
    mem.record_work_update("testing", "Wrote unit tests for voice feature")

    updates = mem.get_work_updates()
    assert len(updates) == 2
    assert updates[0]["agent_id"] == "implementation"
    assert updates[0]["description"] == "Implemented voice chat feature"
    assert updates[1]["agent_id"] == "testing"
    assert updates[1]["description"] == "Wrote unit tests for voice feature"


def test_server_agent_chat_endpoints(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    repo = _register_tmp_app(tmp_path, "chat-app")

    # Mock the run_agent execution so we don't trigger real LLM calls
    mock_result = AsyncMock()
    mock_result.text = "Hello! I am Codebase Agent.\n```json\n{\n  \"work_update\": \"Analyzed the codebase structure\"\n}\n```"
    mock_result.ok = True
    mock_result.cost_usd = 0.01
    mock_result.turns = 1
    mock_result.session_id = "test-session-123"
    mock_run_agent = AsyncMock(return_value=mock_result)

    monkeypatch.setattr("agentra.agents.base.run_agent", mock_run_agent)

    client = TestClient(server.app)

    # 1. GET chat history (empty)
    response = client.get("/apps/chat-app/agents/codebase/chat")
    assert response.status_code == 200
    assert response.json()["messages"] == []

    # 2. POST user message to chat
    response = client.post("/apps/chat-app/agents/codebase/chat", json={"message": "explain the code"})
    assert response.status_code == 200
    body = response.json()
    assert "Hello! I am Codebase Agent." in body["response"]
    assert "```json" not in body["response"]
    assert body["work_update"] == "Analyzed the codebase structure"

    # Verify message was stored in memory
    mem = Memory(repo)
    msgs = mem.get_agent_chat_messages("codebase")
    assert len(msgs) == 2
    assert msgs[0]["sender"] == "human"
    assert msgs[0]["text"] == "explain the code"
    assert msgs[1]["sender"] == "agent"
    assert "Hello! I am Codebase Agent." in msgs[1]["text"]

    # Verify work update was stored
    work_updates = mem.get_work_updates()
    assert len(work_updates) == 1
    assert work_updates[0]["agent_id"] == "codebase"
    assert work_updates[0]["description"] == "Analyzed the codebase structure"

    # 3. GET chat history (now populated)
    response = client.get("/apps/chat-app/agents/codebase/chat")
    assert response.status_code == 200
    assert len(response.json()["messages"]) == 2

    # 4. Session continuity: the session_id from turn 1 was persisted, and a
    # second message resumes it instead of starting cold.
    assert mem.get_agent_session_id("codebase") == "test-session-123"
    client.post("/apps/chat-app/agents/codebase/chat", json={"message": "and now?"})
    _, kwargs = mock_run_agent.call_args
    assert kwargs["resume"] == "test-session-123"


def test_server_agent_chat_stream_endpoint(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    repo = _register_tmp_app(tmp_path, "stream-app")

    async def fake_stream_chat_turn(**kwargs):
        assert kwargs["resume"] is None  # first message, no prior session
        for chunk in ["Hel", "lo! ", "I am ", "Codebase Agent."]:
            yield {"type": "delta", "text": chunk}
        yield {
            "type": "done",
            "ok": True,
            "text": "Hello! I am Codebase Agent.",
            "json_data": None,
            "cost_usd": 0.02,
            "turns": 1,
            "session_id": "stream-session-456",
        }

    monkeypatch.setattr("agentra.agents.base.stream_chat_turn", fake_stream_chat_turn)

    client = TestClient(server.app)
    with client.stream(
        "POST", "/apps/stream-app/agents/codebase/chat/stream", json={"message": "explain the code"}
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        events = [json.loads(line[len("data: "):]) for line in response.iter_lines() if line.startswith("data: ")]

    deltas = [e["delta"] for e in events if "delta" in e]
    assert "".join(deltas) == "Hello! I am Codebase Agent."
    done_events = [e for e in events if e.get("done")]
    assert len(done_events) == 1
    assert done_events[0]["response"] == "Hello! I am Codebase Agent."

    # Same persistence as the buffered endpoint: chat history and session id.
    mem = Memory(repo)
    msgs = mem.get_agent_chat_messages("codebase")
    assert len(msgs) == 2
    assert msgs[0]["text"] == "explain the code"
    assert msgs[1]["text"] == "Hello! I am Codebase Agent."
    assert mem.get_agent_session_id("codebase") == "stream-session-456"


def test_all_apps_loops_endpoint_matches_per_app_shape(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    _register_tmp_app(tmp_path, "app-one")
    _register_tmp_app(tmp_path, "app-two")

    # Deliberately the same objective text for both apps -- loop_id_for()
    # hashes only the objective, so this is exactly the case that used to
    # silently merge two different apps' runs into a single loop once
    # list_loops aggregated across apps.
    same_objective = "Ship useful dashboard improvements."
    loop_id = registry.loop_id_for(same_objective)
    registry.record_run(
        "run-one", app="app-one", source="on-demand", status="completed",
        started_at=time.time(), objective=same_objective, loop_id=loop_id,
    )
    registry.record_run(
        "run-two", app="app-two", source="on-demand", status="completed",
        started_at=time.time(), objective=same_objective, loop_id=loop_id,
    )

    client = TestClient(server.app)

    # Same grouped-by-loop shape as the per-app endpoint, just aggregated.
    response = client.get("/loops")
    assert response.status_code == 200
    loops = response.json()["loops"]
    assert len(loops) == 2, "two apps sharing an objective must not merge into one loop"
    apps_seen = {loop["app"] for loop in loops}
    assert apps_seen == {"app-one", "app-two"}
    for loop in loops:
        assert len(loop["runs"]) == 1

    # Per-app endpoint still returns just that app's one loop.
    response = client.get("/apps/app-one/loops")
    assert response.status_code == 200
    loops = response.json()["loops"]
    assert len(loops) == 1
    assert loops[0]["app"] == "app-one"
    assert len(loops[0]["runs"]) == 1


def test_server_work_update_endpoints(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    repo = _register_tmp_app(tmp_path, "work-app")
    client = TestClient(server.app)

    # 1. GET work updates (empty)
    response = client.get("/apps/work-app/work-updates")
    assert response.status_code == 200
    assert response.json()["updates"] == []

    # 2. POST manual work update
    response = client.post("/apps/work-app/agents/testing/work", json={"description": "Added test cases for server triggers"})
    assert response.status_code == 200
    assert response.json()["ok"] is True

    # 3. GET work updates (populated)
    response = client.get("/apps/work-app/work-updates")
    assert response.status_code == 200
    updates = response.json()["updates"]
    assert len(updates) == 1
    assert updates[0]["agent_id"] == "testing"
    assert updates[0]["description"] == "Added test cases for server triggers"


def test_structured_standup_generation(tmp_path, monkeypatch):
    repo = tmp_path / "standup-repo"
    repo.mkdir()
    mem = Memory(repo)

    # Seed some log lines and a work update
    mem.log("run1", "codebase agent: scanned files")
    mem.record_work_update("implementation", "Fixed a bug in auth token parsing")

    # Mock run_agent call in standup.py
    mock_result = AsyncMock()
    mock_result.text = """
```json
{
  "updates": {
    "orchestrator": "Yesterday: Idle. Today: Idle.",
    "codebase": "Yesterday: Scanned files. Today: Idle.",
    "implementation": "Yesterday: Fixed a bug. Today: Idle."
  }
}
```
"""
    mock_result.ok = True
    mock_result.cost_usd = 0.05
    mock_result.turns = 1
    mock_run_agent = AsyncMock(return_value=mock_result)

    monkeypatch.setattr("agentra.standup.run_agent", mock_run_agent)

    # Run standup
    report = asyncio.run(standup.run_standup(repo, "standup-app", mem=mem))

    assert "## Codebase Agent" in report
    assert "Yesterday: Scanned files. Today: Idle." in report
    assert "## Implementation Agent" in report
    assert "Yesterday: Fixed a bug. Today: Idle." in report

    # Verify structured standup json file is created
    latest = mem.latest_standup()
    assert latest is not None
    assert latest["updates"] is not None
    assert latest["updates"]["codebase"] == "Yesterday: Scanned files. Today: Idle."
    assert latest["updates"]["implementation"] == "Yesterday: Fixed a bug. Today: Idle."


def test_standup_live_channel_websocket(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    repo = _register_tmp_app(tmp_path, "live-standup-app")
    mem = Memory(repo)
    mem.record_work_update("testing", "Ran the full suite, all green")

    # Opening burst: same mechanism test_structured_standup_generation
    # covers, mocked the same way -- generate_standup_updates's one LLM call.
    mock_burst_result = AsyncMock()
    mock_burst_result.text = '```json\n{"updates": {"testing": "Yesterday: Ran the suite. Today: Idle."}}\n```'
    mock_burst_result.ok = True
    monkeypatch.setattr("agentra.standup.run_agent", AsyncMock(return_value=mock_burst_result))

    # The routed reply to a human's @mention -- stream_chat_turn is an
    # async generator, so the mock must actually be one.
    async def fake_stream_chat_turn(**kwargs):
        assert kwargs["agent_label"] == "Testing Agent"
        yield {"type": "delta", "text": "All "}
        yield {"type": "delta", "text": "green."}
        yield {
            "type": "done", "ok": True, "text": "All green.", "json_data": None,
            "cost_usd": 0.01, "turns": 1, "session_id": "standup-session-789",
        }

    monkeypatch.setattr("agentra.agents.base.stream_chat_turn", fake_stream_chat_turn)

    client = TestClient(server.app)
    with client.websocket_connect("/apps/live-standup-app/standup/live") as ws:
        # Opening burst: one message, from the testing agent, flagged fresh
        # (just generated, not replayed) so the frontend knows to reveal it
        # with rotation pacing rather than instantly.
        opening = ws.receive_json()
        assert opening == {
            "type": "message", "fresh": True, "sender": "testing",
            "text": "Yesterday: Ran the suite. Today: Idle.", "ts": opening["ts"],
        }
        assert ws.receive_json() == {"type": "burst_complete"}

        # Human addresses the testing agent directly.
        ws.send_json({"message": "@testing did anything flake?"})

        echoed = ws.receive_json()
        assert echoed["type"] == "message" and echoed["sender"] == "human"

        start = ws.receive_json()
        assert start == {"type": "start", "sender": "testing"}

        d1 = ws.receive_json()
        d2 = ws.receive_json()
        assert d1 == {"type": "delta", "sender": "testing", "text": "All "}
        assert d2 == {"type": "delta", "sender": "testing", "text": "green."}

        final = ws.receive_json()
        assert final["type"] == "message" and final["sender"] == "testing" and final["text"] == "All green."
        assert final["replacing_stream"] is True

    # Persisted to the channel transcript and reused on reconnect.
    date_str = dt.datetime.now(dt.timezone.utc).date().isoformat()
    persisted = mem.get_standup_channel_messages(date_str)
    assert [m["sender"] for m in persisted] == ["testing", "human", "testing"]
    assert mem.get_agent_session_id(f"standup:{date_str}:testing") == "standup-session-789"

    with client.websocket_connect("/apps/live-standup-app/standup/live") as ws:
        replayed = [ws.receive_json() for _ in range(3)]
        assert [m["sender"] for m in replayed] == ["testing", "human", "testing"]


def test_tts_endpoint(monkeypatch):
    from unittest.mock import MagicMock

    fake_response = MagicMock()
    fake_response.audio_content = b"fake-mp3-bytes"
    fake_client = MagicMock()
    fake_client.synthesize_speech.return_value = fake_response
    monkeypatch.setattr(
        "google.cloud.texttospeech.TextToSpeechClient", MagicMock(return_value=fake_client)
    )

    client = TestClient(server.app)
    response = client.post("/tts", json={"text": "hello", "agent_id": "testing"})
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/mpeg"
    assert response.content == b"fake-mp3-bytes"

    # Distinct voice actually requested for the given agent, not just
    # whatever the client happens to default to.
    _, kwargs = fake_client.synthesize_speech.call_args
    assert kwargs["voice"].name == server.AGENT_VOICES["testing"]


def test_tts_endpoint_unavailable_returns_503(monkeypatch):
    from unittest.mock import MagicMock

    fake_client = MagicMock()
    fake_client.synthesize_speech.side_effect = RuntimeError("ADC not configured")
    monkeypatch.setattr(
        "google.cloud.texttospeech.TextToSpeechClient", MagicMock(return_value=fake_client)
    )

    client = TestClient(server.app)
    response = client.post("/tts", json={"text": "hello", "agent_id": "testing"})
    assert response.status_code == 503


def test_agent_metadata_endpoint_covers_every_roster_entry():
    from agentra.agents import catalog as agents_catalog

    client = TestClient(server.app)
    response = client.get("/agents/metadata")
    assert response.status_code == 200
    body = response.json()

    assert body["agents"] == agents_catalog.AGENT_METADATA
    assert body["permission_model_note"] == agents_catalog.PERMISSION_MODEL_NOTE

    # Every agent must have at least one skill and one tool with a
    # recognized permission tier -- an empty entry would silently render
    # as a blank card.
    valid_permissions = {"read", "write", "execute", "network", "delegate"}
    for agent_id, meta in body["agents"].items():
        assert meta["skills"], f"{agent_id} has no skills listed"
        assert meta["tools"], f"{agent_id} has no tools listed"
        for tool in meta["tools"]:
            assert tool["permission"] in valid_permissions

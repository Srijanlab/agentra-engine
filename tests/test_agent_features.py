import time
import json
import asyncio
import datetime as dt
from pathlib import Path
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from agentra import chat_store, registry, server, standup
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


def test_memory_chats(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    repo = tmp_path / "mytestrepo"
    repo.mkdir()

    # Chat Messages -- chat_store.py (server-side, AGENTRA_HOME), not
    # the target repo's own .agentra/.
    assert chat_store.get_agent_chat_messages("mytestrepo", "codebase") == []
    chat_store.record_agent_chat_message("mytestrepo", "codebase", "human", "hello agent")
    chat_store.record_agent_chat_message("mytestrepo", "codebase", "agent", "hello human")

    msgs = chat_store.get_agent_chat_messages("mytestrepo", "codebase")
    assert len(msgs) == 2
    assert msgs[0]["sender"] == "human"
    assert msgs[0]["text"] == "hello agent"
    assert msgs[1]["sender"] == "agent"
    assert msgs[1]["text"] == "hello human"


def test_server_agent_chat_endpoints(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    _register_tmp_app(tmp_path, "chat-app")

    # Mock the run_agent execution so we don't trigger real LLM calls
    mock_result = AsyncMock()
    mock_result.text = "Hello! I am Codebase Agent."
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
    assert body["response"] == "Hello! I am Codebase Agent."

    # Verify message was stored server-side (chat_store.py)
    msgs = chat_store.get_agent_chat_messages("chat-app", "codebase")
    assert len(msgs) == 2
    assert msgs[0]["sender"] == "human"
    assert msgs[0]["text"] == "explain the code"
    assert msgs[1]["sender"] == "agent"
    assert "Hello! I am Codebase Agent." in msgs[1]["text"]

    # 3. GET chat history (now populated)
    response = client.get("/apps/chat-app/agents/codebase/chat")
    assert response.status_code == 200
    assert len(response.json()["messages"]) == 2

    # 4. Session continuity: the session_id from turn 1 was persisted, and a
    # second message resumes it instead of starting cold.
    assert chat_store.get_agent_session_id("chat-app", "codebase") == "test-session-123"
    client.post("/apps/chat-app/agents/codebase/chat", json={"message": "and now?"})
    _, kwargs = mock_run_agent.call_args
    assert kwargs["resume"] == "test-session-123"


def test_server_agent_chat_stream_endpoint(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    _register_tmp_app(tmp_path, "stream-app")

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
    msgs = chat_store.get_agent_chat_messages("stream-app", "codebase")
    assert len(msgs) == 2
    assert msgs[0]["text"] == "explain the code"
    assert msgs[1]["text"] == "Hello! I am Codebase Agent."
    assert chat_store.get_agent_session_id("stream-app", "codebase") == "stream-session-456"


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


def test_list_loops_orders_runs_newest_first(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    _register_tmp_app(tmp_path, "app-one")

    objective = "Ship useful dashboard improvements."
    loop_id = registry.loop_id_for(objective)
    base = time.time()
    # Record out of order to make sure the fix doesn't rely on insertion order.
    registry.record_run(
        "run-mid", app="app-one", source="on-demand", status="completed",
        started_at=base + 100, objective=objective, loop_id=loop_id,
    )
    registry.record_run(
        "run-oldest", app="app-one", source="on-demand", status="completed",
        started_at=base, objective=objective, loop_id=loop_id,
    )
    registry.record_run(
        "run-newest", app="app-one", source="on-demand", status="completed",
        started_at=base + 200, objective=objective, loop_id=loop_id,
    )

    client = TestClient(server.app)
    response = client.get("/loops")
    assert response.status_code == 200
    loops = response.json()["loops"]
    assert len(loops) == 1
    loop = loops[0]

    # Runs within the expanded loop must be newest first.
    started_ats = [r["started_at"] for r in loop["runs"]]
    assert started_ats == sorted(started_ats, reverse=True)
    assert [r["run_key"] for r in loop["runs"]] == ["run-newest", "run-mid", "run-oldest"]

    # loop["started_at"] must reflect the true oldest run, not runs[0].
    assert loop["started_at"] == base


def test_structured_standup_generation(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    repo = tmp_path / "standup-repo"
    repo.mkdir()
    mem = Memory(repo)

    # Seed some log lines
    mem.log("run1", "codebase agent: scanned files")

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

    # Verify structured standup json file is created (chat_store.py, server-side)
    latest = chat_store.latest_standup("standup-app")
    assert latest is not None
    assert latest["updates"] is not None
    assert latest["updates"]["codebase"] == "Yesterday: Scanned files. Today: Idle."
    assert latest["updates"]["implementation"] == "Yesterday: Fixed a bug. Today: Idle."


def test_standup_live_channel_websocket(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    repo = _register_tmp_app(tmp_path, "live-standup-app")
    # Something for generate_standup_updates to find non-empty -- otherwise
    # its "nothing to report" early exit skips the (mocked) LLM call below
    # entirely and the assertions on its output never get exercised.
    Memory(repo).log("run1", "testing agent: ran the full suite, all green")

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

    # Persisted to the channel transcript (chat_store.py) and reused on reconnect.
    date_str = dt.datetime.now(dt.timezone.utc).date().isoformat()
    persisted = chat_store.get_standup_channel_messages("live-standup-app", date_str)
    assert [m["sender"] for m in persisted] == ["testing", "human", "testing"]
    assert chat_store.get_standup_agent_session_id("live-standup-app", date_str, "testing") == "standup-session-789"

    with client.websocket_connect("/apps/live-standup-app/standup/live") as ws:
        replayed = [ws.receive_json() for _ in range(3)]
        assert [m["sender"] for m in replayed] == ["testing", "human", "testing"]


def test_post_standup_then_live_channel_reuses_the_same_generation(tmp_path, monkeypatch):
    """POST /apps/{app}/standup generates first; opening the live WS
    afterward the same day must seed its opening burst from that exact
    content instead of calling the LLM a second time."""
    _isolate_registry(tmp_path, monkeypatch)
    repo = _register_tmp_app(tmp_path, "post-then-live-app")
    Memory(repo).log("run1", "testing agent: ran the full suite, all green")

    mock_result = AsyncMock()
    mock_result.text = '```json\n{"updates": {"testing": "Yesterday: Ran the suite. Today: Idle."}}\n```'
    mock_result.ok = True
    mock_run_agent = AsyncMock(return_value=mock_result)
    monkeypatch.setattr("agentra.standup.run_agent", mock_run_agent)

    client = TestClient(server.app)
    post_response = client.post("/apps/post-then-live-app/standup")
    assert post_response.status_code == 200
    posted_text = post_response.json()["report"]
    assert "Yesterday: Ran the suite. Today: Idle." in posted_text
    assert mock_run_agent.call_count == 1

    with client.websocket_connect("/apps/post-then-live-app/standup/live") as ws:
        opening = ws.receive_json()
        assert opening["sender"] == "testing"
        assert opening["text"] == "Yesterday: Ran the suite. Today: Idle."
        # Reused, not freshly generated by this connection.
        assert "fresh" not in opening
        assert ws.receive_json() == {"type": "burst_complete"}

    # Still exactly one LLM call total -- the WS connection reused it.
    assert mock_run_agent.call_count == 1

    latest = chat_store.latest_standup("post-then-live-app")
    assert latest["updates"]["testing"] == "Yesterday: Ran the suite. Today: Idle."


def test_live_channel_then_post_standup_reuses_the_same_generation(tmp_path, monkeypatch):
    """Opening the live WS first generates the standup and must persist it
    to the durable report store too, so a subsequent POST
    /apps/{app}/standup the same day reuses that text verbatim instead of
    generating an independent report."""
    _isolate_registry(tmp_path, monkeypatch)
    repo = _register_tmp_app(tmp_path, "live-then-post-app")
    Memory(repo).log("run1", "testing agent: ran the full suite, all green")

    mock_result = AsyncMock()
    mock_result.text = '```json\n{"updates": {"testing": "Yesterday: Ran the suite. Today: Idle."}}\n```'
    mock_result.ok = True
    mock_run_agent = AsyncMock(return_value=mock_result)
    monkeypatch.setattr("agentra.standup.run_agent", mock_run_agent)

    client = TestClient(server.app)
    with client.websocket_connect("/apps/live-then-post-app/standup/live") as ws:
        opening = ws.receive_json()
        assert opening["fresh"] is True
        assert opening["text"] == "Yesterday: Ran the suite. Today: Idle."
        assert ws.receive_json() == {"type": "burst_complete"}

    assert mock_run_agent.call_count == 1

    # GET /apps/{app}/standup/latest already reflects the WS-triggered
    # generation via the durable report store.
    latest_response = client.get("/apps/live-then-post-app/standup/latest")
    assert latest_response.json()["standup"]["updates"]["testing"] == "Yesterday: Ran the suite. Today: Idle."

    post_response = client.post("/apps/live-then-post-app/standup")
    assert post_response.status_code == 200
    assert "Yesterday: Ran the suite. Today: Idle." in post_response.json()["report"]

    # No second LLM call -- the POST reused the WS's generation.
    assert mock_run_agent.call_count == 1


def test_post_standup_twice_reuses_without_second_llm_call(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    repo = _register_tmp_app(tmp_path, "double-post-app")
    Memory(repo).log("run1", "codebase agent: refactored a module")

    mock_result = AsyncMock()
    mock_result.text = '```json\n{"updates": {"codebase": "Yesterday: Refactored. Today: Idle."}}\n```'
    mock_result.ok = True
    mock_run_agent = AsyncMock(return_value=mock_result)
    monkeypatch.setattr("agentra.standup.run_agent", mock_run_agent)

    client = TestClient(server.app)
    first = client.post("/apps/double-post-app/standup")
    second = client.post("/apps/double-post-app/standup")

    assert first.json()["report"] == second.json()["report"]
    assert mock_run_agent.call_count == 1


def test_idle_standup_reused_across_entry_points_same_day(tmp_path, monkeypatch):
    """An app with genuinely no logged activity/backlog gets the idle
    placeholder without an LLM call, and every subsequent entry point that
    day reuses that exact idle content rather than recomputing it."""
    _isolate_registry(tmp_path, monkeypatch)
    repo = tmp_path / "idle-app"
    repo.mkdir()
    registry.register_app("idle-app", str(repo), repo_url="https://github.com/acme/idle-app.git", branch="main")

    mock_run_agent = AsyncMock()
    monkeypatch.setattr("agentra.standup.run_agent", mock_run_agent)

    client = TestClient(server.app)
    with client.websocket_connect("/apps/idle-app/standup/live") as ws:
        burst = []
        while True:
            event = ws.receive_json()
            if event["type"] == "burst_complete":
                break
            burst.append(event)
        assert len(burst) == len(standup.AGENT_LABELS)
        assert all(m["text"] == "Yesterday: No activity. Today: Idle." for m in burst)
        assert all(m["fresh"] is True for m in burst)

    post_response = client.post("/apps/idle-app/standup")
    assert "Yesterday: No activity. Today: Idle." in post_response.json()["report"]

    # No LLM call at all -- the empty-context short circuit in
    # generate_standup_updates fired once, and every other entry point
    # reused its persisted output.
    mock_run_agent.assert_not_called()


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
        assert meta.get("capability"), f"{agent_id} has no capability set"
        for tool in meta["tools"]:
            assert tool["permission"] in valid_permissions


def test_reconcile_stale_runs_marks_orphaned_run_failed(tmp_path, monkeypatch):
    """Regression test: reconcile_stale_runs() referenced
    core.STALE_RUN_SECONDS, which was never actually defined on
    registry/core.py (only STALE_PROCESSING_SECONDS exists) -- an
    AttributeError that crashed every call to it with a 500, including
    from GET /runs (systems.py calls it on every request). Confirmed live
    against the deployed VM. No prior test exercised the actual comparison
    line (only ever ran against empty queued/running lists), so this
    real production bug shipped and stayed live undetected."""
    _isolate_registry(tmp_path, monkeypatch)
    _register_tmp_app(tmp_path, "stale-app")

    stale_started = time.time() - registry.STALE_PROCESSING_SECONDS - 60
    registry.record_run("stuck-run", app="stale-app", status="running", started_at=stale_started, source="scheduled")
    registry.record_run("fresh-run", app="stale-app", status="running", started_at=time.time(), source="scheduled")

    marked = registry.reconcile_stale_runs()

    assert marked == ["stuck-run"]
    stuck = registry.get_run("stuck-run")
    assert stuck["status"] == "failed"
    assert "orphaned" in stuck["error"]
    # The still-active run must be left alone.
    assert registry.get_run("fresh-run")["status"] == "running"

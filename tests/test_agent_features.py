import time
import json
import asyncio
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

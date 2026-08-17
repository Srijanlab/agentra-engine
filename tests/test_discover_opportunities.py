"""discover_opportunities (agents/brain/tools.py) -- had zero dedicated
coverage before this file. Covers the normal ranked-opportunities path and
the structured HUMAN_INPUT_REQUIRED routing through
Memory.record_known_bug(needs_human=True), distinct from the pre-existing
"no opportunities found" path (which still counts as a tool failure).

No real LLM call: discovery.run is monkeypatched.
"""

import asyncio
from pathlib import Path

from agentra import registry
from agentra.agents import brain
from agentra.agents.base import AgentResult
from agentra.environments import EnvironmentConfig
from agentra.memory import Memory


def _session(tmp_path: Path, **overrides) -> brain.OrchestratorSession:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    defaults = dict(
        repo=repo,
        objective="test objective",
        env=EnvironmentConfig(),
        mem=Memory(repo),
        run_id="testrun1",
        cb_summary="a codebase summary",
    )
    defaults.update(overrides)
    return brain.OrchestratorSession(**defaults)


def _tool(session, name):
    tools = brain._tools_for(session)
    return next(t for t in tools if t.name == name)


def _patch_registry(monkeypatch):
    monkeypatch.setattr(registry, "record_agent_step", lambda *a, **k: None)


def _stub_backlog(session, monkeypatch):
    monkeypatch.setattr(session.mem, "shipped_features", lambda: [])
    monkeypatch.setattr(session.mem, "known_bugs", lambda: [])
    monkeypatch.setattr(session.mem, "feature_queue", lambda: [])


def test_discover_opportunities_ranks_and_returns_opportunities_on_the_normal_path(tmp_path, monkeypatch):
    _patch_registry(monkeypatch)
    session = _session(tmp_path)
    _stub_backlog(session, monkeypatch)

    async def fake_discovery_run(*a, **k):
        return AgentResult(
            ok=True, text="ok",
            json_data={"opportunities": [{"feature": "add_search", "description": "...", "impact": "high", "effort": "low", "reason": "...", "origin": "autonomous", "id": None}]},
            cost_usd=0.02, turns=3,
        )

    monkeypatch.setattr(brain.discovery, "run", fake_discovery_run)

    result = asyncio.run(_tool(session, "discover_opportunities").handler({}))

    assert result.get("is_error") is not True
    assert "add_search" in result["content"][0]["text"]
    assert session.tool_failure_counts.get("discover_opportunities", 0) == 0


def test_discover_opportunities_routes_human_input_required_through_record_known_bug(tmp_path, monkeypatch):
    _patch_registry(monkeypatch)
    session = _session(tmp_path)
    _stub_backlog(session, monkeypatch)

    async def fake_discovery_run(*a, **k):
        return AgentResult(
            ok=True, text="blocked",
            json_data={
                "status": "HUMAN_INPUT_REQUIRED",
                "reason": "Objective names two mutually exclusive directions.",
                "question": "Should we prioritize retention or acquisition?",
                "options": ["retention", "acquisition"],
            },
            cost_usd=0.01, turns=2,
        )

    monkeypatch.setattr(brain.discovery, "run", fake_discovery_run)
    known_bug_calls = []
    monkeypatch.setattr(
        session.mem,
        "record_known_bug",
        lambda run_id, severity, diagnosis, proposed_fix, **k: known_bug_calls.append((severity, diagnosis, k)),
    )

    result = asyncio.run(_tool(session, "discover_opportunities").handler({}))

    assert result.get("is_error") is True
    assert len(known_bug_calls) == 1
    severity, diagnosis, kwargs = known_bug_calls[0]
    assert kwargs["needs_human"] is True
    assert kwargs.get("blocking_agentra", False) is False
    assert "two mutually exclusive directions" in diagnosis
    assert "Should we prioritize retention or acquisition?" in diagnosis


def test_discover_opportunities_human_input_required_does_not_count_as_a_tool_failure(tmp_path, monkeypatch):
    _patch_registry(monkeypatch)
    session = _session(tmp_path)
    _stub_backlog(session, monkeypatch)

    async def fake_discovery_run(*a, **k):
        return AgentResult(ok=True, text="blocked", json_data={"status": "HUMAN_INPUT_REQUIRED", "reason": "needs a call"}, cost_usd=0.0, turns=1)

    monkeypatch.setattr(brain.discovery, "run", fake_discovery_run)
    monkeypatch.setattr(session.mem, "record_known_bug", lambda *a, **k: None)
    monkeypatch.setattr(
        session.mem, "record_failure", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not be called"))
    )

    asyncio.run(_tool(session, "discover_opportunities").handler({}))

    assert session.tool_failure_counts.get("discover_opportunities", 0) == 0

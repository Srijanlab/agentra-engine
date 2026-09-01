"""agents/brain/tools.py's implement_feature enforces one open loop at a time:
a brand-new backlog item (resolves_origin='new') is refused while another issue
still carries status:in-progress -- unless that in-progress loop is blocked on a
human (need_human), which in_progress_items() already filters out, or unless this
call is itself a human-answer resume.
"""

import asyncio
from pathlib import Path

import pytest

from agentra.agents import brain
from agentra.agents.base import AgentResult
from agentra.environments import EnvironmentConfig
from agentra.memory import Memory
from agentra import registry


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
    return next(t for t in brain._tools_for(session) if t.name == name)


@pytest.fixture(autouse=True)
def _patch_registry(monkeypatch):
    monkeypatch.setattr(registry, "record_agent_step", lambda *a, **k: None)
    monkeypatch.setattr(registry, "record_run", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def _stub_requirements(monkeypatch):
    async def fake_run(*a, **k):
        return AgentResult(ok=False, text="stubbed -- no spec", json_data=None, cost_usd=0.0, turns=0)

    monkeypatch.setattr(brain.requirements, "run", fake_run)


def test_new_work_refused_while_another_loop_is_in_progress(tmp_path, monkeypatch):
    session = _session(tmp_path)
    monkeypatch.setattr(
        session.mem, "in_progress_items",
        lambda: [{"external_id": "42", "diagnosis": "Half-built export feature"}],
    )
    impl_calls = []

    async def fake_impl_run(*a, **k):
        impl_calls.append(a)
        return AgentResult(ok=True, text="done", json_data={"feature": "X"}, cost_usd=0.0, turns=1)

    monkeypatch.setattr(brain.implementation, "run", fake_impl_run)

    result = asyncio.run(
        _tool(session, "implement_feature").handler(
            {"feature_brief": "Start a brand new thing", "resolves_origin": "new"}
        )
    )

    assert result.get("is_error") is True
    assert "Open loop not finished" in result["content"][0]["text"]
    assert "#42" in result["content"][0]["text"]
    assert impl_calls == []


def test_resuming_the_in_progress_item_itself_is_allowed(tmp_path, monkeypatch):
    session = _session(tmp_path)
    monkeypatch.setattr(
        session.mem, "in_progress_items",
        lambda: [{"external_id": "42", "diagnosis": "Half-built export feature"}],
    )
    monkeypatch.setattr(session.mem, "record_code_complete", lambda *a, **k: {"issue_number": 42, "board_issue_number": None})
    monkeypatch.setattr(session.mem, "append_documentation", lambda *a, **k: None)
    monkeypatch.setattr(session.mem, "clear_known_bug", lambda *a, **k: None)

    async def fake_impl_run(*a, **k):
        return AgentResult(ok=True, text="done", json_data={"feature": "Export feature"}, cost_usd=0.0, turns=1)

    monkeypatch.setattr(brain.implementation, "run", fake_impl_run)

    result = asyncio.run(
        _tool(session, "implement_feature").handler(
            {"feature_brief": "Finish the export feature", "resolves_id": "42", "resolves_origin": "known_bug"}
        )
    )

    assert result.get("is_error") is not True


def test_human_answer_resume_may_start_new_work_despite_an_open_loop(tmp_path, monkeypatch):
    session = _session(tmp_path, human_answer="Go ahead.", human_answer_issue=99)
    monkeypatch.setattr(
        session.mem, "in_progress_items",
        lambda: [{"external_id": "42", "diagnosis": "A loop blocked on something"}],
    )
    monkeypatch.setattr(session.mem, "record_code_complete", lambda *a, **k: {"issue_number": None, "board_issue_number": None})
    monkeypatch.setattr(session.mem, "append_documentation", lambda *a, **k: None)

    async def fake_impl_run(*a, **k):
        return AgentResult(ok=True, text="done", json_data={"feature": "New thing"}, cost_usd=0.0, turns=1)

    monkeypatch.setattr(brain.implementation, "run", fake_impl_run)

    result = asyncio.run(
        _tool(session, "implement_feature").handler(
            {"feature_brief": "A different piece of work", "resolves_origin": "new"}
        )
    )

    assert result.get("is_error") is not True

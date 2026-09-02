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


def test_a_second_different_already_tracked_issue_is_refused_within_the_same_run(tmp_path, monkeypatch):
    """Confirmed live (loop d728c61dc0, 2026-09-01): a single run chained #127 -> #131 -> #130
    onto the same feature_branch by calling implement_feature with resolves_origin="known_bug"
    each time -- the resolves_origin="new" check alone never catches a second already-tracked
    item picked up mid-run."""
    session = _session(tmp_path)
    monkeypatch.setattr(session.mem, "in_progress_items", lambda: [])
    monkeypatch.setattr(session.mem, "record_code_complete", lambda *a, **k: {"issue_number": 127, "board_issue_number": None})
    monkeypatch.setattr(session.mem, "append_documentation", lambda *a, **k: None)
    monkeypatch.setattr(session.mem, "clear_known_bug", lambda *a, **k: None)

    impl_calls = []

    async def fake_impl_run(*a, **k):
        impl_calls.append(a)
        return AgentResult(ok=True, text="done", json_data={"feature": "Fix #127"}, cost_usd=0.0, turns=1)

    monkeypatch.setattr(brain.implementation, "run", fake_impl_run)

    first = asyncio.run(
        _tool(session, "implement_feature").handler(
            {"feature_brief": "Fix issue 127", "resolves_id": "127", "resolves_origin": "known_bug"}
        )
    )
    assert first.get("is_error") is not True
    assert session.committed_issue == "127"

    second = asyncio.run(
        _tool(session, "implement_feature").handler(
            {"feature_brief": "Fix issue 131", "resolves_id": "131", "resolves_origin": "known_bug"}
        )
    )
    assert second.get("is_error") is True
    assert "already committed to issue #127" in second["content"][0]["text"]
    assert "#131" in second["content"][0]["text"]
    assert len(impl_calls) == 1  # implementation.run never ran for #131


def test_retrying_the_same_committed_issue_is_allowed(tmp_path, monkeypatch):
    session = _session(tmp_path)
    monkeypatch.setattr(session.mem, "in_progress_items", lambda: [])
    monkeypatch.setattr(session.mem, "record_code_complete", lambda *a, **k: {"issue_number": 127, "board_issue_number": None})
    monkeypatch.setattr(session.mem, "append_documentation", lambda *a, **k: None)
    monkeypatch.setattr(session.mem, "clear_known_bug", lambda *a, **k: None)

    async def fake_impl_run(*a, **k):
        return AgentResult(ok=True, text="done", json_data={"feature": "Fix #127"}, cost_usd=0.0, turns=1)

    monkeypatch.setattr(brain.implementation, "run", fake_impl_run)

    for _ in range(2):
        result = asyncio.run(
            _tool(session, "implement_feature").handler(
                {"feature_brief": "Fix issue 127", "resolves_id": "127", "resolves_origin": "known_bug"}
            )
        )
        assert result.get("is_error") is not True


def test_an_issue_worked_by_too_many_runs_is_escalated_not_re_selected(tmp_path, monkeypatch):
    """Stuck-loop backstop (GitHub #130): an issue that can't reach status:done grinds
    forever -- check_backlog keeps offering it, each run resumes the same branch. After
    MAX_RUNS_PER_TRACKING_ISSUE distinct runs, implement_feature escalates instead."""
    session = _session(tmp_path, feature_branch="dev/stuck-branch")
    monkeypatch.setattr(session.mem, "in_progress_items", lambda: [])
    monkeypatch.setattr(
        session.mem, "run_ids_for",
        lambda ext: [f"run{i}" for i in range(brain.tools.MAX_RUNS_PER_TRACKING_ISSUE)],
    )
    escalations = []
    monkeypatch.setattr(
        brain.tools, "_escalate_to_human",
        lambda session, **kw: escalations.append(kw) or 130,
    )

    impl_calls = []

    async def fake_impl_run(*a, **k):
        impl_calls.append(a)
        return AgentResult(ok=True, text="done", json_data={"feature": "X"}, cost_usd=0.0, turns=1)

    monkeypatch.setattr(brain.implementation, "run", fake_impl_run)

    result = asyncio.run(
        _tool(session, "implement_feature").handler(
            {"feature_brief": "Resume and finish #130", "resolves_id": "130", "resolves_origin": "known_bug"}
        )
    )

    assert result.get("is_error") is True
    assert impl_calls == []
    assert len(escalations) == 1
    assert escalations[0]["category"] == "stuck_loop"
    assert escalations[0]["tracking_issue"] == 130


def test_human_answered_stuck_issue_is_allowed_to_proceed(tmp_path, monkeypatch):
    session = _session(tmp_path, feature_branch="dev/stuck-branch", human_answer="do X", human_answer_issue=130)
    monkeypatch.setattr(session.mem, "in_progress_items", lambda: [])
    monkeypatch.setattr(
        session.mem, "run_ids_for",
        lambda ext: [f"run{i}" for i in range(brain.tools.MAX_RUNS_PER_TRACKING_ISSUE + 3)],
    )
    monkeypatch.setattr(session.mem, "record_code_complete", lambda *a, **k: {"issue_number": 130, "board_issue_number": None})
    monkeypatch.setattr(session.mem, "append_documentation", lambda *a, **k: None)
    monkeypatch.setattr(session.mem, "clear_known_bug", lambda *a, **k: None)
    monkeypatch.setattr(
        brain.tools, "_escalate_to_human",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not escalate a human-answered issue")),
    )

    async def fake_impl_run(*a, **k):
        return AgentResult(ok=True, text="done", json_data={"feature": "Fix #130"}, cost_usd=0.0, turns=1)

    monkeypatch.setattr(brain.implementation, "run", fake_impl_run)

    result = asyncio.run(
        _tool(session, "implement_feature").handler(
            {"feature_brief": "Resume #130 with the human's answer", "resolves_id": "130", "resolves_origin": "known_bug"}
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

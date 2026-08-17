"""assess_design_impact (agents/brain/tools.py) -- the conditional tool that
calls the Architecture Review Agent (agents/architecture_review.py) before
implement_feature, only when the Orchestrator judges a feature brief
architecturally significant. Not called on every implement_feature -- see
agents/brain/prompts.py step 4.

No real LLM call: architecture_review.run is monkeypatched.
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


def test_assess_design_impact_calls_architecture_review_run_with_the_brief_and_codebase_summary(tmp_path, monkeypatch):
    _patch_registry(monkeypatch)
    session = _session(tmp_path)

    captured = {}

    async def fake_run(repo, objective, feature_brief, codebase_summary):
        captured.update(repo=repo, objective=objective, feature_brief=feature_brief, codebase_summary=codebase_summary)
        return AgentResult(ok=True, text="risk_level: low, proceed", json_data={"risk_level": "low"}, cost_usd=0.01, turns=2)

    monkeypatch.setattr(brain.architecture_review, "run", fake_run)

    result = asyncio.run(_tool(session, "assess_design_impact").handler({"feature_brief": "add a users table"}))

    assert result.get("is_error") is not True
    assert captured == {
        "repo": session.repo, "objective": session.objective,
        "feature_brief": "add a users table", "codebase_summary": session.cb_summary,
    }
    assert "risk_level: low, proceed" in result["content"][0]["text"]


def test_assess_design_impact_requires_codebase_summary_first(tmp_path):
    session = _session(tmp_path, cb_summary=None)

    result = asyncio.run(_tool(session, "assess_design_impact").handler({"feature_brief": "add a users table"}))

    assert result.get("is_error") is True
    assert "understand_codebase" in result["content"][0]["text"]


def test_assess_design_impact_failure_calls_record_failure(tmp_path, monkeypatch):
    _patch_registry(monkeypatch)
    session = _session(tmp_path)

    async def failing_run(*a, **k):
        return AgentResult(ok=False, text="could not assess", json_data=None, cost_usd=0.01, turns=1)

    monkeypatch.setattr(brain.architecture_review, "run", failing_run)

    result = asyncio.run(_tool(session, "assess_design_impact").handler({"feature_brief": "add a users table"}))

    assert result.get("is_error") is True
    assert session.tool_failure_counts.get("assess_design_impact", 0) == 1


def test_tools_for_places_assess_design_impact_between_discover_opportunities_and_implement_feature(tmp_path):
    names = [t.name for t in brain._tools_for(_session(tmp_path))]
    assert names.index("discover_opportunities") < names.index("assess_design_impact") < names.index("implement_feature")

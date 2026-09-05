"""deploy_pre_prod (agents/brain/tools.py) routing a structured
HUMAN_INPUT_REQUIRED result from deployment.deploy_pre_prod through
Memory.record_known_bug(needs_human=True), instead of the normal
ok/failed branching. See agents/deployment.py's PRE_PROD_SYSTEM_PROMPT /
PRE_PROD_CI_CD_SYSTEM_PROMPT for the prompt-side shape this responds to.

No real LLM call: deployment.deploy_pre_prod is monkeypatched.
"""

import asyncio
from pathlib import Path

import pytest

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
        tests_passed=True,
        feature_branch="dev/some-branch",
    )
    defaults.update(overrides)
    return brain.OrchestratorSession(**defaults)


def _tool(session, name):
    tools = brain._tools_for(session)
    return next(t for t in tools if t.name == name)


def test_deploy_pre_prod_routes_human_input_required_through_record_known_bug(tmp_path, monkeypatch):
    session = _session(tmp_path)

    async def fake_deploy(*a, **k):
        return AgentResult(
            ok=True, text="deploy agent stopped",
            json_data={
                "status": "HUMAN_INPUT_REQUIRED",
                "reason": "Firebase config looks broken -- points at a real credential issue.",
                "question": "Which Firebase project should pre-prod deploy to?",
                "options": ["staging-a", "staging-b"],
            },
            cost_usd=0.01, turns=2,
        )

    monkeypatch.setattr(brain.deployment, "deploy_pre_prod", fake_deploy)
    known_bug_calls = []
    monkeypatch.setattr(
        session.mem,
        "record_known_bug",
        lambda run_id, severity, diagnosis, proposed_fix, **k: known_bug_calls.append((severity, diagnosis, k)),
    )

    result = asyncio.run(_tool(session, "deploy_pre_prod").handler({}))

    assert result.get("is_error") is True
    assert len(known_bug_calls) == 1
    severity, diagnosis, kwargs = known_bug_calls[0]
    assert kwargs["needs_human"] is True
    assert kwargs.get("blocking_agentra", False) is False
    assert "Firebase config looks broken" in diagnosis
    assert "Which Firebase project should pre-prod deploy to?" in diagnosis
    assert "staging-a" in diagnosis and "staging-b" in diagnosis
    assert session.deployed_to_pre_prod is False
    assert session.pre_prod_url is None


def test_deploy_pre_prod_human_input_required_does_not_touch_the_failure_counter(tmp_path, monkeypatch):
    session = _session(tmp_path)

    async def fake_deploy(*a, **k):
        return AgentResult(ok=True, text="stopped", json_data={"status": "HUMAN_INPUT_REQUIRED", "reason": "needs a call"}, cost_usd=0.0, turns=1)

    monkeypatch.setattr(brain.deployment, "deploy_pre_prod", fake_deploy)
    monkeypatch.setattr(session.mem, "record_known_bug", lambda *a, **k: None)
    monkeypatch.setattr(
        session.mem, "record_failure", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not be called"))
    )

    asyncio.run(_tool(session, "deploy_pre_prod").handler({}))

    assert session.tool_failure_counts.get("deploy_pre_prod", 0) == 0

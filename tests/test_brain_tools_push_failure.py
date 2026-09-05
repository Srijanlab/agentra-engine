"""GitHub issue #78: agents/brain/tools.py must not let a feature whose
push_branch() call failed (even after retries) reach deploy_pre_prod or be
stamped record_code_complete -- a deterministic, per-branch flag
(OrchestratorSession.push_failed_branches / check_push_failure), not
LLM-orchestrator vigilance over result.text, is what enforces this.

No real LLM call: every sub-agent's run() is monkeypatched, exactly like
test_brain_tools_auth_failure.py.
"""

import asyncio
from pathlib import Path

import pytest

from agentra import registry
from agentra.agents import brain
from agentra.agents.base import AgentResult
from agentra.environments import EnvironmentConfig
from agentra.memory import Memory


def _push_failed_result(**overrides) -> AgentResult:
    defaults = dict(
        ok=False,
        text="Implemented the thing.\n\n[agentra] Could not push feature branch 'dev/x' to GitHub after 3 "
        "attempts (work is committed locally only, NOT confirmed durable on GitHub): simulated push failure",
        json_data={"status": "implemented", "feature": "Add login"},
        cost_usd=0.01,
        turns=2,
        push_failed=True,
    )
    defaults.update(overrides)
    return AgentResult(**defaults)


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
    monkeypatch.setattr(registry, "record_run", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def _no_slack(monkeypatch):
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_HUMAN_INPUT_CHANNEL", raising=False)


def test_implement_feature_reports_failure_and_marks_the_branch_when_push_fails(tmp_path, monkeypatch):
    """impl.ok=False (set by implementation.run() itself once retries are
    exhausted) must route this to failure handling, not record_code_complete, and
    the branch must be recorded as push-failed for later gating."""
    _patch_registry(monkeypatch)
    session = _session(tmp_path)

    async def fake_requirements_run(*a, **k):
        return AgentResult(ok=False, text="no spec", json_data=None, cost_usd=0.0, turns=0)

    async def fake_implementation_run(*a, **k):
        return _push_failed_result()

    monkeypatch.setattr(brain.requirements, "run", fake_requirements_run)
    monkeypatch.setattr(brain.implementation, "run", fake_implementation_run)
    shipped_calls = []
    monkeypatch.setattr(session.mem, "record_code_complete", lambda *a, **k: shipped_calls.append(a) or None)
    failure_calls = []
    monkeypatch.setattr(session.mem, "record_failure", lambda *a, **k: failure_calls.append(a))

    result = asyncio.run(_tool(session, "implement_feature").handler({"feature_brief": "Add login", "resolves_origin": "new"}))

    assert result.get("is_error") is True
    assert shipped_calls == []
    assert len(failure_calls) == 1
    assert session.feature_branch in session.push_failed_branches
    # Counts toward MAX_CONSECUTIVE_TOOL_FAILURES the same as any other
    # implementation content failure (documented choice, GitHub issue #78 point 6).
    assert session.tool_failure_counts.get("implement_feature", 0) == 1


def test_implement_feature_refuses_record_code_complete_even_if_ok_is_somehow_true(tmp_path, monkeypatch):
    """Defense in depth: even if impl.ok ends up True for a result that still
    reports push_failed=True, the dedicated per-branch flag (not impl.ok
    alone) must block record_code_complete."""
    _patch_registry(monkeypatch)
    session = _session(tmp_path)

    async def fake_requirements_run(*a, **k):
        return AgentResult(ok=False, text="no spec", json_data=None, cost_usd=0.0, turns=0)

    async def fake_implementation_run(*a, **k):
        return _push_failed_result(ok=True)

    monkeypatch.setattr(brain.requirements, "run", fake_requirements_run)
    monkeypatch.setattr(brain.implementation, "run", fake_implementation_run)
    shipped_calls = []
    monkeypatch.setattr(session.mem, "record_code_complete", lambda *a, **k: shipped_calls.append(a) or None)

    result = asyncio.run(_tool(session, "implement_feature").handler({"feature_brief": "Add login", "resolves_origin": "new"}))

    assert result.get("is_error") is True
    assert shipped_calls == []
    assert session.feature_branch in session.push_failed_branches


def test_deploy_pre_prod_refuses_when_the_feature_branch_failed_to_push(tmp_path, monkeypatch):
    """Even with tests_passed=True, deploy_pre_prod must refuse to proceed
    for a branch flagged push-failed -- not rely on the orchestrator noticing
    the earlier implement_feature failure text."""
    _patch_registry(monkeypatch)
    session = _session(tmp_path, feature_branch="dev/push-failed-branch", tests_passed=True)
    session.mark_push_failed("dev/push-failed-branch")

    called = {"merge": False}

    async def fake_merge(*a, **k):
        called["merge"] = True
        return AgentResult(ok=True, text="merged", json_data={"status": "skipped_light"}, cost_usd=0.0, turns=0)

    monkeypatch.setattr(brain.deployment, "merge_to_pre_prod_only", fake_merge)
    monkeypatch.setattr(brain.tools.change_risk, "classify_change", lambda *a, **k: "trivial")
    monkeypatch.setattr("agentra.agents.git_ops.fetch_ref", lambda *a, **k: None)

    result = asyncio.run(_tool(session, "deploy_pre_prod").handler({}))

    assert result.get("is_error") is True
    assert "failed to push" in result["content"][0]["text"]
    assert called["merge"] is False
    assert session.deployed_to_pre_prod is False


def test_deploy_pre_prod_proceeds_normally_for_an_unaffected_branch(tmp_path, monkeypatch):
    """Sanity check: the new gate is scoped to the specific failed branch,
    not a blanket session-wide stop -- an unrelated/healthy branch must still
    be deployable."""
    _patch_registry(monkeypatch)
    session = _session(tmp_path, feature_branch="dev/healthy-branch", tests_passed=True)
    session.mark_push_failed("dev/some-other-branch")

    async def fake_merge(*a, **k):
        return AgentResult(ok=True, text="merged", json_data={"status": "skipped_light"}, cost_usd=0.0, turns=0)

    monkeypatch.setattr(brain.deployment, "merge_to_pre_prod_only", fake_merge)
    monkeypatch.setattr(brain.tools.change_risk, "classify_change", lambda *a, **k: "trivial")
    monkeypatch.setattr("agentra.agents.git_ops.fetch_ref", lambda *a, **k: None)

    result = asyncio.run(_tool(session, "deploy_pre_prod").handler({}))

    assert result.get("is_error") is not True
    assert session.deployed_to_pre_prod is True

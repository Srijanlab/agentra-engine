"""agents/brain/tools.py::_check_auth_failure (GitHub issue #42) -- a
Claude Code CLI auth/login failure (AgentResult.auth_failure, set distinctly
by agents/base.py's run_agent -- see test_run_agent_auth_failure.py for that
layer) must be detected and escalated the same way no matter which of the
nine tool-wrapped agents produced it, not just the top-level orchestrator's
own query() call (test_brain_autonomous_cycle_auth_failure.py covers that).

Covers, per call site: the tool short-circuits with is_error=True instead of
its normal failure handling, files a needs_human/blocking_agentra bug (via
Memory.record_failure -- which also sends the Slack escalation, see
test_failure_triage.py), and ends the *whole cycle* immediately
(hard_stop_reason set) rather than only counting as one ordinary tool
failure -- bypassing MAX_CONSECUTIVE_TOOL_FAILURES entirely, since a second
attempt (this tool or any other) would just hit the identical missing
credentials.

No real LLM call: every sub-agent's run()/run_cached() is monkeypatched.
"""

import asyncio
from pathlib import Path

import pytest

from agentra import registry
from agentra.agents import brain
from agentra.agents.base import AgentResult
from agentra.environments import EnvironmentConfig
from agentra.memory import Memory

_AUTH_FAILURE_TEXT = (
    "Claude Code authentication failure -- the CLI reported it is not usable on this runner "
    "(Claude Code returned an error result: Not logged in · Please run /login (exit code: 1)). "
    "This needs a human to run `claude /login` or otherwise refresh credentials here; retrying "
    "automatically will not help."
)


def _auth_failure_result() -> AgentResult:
    return AgentResult(ok=False, text=_AUTH_FAILURE_TEXT, json_data=None, cost_usd=0.0, turns=0, auth_failure=True)


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
    monkeypatch.setattr(registry, "record_run", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def _no_slack(monkeypatch):
    # Unconfigured in every test here (no SLACK_BOT_TOKEN/SLACK_HUMAN_INPUT_CHANNEL)
    # -- notify_human_input_required silently no-ops, exercised for real
    # rather than mocked, matching connectors/slack.py's own "never raise"
    # contract. Dedicated Slack-call assertions live in test_failure_triage.py.
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_HUMAN_INPUT_CHANNEL", raising=False)


def _bug_capture(monkeypatch, session):
    calls = []
    monkeypatch.setattr(
        session.mem, "record_known_bug",
        lambda run_id, severity, diagnosis, proposed_fix, **k: calls.append(k) or 1,
    )
    return calls


def test_understand_codebase_escalates_on_auth_failure(tmp_path, monkeypatch):
    _patch_registry(monkeypatch)
    session = _session(tmp_path)
    bug_calls = _bug_capture(monkeypatch, session)
    async def fake_run_cached(*a, **k):
        return _auth_failure_result()

    monkeypatch.setattr(brain.codebase, "run_cached", fake_run_cached)

    result = asyncio.run(_tool(session, "understand_codebase").handler({}))

    assert result.get("is_error") is True
    assert len(bug_calls) == 1
    assert bug_calls[0]["needs_human"] is True
    assert bug_calls[0]["blocking_agentra"] is True
    assert session.hard_stop_reason is not None
    assert "run /login" in session.hard_stop_reason
    # Not counted as an ordinary tool failure -- MAX_CONSECUTIVE_TOOL_FAILURES
    # never gets a chance to matter, the cycle stops on the very first hit.
    assert session.tool_failure_counts.get("understand_codebase", 0) == 0


def test_discover_opportunities_escalates_on_auth_failure(tmp_path, monkeypatch):
    _patch_registry(monkeypatch)
    session = _session(tmp_path)
    _bug_capture(monkeypatch, session)

    async def fake_run(*a, **k):
        return _auth_failure_result()

    monkeypatch.setattr(brain.discovery, "run", fake_run)

    result = asyncio.run(_tool(session, "discover_opportunities").handler({}))

    assert result.get("is_error") is True
    assert session.hard_stop_reason is not None


def test_assess_design_impact_escalates_on_auth_failure(tmp_path, monkeypatch):
    _patch_registry(monkeypatch)
    session = _session(tmp_path)
    _bug_capture(monkeypatch, session)

    async def fake_run(*a, **k):
        return _auth_failure_result()

    monkeypatch.setattr(brain.architecture_review, "run", fake_run)

    result = asyncio.run(_tool(session, "assess_design_impact").handler({"feature_brief": "Add search"}))

    assert result.get("is_error") is True
    assert session.hard_stop_reason is not None


def test_implement_feature_escalates_on_requirements_auth_failure(tmp_path, monkeypatch):
    _patch_registry(monkeypatch)
    session = _session(tmp_path)
    _bug_capture(monkeypatch, session)

    async def fake_requirements_run(*a, **k):
        return _auth_failure_result()

    called_implementation = {"called": False}

    async def fake_implementation_run(*a, **k):
        called_implementation["called"] = True
        return AgentResult(ok=True, text="ok", json_data={"status": "implemented"}, cost_usd=0.01, turns=1)

    monkeypatch.setattr(brain.requirements, "run", fake_requirements_run)
    monkeypatch.setattr(brain.implementation, "run", fake_implementation_run)

    result = asyncio.run(_tool(session, "implement_feature").handler({"feature_brief": "Add login"}))

    assert result.get("is_error") is True
    assert session.hard_stop_reason is not None
    # Fails fast -- never proceeds to spend an Implementation Agent turn on
    # top of a broken Claude Code session.
    assert called_implementation["called"] is False


def test_implement_feature_escalates_on_implementation_auth_failure_and_resumes_reuse_original_branch(tmp_path, monkeypatch):
    """The tracking-issue variant: previously this fell through to
    record_failure_on_issue (a plain, non-gating comment, no needs_human/
    blocking_agentra label, no Slack) -- which would let every future cycle
    keep re-resuming the same branch and re-hitting the identical auth
    failure forever. Must now escalate exactly like the no-tracking-issue
    case."""
    _patch_registry(monkeypatch)
    session = _session(tmp_path)
    bug_calls = _bug_capture(monkeypatch, session)

    async def fake_requirements_run(*a, **k):
        return AgentResult(ok=False, text="no spec", json_data=None, cost_usd=0.0, turns=0)

    async def fake_implementation_run(*a, **k):
        return _auth_failure_result()

    monkeypatch.setattr(brain.requirements, "run", fake_requirements_run)
    monkeypatch.setattr(brain.implementation, "run", fake_implementation_run)
    on_issue_calls = []
    monkeypatch.setattr(
        session.mem, "record_failure_on_issue",
        lambda *a, **k: on_issue_calls.append(a),
    )

    result = asyncio.run(
        _tool(session, "implement_feature").handler(
            {"feature_brief": "Add login", "resolves_origin": "known_bug", "resolves_id": "17"}
        )
    )

    assert result.get("is_error") is True
    assert len(bug_calls) == 1
    assert bug_calls[0]["needs_human"] is True
    assert bug_calls[0]["blocking_agentra"] is True
    # The old, non-gating "just comment on the tracking issue" path must not
    # have fired for this -- the auth failure is handled by the shared,
    # gating record_failure path instead.
    assert on_issue_calls == []


def test_run_local_tests_escalates_on_auth_failure(tmp_path, monkeypatch):
    _patch_registry(monkeypatch)
    session = _session(tmp_path, feature_branch="dev/abc123-fix-thing")
    _bug_capture(monkeypatch, session)

    async def fake_run_local(*a, **k):
        return _auth_failure_result()

    monkeypatch.setattr(brain.testing, "run_local", fake_run_local)

    result = asyncio.run(_tool(session, "run_local_tests").handler({}))

    assert result.get("is_error") is True
    assert session.hard_stop_reason is not None


def test_run_local_tests_self_heal_escalates_on_auth_failure(tmp_path, monkeypatch):
    _patch_registry(monkeypatch)
    session = _session(tmp_path, feature_branch="dev/abc123-fix-thing")
    _bug_capture(monkeypatch, session)

    async def fake_run_local(*a, **k):
        return AgentResult(
            ok=True, text="```json\n{}\n```", json_data={"status": "fail", "failed_tests": ["test_x"]},
            cost_usd=0.01, turns=1,
        )

    async def fake_implementation_run(*a, **k):
        return _auth_failure_result()

    monkeypatch.setattr(brain.testing, "run_local", fake_run_local)
    monkeypatch.setattr(brain.implementation, "run", fake_implementation_run)

    result = asyncio.run(_tool(session, "run_local_tests").handler({}))

    assert result.get("is_error") is True
    assert session.hard_stop_reason is not None


def test_deploy_pre_prod_trivial_escalates_on_auth_failure(tmp_path, monkeypatch):
    _patch_registry(monkeypatch)
    session = _session(
        tmp_path, feature_branch="dev/abc123-fix-thing", tests_passed=True,
    )
    _bug_capture(monkeypatch, session)
    monkeypatch.setattr(brain.tools.change_risk, "classify_change", lambda *a, **k: "trivial")
    monkeypatch.setattr("agentra.agents.git_ops.fetch_ref", lambda *a, **k: None)

    async def fake_merge(*a, **k):
        return _auth_failure_result()

    monkeypatch.setattr(brain.deployment, "merge_to_pre_prod_only", fake_merge)

    result = asyncio.run(_tool(session, "deploy_pre_prod").handler({}))

    assert result.get("is_error") is True
    assert session.hard_stop_reason is not None


def test_verify_pre_prod_escalates_on_auth_failure(tmp_path, monkeypatch):
    _patch_registry(monkeypatch)
    session = _session(tmp_path, pre_prod_url="https://preview.example.com")
    _bug_capture(monkeypatch, session)

    async def fake_run_pre_prod(*a, **k):
        return _auth_failure_result()

    monkeypatch.setattr(brain.testing, "run_pre_prod", fake_run_pre_prod)

    result = asyncio.run(_tool(session, "verify_pre_prod").handler({}))

    assert result.get("is_error") is True
    assert session.hard_stop_reason is not None


def test_assess_feedback_escalates_on_auth_failure(tmp_path, monkeypatch):
    _patch_registry(monkeypatch)
    session = _session(tmp_path, current_feature="Add login")
    _bug_capture(monkeypatch, session)

    async def fake_feedback_run(*a, **k):
        return _auth_failure_result()

    monkeypatch.setattr(brain.feedback, "run", fake_feedback_run)

    result = asyncio.run(_tool(session, "assess_feedback").handler({}))

    assert result.get("is_error") is True
    assert session.hard_stop_reason is not None


def test_spawn_custom_agent_escalates_on_auth_failure(tmp_path, monkeypatch):
    _patch_registry(monkeypatch)
    session = _session(tmp_path)
    _bug_capture(monkeypatch, session)

    async def fake_spawn(*a, **k):
        return _auth_failure_result()

    monkeypatch.setattr(brain.tools, "spawn_generic", fake_spawn)

    result = asyncio.run(
        _tool(session, "spawn_custom_agent").handler(
            {"task_name": "audit", "prompt": "look around", "system_prompt": "you are an agent", "allowed_tools": "Read"}
        )
    )

    assert result.get("is_error") is True
    assert session.hard_stop_reason is not None


def test_auth_failure_hard_stop_blocks_every_subsequent_tool_call_this_cycle(tmp_path, monkeypatch):
    """The whole point of ending the cycle immediately (not waiting for
    MAX_CONSECUTIVE_TOOL_FAILURES) -- once one tool hits an auth failure,
    every other tool call this same cycle must refuse immediately via
    check_hard_stop, never spending another agent turn."""
    _patch_registry(monkeypatch)
    session = _session(tmp_path)
    _bug_capture(monkeypatch, session)
    async def fake_run_cached(*a, **k):
        return _auth_failure_result()

    monkeypatch.setattr(brain.codebase, "run_cached", fake_run_cached)

    asyncio.run(_tool(session, "understand_codebase").handler({}))
    assert session.hard_stop_reason is not None

    called = {"discovery": False}

    async def fake_discovery_run(*a, **k):
        called["discovery"] = True
        return AgentResult(ok=True, text="ok", json_data={"opportunities": []}, cost_usd=0.01, turns=1)

    monkeypatch.setattr(brain.discovery, "run", fake_discovery_run)

    second = asyncio.run(_tool(session, "discover_opportunities").handler({}))

    assert second.get("is_error") is True
    assert called["discovery"] is False

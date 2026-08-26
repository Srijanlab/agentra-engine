"""run_local_tests self-heals a failing local test suite once, automatically,
via Implementation Agent, instead of just gating deploy_pre_prod and leaving
the retry decision to the Orchestrator LLM -- deterministic, capped at
MAX_SELF_HEAL_ATTEMPTS, same control-flow-in-Python reasoning as this
module's other breakers (see brain.py's own comment on the loop).

The fix-up call is made with resume=True, which is load-bearing: it must
continue session.feature_branch's already-pushed commits, not
_checkout_feature_branch's non-resume path (which would fork a fresh branch
from pre_prod_branch's tip, silently discarding the feature the tests are
even failing against). A dedicated regression test below pins that.

No real LLM call: testing.run_local and implementation.run are both
monkeypatched, matching tests/test_incidental_findings.py's pattern.
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


def _test_result(status: str, **extra) -> AgentResult:
    data = {"status": status, **extra}
    return AgentResult(ok=True, text="```json\n{}\n```", json_data=data, cost_usd=0.01, turns=1)


def _tool(session, name):
    tools = brain._tools_for(session)
    return next(t for t in tools if t.name == name)


def _patch_registry(monkeypatch):
    monkeypatch.setattr(registry, "record_agent_step", lambda *a, **k: None)


def test_self_heals_a_failing_suite_and_passes_on_the_retest(tmp_path, monkeypatch):
    _patch_registry(monkeypatch)
    session = _session(tmp_path, feature_branch="dev/abc123-fix-thing")

    test_results = [
        _test_result("fail", failed_tests=["test_x"], lint_status="pass", typecheck_status="pass"),
        _test_result("pass", lint_status="pass", typecheck_status="pass"),
    ]

    async def fake_run_local(repo, cb_summary, mem=None, session_id=None):
        return test_results.pop(0)

    fix_calls = []

    async def fake_implementation_run(repo, objective, brief, cb_summary, env, feature_branch, resume=False, spec="", session_id=None, mem=None, run_id=None):
        fix_calls.append({"brief": brief, "feature_branch": feature_branch, "resume": resume})
        return AgentResult(ok=True, text="fixed it", json_data={"status": "implemented"}, cost_usd=0.02, turns=3)

    monkeypatch.setattr(brain.testing, "run_local", fake_run_local)
    monkeypatch.setattr(brain.implementation, "run", fake_implementation_run)

    result = asyncio.run(_tool(session, "run_local_tests").handler({}))

    assert result.get("is_error") is not True
    assert session.tests_passed is True
    assert len(fix_calls) == 1
    assert fix_calls[0]["feature_branch"] == "dev/abc123-fix-thing"
    # Load-bearing: must continue the already-pushed branch, not fork a fresh
    # one from pre_prod_branch (which would discard the feature being fixed).
    assert fix_calls[0]["resume"] is True
    assert "test_x" in fix_calls[0]["brief"]


def test_gives_up_after_max_self_heal_attempts_if_still_failing(tmp_path, monkeypatch):
    _patch_registry(monkeypatch)
    session = _session(tmp_path, feature_branch="dev/abc123-fix-thing")

    async def fake_run_local(repo, cb_summary, mem=None, session_id=None):
        return _test_result("fail", failed_tests=["test_x"], lint_status="pass", typecheck_status="pass")

    fix_calls = []

    async def fake_implementation_run(repo, objective, brief, cb_summary, env, feature_branch, resume=False, spec="", session_id=None, mem=None, run_id=None):
        fix_calls.append(1)
        return AgentResult(ok=True, text="tried", json_data={"status": "implemented"}, cost_usd=0.02, turns=3)

    monkeypatch.setattr(brain.testing, "run_local", fake_run_local)
    monkeypatch.setattr(brain.implementation, "run", fake_implementation_run)

    result = asyncio.run(_tool(session, "run_local_tests").handler({}))

    assert result.get("is_error") is True
    assert session.tests_passed is False
    # Bounded at MAX_SELF_HEAL_ATTEMPTS -- not an unbounded retry loop.
    assert len(fix_calls) == brain.MAX_SELF_HEAL_ATTEMPTS


def test_stops_retesting_if_the_fix_attempt_itself_fails(tmp_path, monkeypatch):
    _patch_registry(monkeypatch)
    session = _session(tmp_path, feature_branch="dev/abc123-fix-thing")

    async def fake_run_local(repo, cb_summary, mem=None, session_id=None):
        return _test_result("fail", failed_tests=["test_x"], lint_status="pass", typecheck_status="pass")

    retest_calls = []
    real_fake_run_local = fake_run_local

    async def counting_run_local(repo, cb_summary, mem=None, session_id=None):
        retest_calls.append(1)
        return await real_fake_run_local(repo, cb_summary, mem, session_id)

    async def failing_implementation_run(repo, objective, brief, cb_summary, env, feature_branch, resume=False, spec="", session_id=None, mem=None, run_id=None):
        return AgentResult(ok=False, text="could not fix it", json_data=None, cost_usd=0.02, turns=5)

    monkeypatch.setattr(brain.testing, "run_local", counting_run_local)
    monkeypatch.setattr(brain.implementation, "run", failing_implementation_run)

    asyncio.run(_tool(session, "run_local_tests").handler({}))

    # Only the original test run -- no point re-testing after a failed fix attempt.
    assert len(retest_calls) == 1


def test_does_not_self_heal_without_a_feature_branch(tmp_path, monkeypatch):
    # No feature_branch set (e.g. run_local_tests called without a prior
    # implement_feature this run) -- nothing to check out/commit a fix onto.
    _patch_registry(monkeypatch)
    session = _session(tmp_path)
    assert session.feature_branch is None

    async def fake_run_local(repo, cb_summary, mem=None, session_id=None):
        return _test_result("fail", failed_tests=["test_x"], lint_status="pass", typecheck_status="pass")

    monkeypatch.setattr(brain.testing, "run_local", fake_run_local)
    monkeypatch.setattr(
        brain.implementation, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not attempt a fix"))
    )

    result = asyncio.run(_tool(session, "run_local_tests").handler({}))

    assert result.get("is_error") is True
    assert session.tests_passed is False


def test_agent_execution_error_retries_the_test_run_not_a_bogus_fix(tmp_path, monkeypatch):
    # When the Testing Agent itself errors out (e.g. hits its own max_turns
    # budget), run_agent returns json_data=None and the raw exception text as
    # test.text -- there is no real failing-test list to hand to Implementation
    # Agent, so run_local_tests must retry the test run itself instead of
    # dispatching a "fix these failing tests" brief containing the raw
    # exception text.
    _patch_registry(monkeypatch)
    session = _session(tmp_path, feature_branch="dev/abc123-fix-thing")

    agent_error_text = (
        "agent turn raised: Claude Code returned an error result: "
        "Reached maximum number of turns (30) (exit code: 1)"
    )
    test_results = [
        AgentResult(ok=False, text=agent_error_text, json_data=None, cost_usd=0.01, turns=30),
        _test_result("pass", lint_status="pass", typecheck_status="pass"),
    ]

    async def fake_run_local(repo, cb_summary, mem=None, session_id=None):
        return test_results.pop(0)

    monkeypatch.setattr(brain.testing, "run_local", fake_run_local)
    monkeypatch.setattr(
        brain.implementation, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not attempt a fix"))
    )

    result = asyncio.run(_tool(session, "run_local_tests").handler({}))

    assert result.get("is_error") is not True
    assert session.tests_passed is True


def test_does_not_self_heal_when_the_suite_already_passes(tmp_path, monkeypatch):
    _patch_registry(monkeypatch)
    session = _session(tmp_path, feature_branch="dev/abc123-fix-thing")

    async def fake_run_local(repo, cb_summary, mem=None, session_id=None):
        return _test_result("pass", lint_status="pass", typecheck_status="pass")

    monkeypatch.setattr(brain.testing, "run_local", fake_run_local)
    monkeypatch.setattr(
        brain.implementation, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not attempt a fix"))
    )

    result = asyncio.run(_tool(session, "run_local_tests").handler({}))

    assert result.get("is_error") is not True
    assert session.tests_passed is True

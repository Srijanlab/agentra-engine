"""Part 2/2 of the deterministic infra-cost gate: agents/brain/tools.py's
implement_feature must not be allowed to run Implementation Agent on a
feature_brief whose architecture review (this cycle, for that exact brief)
says infra_cost_impact == "material", or risk_level == "high" with an
"infra" layer touched -- a real Python `if` check, not a prompt
instruction, mirroring the existing prod-promotion gate / cost-cap circuit
breaker pattern elsewhere in this codebase.

Also covers the coverage-gap closer: a feature_brief matching the
deterministic keyword heuristic (terraform, cloud run, min-instances, ...)
with no design review performed yet this cycle automatically gets one
before implementation is attempted, and per-brief-scoped review state
(session.design_reviews) never leaks between two different briefs handled
back-to-back in the same cycle.

No real LLM call: architecture_review.run/requirements.run/implementation.run
are all monkeypatched.
"""

import asyncio
from pathlib import Path

import pytest

from agentra import registry
from agentra.agents import brain
from agentra.agents.base import AgentResult
from agentra.environments import EnvironmentConfig
from agentra.memory import Memory


@pytest.fixture(autouse=True)
def _stub_requirements(monkeypatch):
    async def fake_run(*a, **k):
        return AgentResult(ok=False, text="stubbed -- no spec", json_data=None, cost_usd=0.0, turns=0)

    monkeypatch.setattr(brain.requirements, "run", fake_run)


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


def _patch_escalation_plumbing(monkeypatch, session, known_bug_calls: list, slack_calls: list):
    """Mocks everything _escalate_to_human touches so we can assert on the shape
    of the call (category, diagnosis) without hitting real GitHub/Slack."""
    from agentra import urls
    from agentra.connectors import slack

    def fake_record_known_bug(run_id, severity, diagnosis, proposed_fix, **k):
        known_bug_calls.append((severity, diagnosis, k))
        return 77

    monkeypatch.setattr(session.mem, "record_known_bug", fake_record_known_bug)
    monkeypatch.setattr(session.mem, "issue_html_url", lambda n: f"https://github.com/acme/app/issues/{n}")
    monkeypatch.setattr(session.mem, "record_human_input_context", lambda *a, **k: None)
    monkeypatch.setattr(urls, "dashboard_run_url", lambda run_id, app_name, **k: "https://dash/x")
    monkeypatch.setattr(slack, "notify_human_input_required", lambda **k: slack_calls.append(k) or True)


def _patch_successful_ship(monkeypatch, session):
    """Mocks everything implement_feature's happy path touches past implementation.run."""
    monkeypatch.setattr(session.mem, "record_shipped", lambda *a, **k: {"issue_number": None, "board_issue_number": None})
    monkeypatch.setattr(session.mem, "append_documentation", lambda *a, **k: None)


def _review_result(**overrides) -> AgentResult:
    defaults = dict(
        ok=True,
        text="```json\n{}\n```",
        json_data={
            "layers_touched": ["backend"],
            "risk_level": "low",
            "concerns": [],
            "recommendation": "proceed",
            "infra_cost_impact": "none",
        },
        cost_usd=0.01,
        turns=2,
    )
    defaults.update(overrides)
    return AgentResult(**defaults)


def test_material_infra_cost_impact_blocks_and_escalates(tmp_path, monkeypatch):
    _patch_registry(monkeypatch)
    known_bug_calls, slack_calls = [], []
    session = _session(tmp_path)
    _patch_escalation_plumbing(monkeypatch, session, known_bug_calls, slack_calls)

    async def fake_review_run(repo, objective, feature_brief, codebase_summary):
        return _review_result(
            json_data={
                "layers_touched": ["infra"],
                "risk_level": "medium",
                "concerns": ["Adds a new always-on Cloud Run service"],
                "recommendation": "proceed_with_caution",
                "infra_cost_impact": "material",
            }
        )

    monkeypatch.setattr(brain.architecture_review, "run", fake_review_run)

    impl_calls = []

    async def fake_impl_run(*a, **k):
        impl_calls.append((a, k))
        return AgentResult(ok=True, text="done", json_data={"feature": "Add worker"}, cost_usd=0.01, turns=2)

    monkeypatch.setattr(brain.implementation, "run", fake_impl_run)

    asyncio.run(_tool(session, "assess_design_impact").handler({"feature_brief": "Add a background worker"}))
    result = asyncio.run(_tool(session, "implement_feature").handler({"feature_brief": "Add a background worker"}))

    assert result.get("is_error") is True
    assert impl_calls == []  # implementation.run must never have been called
    assert len(known_bug_calls) == 1
    _, diagnosis, kwargs = known_bug_calls[0]
    assert "Category: infra_cost" in diagnosis
    assert kwargs["needs_human"] is True
    assert len(slack_calls) == 1
    assert session.waiting_for_human is True
    assert session.current_feature is None  # nothing shipped


def test_high_risk_with_infra_layer_touched_blocks_even_without_material_impact(tmp_path, monkeypatch):
    _patch_registry(monkeypatch)
    known_bug_calls, slack_calls = [], []
    session = _session(tmp_path)
    _patch_escalation_plumbing(monkeypatch, session, known_bug_calls, slack_calls)

    async def fake_review_run(repo, objective, feature_brief, codebase_summary):
        return _review_result(
            json_data={
                "layers_touched": ["backend", "infra"],
                "risk_level": "high",
                "concerns": ["Breaking API change plus a new min-instances setting"],
                "recommendation": "needs_narrower_scope",
                "infra_cost_impact": "moderate",
            }
        )

    monkeypatch.setattr(brain.architecture_review, "run", fake_review_run)

    impl_calls = []

    async def fake_impl_run(*a, **k):
        impl_calls.append((a, k))
        return AgentResult(ok=True, text="done", json_data={"feature": "X"}, cost_usd=0.01, turns=2)

    monkeypatch.setattr(brain.implementation, "run", fake_impl_run)

    asyncio.run(_tool(session, "assess_design_impact").handler({"feature_brief": "Rework the checkout flow"}))
    result = asyncio.run(_tool(session, "implement_feature").handler({"feature_brief": "Rework the checkout flow"}))

    assert result.get("is_error") is True
    assert impl_calls == []
    assert len(known_bug_calls) == 1
    _, diagnosis, kwargs = known_bug_calls[0]
    assert "Category: infra_cost" in diagnosis
    assert session.waiting_for_human is True


def test_low_risk_none_impact_proceeds_normally_with_no_escalation(tmp_path, monkeypatch):
    _patch_registry(monkeypatch)
    known_bug_calls, slack_calls = [], []
    session = _session(tmp_path)
    _patch_escalation_plumbing(monkeypatch, session, known_bug_calls, slack_calls)
    _patch_successful_ship(monkeypatch, session)

    async def fake_review_run(repo, objective, feature_brief, codebase_summary):
        return _review_result()  # risk_level=low, infra_cost_impact=none

    monkeypatch.setattr(brain.architecture_review, "run", fake_review_run)

    async def fake_impl_run(*a, **k):
        return AgentResult(ok=True, text="done", json_data={"feature": "Tweak copy"}, cost_usd=0.01, turns=2)

    monkeypatch.setattr(brain.implementation, "run", fake_impl_run)

    asyncio.run(_tool(session, "assess_design_impact").handler({"feature_brief": "Tweak the button copy"}))
    result = asyncio.run(_tool(session, "implement_feature").handler({"feature_brief": "Tweak the button copy"}))

    assert result.get("is_error") is not True
    assert "Implemented and committed" in result["content"][0]["text"]
    assert known_bug_calls == []
    assert slack_calls == []
    assert session.waiting_for_human is False
    assert session.current_feature == "Tweak copy"


def test_no_review_and_no_keyword_match_proceeds_normally(tmp_path, monkeypatch):
    """No assess_design_impact call, and the brief doesn't match the keyword
    heuristic either -- implement_feature proceeds exactly as it did before
    this gate existed."""
    _patch_registry(monkeypatch)
    known_bug_calls, slack_calls = [], []
    session = _session(tmp_path)
    _patch_escalation_plumbing(monkeypatch, session, known_bug_calls, slack_calls)
    _patch_successful_ship(monkeypatch, session)

    review_calls = []

    async def fake_review_run(*a, **k):
        review_calls.append(a)
        return _review_result()

    monkeypatch.setattr(brain.architecture_review, "run", fake_review_run)

    async def fake_impl_run(*a, **k):
        return AgentResult(ok=True, text="done", json_data={"feature": "Fix typo"}, cost_usd=0.01, turns=2)

    monkeypatch.setattr(brain.implementation, "run", fake_impl_run)

    result = asyncio.run(_tool(session, "implement_feature").handler({"feature_brief": "Fix a typo in the footer"}))

    assert result.get("is_error") is not True
    assert review_calls == []  # no automatic review triggered, no keyword match
    assert known_bug_calls == []
    assert session.waiting_for_human is False


def test_keyword_heuristic_auto_triggers_a_design_review_before_implementation(tmp_path, monkeypatch):
    _patch_registry(monkeypatch)
    known_bug_calls, slack_calls = [], []
    session = _session(tmp_path)
    _patch_escalation_plumbing(monkeypatch, session, known_bug_calls, slack_calls)
    _patch_successful_ship(monkeypatch, session)

    review_calls = []

    async def fake_review_run(repo, objective, feature_brief, codebase_summary):
        review_calls.append(feature_brief)
        return _review_result()  # non-blocking result

    monkeypatch.setattr(brain.architecture_review, "run", fake_review_run)

    impl_calls = []

    async def fake_impl_run(*a, **k):
        impl_calls.append(a)
        return AgentResult(ok=True, text="done", json_data={"feature": "Add integration"}, cost_usd=0.01, turns=2)

    monkeypatch.setattr(brain.implementation, "run", fake_impl_run)

    brief = "Provision a new Cloud Run service via Terraform for the worker"
    result = asyncio.run(_tool(session, "implement_feature").handler({"feature_brief": brief}))

    # assess_design_impact was never explicitly called -- the keyword heuristic
    # (terraform, cloud run) forced one automatically, before implementation.run.
    assert review_calls == [brief]
    assert brief in session.design_reviews
    assert len(impl_calls) == 1
    assert result.get("is_error") is not True


def test_design_review_state_does_not_leak_between_two_different_briefs_in_one_cycle(tmp_path, monkeypatch):
    """Brief A's material review is stored (session.design_reviews) *before* brief B is
    handled, so if the gate incorrectly looked at "any review this cycle" instead of the
    review for this exact brief, brief B would be wrongly blocked too. Once brief A is
    escalated the session halts (check_hard_stop) -- matching every other HUMAN_INPUT_REQUIRED
    path in this codebase -- so brief B is handled first here to prove the two briefs' state
    stays independent."""
    _patch_registry(monkeypatch)
    known_bug_calls, slack_calls = [], []
    session = _session(tmp_path)
    _patch_escalation_plumbing(monkeypatch, session, known_bug_calls, slack_calls)
    _patch_successful_ship(monkeypatch, session)

    brief_a = "Add a new always-on payments microservice"
    brief_b = "Fix the footer copyright year"

    async def fake_review_run(repo, objective, feature_brief, codebase_summary):
        assert feature_brief == brief_a  # only brief A is ever reviewed in this test
        return _review_result(
            json_data={
                "layers_touched": ["infra", "backend"],
                "risk_level": "high",
                "concerns": ["New always-on service"],
                "recommendation": "needs_narrower_scope",
                "infra_cost_impact": "material",
            }
        )

    monkeypatch.setattr(brain.architecture_review, "run", fake_review_run)

    impl_calls = []

    async def fake_impl_run(*a, **k):
        impl_calls.append(a)
        feature_brief = a[2]
        return AgentResult(ok=True, text="done", json_data={"feature": feature_brief}, cost_usd=0.01, turns=2)

    monkeypatch.setattr(brain.implementation, "run", fake_impl_run)

    # Brief A's material review is performed and stored first.
    asyncio.run(_tool(session, "assess_design_impact").handler({"feature_brief": brief_a}))
    assert session.design_reviews[brief_a]["infra_cost_impact"] == "material"

    # Brief B: a completely different, low-risk brief with no review of its own and no
    # keyword match -- must proceed to implementation normally, unaffected by brief A's
    # material review already sitting in session.design_reviews.
    result_b = asyncio.run(_tool(session, "implement_feature").handler({"feature_brief": brief_b}))
    assert result_b.get("is_error") is not True
    assert "Implemented and committed" in result_b["content"][0]["text"]
    assert len(impl_calls) == 1
    assert impl_calls[0][2] == brief_b
    assert brief_b not in session.design_reviews
    assert known_bug_calls == []
    assert session.waiting_for_human is False

    # Brief A: blocked/escalated by its own material review, independent of brief B.
    result_a = asyncio.run(_tool(session, "implement_feature").handler({"feature_brief": brief_a}))
    assert result_a.get("is_error") is True
    assert len(known_bug_calls) == 1
    assert session.waiting_for_human is True

"""agents/brain/tools.py::implement_feature's HUMAN_INPUT_REQUIRED path
(GitHub issue #34) -- implement_feature is THE flow with an existing resume
contract (feature_branch + session_id), so its escalation must go through
the same _escalate_to_human helper discover_opportunities already used
(see test_discover_opportunities.py), not a bare record_known_bug call:
resume-correlation data (including the tracking issue, so a later answer
gets woven back into the right spec), a best-effort Slack notification, and
marking the run waiting_for_human immediately.

Also covers weaving session.human_answer into the resumed spec text
(_format_spec) when this implement_feature call is for the exact tracking
issue that answer belongs to -- and NOT weaving it in for an unrelated
issue, or a second call after it's already been consumed once.

No real LLM call: implementation.run/requirements.run are monkeypatched.
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


def _patch_slack_and_urls(monkeypatch, calls: list):
    from agentra import urls
    from agentra.connectors import slack

    monkeypatch.setattr(urls, "dashboard_run_url", lambda run_id, app_name, **k: f"https://dash/{app_name}/{run_id}")
    monkeypatch.setattr(slack, "notify_human_input_required", lambda **k: calls.append(k) or True)


def _human_input_required_result(**overrides) -> AgentResult:
    defaults = dict(
        ok=True,
        text="blocked",
        json_data={
            "status": "HUMAN_INPUT_REQUIRED",
            "reason": "Two auth providers are equally valid, no clear default.",
            "question": "Should we use OAuth or magic links?",
            "options": ["oauth", "magic_link"],
        },
        cost_usd=0.02,
        turns=4,
        session_id="sess-abc123",
    )
    defaults.update(overrides)
    return AgentResult(**defaults)


def test_implement_feature_human_input_required_escalates_via_shared_helper(tmp_path, monkeypatch):
    """With a tracking issue (#17, via resolves_id), the blocking question lands directly on
    that issue -- never a separate needs_human issue (confirmed live: issues #79, #80, #81,
    same interrupted item, three separate escalation issues)."""
    _patch_registry(monkeypatch)
    slack_calls: list = []
    _patch_slack_and_urls(monkeypatch, slack_calls)
    session = _session(tmp_path)

    async def fake_impl_run(*a, **k):
        return _human_input_required_result()

    monkeypatch.setattr(brain.implementation, "run", fake_impl_run)

    known_bug_calls = []

    def fake_record_known_bug(run_id, severity, diagnosis, proposed_fix, **k):
        known_bug_calls.append((severity, diagnosis, k))
        return 99  # only used if this call is ever reached -- it shouldn't be

    escalate_existing_calls = []

    def fake_escalate_existing_issue(issue_number, run_id, full_diagnosis):
        escalate_existing_calls.append((issue_number, full_diagnosis))

    monkeypatch.setattr(session.mem, "record_known_bug", fake_record_known_bug)
    monkeypatch.setattr(session.mem, "escalate_existing_issue", fake_escalate_existing_issue)
    monkeypatch.setattr(session.mem, "issue_html_url", lambda n: f"https://github.com/acme/app/issues/{n}")
    context_calls = []
    monkeypatch.setattr(
        session.mem,
        "record_human_input_context",
        lambda issue_number, **k: context_calls.append((issue_number, k)),
    )

    result = asyncio.run(
        _tool(session, "implement_feature").handler(
            {"feature_brief": "Add login", "resolves_origin": "known_bug", "resolves_id": "17"}
        )
    )

    assert result.get("is_error") is True
    assert known_bug_calls == []  # never a separate needs_human issue
    assert len(escalate_existing_calls) == 1
    issue_number, diagnosis = escalate_existing_calls[0]
    assert issue_number == 17  # the tracking issue itself
    assert "OAuth or magic links" in diagnosis
    # The structured run record must carry the escalation category too, not
    # just the GitHub issue body -- so it's queryable/chartable by reason.
    assert session.human_input["category"] == "implementation"

    # Resume-correlation data stamped on the tracking issue (#17) itself.
    assert len(context_calls) == 1
    ctx_issue_number, ctx_kwargs = context_calls[0]
    assert ctx_issue_number == 17
    assert ctx_kwargs["tracking_issue"] == 17
    assert ctx_kwargs["session_id"] == "sess-abc123"
    assert ctx_kwargs["branch"] == session.feature_branch

    # Slack notified.
    assert len(slack_calls) == 1
    assert "OAuth or magic links" in slack_calls[0]["question"]
    assert slack_calls[0]["issue_url"] == "https://github.com/acme/app/issues/17"

    # Run marked waiting_for_human immediately.
    assert session.waiting_for_human is True
    assert session.human_input["issue_number"] == 17
    assert session.hard_stop_reason is not None
    assert "#17" in session.hard_stop_reason


def test_implement_feature_human_input_required_uses_the_app_s_own_slack_channel(tmp_path, monkeypatch):
    """GitHub issue #69's "more requirement": every need_human escalation must route to the
    app's own configured Slack channel (registry.get_slack_channel), not just the global env var."""
    _patch_registry(monkeypatch)
    slack_calls: list = []
    _patch_slack_and_urls(monkeypatch, slack_calls)
    session = _session(tmp_path)
    monkeypatch.setattr(
        registry, "get_slack_channel",
        lambda app_name: "C_APP_CHANNEL" if app_name == session.app_name else None,
    )

    async def fake_impl_run(*a, **k):
        return _human_input_required_result()

    monkeypatch.setattr(brain.implementation, "run", fake_impl_run)
    monkeypatch.setattr(session.mem, "escalate_existing_issue", lambda *a, **k: None)
    monkeypatch.setattr(session.mem, "issue_html_url", lambda n: f"https://github.com/acme/app/issues/{n}")
    monkeypatch.setattr(session.mem, "record_human_input_context", lambda *a, **k: None)

    asyncio.run(
        _tool(session, "implement_feature").handler(
            {"feature_brief": "Add login", "resolves_origin": "known_bug", "resolves_id": "17"}
        )
    )

    assert len(slack_calls) == 1
    assert slack_calls[0]["channel"] == "C_APP_CHANNEL"


def test_implement_feature_human_input_required_does_not_count_as_a_tool_failure(tmp_path, monkeypatch):
    _patch_registry(monkeypatch)
    _patch_slack_and_urls(monkeypatch, [])
    session = _session(tmp_path)

    async def fake_impl_run(*a, **k):
        return _human_input_required_result()

    monkeypatch.setattr(brain.implementation, "run", fake_impl_run)
    monkeypatch.setattr(session.mem, "record_known_bug", lambda *a, **k: None)
    monkeypatch.setattr(session.mem, "issue_html_url", lambda n: None)
    monkeypatch.setattr(session.mem, "record_human_input_context", lambda *a, **k: None)
    monkeypatch.setattr(
        session.mem, "record_failure", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not be called"))
    )

    asyncio.run(_tool(session, "implement_feature").handler({"feature_brief": "Add login"}))

    assert session.tool_failure_counts.get("implement_feature", 0) == 0


def test_implement_feature_weaves_human_answer_into_the_matching_tracking_issue_spec(tmp_path, monkeypatch):
    _patch_registry(monkeypatch)
    _patch_slack_and_urls(monkeypatch, [])
    session = _session(tmp_path, human_answer="Use OAuth via the existing GitHub App.", human_answer_issue=17)

    captured_spec = {}

    async def fake_impl_run(repo, objective, brief, cb_summary, env, branch, resume=False, spec="", session_id=None, mem=None, run_id=None):
        captured_spec["spec"] = spec
        return AgentResult(ok=True, text="done", json_data={"feature": "Add login", "status": "implemented"}, cost_usd=0.01, turns=2)

    monkeypatch.setattr(brain.implementation, "run", fake_impl_run)
    monkeypatch.setattr(session.mem, "get_spec", lambda tracking_issue: {"spec": "Add a login flow.", "acceptance_criteria": ["Users can log in"]})
    monkeypatch.setattr(session.mem, "record_in_progress_branch", lambda *a, **k: None)
    monkeypatch.setattr(session.mem, "record_commit", lambda *a, **k: None)
    monkeypatch.setattr(session.mem, "record_code_complete", lambda *a, **k: {"issue_number": 17, "board_issue_number": None})
    monkeypatch.setattr(session.mem, "append_documentation", lambda *a, **k: None)

    asyncio.run(
        _tool(session, "implement_feature").handler(
            {"feature_brief": "Add login", "resolves_origin": "known_bug", "resolves_id": "17"}
        )
    )

    assert "Use OAuth via the existing GitHub App." in captured_spec["spec"]
    assert "A human has answered your previous blocking question" in captured_spec["spec"]
    # Consumed after use -- must not leak into a later call this same session.
    assert session.human_answer is None
    assert session.human_answer_issue is None


def test_implement_feature_does_not_weave_human_answer_into_an_unrelated_tracking_issue(tmp_path, monkeypatch):
    _patch_registry(monkeypatch)
    _patch_slack_and_urls(monkeypatch, [])
    # The pending answer belongs to issue #17, but this implement_feature call
    # is for issue #23 -- must not get the wrong answer woven in.
    session = _session(tmp_path, human_answer="Use OAuth via the existing GitHub App.", human_answer_issue=17)

    captured_spec = {}

    async def fake_impl_run(repo, objective, brief, cb_summary, env, branch, resume=False, spec="", session_id=None, mem=None, run_id=None):
        captured_spec["spec"] = spec
        return AgentResult(ok=True, text="done", json_data={"feature": "Add search", "status": "implemented"}, cost_usd=0.01, turns=2)

    monkeypatch.setattr(brain.implementation, "run", fake_impl_run)
    monkeypatch.setattr(session.mem, "get_spec", lambda tracking_issue: None)
    monkeypatch.setattr(session.mem, "record_in_progress_branch", lambda *a, **k: None)
    monkeypatch.setattr(session.mem, "record_commit", lambda *a, **k: None)
    monkeypatch.setattr(session.mem, "record_code_complete", lambda *a, **k: {"issue_number": 23, "board_issue_number": None})
    monkeypatch.setattr(session.mem, "append_documentation", lambda *a, **k: None)

    asyncio.run(
        _tool(session, "implement_feature").handler(
            {"feature_brief": "Add search", "resolves_origin": "known_bug", "resolves_id": "23"}
        )
    )

    assert captured_spec["spec"] == ""
    # Still pending -- unrelated to issue #23, so not consumed.
    assert session.human_answer == "Use OAuth via the existing GitHub App."
    assert session.human_answer_issue == 17

"""agents/brain/tools.py's notify_shipped choke points: a Slack 'shipped to pre-prod'
message must fire exactly once per real ship event, strictly from confirmed pre-prod
delivery -- deploy_pre_prod's TRIVIAL-classification merge success, or verify_pre_prod's
live-verification pass for a non-trivial change -- never from implement_feature's earlier
status:shipped label stamp, and never from deploy_pre_prod's own non-trivial success alone
or a failed/HUMAN_INPUT_REQUIRED deploy/verify.

No real git/docker/LLM/Slack calls: deployment.merge_to_pre_prod_only,
deployment.PRE_PROD_STRATEGIES entries, change_risk.classify_change, testing.run_pre_prod,
and connectors.slack.notify_shipped are all monkeypatched.
"""

import asyncio
from pathlib import Path

from agentra import registry
from agentra.agents import brain
from agentra.agents.base import AgentResult
from agentra.connectors import slack
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


def _patch_registry(monkeypatch):
    monkeypatch.setattr(registry, "record_agent_step", lambda *a, **k: None)


def _patch_issue_url(monkeypatch, session):
    monkeypatch.setattr(session.mem, "issue_html_url", lambda n: f"https://github.com/acme/app/issues/{n}")


def _capture_notify_shipped(monkeypatch):
    calls = []
    monkeypatch.setattr(slack, "notify_shipped", lambda **kw: calls.append(kw) or True)
    return calls


def test_deploy_pre_prod_trivial_merge_success_notifies_once_per_pending_item(tmp_path, monkeypatch):
    _patch_registry(monkeypatch)
    session = _session(
        tmp_path,
        pending_shipped_notifications=[
            {"issue_number": "5", "board_issue_number": 5, "title": "Fix flaky test"},
            {"issue_number": "6", "board_issue_number": 6, "title": "Docs typo"},
        ],
        code_complete_issue_numbers=["5", "6"],
    )
    _patch_issue_url(monkeypatch, session)
    calls = _capture_notify_shipped(monkeypatch)
    monkeypatch.setattr(brain.tools.change_risk, "classify_change", lambda *a, **k: "trivial")
    monkeypatch.setattr(session.mem, "record_shipped_to_preprod", lambda ids, run_id=None: list(ids))

    async def fake_merge(repo, env, feature_branch):
        return AgentResult(ok=True, text="merged, no deploy", json_data={"status": "skipped_light"}, cost_usd=0.0, turns=0)

    monkeypatch.setattr(brain.deployment, "merge_to_pre_prod_only", fake_merge)

    result = asyncio.run(_tool(session, "deploy_pre_prod").handler({}))

    assert result.get("is_error") is not True
    assert len(calls) == 2
    titles = {c["feature_title"] for c in calls}
    assert titles == {"Fix flaky test", "Docs typo"}
    for c in calls:
        assert "trivial change" in c["verification_result"]
        assert c["issue_url"] in (
            "https://github.com/acme/app/issues/5",
            "https://github.com/acme/app/issues/6",
        )
    assert session.pending_shipped_notifications == []


def test_deploy_pre_prod_trivial_merge_failure_does_not_notify(tmp_path, monkeypatch):
    _patch_registry(monkeypatch)
    session = _session(
        tmp_path,
        pending_shipped_notifications=[{"issue_number": "5", "board_issue_number": 5, "title": "Fix flaky test"}],
    )
    _patch_issue_url(monkeypatch, session)
    calls = _capture_notify_shipped(monkeypatch)
    monkeypatch.setattr(brain.tools.change_risk, "classify_change", lambda *a, **k: "trivial")

    async def fake_merge(repo, env, feature_branch):
        return AgentResult(ok=False, text="merge conflict", json_data={"status": "failed"}, cost_usd=0.0, turns=0)

    monkeypatch.setattr(brain.deployment, "merge_to_pre_prod_only", fake_merge)

    result = asyncio.run(_tool(session, "deploy_pre_prod").handler({}))

    assert result.get("is_error") is True
    assert calls == []
    # Left un-drained -- nothing was actually delivered.
    assert session.pending_shipped_notifications == [{"issue_number": "5", "board_issue_number": 5, "title": "Fix flaky test"}]


def test_deploy_pre_prod_non_trivial_success_alone_does_not_notify(tmp_path, monkeypatch):
    _patch_registry(monkeypatch)
    session = _session(
        tmp_path,
        pending_shipped_notifications=[{"issue_number": "7", "board_issue_number": 7, "title": "Big feature"}],
    )
    _patch_issue_url(monkeypatch, session)
    calls = _capture_notify_shipped(monkeypatch)
    monkeypatch.setattr(brain.tools.change_risk, "classify_change", lambda *a, **k: "standard")

    async def fake_strategy(repo, env, feature_branch, run_id, session_id):
        return AgentResult(ok=True, text="deployed", json_data={"status": "deployed", "preview_url": "https://preview.example.com"}, cost_usd=0.0, turns=0)

    monkeypatch.setitem(brain.deployment.PRE_PROD_STRATEGIES, session.env.deploy_strategy, fake_strategy)

    result = asyncio.run(_tool(session, "deploy_pre_prod").handler({}))

    assert result.get("is_error") is not True
    assert calls == []
    assert session.pending_shipped_notifications == [{"issue_number": "7", "board_issue_number": 7, "title": "Big feature"}]


def test_verify_pre_prod_pass_notifies_and_includes_preview_url(tmp_path, monkeypatch):
    _patch_registry(monkeypatch)
    session = _session(
        tmp_path,
        change_risk="standard",
        pre_prod_url="https://preview.example.com",
        pending_shipped_notifications=[{"issue_number": "7", "board_issue_number": 7, "title": "Big feature"}],
    )
    _patch_issue_url(monkeypatch, session)
    calls = _capture_notify_shipped(monkeypatch)

    async def fake_run_pre_prod(repo, spec, url, run_id, session_id=None):
        return AgentResult(ok=True, text="verified", json_data={"status": "pass", "reachable": True, "feature_verified": True}, cost_usd=0.0, turns=0)

    monkeypatch.setattr(brain.testing, "run_pre_prod", fake_run_pre_prod)

    result = asyncio.run(_tool(session, "verify_pre_prod").handler({}))

    assert result.get("is_error") is not True
    assert len(calls) == 1
    assert calls[0]["feature_title"] == "Big feature"
    assert calls[0]["issue_url"] == "https://github.com/acme/app/issues/7"
    assert "verify_pre_prod passed" in calls[0]["verification_result"]
    assert "https://preview.example.com" in calls[0]["verification_result"]
    assert session.pending_shipped_notifications == []


def test_verify_pre_prod_failure_does_not_notify(tmp_path, monkeypatch):
    _patch_registry(monkeypatch)
    session = _session(
        tmp_path,
        change_risk="standard",
        pre_prod_url="https://preview.example.com",
        pending_shipped_notifications=[{"issue_number": "7", "board_issue_number": 7, "title": "Big feature"}],
    )
    _patch_issue_url(monkeypatch, session)
    calls = _capture_notify_shipped(monkeypatch)

    async def fake_run_pre_prod(repo, spec, url, run_id, session_id=None):
        return AgentResult(ok=False, text="broken", json_data={"status": "fail", "reachable": True, "feature_verified": False}, cost_usd=0.0, turns=0)

    monkeypatch.setattr(brain.testing, "run_pre_prod", fake_run_pre_prod)

    result = asyncio.run(_tool(session, "verify_pre_prod").handler({}))

    assert result.get("is_error") is True
    assert calls == []
    assert session.pending_shipped_notifications == [{"issue_number": "7", "board_issue_number": 7, "title": "Big feature"}]


def test_no_pending_notifications_means_no_slack_call(tmp_path, monkeypatch):
    _patch_registry(monkeypatch)
    session = _session(tmp_path, change_risk="trivial")
    calls = _capture_notify_shipped(monkeypatch)

    result = asyncio.run(_tool(session, "verify_pre_prod").handler({}))

    assert result.get("is_error") is not True
    assert calls == []

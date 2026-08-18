"""agents/brain/tools.py's deploy_pre_prod/verify_pre_prod/check_backlog --
the change_risk-driven gate that decides whether a run's accumulated diff
gets the full pre-prod deploy + live Testing Agent verification, or merges
straight to pre-prod because change_risk.classify_change() says TRIVIAL
(test fix, docs/config edit, rename, or a couple-line bug fix). See
change_risk.py's own docstring for why this is deterministic, not an LLM
judgment call, and agents/deployment.py's merge_to_pre_prod_only for the
light path itself.

No real git/docker/LLM calls: deployment.merge_to_pre_prod_only,
deployment.PRE_PROD_STRATEGIES entries, and change_risk.classify_change are
all monkeypatched.
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


def test_deploy_pre_prod_merges_only_for_a_trivial_change(tmp_path, monkeypatch):
    _patch_registry(monkeypatch)
    session = _session(tmp_path)
    monkeypatch.setattr(brain.tools.change_risk, "classify_change", lambda *a, **k: "trivial")

    strategy_calls = []
    monkeypatch.setitem(
        brain.deployment.PRE_PROD_STRATEGIES, "vercel_firebase",
        lambda *a, **k: strategy_calls.append((a, k)),
    )
    merge_calls = []

    async def fake_merge(repo, env, feature_branch):
        merge_calls.append((repo, env, feature_branch))
        return AgentResult(ok=True, text="merged, no deploy", json_data={"status": "skipped_light", "preview_url": None}, cost_usd=0.0, turns=0)

    monkeypatch.setattr(brain.deployment, "merge_to_pre_prod_only", fake_merge)

    result = asyncio.run(_tool(session, "deploy_pre_prod").handler({}))

    assert result.get("is_error") is not True
    assert "No verify_pre_prod call needed" in result["content"][0]["text"]
    assert merge_calls == [(session.repo, session.env, session.feature_branch)]
    assert strategy_calls == []
    assert session.change_risk == "trivial"
    assert session.pre_prod_url is None
    assert session.pre_prod_verified is True
    assert session.deployed_to_pre_prod is True


def test_deploy_pre_prod_uses_the_full_strategy_for_a_standard_change(tmp_path, monkeypatch):
    _patch_registry(monkeypatch)
    session = _session(tmp_path)
    monkeypatch.setattr(brain.tools.change_risk, "classify_change", lambda *a, **k: "standard")

    monkeypatch.setattr(
        brain.deployment, "merge_to_pre_prod_only",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not take the light merge-only path")),
    )

    async def fake_strategy(repo, env, feature_branch, run_id, session_id):
        return AgentResult(ok=True, text="deployed", json_data={"status": "deployed", "preview_url": "https://preview.example.com"}, cost_usd=0.0, turns=0)

    monkeypatch.setitem(brain.deployment.PRE_PROD_STRATEGIES, session.env.deploy_strategy, fake_strategy)

    result = asyncio.run(_tool(session, "deploy_pre_prod").handler({}))

    assert result.get("is_error") is not True
    assert session.change_risk == "standard"
    assert session.pre_prod_url == "https://preview.example.com"
    assert session.pre_prod_verified is False  # still needs verify_pre_prod


def test_verify_pre_prod_skips_when_change_was_classified_trivial(tmp_path, monkeypatch):
    _patch_registry(monkeypatch)
    session = _session(tmp_path, change_risk="trivial", pre_prod_url=None)

    monkeypatch.setattr(
        brain.testing, "run_pre_prod",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not call the Testing Agent for a trivial change")),
    )

    result = asyncio.run(_tool(session, "verify_pre_prod").handler({}))

    assert result.get("is_error") is not True
    assert "trivial" in result["content"][0]["text"].lower()


def test_check_backlog_hints_at_batching_when_multiple_items_are_pending(tmp_path, monkeypatch):
    _patch_registry(monkeypatch)
    session = _session(tmp_path)
    monkeypatch.setattr(session.mem, "shipped_features", lambda: [])
    monkeypatch.setattr(session.mem, "in_progress_features", lambda: [])
    monkeypatch.setattr(session.mem, "known_bugs", lambda: [])
    monkeypatch.setattr(
        session.mem, "feature_queue",
        lambda: [{"feature": "A", "external_id": "1"}, {"feature": "B", "external_id": "2"}],
    )
    monkeypatch.setattr(session.mem, "resume_branch_for", lambda ext_id: None)
    monkeypatch.setattr(session.mem, "resume_run_id_for", lambda ext_id: None)

    result = asyncio.run(_tool(session, "check_backlog").handler({}))

    text = result["content"][0]["text"]
    assert "1 more item(s)" in text
    assert "batch" in text.lower() or "batching" in text.lower() or "consider implementing" in text.lower()


def test_check_backlog_has_no_batching_hint_with_a_single_item(tmp_path, monkeypatch):
    _patch_registry(monkeypatch)
    session = _session(tmp_path)
    monkeypatch.setattr(session.mem, "shipped_features", lambda: [])
    monkeypatch.setattr(session.mem, "in_progress_features", lambda: [])
    monkeypatch.setattr(session.mem, "known_bugs", lambda: [])
    monkeypatch.setattr(session.mem, "feature_queue", lambda: [{"feature": "A", "external_id": "1"}])
    monkeypatch.setattr(session.mem, "resume_branch_for", lambda ext_id: None)
    monkeypatch.setattr(session.mem, "resume_run_id_for", lambda ext_id: None)

    result = asyncio.run(_tool(session, "check_backlog").handler({}))

    assert "more item(s)" not in result["content"][0]["text"]

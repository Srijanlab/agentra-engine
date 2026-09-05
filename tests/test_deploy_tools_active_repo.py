"""Multi-repo Phase 2 step 4: run_local_tests/deploy_pre_prod/verify_pre_prod all operate
on session.active_repo_path (the code repo implement_feature resolved) instead of
session.repo (the coordination repo) -- confirms the fix, not just that legacy single-
repo behavior is unaffected (see test_run_local_tests_self_heal.py and friends for that).

No real LLM/git call: testing.run_local/run_pre_prod, change_risk.classify_change, and
deployment.merge_to_pre_prod_only are all monkeypatched.
"""

import asyncio
from pathlib import Path

from agentra import registry
from agentra.agents import brain
from agentra.agents.base import AgentResult
from agentra.environments import EnvironmentConfig
from agentra.memory import Memory
from agentra.registry.core import RepoSpec


def _tool(session, name):
    tools = brain._tools_for(session)
    return next(t for t in tools if t.name == name)


def _patch_registry(monkeypatch):
    monkeypatch.setattr(registry, "record_run", lambda *a, **k: None)


def _multi_repo_session(tmp_path: Path, **overrides) -> brain.OrchestratorSession:
    repo = tmp_path / "repo"
    engine_repo = tmp_path / "engine"
    ui_repo = tmp_path / "ui"
    repo.mkdir(exist_ok=True)
    engine_repo.mkdir(exist_ok=True)
    ui_repo.mkdir(exist_ok=True)
    defaults = dict(
        repo=repo,
        objective="test objective",
        env=EnvironmentConfig(),
        mem=Memory(repo),
        run_id="testrun1",
        code_repos={
            "engine": RepoSpec(name="engine", path=engine_repo, repo_url=None, branch="main", role="code"),
            "ui": RepoSpec(name="ui", path=ui_repo, repo_url=None, branch="main", role="code"),
        },
        cb_summaries={"engine": "engine summary", "ui": "ui summary"},
        feature_branch="dev/testrun1-a-feature",
    )
    defaults.update(overrides)
    session = brain.OrchestratorSession(**defaults)
    session.set_active_repo("ui")
    return session


def test_run_local_tests_uses_the_active_repo_not_the_coordination_repo(tmp_path, monkeypatch):
    _patch_registry(monkeypatch)
    session = _multi_repo_session(tmp_path)
    captured = {}

    async def fake_run_local(repo, *a, **k):
        captured["repo"] = repo
        return AgentResult(ok=True, text="ok", json_data={"status": "pass"}, cost_usd=0.0, turns=1)

    monkeypatch.setattr(brain.testing, "run_local", fake_run_local)

    result = asyncio.run(_tool(session, "run_local_tests").handler({}))

    assert result.get("is_error") is not True
    assert captured["repo"] == session.code_repos["ui"].path
    assert captured["repo"] != session.repo


def test_deploy_pre_prod_trivial_merge_uses_the_active_repo(tmp_path, monkeypatch):
    _patch_registry(monkeypatch)
    session = _multi_repo_session(tmp_path)
    session.tests_passed = True
    monkeypatch.setattr(brain.tools.change_risk, "classify_change", lambda repo, *a, **k: "trivial")
    captured = {}

    async def fake_merge(repo, env, feature_branch):
        captured["repo"] = repo
        return AgentResult(ok=True, text="merged", json_data=None, cost_usd=0.0, turns=0)

    monkeypatch.setattr(brain.deployment, "merge_to_pre_prod_only", fake_merge)

    result = asyncio.run(_tool(session, "deploy_pre_prod").handler({}))

    assert result.get("is_error") is not True
    assert captured["repo"] == session.code_repos["ui"].path


def test_deploy_pre_prod_external_strategy_skips_verify_pre_prod_entirely(tmp_path, monkeypatch):
    """The "external" strategy never produces a preview_url (the repo's own CI/CD deploys
    asynchronously, on infra agentra has no live URL for) -- verify_pre_prod would refuse
    forever ("no live URL to verify yet") if deploy_pre_prod didn't treat a successful
    external deploy as the terminal pre-prod confirmation itself, same as the change-risk
    skip path."""
    _patch_registry(monkeypatch)
    session = _multi_repo_session(tmp_path)
    session.env.deploy_strategy = "external"
    session.tests_passed = True
    monkeypatch.setattr(brain.tools.change_risk, "classify_change", lambda repo, *a, **k: "standard")

    async def fake_external_strategy(repo, env, feature_branch, run_id, session_id):
        return AgentResult(
            ok=True, text="merged, external CI/CD handles the deploy",
            json_data={"status": "external", "preview_url": None}, cost_usd=0.0, turns=0,
        )

    monkeypatch.setattr(brain.deployment, "PRE_PROD_STRATEGIES", {"external": fake_external_strategy})

    deploy_result = asyncio.run(_tool(session, "deploy_pre_prod").handler({}))

    assert deploy_result.get("is_error") is not True
    assert "No verify_pre_prod call needed" in deploy_result["content"][0]["text"]
    assert session.pre_prod_verified is True
    assert session.pre_prod_url is None

    verify_result = asyncio.run(_tool(session, "verify_pre_prod").handler({}))
    assert verify_result.get("is_error") is not True  # would refuse ("no live URL") without the fix


def test_verify_pre_prod_uses_the_active_repo(tmp_path, monkeypatch):
    _patch_registry(monkeypatch)
    session = _multi_repo_session(tmp_path)
    session.pre_prod_url = "https://ui-preview.example.com"
    captured = {}

    async def fake_run_pre_prod(repo, *a, **k):
        captured["repo"] = repo
        return AgentResult(ok=True, text="verified", json_data={"status": "pass", "reachable": True, "feature_verified": True}, cost_usd=0.0, turns=1)

    monkeypatch.setattr(brain.testing, "run_pre_prod", fake_run_pre_prod)

    result = asyncio.run(_tool(session, "verify_pre_prod").handler({}))

    assert result.get("is_error") is not True
    assert captured["repo"] == session.code_repos["ui"].path

"""deploy_pre_prod/verify_pre_prod (agents/brain/tools.py) dispatching via
deployment.PRE_PROD_STRATEGIES[env.deploy_strategy] -- an app with
deploy_strategy="self_hosted_vm" takes the deployment.deploy_pre_prod_self_hosted
path instead of the generic Vercel/Firebase one, and verify_pre_prod tears the
sibling container down once its report is produced. See agents/deployment.py's
own docstring for why this path is deterministic (no run_agent/LLM call at all),
and PRE_PROD_STRATEGIES' docstring for why dispatch goes through a registry
rather than a hardcoded branch.

No real Docker calls: deployment.deploy_pre_prod_self_hosted/
teardown_self_hosted_preprod and testing.run_pre_prod are monkeypatched.
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
        env=EnvironmentConfig(deploy_strategy="self_hosted_vm"),
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


def test_deploy_pre_prod_calls_the_self_hosted_path_when_env_flag_is_set(tmp_path, monkeypatch):
    _patch_registry(monkeypatch)
    session = _session(tmp_path)

    generic_calls = []
    self_hosted_calls = []
    monkeypatch.setattr(brain.deployment, "deploy_pre_prod", lambda *a, **k: generic_calls.append((a, k)))

    async def fake_self_hosted(repo, env, feature_branch, run_id):
        self_hosted_calls.append((repo, env, feature_branch, run_id))
        return AgentResult(ok=True, text="up", json_data={"status": "deployed", "preview_url": "http://agentra-preprod-testrun1:8080"}, cost_usd=0.0, turns=0)

    monkeypatch.setattr(brain.deployment, "deploy_pre_prod_self_hosted", fake_self_hosted)

    result = asyncio.run(_tool(session, "deploy_pre_prod").handler({}))

    assert result.get("is_error") is not True
    assert generic_calls == []
    assert self_hosted_calls == [(session.repo, session.env, session.feature_branch, session.run_id)]
    assert session.pre_prod_url == "http://agentra-preprod-testrun1:8080"
    assert session.deployed_to_pre_prod is True


def test_deploy_pre_prod_uses_the_generic_path_when_strategy_is_vercel_firebase(tmp_path, monkeypatch):
    _patch_registry(monkeypatch)
    session = _session(tmp_path, env=EnvironmentConfig())

    self_hosted_calls = []
    monkeypatch.setattr(
        brain.deployment, "deploy_pre_prod_self_hosted",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not call the self-hosted path")),
    )

    async def fake_generic(repo, env, feature_branch, session_id=None):
        return AgentResult(ok=True, text="deployed", json_data={"status": "deployed", "preview_url": "https://preview.example.com"}, cost_usd=0.0, turns=0)

    monkeypatch.setattr(brain.deployment, "deploy_pre_prod", fake_generic)

    result = asyncio.run(_tool(session, "deploy_pre_prod").handler({}))

    assert result.get("is_error") is not True
    assert session.pre_prod_url == "https://preview.example.com"


def test_verify_pre_prod_tears_down_the_self_hosted_sibling_after_a_passing_report(tmp_path, monkeypatch):
    _patch_registry(monkeypatch)
    session = _session(tmp_path, pre_prod_url="http://agentra-preprod-testrun1:8080")

    async def fake_run_pre_prod(repo, spec, preview_url, run_id, session_id=None):
        return AgentResult(ok=True, text="ok", json_data={"status": "pass", "reachable": True, "feature_verified": True}, cost_usd=0.0, turns=1)

    monkeypatch.setattr(brain.testing, "run_pre_prod", fake_run_pre_prod)
    teardown_calls = []
    monkeypatch.setattr(brain.deployment, "teardown_self_hosted_preprod", lambda repo, run_id: teardown_calls.append((repo, run_id)))

    asyncio.run(_tool(session, "verify_pre_prod").handler({}))

    assert teardown_calls == [(session.repo, session.run_id)]


def test_verify_pre_prod_tears_down_the_self_hosted_sibling_even_after_a_failing_report(tmp_path, monkeypatch):
    _patch_registry(monkeypatch)
    session = _session(tmp_path, pre_prod_url="http://agentra-preprod-testrun1:8080")

    async def fake_run_pre_prod(repo, spec, preview_url, run_id, session_id=None):
        return AgentResult(ok=True, text="broken", json_data={"status": "fail", "reachable": True, "feature_verified": False}, cost_usd=0.0, turns=1)

    monkeypatch.setattr(brain.testing, "run_pre_prod", fake_run_pre_prod)
    monkeypatch.setattr(session.mem, "record_failure", lambda *a, **k: None)
    teardown_calls = []
    monkeypatch.setattr(brain.deployment, "teardown_self_hosted_preprod", lambda repo, run_id: teardown_calls.append((repo, run_id)))

    asyncio.run(_tool(session, "verify_pre_prod").handler({}))

    assert teardown_calls == [(session.repo, session.run_id)]


def test_verify_pre_prod_does_not_teardown_when_strategy_is_vercel_firebase(tmp_path, monkeypatch):
    _patch_registry(monkeypatch)
    session = _session(
        tmp_path, env=EnvironmentConfig(), pre_prod_url="https://preview.example.com",
    )

    async def fake_run_pre_prod(repo, spec, preview_url, run_id, session_id=None):
        return AgentResult(ok=True, text="ok", json_data={"status": "pass"}, cost_usd=0.0, turns=1)

    monkeypatch.setattr(brain.testing, "run_pre_prod", fake_run_pre_prod)
    monkeypatch.setattr(
        brain.deployment, "teardown_self_hosted_preprod",
        lambda repo, run_id: (_ for _ in ()).throw(AssertionError("must not tear down when strategy is vercel_firebase")),
    )

    asyncio.run(_tool(session, "verify_pre_prod").handler({}))

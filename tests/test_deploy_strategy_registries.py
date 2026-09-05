"""agents/deployment.py's PRE_PROD_STRATEGIES/PROD_STRATEGIES -- the single
source of truth EnvironmentConfig.deploy_strategy resolves through, replacing
what used to be a hardcoded if/else in agents/brain/tools.py's deploy_pre_prod
tool and orchestrator.py's run_promote. Adding a third strategy later means
adding one function + one registry entry, not touching either call site.

"external" (added for multi-repo Phase 3: a code repo with no Vercel/Firebase/
self-hosted-VM target agentra can drive directly, e.g. agentra-loop's own
push-triggered GitHub Actions workflow to AWS) does real git merge/push work via
_merge_and_push/_sync_branch_to_remote (same primitives merge_to_pre_prod_only
uses) rather than delegating to another already-tested function, so its own
tests exercise a real local git repo (see test_deploy_pre_prod_external.py)
instead of monkeypatching a delegate the way the two adapters below do.

No real docker/git/LLM calls: the underlying deploy_pre_prod/deploy_pre_prod_self_hosted/
promote_prod/promote_prod_self_hosted functions are all monkeypatched -- this
file only tests that the registries route to the right one with the right args.
"""

import asyncio

from agentra.agents import deployment
from agentra.agents.base import AgentResult
from agentra.environments import EnvironmentConfig


def test_pre_prod_strategies_has_exactly_the_three_known_strategies():
    assert set(deployment.PRE_PROD_STRATEGIES) == {"vercel_firebase", "self_hosted_vm", "external"}


def test_prod_strategies_has_exactly_the_three_known_strategies():
    assert set(deployment.PROD_STRATEGIES) == {"vercel_firebase", "self_hosted_vm", "external"}


def test_pre_prod_vercel_firebase_adapter_calls_deploy_pre_prod_with_session_id(monkeypatch):
    calls = []

    async def fake(repo, env, feature_branch, session_id=None):
        calls.append((repo, env, feature_branch, session_id))
        return AgentResult(ok=True, text="ok", json_data={"status": "deployed"}, cost_usd=0.0, turns=0)

    monkeypatch.setattr(deployment, "deploy_pre_prod", fake)

    strategy = deployment.PRE_PROD_STRATEGIES["vercel_firebase"]
    env = EnvironmentConfig()
    asyncio.run(strategy("repo", env, "feature-branch", "run1", "sess1"))

    assert calls == [("repo", env, "feature-branch", "sess1")]


def test_pre_prod_self_hosted_adapter_calls_deploy_pre_prod_self_hosted_with_run_id(monkeypatch):
    calls = []

    async def fake(repo, env, feature_branch, run_id):
        calls.append((repo, env, feature_branch, run_id))
        return AgentResult(ok=True, text="ok", json_data={"status": "deployed"}, cost_usd=0.0, turns=0)

    monkeypatch.setattr(deployment, "deploy_pre_prod_self_hosted", fake)

    strategy = deployment.PRE_PROD_STRATEGIES["self_hosted_vm"]
    env = EnvironmentConfig(deploy_strategy="self_hosted_vm")
    asyncio.run(strategy("repo", env, "feature-branch", "run1", "sess1"))

    assert calls == [("repo", env, "feature-branch", "run1")]


def test_prod_vercel_firebase_adapter_calls_promote_prod_with_session_id(monkeypatch):
    calls = []

    async def fake(repo, env, session_id=None):
        calls.append((repo, env, session_id))
        return AgentResult(ok=True, text="ok", json_data={"status": "deployed"}, cost_usd=0.0, turns=0)

    monkeypatch.setattr(deployment, "promote_prod", fake)

    strategy = deployment.PROD_STRATEGIES["vercel_firebase"]
    env = EnvironmentConfig()
    asyncio.run(strategy("repo", env, "run1", "sess1"))

    assert calls == [("repo", env, "sess1")]


def test_prod_self_hosted_adapter_calls_promote_prod_self_hosted_with_run_id(monkeypatch):
    calls = []

    async def fake(repo, env, run_id):
        calls.append((repo, env, run_id))
        return AgentResult(ok=True, text="ok", json_data={"status": "deployed"}, cost_usd=0.0, turns=0)

    monkeypatch.setattr(deployment, "promote_prod_self_hosted", fake)

    strategy = deployment.PROD_STRATEGIES["self_hosted_vm"]
    env = EnvironmentConfig(deploy_strategy="self_hosted_vm")
    asyncio.run(strategy("repo", env, "run1", "sess1"))

    assert calls == [("repo", env, "run1")]

"""orchestrator.py::run_promote() resumes the Claude session of whichever
pending (shipped, not-yet-released) feature was shipped most recently,
rather than always cold-starting a disconnected session for promotion.
deployment.promote_prod and Memory.pending_promotion_features are both
monkeypatched -- no real git/network/LLM involved, same convention as
tests/test_deployment.py."""

import asyncio

from agentra import orchestrator
from agentra.agents.base import AgentResult
from agentra.environments import EnvironmentConfig
from agentra.memory import Memory


def _pending(feature: str, external_id: str, session_id: str | None, updated_at: str | None) -> dict:
    return {
        "feature": feature, "external_id": external_id, "session_id": session_id,
        "updated_at": updated_at, "ts": None, "commit_sha": None, "html_url": None, "status_done": False,
    }


def test_run_promote_resumes_the_most_recently_shipped_session(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(orchestrator.environments, "load", lambda repo: EnvironmentConfig())
    monkeypatch.setattr(
        Memory,
        "pending_promotion_features",
        lambda self: [
            _pending("Older feature", "10", "sess-old", "2026-08-01T00:00:00Z"),
            _pending("Newer feature", "20", "sess-new", "2026-08-10T00:00:00Z"),
        ],
    )
    promote_calls = []

    async def fake_promote_prod(repo, env, session_id=None):
        promote_calls.append(session_id)
        return AgentResult(ok=True, text="promoted", json_data={"status": "deployed"}, cost_usd=0.01, turns=1)

    monkeypatch.setattr(orchestrator.deployment, "promote_prod", fake_promote_prod)

    result = asyncio.run(orchestrator.run_promote(repo))

    assert result["ok"] is True
    assert promote_calls == ["sess-new"]


def test_run_promote_logs_older_pending_features_as_carried_not_resumed(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(orchestrator.environments, "load", lambda repo: EnvironmentConfig())
    monkeypatch.setattr(
        Memory,
        "pending_promotion_features",
        lambda self: [
            _pending("Older feature", "10", "sess-old", "2026-08-01T00:00:00Z"),
            _pending("Newer feature", "20", "sess-new", "2026-08-10T00:00:00Z"),
        ],
    )
    async def fake_promote_prod(repo, env, session_id=None):
        return AgentResult(ok=True, text="promoted", json_data={"status": "deployed"}, cost_usd=0.01, turns=1)

    monkeypatch.setattr(orchestrator.deployment, "promote_prod", fake_promote_prod)
    logged = []
    monkeypatch.setattr(Memory, "log", lambda self, run_id, line: logged.append(line))

    asyncio.run(orchestrator.run_promote(repo))

    assert any("Newer feature" in line and "resuming build session" in line for line in logged)
    assert any("Older feature" in line and "carried by this promote run" in line for line in logged)


def test_run_promote_does_not_resume_when_nothing_pending(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(orchestrator.environments, "load", lambda repo: EnvironmentConfig())
    monkeypatch.setattr(Memory, "pending_promotion_features", lambda self: [])
    promote_calls = []

    async def fake_promote_prod(repo, env, session_id=None):
        promote_calls.append(session_id)
        return AgentResult(ok=True, text="promoted", json_data={"status": "deployed"}, cost_usd=0.01, turns=1)

    monkeypatch.setattr(orchestrator.deployment, "promote_prod", fake_promote_prod)

    asyncio.run(orchestrator.run_promote(repo))

    assert promote_calls == [None]


def test_run_promote_dispatches_to_promote_prod_self_hosted_for_that_strategy(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(orchestrator.environments, "load", lambda repo: EnvironmentConfig(deploy_strategy="self_hosted_vm"))
    monkeypatch.setattr(Memory, "pending_promotion_features", lambda self: [])
    generic_calls = []
    self_hosted_calls = []
    monkeypatch.setattr(
        orchestrator.deployment, "promote_prod", lambda *a, **k: generic_calls.append((a, k)),
    )

    async def fake_promote_prod_self_hosted(repo, env, run_id):
        self_hosted_calls.append((repo, env, run_id))
        return AgentResult(ok=True, text="promoted", json_data={"status": "deployed"}, cost_usd=0.0, turns=0)

    monkeypatch.setattr(orchestrator.deployment, "promote_prod_self_hosted", fake_promote_prod_self_hosted)

    result = asyncio.run(orchestrator.run_promote(repo, run_id="run1"))

    assert result["ok"] is True
    assert generic_calls == []
    assert len(self_hosted_calls) == 1
    assert self_hosted_calls[0][2] == "run1"

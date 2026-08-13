"""Tests agents/brain.py's pre-flight check: run_autonomous_cycle must
refuse to start an autonomous cycle at all while an open bug is labeled
both "bug" and "blocking_agentra" (Memory.blocking_bugs()) -- see the
label-pair docstring in memory.py and the comment right above the check in
run_autonomous_cycle. This is deterministic Python gating the SDK's
query() call, not a system-prompt instruction, so it's tested the same
way -- no real LLM call should ever happen when a blocking bug is open.

Costs nothing to run (query() is monkeypatched to fail loudly if called at
all) -- part of the normal pytest suite, unlike tests/test_safety_integration.py.
"""

import asyncio

from agentra import registry
from agentra.agents import brain
from agentra.environments import EnvironmentConfig
from agentra.memory import Memory


async def _fail_if_called(*args, **kwargs):
    raise AssertionError("query() must not be called while a blocking_agentra bug is open")


def test_run_autonomous_cycle_stops_immediately_on_a_blocking_bug(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "record_agent_step", lambda *a, **k: None)
    monkeypatch.setattr("agentra.agents.brain.deployment.persist_audit_trail", lambda *a, **k: None)
    monkeypatch.setattr(brain, "query", _fail_if_called)
    monkeypatch.setattr(
        Memory,
        "blocking_bugs",
        lambda self: [
            {
                "run_id": "7", "severity": "medium", "diagnosis": "403 Write access to repository not granted",
                "proposed_fix": "", "source": "github", "external_id": "7",
                "html_url": "https://github.com/acme/app/issues/7", "needs_human": True, "blocking_agentra": True,
            }
        ],
    )

    repo = tmp_path / "repo"
    repo.mkdir()

    report = asyncio.run(brain.run_autonomous_cycle(repo, "Ship useful features.", EnvironmentConfig()))

    assert report.cost_usd == 0.0
    assert "#7" in report.final_message
    assert "blocking_agentra" in report.final_message
    assert any("blocked by open blocking_agentra bug" in a for a in report.actions)


def test_run_autonomous_cycle_does_not_short_circuit_without_a_blocking_bug(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "record_agent_step", lambda *a, **k: None)
    monkeypatch.setattr("agentra.agents.brain.deployment.persist_audit_trail", lambda *a, **k: None)
    monkeypatch.setattr(Memory, "blocking_bugs", lambda self: [])
    called = {"query": False}

    async def _fake_query(*args, **kwargs):
        called["query"] = True
        return
        yield  # pragma: no cover -- makes this an async generator, never reached

    monkeypatch.setattr(brain, "query", _fake_query)

    repo = tmp_path / "repo"
    repo.mkdir()

    asyncio.run(brain.run_autonomous_cycle(repo, "Ship useful features.", EnvironmentConfig()))

    assert called["query"] is True

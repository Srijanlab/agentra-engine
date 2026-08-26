"""GitHub issue #74: the orchestrator's own reasoning cost (the top-level
query() call in agents/brain/__init__.py::run_autonomous_cycle, not any
sub-agent tool call) was only ever folded into session.cost_usd -- the
run's aggregate total -- and never recorded as its own agent_steps entry.
Every one of the 10 lifecycle session.note(..., agent="cycle", ...) call
sites defaults cost_usd=0.0, so the dashboard's per-agent cost breakdown
always showed $0.00 for "Orchestrator" even though real spend happens on
every ResultMessage the top-level query() yields. That same ResultMessage
also carries token usage (model_usage), now threaded through alongside
cost_usd.

Fast unit tests (query() monkeypatched, no real LLM call) -- same style as
tests/test_brain_blocking_bugs.py.
"""

import asyncio

from claude_agent_sdk import ResultMessage

from agentra import registry
from agentra.agents import brain
from agentra.environments import EnvironmentConfig
from agentra.memory import Memory


def _result_message(
    cost: float = 0.0123, num_turns: int = 4, is_error: bool = False, model_usage: dict | None = None,
) -> ResultMessage:
    return ResultMessage(
        subtype="success", duration_ms=10, duration_api_ms=9, is_error=is_error, num_turns=num_turns,
        session_id="s1", total_cost_usd=cost, result="done", terminal_reason="completed",
        model_usage=model_usage,
    )


def _common_monkeypatches(monkeypatch):
    monkeypatch.setattr("agentra.agents.brain.deployment.persist_audit_trail", lambda *a, **k: None)
    monkeypatch.setattr(Memory, "blocking_bugs", lambda self: [])


def test_orchestrators_own_result_message_cost_is_recorded_as_a_cycle_agent_step(tmp_path, monkeypatch):
    _common_monkeypatches(monkeypatch)
    recorded_steps = []

    def fake_record_agent_step(app, run_id, agent, ok, cost_usd, turns, summary, **kwargs):
        recorded_steps.append(dict(app=app, run_id=run_id, agent=agent, ok=ok, cost_usd=cost_usd, turns=turns, summary=summary, **kwargs))

    monkeypatch.setattr(registry, "record_agent_step", fake_record_agent_step)

    async def _fake_query(*args, **kwargs):
        yield _result_message(cost=0.0123, num_turns=4)

    monkeypatch.setattr(brain, "query", _fake_query)

    repo = tmp_path / "repo"
    repo.mkdir()

    report = asyncio.run(brain.run_autonomous_cycle(repo, "Ship useful features.", EnvironmentConfig()))

    # The run aggregate must still include it (unchanged prior behavior).
    assert report.cost_usd == 0.0123

    cycle_steps_with_cost = [s for s in recorded_steps if s["agent"] == "cycle" and s["cost_usd"] > 0]
    assert len(cycle_steps_with_cost) == 1
    step = cycle_steps_with_cost[0]
    assert step["cost_usd"] == 0.0123
    assert step["turns"] == 4
    assert step["ok"] is True

    # Every other "cycle" lifecycle step (start/complete/etc.) still defaults
    # to cost_usd=0.0 -- only the ResultMessage-driven step carries real cost,
    # so the aggregate isn't double counted across the 10 lifecycle call sites.
    other_cycle_steps = [s for s in recorded_steps if s["agent"] == "cycle" and s is not step]
    assert other_cycle_steps  # cycle start/complete notes still fire
    assert all(s["cost_usd"] == 0.0 for s in other_cycle_steps)


def test_orchestrator_cost_step_reflects_a_failed_result_message(tmp_path, monkeypatch):
    _common_monkeypatches(monkeypatch)
    recorded_steps = []

    def fake_record_agent_step(app, run_id, agent, ok, cost_usd, turns, summary, **kwargs):
        recorded_steps.append(dict(agent=agent, ok=ok, cost_usd=cost_usd))

    monkeypatch.setattr(registry, "record_agent_step", fake_record_agent_step)

    async def _fake_query(*args, **kwargs):
        yield _result_message(cost=0.05, is_error=True)

    monkeypatch.setattr(brain, "query", _fake_query)

    repo = tmp_path / "repo"
    repo.mkdir()

    asyncio.run(brain.run_autonomous_cycle(repo, "Ship useful features.", EnvironmentConfig()))

    cost_steps = [s for s in recorded_steps if s["agent"] == "cycle" and s["cost_usd"] == 0.05]
    assert len(cost_steps) == 1
    assert cost_steps[0]["ok"] is False


def test_orchestrator_cost_step_carries_token_usage_from_model_usage(tmp_path, monkeypatch):
    _common_monkeypatches(monkeypatch)
    recorded_steps = []

    def fake_record_agent_step(app, run_id, agent, ok, cost_usd, turns, summary, **kwargs):
        recorded_steps.append(dict(agent=agent, cost_usd=cost_usd, **kwargs))

    monkeypatch.setattr(registry, "record_agent_step", fake_record_agent_step)

    usage = {"claude-opus-4-7": {"inputTokens": 900, "outputTokens": 210, "cacheReadInputTokens": 40, "cacheCreationInputTokens": 3}}

    async def _fake_query(*args, **kwargs):
        yield _result_message(cost=0.03, model_usage=usage)

    monkeypatch.setattr(brain, "query", _fake_query)

    repo = tmp_path / "repo"
    repo.mkdir()

    asyncio.run(brain.run_autonomous_cycle(repo, "Ship useful features.", EnvironmentConfig()))

    cost_step = next(s for s in recorded_steps if s["agent"] == "cycle" and s["cost_usd"] == 0.03)
    assert cost_step["input_tokens"] == 900
    assert cost_step["output_tokens"] == 210
    assert cost_step["cache_read_input_tokens"] == 40
    assert cost_step["cache_creation_input_tokens"] == 3


def test_no_orchestrator_cost_step_recorded_when_query_never_yields_a_result_message(tmp_path, monkeypatch):
    """A hard-stop before query() is even called (e.g. a blocking bug) must
    not fabricate a cost step -- there was no real ResultMessage/spend."""
    monkeypatch.setattr("agentra.agents.brain.deployment.persist_audit_trail", lambda *a, **k: None)
    recorded_steps = []

    def fake_record_agent_step(app, run_id, agent, ok, cost_usd, turns, summary, **kwargs):
        recorded_steps.append(dict(agent=agent, cost_usd=cost_usd))

    monkeypatch.setattr(registry, "record_agent_step", fake_record_agent_step)
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

    async def _fail_if_called(*args, **kwargs):
        raise AssertionError("query() must not be called while a blocking_agentra bug is open")

    monkeypatch.setattr(brain, "query", _fail_if_called)

    repo = tmp_path / "repo"
    repo.mkdir()

    report = asyncio.run(brain.run_autonomous_cycle(repo, "Ship useful features.", EnvironmentConfig()))

    assert report.cost_usd == 0.0
    assert all(s["cost_usd"] == 0.0 for s in recorded_steps)

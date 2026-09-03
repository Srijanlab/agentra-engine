"""GitHub issue #74: the orchestrator's own reasoning cost (the top-level
query() call in agents/brain/__init__.py::run_autonomous_cycle, not any
sub-agent tool call) must be attributed to the orchestrator, not hidden in
the run's cost aggregate.

Per-agent cost/tokens now land in Langfuse as an `orchestrator` generation
(_emit_orchestrator_generation) rather than a Firestore agent_steps row.
These fast unit tests monkeypatch query() and the Langfuse client.
"""

import asyncio

from claude_agent_sdk import ResultMessage

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


class _FakeGeneration:
    def __init__(self, sink, kwargs):
        self._sink = sink
        self._kwargs = kwargs

    def end(self, **_):
        self._sink.append(self._kwargs)


class _FakeClient:
    def __init__(self, sink):
        self._sink = sink

    def start_observation(self, **kwargs):
        return _FakeGeneration(self._sink, kwargs)

    def update_current_span(self, **_):
        pass

    def get_current_trace_id(self):
        return "trace-abc"


def _common_monkeypatches(monkeypatch):
    monkeypatch.setattr("agentra.agents.brain.deployment.persist_audit_trail", lambda *a, **k: None)
    monkeypatch.setattr(Memory, "blocking_bugs", lambda self: [])


def _run(monkeypatch, tmp_path, msg):
    generations: list[dict] = []
    monkeypatch.setattr(brain, "get_client", lambda: _FakeClient(generations))

    async def _fake_query(*args, **kwargs):
        yield msg

    monkeypatch.setattr(brain, "query", _fake_query)
    repo = tmp_path / "repo"
    repo.mkdir()
    report = asyncio.run(brain.run_autonomous_cycle(repo, "Ship useful features.", EnvironmentConfig()))
    return report, [g for g in generations if g.get("name") == "orchestrator"]


def test_orchestrator_result_message_cost_becomes_a_generation(tmp_path, monkeypatch):
    _common_monkeypatches(monkeypatch)
    report, gens = _run(monkeypatch, tmp_path, _result_message(cost=0.0123, num_turns=4))

    assert report.cost_usd == 0.0123  # run aggregate unchanged
    assert len(gens) == 1
    assert gens[0]["cost_details"] == {"total": 0.0123}
    assert gens[0]["metadata"]["turns"] == 4
    assert gens[0]["level"] == "DEFAULT"


def test_failed_result_message_marks_the_generation_as_error(tmp_path, monkeypatch):
    _common_monkeypatches(monkeypatch)
    _report, gens = _run(monkeypatch, tmp_path, _result_message(cost=0.05, is_error=True))

    assert len(gens) == 1
    assert gens[0]["level"] == "ERROR"
    assert gens[0]["cost_details"] == {"total": 0.05}


def test_generation_carries_token_usage_from_model_usage(tmp_path, monkeypatch):
    _common_monkeypatches(monkeypatch)
    usage = {"claude-opus-4-7": {"inputTokens": 900, "outputTokens": 210, "cacheReadInputTokens": 40, "cacheCreationInputTokens": 3}}
    _report, gens = _run(monkeypatch, tmp_path, _result_message(cost=0.03, model_usage=usage))

    assert len(gens) == 1
    details = gens[0]["usage_details"]
    assert details["input_tokens"] == 900
    assert details["output_tokens"] == 210
    assert details["cache_read_input_tokens"] == 40
    assert details["cache_creation_input_tokens"] == 3


def test_no_generation_when_query_never_yields_a_result_message(tmp_path, monkeypatch):
    """A hard-stop before query() runs (e.g. a blocking bug) must not fabricate
    an orchestrator generation -- there was no real spend."""
    monkeypatch.setattr("agentra.agents.brain.deployment.persist_audit_trail", lambda *a, **k: None)
    monkeypatch.setattr(
        Memory, "blocking_bugs",
        lambda self: [{"external_id": "9", "diagnosis": "x", "proposed_fix": "y"}],
    )
    generations: list[dict] = []
    monkeypatch.setattr(brain, "get_client", lambda: _FakeClient(generations))

    async def _fake_query(*args, **kwargs):
        raise AssertionError("query() should not be reached")
        yield  # pragma: no cover

    monkeypatch.setattr(brain, "query", _fake_query)
    repo = tmp_path / "repo"
    repo.mkdir()
    asyncio.run(brain.run_autonomous_cycle(repo, "Ship useful features.", EnvironmentConfig()))

    assert [g for g in generations if g.get("name") == "orchestrator"] == []

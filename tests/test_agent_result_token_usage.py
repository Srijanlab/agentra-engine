"""GitHub issue #74 (token-usage half): AgentResult now carries token usage
(input/output/cache-read/cache-creation), summed across every model used in
a turn (ResultMessage.model_usage) via agents/base.py::_sum_model_usage --
populated at both call sites that used to only pull total_cost_usd
(run_agent and stream_chat_turn).

Fast unit tests (query() monkeypatched, no real LLM call) -- same pattern
as tests/test_run_agent_tool_isolation.py.
"""

import asyncio

from claude_agent_sdk import ResultMessage

from agentra.agents import base


def _result_message(model_usage: dict | None = None, cost: float = 0.02) -> ResultMessage:
    return ResultMessage(
        subtype="success", duration_ms=10, duration_api_ms=9, is_error=False, num_turns=3,
        session_id="s1", total_cost_usd=cost, result="done", terminal_reason="completed",
        model_usage=model_usage,
    )


def _fake_query_yielding(message):
    async def _fake_query(prompt, options):
        yield message

    return _fake_query


# -- _sum_model_usage ---------------------------------------------------------


def test_sum_model_usage_returns_zeros_for_none():
    assert base._sum_model_usage(None) == {
        "input_tokens": 0, "output_tokens": 0,
        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
    }


def test_sum_model_usage_returns_zeros_for_empty_dict():
    assert base._sum_model_usage({}) == {
        "input_tokens": 0, "output_tokens": 0,
        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
    }


def test_sum_model_usage_reads_a_single_models_counters():
    usage = {
        "claude-opus-4-7": {
            "inputTokens": 100, "outputTokens": 50,
            "cacheReadInputTokens": 20, "cacheCreationInputTokens": 5,
            "costUSD": 0.02, "webSearchRequests": 0, "contextWindow": 200000, "maxOutputTokens": 8192,
        }
    }
    assert base._sum_model_usage(usage) == {
        "input_tokens": 100, "output_tokens": 50,
        "cache_read_input_tokens": 20, "cache_creation_input_tokens": 5,
    }


def test_sum_model_usage_sums_across_more_than_one_model():
    usage = {
        "claude-opus-4-7": {"inputTokens": 100, "outputTokens": 50, "cacheReadInputTokens": 20, "cacheCreationInputTokens": 5},
        "claude-haiku-4-5": {"inputTokens": 30, "outputTokens": 10, "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0},
    }
    assert base._sum_model_usage(usage) == {
        "input_tokens": 130, "output_tokens": 60,
        "cache_read_input_tokens": 20, "cache_creation_input_tokens": 5,
    }


def test_sum_model_usage_treats_a_missing_key_within_a_models_entry_as_zero():
    usage = {"claude-opus-4-7": {"inputTokens": 100}}
    assert base._sum_model_usage(usage) == {
        "input_tokens": 100, "output_tokens": 0,
        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
    }


# -- run_agent populates AgentResult's token fields --------------------------


def test_run_agent_populates_token_fields_from_model_usage(tmp_path, monkeypatch):
    usage = {"claude-opus-4-7": {"inputTokens": 1200, "outputTokens": 340, "cacheReadInputTokens": 500, "cacheCreationInputTokens": 60}}
    monkeypatch.setattr(base, "query", _fake_query_yielding(_result_message(model_usage=usage)))

    result = asyncio.run(
        base.run_agent(
            prompt="do the thing", system_prompt="you are an agent", cwd=tmp_path,
            allowed_tools=["Read"], agent_label="Test Agent",
        )
    )

    assert result.input_tokens == 1200
    assert result.output_tokens == 340
    assert result.cache_read_input_tokens == 500
    assert result.cache_creation_input_tokens == 60


def test_run_agent_defaults_token_fields_to_zero_when_model_usage_is_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(base, "query", _fake_query_yielding(_result_message(model_usage=None)))

    result = asyncio.run(
        base.run_agent(
            prompt="do the thing", system_prompt="you are an agent", cwd=tmp_path,
            allowed_tools=["Read"], agent_label="Test Agent",
        )
    )

    assert result.input_tokens == 0
    assert result.output_tokens == 0
    assert result.cache_read_input_tokens == 0
    assert result.cache_creation_input_tokens == 0


def test_agent_result_early_return_paths_default_token_fields_to_zero():
    """AgentResult built without a real ResultMessage (e.g. an early-return
    error path in run_agent) must not need every caller to pass token
    fields explicitly."""
    result = base.AgentResult(ok=False, text="agent turn raised: boom", json_data=None, cost_usd=0.0, turns=0)
    assert result.input_tokens == 0
    assert result.output_tokens == 0
    assert result.cache_read_input_tokens == 0
    assert result.cache_creation_input_tokens == 0


# -- stream_chat_turn also threads token usage (same call-site pattern) -----


def test_stream_chat_turn_yields_token_fields_in_its_done_event(tmp_path, monkeypatch):
    usage = {"claude-opus-4-7": {"inputTokens": 400, "outputTokens": 90}}
    monkeypatch.setattr(base, "query", _fake_query_yielding(_result_message(model_usage=usage)))

    async def _collect():
        events = []
        async for event in base.stream_chat_turn(
            prompt="hi", system_prompt="you are an agent", cwd=tmp_path, allowed_tools=["Read"],
        ):
            events.append(event)
        return events

    events = asyncio.run(_collect())
    done = next(e for e in events if e["type"] == "done")
    assert done["input_tokens"] == 400
    assert done["output_tokens"] == 90
    assert done["cache_read_input_tokens"] == 0
    assert done["cache_creation_input_tokens"] == 0

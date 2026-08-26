"""GitHub issue #74: registry.record_agent_step/list_agent_steps must round-
trip the new optional token-usage fields (input_tokens, output_tokens,
cache_read_input_tokens, cache_creation_input_tokens) alongside cost_usd,
without breaking existing callers that only ever passed the original
positional args (e.g. orchestrator.py's fixed pipeline).

Local-JSON backend only (core._db left None) -- same as every other
registry test in this suite; Firestore itself isn't exercised here.
"""

from pathlib import Path

import pytest

from agentra.registry import core, runs


@pytest.fixture
def agent_steps_path(tmp_path, monkeypatch):
    path = tmp_path / "agent_steps.jsonl"
    monkeypatch.setattr(core, "_AGENT_STEPS_PATH", path)
    monkeypatch.setattr(core, "_db", None)
    return path


def test_record_agent_step_persists_token_fields_when_provided(agent_steps_path: Path):
    runs.record_agent_step(
        "myapp", "run1", "cycle", True, 0.05, 3, "orchestrator reasoning",
        input_tokens=1000, output_tokens=200, cache_read_input_tokens=50, cache_creation_input_tokens=10,
    )

    steps = runs.list_agent_steps(app="myapp")

    assert len(steps) == 1
    assert steps[0]["input_tokens"] == 1000
    assert steps[0]["output_tokens"] == 200
    assert steps[0]["cache_read_input_tokens"] == 50
    assert steps[0]["cache_creation_input_tokens"] == 10


def test_record_agent_step_defaults_token_fields_to_none_when_not_provided(agent_steps_path: Path):
    """Existing callers (e.g. orchestrator.py) that only pass the original
    7 positional args must keep working unchanged -- None means "not
    reported", not a fabricated zero."""
    runs.record_agent_step("myapp", "run1", "understand_codebase", True, 0.01, 2, "understand_codebase: ok=True")

    steps = runs.list_agent_steps(app="myapp")

    assert len(steps) == 1
    assert steps[0]["input_tokens"] is None
    assert steps[0]["output_tokens"] is None
    assert steps[0]["cache_read_input_tokens"] is None
    assert steps[0]["cache_creation_input_tokens"] is None
    assert steps[0]["cost_usd"] == 0.01

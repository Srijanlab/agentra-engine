"""_tools_for() (agents/brain/tools.py) derives its exposed tool list and
order from agents/catalog.py's AGENT_METADATA["orchestrator"]["tools"] --
that registry is now the single source of truth for "what capabilities
exist and in what order", instead of a literal array in tools.py that could
silently drift out of sync with catalog.py.
"""

from pathlib import Path

import pytest

from agentra.agents import brain, catalog as agents_catalog
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
    )
    defaults.update(overrides)
    return brain.OrchestratorSession(**defaults)


def test_tools_for_matches_orchestrator_catalog_entry_exactly(tmp_path):
    tools = brain._tools_for(_session(tmp_path))
    expected = [m["name"] for m in agents_catalog.AGENT_METADATA["orchestrator"]["tools"]]
    assert [t.name for t in tools] == expected


def test_tools_for_raises_a_clear_error_on_a_catalog_tool_name_with_no_matching_closure(tmp_path, monkeypatch):
    bogus = {**agents_catalog.AGENT_METADATA["orchestrator"], "tools": [{"name": "not_a_real_tool", "permission": "delegate"}]}
    monkeypatch.setitem(agents_catalog.AGENT_METADATA, "orchestrator", bogus)

    with pytest.raises(RuntimeError, match="not_a_real_tool"):
        brain._tools_for(_session(tmp_path))

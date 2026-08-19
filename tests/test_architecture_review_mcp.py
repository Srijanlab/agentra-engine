"""agents/architecture_review.py's MCP wiring -- Architecture Review Agent is
deliberately kept Bash-free (strictly read-only, see its own module
docstring), so the only way to give it live graph queries is scoped MCP
tools rather than a shell. codegraph.mcp_config(repo) is the single source
of truth for whether a graph exists to query; run() must reflect that in
both allowed_tools and the mcp_servers config it passes to run_agent,
never granting mcp__graphify__* tool names with no server behind them.
"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

from agentra.agents import architecture_review, codegraph
from agentra.agents.base import AgentResult


def test_run_grants_mcp_tools_and_server_when_a_graph_exists(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    fake_config = {"graphify": {"command": "graphify-mcp", "args": ["/fake/graph.json"]}}
    monkeypatch.setattr(codegraph, "mcp_config", lambda repo_arg: fake_config)

    captured = {}

    async def fake_run_agent(**kwargs):
        captured.update(kwargs)
        return AgentResult(ok=True, text="```json\n{}\n```", json_data={}, cost_usd=0.01, turns=1)

    monkeypatch.setattr(architecture_review, "run_agent", fake_run_agent)

    asyncio.run(architecture_review.run(repo, "objective", "a feature brief", "codebase summary"))

    assert captured["mcp_servers"] == fake_config
    assert set(codegraph.READ_ONLY_MCP_TOOLS).issubset(captured["allowed_tools"])
    assert {"Read", "Glob", "Grep"}.issubset(captured["allowed_tools"])
    # Never granted: these hit the GitHub API, not a local graph read.
    assert not any("pr" in t.lower() for t in captured["allowed_tools"])


def test_run_stays_read_only_tools_when_no_graph_exists_yet(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(codegraph, "mcp_config", lambda repo_arg: {})

    captured = {}

    async def fake_run_agent(**kwargs):
        captured.update(kwargs)
        return AgentResult(ok=True, text="```json\n{}\n```", json_data={}, cost_usd=0.01, turns=1)

    monkeypatch.setattr(architecture_review, "run_agent", fake_run_agent)

    asyncio.run(architecture_review.run(repo, "objective", "a feature brief", "codebase summary"))

    assert captured["mcp_servers"] == {}
    assert captured["allowed_tools"] == ["Read", "Glob", "Grep"]  # unchanged from before MCP wiring existed

"""agents/base.py::run_agent must set ClaudeAgentOptions.tools, not just
allowed_tools -- same class of gap tests/test_brain_tool_isolation.py caught
live in brain/__init__.py's own separate query() call (run 26bf7dee): with
tools=None (the SDK default) and permission_mode="bypassPermissions" (every
agent call here), the full built-in Claude Code toolset (Bash, Write,
WebSearch, ...) is available regardless of allowed_tools -- allowed_tools
only governs auto-approval, and bypassPermissions auto-approves everything
anyway. A "read-only" agent (allowed_tools=["Read","Glob","Grep"]) was
therefore never actually prevented from calling Bash/Write, only asked not
to in its system prompt.

Fast unit tests (query() monkeypatched, no real LLM call) -- same split as
test_brain_tool_isolation.py's own docstring describes.
"""

import asyncio

from agentra.agents import base


def _fake_query_capturing(captured):
    async def _fake_query(prompt, options):
        captured["options"] = options
        return
        yield  # pragma: no cover -- makes this an async generator, never reached

    return _fake_query


def test_run_agent_restricts_tools_to_allowed_tools(tmp_path, monkeypatch):
    captured = {}
    monkeypatch.setattr(base, "query", _fake_query_capturing(captured))

    asyncio.run(
        base.run_agent(
            prompt="do the thing",
            system_prompt="you are an agent",
            cwd=tmp_path,
            allowed_tools=["Read", "Glob", "Grep"],
            agent_label="Test Agent",
        )
    )

    options = captured["options"]
    assert options.tools == ["Read", "Glob", "Grep"], (
        "tools= must mirror allowed_tools -- leaving it unset grants the full "
        "built-in toolset regardless of what allowed_tools claims to restrict"
    )
    assert options.allowed_tools == ["Read", "Glob", "Grep"]


def test_run_agent_excludes_mcp_tool_names_from_tools_but_keeps_them_in_allowed_tools(tmp_path, monkeypatch):
    """mcp__<server>__<tool> names aren't built-in tool names -- tools=
    only governs the built-in set; MCP tool availability comes from
    mcp_servers instead. Passing them into tools= would be meaningless at
    best and could reject the option outright at worst."""
    captured = {}
    monkeypatch.setattr(base, "query", _fake_query_capturing(captured))

    asyncio.run(
        base.run_agent(
            prompt="do the thing",
            system_prompt="you are an agent",
            cwd=tmp_path,
            allowed_tools=["Read", "Glob", "Grep", "mcp__graphify__query_graph"],
            mcp_servers={"graphify": {"command": "graphify-mcp", "args": ["/fake/graph.json"]}},
            agent_label="Test Agent",
        )
    )

    options = captured["options"]
    assert options.tools == ["Read", "Glob", "Grep"]
    assert options.allowed_tools == ["Read", "Glob", "Grep", "mcp__graphify__query_graph"]
    assert options.mcp_servers == {"graphify": {"command": "graphify-mcp", "args": ["/fake/graph.json"]}}


def test_run_agent_defaults_mcp_servers_to_empty_dict(tmp_path, monkeypatch):
    captured = {}
    monkeypatch.setattr(base, "query", _fake_query_capturing(captured))

    asyncio.run(
        base.run_agent(
            prompt="do the thing",
            system_prompt="you are an agent",
            cwd=tmp_path,
            allowed_tools=["Read"],
            agent_label="Test Agent",
        )
    )

    assert captured["options"].mcp_servers == {}

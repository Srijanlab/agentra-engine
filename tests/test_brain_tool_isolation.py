"""run_autonomous_cycle's ClaudeAgentOptions must actually restrict the
orchestrator to its ten MCP tools -- not just claim to in the system prompt.

Confirmed live (run 26bf7dee, 2026-08-18): allowed_tools alone does NOT
restrict tool *availability*, only auto-approval -- per claude_agent_sdk's
own docs, availability is controlled by `tools`, and
permission_mode="bypassPermissions" auto-approves every tool regardless of
allowed_tools. Without `tools=[]`, the orchestrator had the full built-in
Claude Code toolset (Bash, Read, Write, WebSearch, ToolSearch, ...)
available and unrestricted -- that live run minted its own GitHub App
installation token via raw Bash/python3 and ran arbitrary git/gh/curl
commands, entirely outside the sanctioned MCP tools and their audit trail,
with no PreToolUse hook (agents/safety.py's make_hooks, which every other
agent gets via agents/base.py's run_agent) to catch it either.

This is a fast unit test (query() monkeypatched, no real LLM call) -- it
proves *this codebase's own code* sets the right options, same class of
regression as tests/test_brain_blocking_bugs.py's deterministic pre-flight
check. It cannot prove the SDK actually enforces `tools=[]`/hooks= as
documented -- that's covered by the real, costed integration test in
tests/test_safety_integration.py, run explicitly, same split as that file's
own docstring describes for the can_use_tool regression it caught."""

import asyncio

from agentra import registry
from agentra.agents import brain
from agentra.environments import EnvironmentConfig
from agentra.memory import Memory


def test_run_autonomous_cycle_disables_every_built_in_tool_and_wires_the_safety_hook(tmp_path, monkeypatch):
    monkeypatch.setattr(brain.deployment, "persist_audit_trail", lambda *a, **k: None)
    monkeypatch.setattr(Memory, "blocking_bugs", lambda self: [])

    captured = {}

    async def _fake_query(prompt, options):
        captured["options"] = options
        return
        yield  # pragma: no cover -- makes this an async generator, never reached

    monkeypatch.setattr(brain, "query", _fake_query)

    repo = tmp_path / "repo"
    repo.mkdir()

    asyncio.run(brain.run_autonomous_cycle(repo, "Ship useful features.", EnvironmentConfig()))

    options = captured["options"]
    assert options.tools == [], (
        "orchestrator must have zero built-in tools (Bash/Read/Write/WebSearch/...) -- "
        "only the mcp__agentra_brain__* tools granted via allowed_tools/mcp_servers"
    )
    assert options.hooks is not None, (
        "orchestrator must have the PreToolUse safety hook wired (agents/safety.py's "
        "make_hooks), matching every other agent's run_agent() call, as defense-in-depth"
    )
    assert all(name.startswith("mcp__agentra_brain__") for name in options.allowed_tools)

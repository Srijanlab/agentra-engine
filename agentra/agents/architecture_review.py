"""Architecture Review Agent (vision.md 5.5) -- called conditionally by the
Orchestrator, before Implementation Agent, only when a feature brief looks
architecturally significant (a schema change, a new API surface, a
cross-cutting refactor spanning more than one layer). Not called on every
implement_feature -- same "last resort"-style conditionality as
discover_opportunities being called only once the backlog is empty.

Strictly advisory and read-only: assesses blast radius and risk, never
proposes specific code, never edits anything, and has no authority to block
implementation or escalate to a human -- the Orchestrator reads its
assessment and decides what to do with it.
"""

from pathlib import Path

from agentra.agents import codegraph
from agentra.agents.base import AgentResult, run_agent

SYSTEM_PROMPT = """You are the Architecture Review Agent in an autonomous product \
engineering system. You're called conditionally, before Implementation Agent starts, only \
when the Orchestrator judges a feature brief architecturally significant -- most features \
skip you entirely. Your job is to assess blast radius and risk, not to design the solution \
or write any code: you are strictly read-only and never propose specific code changes, only \
flag what an implementer should be careful about.

1. Read the brief and the codebase summary. Identify every layer of the system the brief \
   plausibly touches: frontend, backend, database/schema, public API surface, \
   infra/deployment config, or cross-cutting shared code used by multiple features. If \
   mcp__graphify__* tools are available, use them to check real blast radius before guessing \
   from memory: query_graph/get_neighbors for what touches a given area, shortest_path for \
   how two things connect, god_nodes for the hubs most likely to ripple across layers.
2. For each layer touched, name the concrete risk in plain terms -- not a generic warning. \
   "Adding a NOT NULL column to the users table requires a backfill for existing rows" is a \
   valid concern; "this could affect the database" is not.
3. Judge an overall risk_level: "low" (single layer, additive, no schema/API/shared-code \
   change), "medium" (spans two layers, or a backward-compatible schema/API addition), or \
   "high" (a breaking schema change, a new public API surface, or a change to code shared \
   across multiple existing features).
4. Give a plain recommendation: "proceed" (low risk, safe to build as briefed), \
   "proceed_with_caution" (medium/high risk but still buildable -- name exactly what \
   Implementation Agent should be careful of), or "needs_narrower_scope" (the brief as \
   written is too broad/risky to build in one pass -- suggest how to split it).

You do not have the authority to block implementation or escalate to a human -- that decision \
belongs to the Orchestrator, who reads your assessment and decides what to do next.

End your response with a fenced ```json block shaped like:
{
  "layers_touched": ["frontend" | "backend" | "database" | "api" | "infra" | "shared_code", ...],
  "risk_level": "low" | "medium" | "high",
  "concerns": ["...", "..."],
  "recommendation": "proceed" | "proceed_with_caution" | "needs_narrower_scope"
}
"""


async def run(repo: Path, objective: str, feature_brief: str, codebase_summary: str) -> AgentResult:
    prompt = f"""Business objective: {objective}

Codebase summary:
{codebase_summary}

Feature brief to assess: {feature_brief}

Assess architectural blast radius and risk now, following your system prompt."""
    # mcp_config is {} when no graph has been built for `repo` yet (best-effort,
    # see codegraph.py) -- allowed_tools then stays exactly Read/Glob/Grep, same
    # as before this existed, rather than granting mcp__graphify__* tool names
    # with no server behind them.
    mcp_servers = codegraph.mcp_config(repo)
    allowed_tools = ["Read", "Glob", "Grep"] + (codegraph.READ_ONLY_MCP_TOOLS if mcp_servers else [])
    return await run_agent(
        prompt=prompt,
        system_prompt=SYSTEM_PROMPT,
        cwd=repo,
        allowed_tools=allowed_tools,
        permission_mode="bypassPermissions",
        max_turns=15,
        agent_label="Architecture Review Agent",
        mcp_servers=mcp_servers,
    )

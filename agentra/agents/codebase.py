"""Codebase Understanding Agent (vision.md 5.2).

Read-only scan of the target repo: framework, architecture, existing
features. Output feeds every downstream agent, so it never touches Write/Edit/Bash.
"""

from pathlib import Path

from agentra.agents.base import AgentResult, run_agent

SYSTEM_PROMPT = """You are the Codebase Understanding Agent in an autonomous \
product engineering system. Your only job is to build an accurate, concise \
picture of the repository you are pointed at. You are read-only: never \
propose edits, never run mutating commands.

Investigate:
- Framework(s) and language(s) in use
- Overall architecture (monolith, serverless, microservices, etc.)
- Backend/data layer
- Existing user-facing features
- Test and build tooling already configured (so later agents know what to run)

End your response with a fenced ```json block shaped like:
{
  "framework": "...",
  "backend": "...",
  "architecture": "...",
  "features": ["...", "..."],
  "test_commands": ["..."],
  "build_commands": ["..."],
  "notes": "..."
}
"""


async def run(repo: Path) -> AgentResult:
    prompt = (
        "Scan this repository and produce the codebase understanding summary "
        "described in your system prompt."
    )
    return await run_agent(
        prompt=prompt,
        system_prompt=SYSTEM_PROMPT,
        cwd=repo,
        allowed_tools=["Read", "Glob", "Grep"],
        permission_mode="bypassPermissions",
        agent_label="Codebase Agent",
    )

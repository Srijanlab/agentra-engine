"""Implementation Agent (vision.md 5.6).

Owns the implement -> test -> fix -> retry loop for a single feature. Runs
inside one long agent turn so it can self-correct against its own test runs
before handing off to the independent Testing Agent for final QA.
"""

from pathlib import Path

from agentos.agents.base import AgentResult, run_agent

SYSTEM_PROMPT = """You are the Implementation Agent in an autonomous product \
engineering system. You are given a codebase summary and a specific feature \
to build. Work in a tight loop:

1. Implement the smallest coherent version of the feature.
2. Run the project's existing test/build commands yourself via Bash.
3. If anything fails, fix it and re-run. Repeat until green or you are \
   confident further attempts won't help.
4. Make a git commit of your change on the current branch once it's working. \
   Do not push, do not open a PR, do not touch git history beyond one commit.

Constraints:
- Prefer minimal, targeted changes over refactors.
- Never touch secrets, billing, or production config.
- Never run destructive or irreversible commands.

End your response with a fenced ```json block shaped like:
{
  "feature": "...",
  "status": "implemented" | "partially_implemented" | "blocked",
  "files_changed": ["..."],
  "self_test_result": "pass" | "fail" | "not_run",
  "notes": "..."
}
"""


async def run(repo: Path, objective: str, feature: str, codebase_summary: str) -> AgentResult:
    prompt = f"""Business objective: {objective}

Feature to implement: {feature}

Codebase summary:
{codebase_summary}

Implement this feature now, following the loop in your system prompt."""
    return await run_agent(
        prompt=prompt,
        system_prompt=SYSTEM_PROMPT,
        cwd=repo,
        allowed_tools=["Read", "Write", "Edit", "Glob", "Grep", "Bash"],
        permission_mode="bypassPermissions",
        max_turns=60,
    )

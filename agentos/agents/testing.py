"""Testing Agent (vision.md 5.7).

Independent QA pass after implementation: lint, typecheck, unit/integration/e2e
as available. Deliberately separate from the Implementation Agent's own
self-testing loop so a broken "it works on my machine" self-report can still
be caught.
"""

from pathlib import Path

from agentos.agents.base import AgentResult, run_agent

SYSTEM_PROMPT = """You are the Testing Agent in an autonomous product \
engineering system. A feature was just implemented. Independently verify it:

1. Run lint and typecheck if configured.
2. Run the unit and integration test suites.
3. Run end-to-end/browser tests if the project has them configured (e.g. \
   Playwright); do not set up new e2e infrastructure yourself.
4. Do not modify source files. You may only fix trivial test-runner \
   configuration blockers (e.g. a missing test script); if you find real \
   bugs, report them rather than patching product code.

End your response with a fenced ```json block shaped like:
{
  "status": "pass" | "fail",
  "failed_tests": ["..."],
  "lint_status": "pass" | "fail" | "not_configured",
  "typecheck_status": "pass" | "fail" | "not_configured",
  "notes": "..."
}
"""


async def run(repo: Path, codebase_summary: str) -> AgentResult:
    prompt = f"""Codebase summary:
{codebase_summary}

Run the full test/QA pass now, following your system prompt."""
    return await run_agent(
        prompt=prompt,
        system_prompt=SYSTEM_PROMPT,
        cwd=repo,
        allowed_tools=["Read", "Bash", "Glob", "Grep"],
        permission_mode="bypassPermissions",
        max_turns=30,
    )

"""Implementation Agent (vision.md 5.6).

Owns the implement -> test -> fix -> retry loop for a single feature. Runs
inside one long agent turn so it can self-correct against its own test runs
before handing off to the independent Testing Agent for final QA.

Must commit to the app's configured dev branch, not just "whatever is
currently checked out" — Deployment Agent's beta step merges from
dev_branch specifically (see agents/deployment.py), so a commit landing
anywhere else is silently invisible to beta and gets deployed over.
"""

from pathlib import Path

from agentos.agents.base import AgentResult, run_agent
from agentos.environments import EnvironmentConfig

SYSTEM_PROMPT = """You are the Implementation Agent in an autonomous product \
engineering system. You are given a codebase summary and a specific feature \
to build. Work in a tight loop:

0. Before anything else, make sure you are on the dev branch: \
   `git checkout {dev_branch}` if it exists, otherwise \
   `git checkout -B {dev_branch}` to create it from the current HEAD. All \
   work this run happens on {dev_branch} — never main/{prod_branch} or any \
   other branch.
1. Implement the smallest coherent version of the feature.
2. Run the project's existing test/build commands yourself via Bash.
3. If anything fails, fix it and re-run. Repeat until green or you are \
   confident further attempts won't help.
4. Make a git commit of your change on {dev_branch} once it's working. Do \
   not push, do not open a PR, do not touch git history beyond one commit.

Constraints:
- Prefer minimal, targeted changes over refactors.
- Never touch secrets, billing, or production config.
- Never run destructive or irreversible commands.

End your response with a fenced ```json block shaped like:
{{
  "feature": "...",
  "status": "implemented" | "partially_implemented" | "blocked",
  "files_changed": ["..."],
  "self_test_result": "pass" | "fail" | "not_run",
  "notes": "..."
}}
"""


async def run(repo: Path, objective: str, feature: str, codebase_summary: str, env: EnvironmentConfig) -> AgentResult:
    prompt = f"""Business objective: {objective}

Feature to implement: {feature}

Codebase summary:
{codebase_summary}

Implement this feature now, following the loop in your system prompt."""
    system_prompt = SYSTEM_PROMPT.format(dev_branch=env.dev_branch, prod_branch=env.prod_branch)
    return await run_agent(
        prompt=prompt,
        system_prompt=system_prompt,
        cwd=repo,
        allowed_tools=["Read", "Write", "Edit", "Glob", "Grep", "Bash"],
        permission_mode="bypassPermissions",
        max_turns=60,
    )

"""Deployment Agent (vision.md 5.8) — beta/preview only.

Production deployment is out of scope by design (see safety.py) and requires
human approval regardless of what this agent decides.
"""

from pathlib import Path

from agentos.agents.base import AgentResult, run_agent

SYSTEM_PROMPT = """You are the Deployment Agent in an autonomous product \
engineering system. Your only allowed target is a preview/staging \
environment.

1. Detect whether this repo has a configured deploy target (Vercel, \
   Netlify, Firebase Hosting, etc.).
2. If none is configured, do nothing and report that deployment was skipped.
3. If one is configured, deploy to preview/staging only.
4. Never deploy to production, never pass --prod or equivalent flags, never \
   touch production environment variables or secrets.
5. After deploying, run any available smoke test and report the preview URL \
   if one was produced.

End your response with a fenced ```json block shaped like:
{
  "status": "deployed" | "skipped" | "failed",
  "preview_url": "..." ,
  "notes": "..."
}
"""


async def run(repo: Path) -> AgentResult:
    prompt = "Deploy the current state of this repo to a preview/staging environment, following your system prompt."
    return await run_agent(
        prompt=prompt,
        system_prompt=SYSTEM_PROMPT,
        cwd=repo,
        allowed_tools=["Read", "Bash", "Glob", "Grep"],
        permission_mode="bypassPermissions",
        max_turns=20,
    )

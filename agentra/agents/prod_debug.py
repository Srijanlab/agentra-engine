"""Production Debugging Agent (vision.md — post-launch monitoring)."""

from pathlib import Path

from agentra.agents.base import AgentResult, run_agent
from agentra.environments import EnvironmentConfig

SYSTEM_PROMPT = """You are the Production Debugging Agent in an autonomous \
product engineering system. You are investigating a possible issue in a live \
production environment. You are strictly read-only against production: \
inspect logs, do not deploy, do not modify prod config, do not run any \
mutating command against it.

Environment:
- Vercel configured: {vercel}
- Firebase configured: {firebase} (prod alias: {firebase_prod_alias})
- prod branch: {prod_branch}

Investigate using whatever is available:
- `vercel logs <deployment-url-or-project>` for frontend/runtime errors
- `firebase functions:log --only <fn>` for backend function errors
- `git log`, `git blame`, and the source itself to correlate errors with \
  recent changes
- The reported symptom, if one was given; otherwise scan for elevated error \
  rates or recent exceptions

Produce:
1. A root cause hypothesis, with the evidence that supports it.
2. A concrete, minimally-scoped fix description (specific enough to hand to \
   the Implementation Agent) — or, if you cannot find a confident root \
   cause, say so rather than guessing.
3. A severity assessment.

End your response with a fenced ```json block shaped like:
{{
  "root_cause_found": true | false,
  "severity": "low" | "medium" | "high" | "critical",
  "diagnosis": "...",
  "evidence": ["...", "..."],
  "proposed_fix": "...",
  "confidence": "low" | "medium" | "high"
}}
"""


async def diagnose(repo: Path, env: EnvironmentConfig, symptom: str | None) -> AgentResult:
    prompt = f"""Reported symptom: {symptom or "none given — scan production logs for issues yourself"}

Investigate the production issue now, following your system prompt."""
    system_prompt = SYSTEM_PROMPT.format(
        vercel=env.vercel,
        firebase=env.firebase,
        firebase_prod_alias=env.firebase_prod_alias,
        prod_branch=env.prod_branch,
    )
    return await run_agent(
        prompt=prompt,
        system_prompt=system_prompt,
        cwd=repo,
        allowed_tools=["Read", "Bash", "Glob", "Grep"],
        permission_mode="bypassPermissions",
        max_turns=25,
        allow_prod=False,
        agent_label="Production Debugging Agent",
    )

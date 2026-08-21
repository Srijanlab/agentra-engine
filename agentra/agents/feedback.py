"""Analytics Feedback Agent."""

from pathlib import Path

from agentra.agents.base import AgentResult, run_agent

SYSTEM_PROMPT = """You are the Analytics Feedback Agent in an autonomous \
product engineering system. A feature was just implemented and tested. \
No live analytics provider is connected in this environment, so you cannot \
report real DAU/retention numbers yet.

Instead:
1. Check whether the new code emits any analytics/tracking events (grep for \
   the project's existing analytics calls — e.g. logEvent, track(), \
   posthog.capture, analytics.track — and see if the new feature uses the \
   same convention). Do not add instrumentation yourself; only report on it.
2. Based on the feature and the business objective, name the 2-3 metrics \
   that would prove or disprove impact, and where they'd come from.
3. If instrumentation is missing, say so plainly — this is a real gap, not \
   a nice-to-have.

End your response with a fenced ```json block shaped like:
{
  "instrumented": true | false,
  "instrumentation_notes": "...",
  "success_metrics": ["...", "..."],
  "recommendation": "..."
}
"""


async def run(repo: Path, objective: str, feature: str, session_id: str | None = None) -> AgentResult:
    prompt = f"""Business objective: {objective}
Feature just shipped: {feature}

Assess measurability of this feature's impact, following your system prompt."""
    return await run_agent(
        prompt=prompt,
        system_prompt=SYSTEM_PROMPT,
        cwd=repo,
        allowed_tools=["Read", "Glob", "Grep"],
        permission_mode="bypassPermissions",
        max_turns=15,
        agent_label="Analytics Feedback Agent",
        resume=session_id,
    )

"""Product Discovery Agent (vision.md 5.3) — the critical component.

Decides *what to build next* without a human naming a feature. Looks at the
codebase, whatever analytics are available, what's already been shipped by
this system, and (via WebSearch) competitor products, then produces a ranked
list of feature opportunities.
"""

from pathlib import Path

from agentos.agents.base import AgentResult, run_agent

SYSTEM_PROMPT = """You are the Product Discovery Agent in an autonomous \
product engineering system. Your job is to decide what to build next, given \
only a business objective — nobody will tell you the feature.

Inputs you're given: a codebase summary, an analytics summary (may say "not \
available" — work around that by reasoning from the code itself: what \
engagement loops, retention mechanics, or sharing mechanisms are conspicuously \
missing), a list of features this system has already shipped (do not repeat \
these), and a list of known bugs found in production by the Production \
Debugging Agent. Known bugs are never optional background noise — a \
confirmed production bug always outranks a nice-to-have feature, so include \
each one as its own opportunity with impact "very_high" unless it's trivial.

You may use WebSearch to check what comparable products in this space do, \
if it would sharpen your recommendations. Do not guess at competitor \
features you haven't verified.

Produce 3-5 ranked feature opportunities. For each, weigh impact against \
effort and give a concrete reason grounded in what you observed (a specific \
drop-off, a missing mechanic, a competitor gap) — not a generic platitude.

End your response with a fenced ```json block shaped like:
{
  "opportunities": [
    {
      "feature": "short_snake_case_name",
      "description": "one paragraph, concrete enough to hand to an engineer",
      "impact": "low" | "medium" | "high" | "very_high",
      "effort": "low" | "medium" | "high",
      "reason": "..."
    }
  ]
}
"""


async def run(
    repo: Path,
    objective: str,
    codebase_summary: str,
    analytics_summary: str,
    already_shipped: list[str],
    known_bugs: list[dict] | None = None,
) -> AgentResult:
    shipped_text = "\n".join(f"- {f}" for f in already_shipped) or "(none yet)"
    bugs_text = (
        "\n".join(
            f"- [{b.get('severity', 'unknown')}] {b.get('diagnosis', '')} "
            f"(proposed fix: {b.get('proposed_fix', '')})"
            for b in (known_bugs or [])
        )
        or "(none)"
    )
    prompt = f"""Business objective: {objective}

Codebase summary:
{codebase_summary}

Analytics summary:
{analytics_summary}

Already shipped by this system (do not repeat):
{shipped_text}

Known bugs from production, awaiting a fix:
{bugs_text}

Identify what to build next, following your system prompt."""
    return await run_agent(
        prompt=prompt,
        system_prompt=SYSTEM_PROMPT,
        cwd=repo,
        allowed_tools=["Read", "Glob", "Grep", "WebSearch"],
        permission_mode="bypassPermissions",
        max_turns=20,
    )

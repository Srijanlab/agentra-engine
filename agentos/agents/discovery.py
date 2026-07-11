"""Product Discovery Agent (vision.md 5.3) — the critical component.

Decides *what to build next* without a human naming a feature. Looks at the
codebase, whatever analytics are available, what's already been shipped by
this system, and (via WebSearch) competitor products, then produces a ranked
list of feature opportunities. Known production bugs and the customer
feature-request queue (agentos/registry.py's inbox, absorbed by
`agentos dispatch`) both take priority over the agent's own autonomous
ideation -- see the priority ordering in the system prompt.
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
these), a list of known bugs found in production (by the Production Debugging \
Agent or reported by customers), and a queue of feature requests (from \
customers or added by an admin).

Priority order, strictly — do not reorder this based on your own judgment of \
impact/effort:
1. Known bugs. A confirmed production bug always outranks everything else. \
   Include each one as its own opportunity with impact "very_high" unless \
   it's genuinely trivial.
2. The feature request queue. A customer or admin already asked for these \
   specifically — include each as its own opportunity with impact at least \
   "high", ahead of anything you come up with yourself.
3. Only once 1 and 2 are covered, propose your own autonomously-discovered \
   opportunities using WebSearch (to check comparable products, if it would \
   sharpen your recommendations -- do not guess at competitor features you \
   haven't verified) and your own reasoning about the codebase and objective.

Produce 3-5 ranked feature opportunities in total, following that priority \
order. For each, give a concrete reason grounded in what you observed (a \
specific drop-off, a missing mechanic, a competitor gap, or -- for queued \
items -- who asked and why) — not a generic platitude.

For an opportunity sourced from a known bug or the feature queue, copy its \
`id` exactly as given in the input below into your output -- that's how the \
system marks the original backlog entry resolved once shipped, instead of it \
resurfacing every future cycle. Omit/null `id` for your own autonomous ideas.

End your response with a fenced ```json block shaped like:
{
  "opportunities": [
    {
      "feature": "short_snake_case_name",
      "description": "one paragraph, concrete enough to hand to an engineer",
      "impact": "low" | "medium" | "high" | "very_high",
      "effort": "low" | "medium" | "high",
      "reason": "...",
      "origin": "known_bug" | "feature_queue" | "autonomous",
      "id": "the bug's run_id or the feature request's external_id, or null"
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
    feature_queue: list[dict] | None = None,
) -> AgentResult:
    shipped_text = "\n".join(f"- {f}" for f in already_shipped) or "(none yet)"
    bugs_text = (
        "\n".join(
            f"- id={b.get('run_id', '')!r} [{b.get('severity', 'unknown')}] {b.get('diagnosis', '')} "
            f"(proposed fix: {b.get('proposed_fix', '')})"
            for b in (known_bugs or [])
        )
        or "(none)"
    )
    queue_text = (
        "\n".join(
            f"- id={f.get('external_id', '')!r} [{f.get('source', 'unknown')}] {f.get('description', '')}"
            for f in (feature_queue or [])
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

Feature request queue (customer/admin submitted, awaiting triage):
{queue_text}

Identify what to build next, following your system prompt's priority order."""
    return await run_agent(
        prompt=prompt,
        system_prompt=SYSTEM_PROMPT,
        cwd=repo,
        allowed_tools=["Read", "Glob", "Grep", "WebSearch"],
        permission_mode="bypassPermissions",
        max_turns=20,
    )

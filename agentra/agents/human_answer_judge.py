"""Human-Answer Judge Agent -- called by the deterministic infra-cost gate (agents/brain/infra_cost_gate.py) once a human has answered its blocking question, to decide whether that answer is a clear enough decision to proceed past the gate on this resumed cycle, or whether it still needs escalation."""

from pathlib import Path

from agentra.agents.base import AgentResult, run_agent

SYSTEM_PROMPT = """You are the Human-Answer Judge in an autonomous product engineering system. \
The deterministic infra-cost gate blocked a feature brief before implementation started and \
escalated a question to a human. A human has now answered. Your only job: decide whether that \
answer is a clear enough decision to act on, or whether it still needs escalation.

You are not re-litigating whether the brief is a good idea -- the human already made that \
call, and you have no authority to overrule it. You are only checking whether their answer \
actually resolves the specific question that was asked.

- "proceed": the answer gives clear enough direction to continue -- approval, an approved \
  narrower scope, explicit constraints or guidance to follow during implementation. Proceeding \
  with caveats to honor still counts as "proceed".
- "still_needs_escalation": the answer does not resolve the original question -- it's a \
  question back, a deferral ("let me think about it"), or an unclear/ambiguous reply. Do not \
  guess at what an unclear answer meant; when genuinely unsure, escalate again rather than \
  assume approval.

End your response with a fenced ```json block shaped like:
{
  "decision": "proceed" | "still_needs_escalation",
  "reason": "one concrete sentence explaining the decision"
}
"""


async def run(repo: Path, review: dict, human_answer: str, feature_brief: str) -> AgentResult:
    prompt = f"""Original architecture review that triggered the gate:
infra_cost_impact={review.get('infra_cost_impact')!r} risk_level={review.get('risk_level')!r}
Concerns: {review.get('concerns') or []}

Feature brief (as currently written, possibly already revised in light of the human's answer):
{feature_brief}

The human's answer to the blocking question:
{human_answer}

Decide now, following your system prompt."""
    return await run_agent(
        prompt=prompt,
        system_prompt=SYSTEM_PROMPT,
        cwd=repo,
        allowed_tools=[],
        permission_mode="bypassPermissions",
        max_turns=1,
        agent_label="Human-Answer Judge",
    )

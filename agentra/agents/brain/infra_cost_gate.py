"""Deterministic (plain Python, not LLM-judged) infra-cost gate -- Part 2/2 of the infra-cost
gate feature. Kept in its own module (SRP / subfolder organization) rather than growing the
already-large brain/tools.py further; tools.py wires this into implement_feature/
assess_design_impact and owns the session/escalation plumbing this depends on."""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from agentra.agents import architecture_review
from agentra.agents.base import AgentResult

if TYPE_CHECKING:
    from agentra.agents.brain import OrchestratorSession

# Deterministic keyword pre-filter: a feature_brief matching any of these plausibly touches
# infra and must not be allowed to skip a design-impact review just because the brain never
# called assess_design_impact itself.
_INFRA_COST_KEYWORDS = (
    "terraform", "cloud run", "min-instances", "min_instances", "always-on", "always on",
    "new service", "third-party api", "paid api", "new integration", "autoscal",
)


def brief_plausibly_touches_infra(feature_brief: str) -> bool:
    """Cheap, deterministic substring heuristic -- not an LLM judgment call."""
    text = feature_brief.lower()
    return any(keyword in text for keyword in _INFRA_COST_KEYWORDS)


def should_block(review: dict) -> bool:
    """True when a design-review result must deterministically block implementation:
    infra_cost_impact == "material", or risk_level == "high" with an "infra" layer touched."""
    infra_cost_impact = review.get("infra_cost_impact")
    risk_level = review.get("risk_level")
    layers_touched = review.get("layers_touched") or []
    infra_touched = "infra" in [str(layer).lower() for layer in layers_touched]
    return infra_cost_impact == "material" or (risk_level == "high" and infra_touched)


def build_diagnosis(review: dict, feature_brief: str) -> str:
    return (
        "Deterministic infra-cost gate blocked this feature brief before implementation started: "
        f"infra_cost_impact={review.get('infra_cost_impact')!r}, risk_level={review.get('risk_level')!r}, "
        f"layers_touched={review.get('layers_touched')!r}.\n\nConcerns from the architecture review: "
        f"{review.get('concerns') or []}\n\nFeature brief: {feature_brief}"
    )


CheckAuthFailure = Callable[["OrchestratorSession", str, AgentResult], "dict | None"]


async def run_design_review(
    session: "OrchestratorSession", feature_brief: str, *, check_auth_failure: CheckAuthFailure
) -> tuple["dict | None", "AgentResult | None"]:
    """Runs the Architecture Review Agent for feature_brief and stores its parsed result in
    session.design_reviews, keyed by the exact brief text (Part 1 groundwork: per-brief-scoped
    state that can never leak into gating a different brief). Shared by the assess_design_impact
    tool and implement_feature's automatic keyword-triggered review. Returns (stop, review): stop
    is a tool-result dict the caller should return immediately on an auth failure, review is the
    raw agent result (None only when stop is set). check_auth_failure is injected (rather than
    imported) to avoid a circular import with tools.py, which owns it."""
    review = await architecture_review.run(session.repo, session.objective, feature_brief, session.cb_summary)
    if stop := check_auth_failure(session, "assess_design_impact", review):
        return stop, None
    session.cost_usd += review.cost_usd
    session.note(f"assess_design_impact: ok={review.ok}", ok=review.ok, cost_usd=review.cost_usd, turns=review.turns)
    if not review.ok:
        session.record_failure("assess_design_impact")
        return None, review
    session.record_success("assess_design_impact")
    if isinstance(review.json_data, dict):
        session.design_reviews[feature_brief] = review.json_data
    return None, review


EscalateToHuman = Callable[..., "int | None"]


def gate(
    session: "OrchestratorSession",
    feature_brief: str,
    tracking_issue: "int | None",
    *,
    escalate_to_human: EscalateToHuman,
) -> "dict | None":
    """Deterministic Python-level enforcement -- a real `if` check reachable no matter what the
    brain says or does, mirroring the existing prod-promotion gate and cost-cap circuit breaker
    elsewhere in this codebase; it cannot be bypassed by the brain skipping assess_design_impact
    or by prompt wording alone. Returns a tool-result dict to return immediately if blocked, else
    None (proceed as normal). escalate_to_human is injected to avoid a circular import with
    tools.py, which owns it."""
    review = session.design_reviews.get(feature_brief)
    if not review or not should_block(review):
        return None
    escalate_to_human(
        session,
        category="infra_cost",
        diagnosis=build_diagnosis(review, feature_brief),
        question=(
            "This feature brief's architecture review flagged material infra cost impact -- "
            "proceed as briefed, narrow the scope, or reject?"
        ),
        source="infra-cost-gate",
        title=f"Human input required: infra cost impact -- {feature_brief[:80]}",
        branch=session.feature_branch,
        tracking_issue=tracking_issue,
    )
    session.note(
        f"implement_feature: blocked by the deterministic infra-cost gate "
        f"(infra_cost_impact={review.get('infra_cost_impact')!r} risk_level={review.get('risk_level')!r})",
        ok=None,
    )
    return {
        "content": [{
            "type": "text",
            "text": (
                "Blocked: the deterministic infra-cost gate flagged this brief's architecture "
                "review as material infra cost impact. Escalated to a human (category=infra_cost) "
                "-- filed as a GitHub issue (needs_human) and notified via Slack (if configured); "
                "this run is now waiting_for_human. Implementation was not attempted."
            ),
        }],
        "is_error": True,
    }

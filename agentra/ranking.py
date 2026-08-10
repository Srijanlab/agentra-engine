"""Impact-vs-effort ranking for Product Discovery Agent output (vision.md 5.6)."""

from typing import Any

_IMPACT_SCORE = {"low": 1, "medium": 2, "high": 3, "very_high": 4}
_EFFORT_SCORE = {"low": 1, "medium": 2, "high": 3}


def score(opportunity: dict[str, Any]) -> float:
    impact = _IMPACT_SCORE.get(str(opportunity.get("impact", "")).lower(), 2)
    effort = _EFFORT_SCORE.get(str(opportunity.get("effort", "")).lower(), 2)
    return impact / effort


def rank(opportunities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(opportunities, key=score, reverse=True)

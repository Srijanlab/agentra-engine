"""Helpers for reading acceptance_criteria, which may be a plain string or a
structured ``{"text": str, "page_url": str}`` (GitHub issue #108). Specs persisted
before #108 are plain ``list[str]`` and must keep working."""

from __future__ import annotations


def criterion_text(criterion: object) -> str:
    """The human-readable check, whichever shape the criterion is in."""
    if isinstance(criterion, dict):
        return str(criterion.get("text") or "").strip()
    return str(criterion or "").strip()


def criterion_page_url(criterion: object) -> str:
    """The route/page a UI criterion lives on, or "" for API-only / legacy criteria."""
    if isinstance(criterion, dict):
        return str(criterion.get("page_url") or "").strip()
    return ""


def page_urls(criteria: list | None) -> list[str]:
    """Distinct, order-preserving list of the page_urls across all criteria."""
    seen: list[str] = []
    for c in criteria or []:
        url = criterion_page_url(c)
        if url and url not in seen:
            seen.append(url)
    return seen

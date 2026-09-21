"""Once-per-day Slack digest of items stuck at the awaiting-testing stage."""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone

from agentra import registry, urls
from agentra.connectors import slack
from agentra.memory import Memory

logger = logging.getLogger(__name__)

THRESHOLD_HOURS_ENV = "AGENTRA_AWAITING_TESTING_DIGEST_HOURS"
DEFAULT_THRESHOLD_HOURS = 24.0
DIGEST_INTERVAL_SECONDS = 24 * 3600


def threshold_hours() -> float:
    """Configured staleness threshold in hours; falls back to the default on a missing/invalid/non-positive value."""
    try:
        value = float(os.environ.get(THRESHOLD_HOURS_ENV, ""))
    except ValueError:
        return DEFAULT_THRESHOLD_HOURS
    return value if value > 0 and value == value and value != float("inf") else DEFAULT_THRESHOLD_HOURS


def _age_hours(updated_at: object, now: float) -> float | None:
    if not isinstance(updated_at, str) or not updated_at:
        return None
    try:
        parsed = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (now - parsed.timestamp()) / 3600


def _stale_items(entries: list[dict], now: float, threshold: float) -> list[dict]:
    stale = []
    for entry in entries:
        age = _age_hours(entry.get("updated_at"), now)
        if age is None or age <= threshold:
            continue
        stale.append({
            "number": entry.get("external_id"),
            "title": entry.get("diagnosis") or entry.get("description") or "",
            "html_url": entry.get("html_url"),
            "age_hours": age,
        })
    stale.sort(key=lambda item: item["age_hours"], reverse=True)
    return stale


def post_awaiting_testing_digest(app_name: str) -> bool:
    """Posts at most one digest per app per 24h of stale awaiting-testing items; never raises."""
    try:
        if not slack.is_configured():
            return False
        now = time.time()
        last = registry.get_last_awaiting_digest_at(app_name)
        if last is not None and now - last < DIGEST_INTERVAL_SECONDS:
            return False
        repo = registry.get_app_repo(app_name)
        if repo is None:
            return False
        entries = Memory(repo).shipped_pending_test_items()
        items = _stale_items(entries, now, threshold_hours())
        if not items:
            return False
        posted = slack.notify_awaiting_testing_digest(
            app=app_name,
            items=items,
            dashboard_url=urls.dashboard_app_url(app_name, tab="awaiting-testing"),
            channel=registry.get_slack_channel(app_name),
        )
        if posted:
            registry.record_awaiting_digest(app_name, now)
        return posted
    except Exception:
        logger.warning("awaiting-testing digest failed for app=%r", app_name, exc_info=True)
        return False

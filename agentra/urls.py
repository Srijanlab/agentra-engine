"""agentra.urls — deep links into the dashboard for outbound notifications (GitHub issue #34: Slack/GitHub-issue human-in-the-loop escalation)."""

from __future__ import annotations

import os

# Optional -- the dashboard is currently a single-page tabbed app with no
_DASHBOARD_BASE_URL_ENV = "AGENTRA_DASHBOARD_BASE_URL"


def dashboard_base_url() -> str | None:
    raw = os.environ.get(_DASHBOARD_BASE_URL_ENV)
    return raw.rstrip("/") if raw else None


def dashboard_run_url(run_id: str, app_name: str, *, tab: str = "needs-input") -> str | None:
    """Deep link into the dashboard for a specific run, or None if AGENTRA_DASHBOARD_BASE_URL isn't configured for this deployment (e.g."""
    base = dashboard_base_url()
    if base is None:
        return None
    return f"{base}/?app={app_name}&run={run_id}&tab={tab}"

"""agentra.urls — deep links into the dashboard for outbound notifications
(GitHub issue #34: Slack/GitHub-issue human-in-the-loop escalation).

Kept as one tiny module, not folded into server/ or connectors/slack.py,
because both an outbound Slack notification (connectors/slack.py) and a
GitHub issue comment (memory/issues.py -> github_issue_lifecycle.py) need
the same "link back to the dashboard" logic and neither should import the
other just for this.
"""

from __future__ import annotations

import os

# Optional -- the dashboard is currently a single-page tabbed app with no
# per-run server-side route (see agentra/web/src/App.tsx), so this is a
# best-effort deep link via query params (?app=...&run=...&tab=needs-input)
# that pre-selects the right app and tab on load, not a guarantee the exact
# run is scrolled into view. None (unset) means this deployment hasn't
# configured a public dashboard hostname (e.g. no Cloudflare Tunnel domain
# yet) -- callers must treat a None return as "omit the link" rather than
# constructing a broken/relative URL, since a Slack message or GitHub
# comment posted from a backend process has no notion of "relative to what."
_DASHBOARD_BASE_URL_ENV = "AGENTRA_DASHBOARD_BASE_URL"


def dashboard_base_url() -> str | None:
    raw = os.environ.get(_DASHBOARD_BASE_URL_ENV)
    return raw.rstrip("/") if raw else None


def dashboard_run_url(run_id: str, app_name: str, *, tab: str = "needs-input") -> str | None:
    """Deep link into the dashboard for a specific run, or None if
    AGENTRA_DASHBOARD_BASE_URL isn't configured for this deployment (e.g.
    local dev, or a deployment that hasn't set up a public hostname yet).
    Callers (connectors/slack.py, the needs_human GitHub issue body) must
    handle None by omitting the link, not printing a broken one."""
    base = dashboard_base_url()
    if base is None:
        return None
    return f"{base}/?app={app_name}&run={run_id}&tab={tab}"

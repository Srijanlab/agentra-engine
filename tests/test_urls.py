"""agentra/urls.py -- dashboard deep links for outbound notifications
(GitHub issue #34). Best-effort: None when this deployment hasn't
configured AGENTRA_DASHBOARD_BASE_URL, never a broken/relative URL.
"""

from agentra import urls


def test_dashboard_run_url_is_none_when_base_url_unconfigured(monkeypatch):
    monkeypatch.delenv("AGENTRA_DASHBOARD_BASE_URL", raising=False)

    assert urls.dashboard_base_url() is None
    assert urls.dashboard_run_url("run1", "myapp") is None


def test_dashboard_run_url_builds_a_deep_link_when_configured(monkeypatch):
    monkeypatch.setenv("AGENTRA_DASHBOARD_BASE_URL", "https://dashboard.example.com/")

    url = urls.dashboard_run_url("run1", "myapp")

    assert url == "https://dashboard.example.com/?app=myapp&run=run1&tab=needs-input"


def test_dashboard_run_url_respects_a_custom_tab(monkeypatch):
    monkeypatch.setenv("AGENTRA_DASHBOARD_BASE_URL", "https://dashboard.example.com")

    url = urls.dashboard_run_url("run1", "myapp", tab="activity")

    assert url == "https://dashboard.example.com/?app=myapp&run=run1&tab=activity"

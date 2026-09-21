"""The /trigger/cron tick posts a once-per-day per-app Slack digest of items stuck at awaiting-testing."""

import time
from datetime import datetime, timedelta, timezone

import pytest

from agentra import registry
from agentra.connectors import github_fake, github_issues, slack
from agentra.memory import Memory
from agentra.server.digests import awaiting_testing
from test_server_triggers import _client, _isolate, _register_tmp_app

_BASE = "https://dash.example.com"


def _iso(hours_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def env(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-fake")
    monkeypatch.setenv("SLACK_HUMAN_INPUT_CHANNEL", "CGLOBAL")
    monkeypatch.setenv("AGENTRA_DASHBOARD_BASE_URL", _BASE)
    monkeypatch.delenv("AGENTRA_AWAITING_TESTING_DIGEST_HOURS", raising=False)
    monkeypatch.delenv("AGENTRA_INTERNAL_TOKEN", raising=False)
    monkeypatch.delenv("CRON_SECRET", raising=False)
    posts: list[dict] = []

    def fake_post(text, channel=None, thread_ts=None):
        posts.append({"text": text, "channel": channel})
        return {"ok": True, "ts": "1.1"}

    monkeypatch.setattr(slack, "_post_message", fake_post)
    repo = _register_tmp_app(tmp_path)
    mem = Memory(repo)

    class Env:
        pass

    e = Env()
    e.posts, e.tmp_path, e.monkeypatch, e.repo, e.url = posts, tmp_path, monkeypatch, repo, mem._repo_url()
    e.backend = github_issues.list_open_issues.__self__
    return e


def _add(env, title, labels, hours_ago=30, updated=True):
    issue = env.backend.create_issue(env.url, title, "", labels)
    if updated:
        env.backend.issues[env.url][issue["number"]]["updated_at"] = _iso(hours_ago)
    return issue


def _cron():
    resp = _client().get("/trigger/cron")
    assert resp.status_code == 200
    body = resp.json()
    assert list(body) == ["apps"]
    return body


def test_stale_item_posts_once_and_names_app_link_and_dashboard(env):
    issue = _add(env, "Stuck thing", ["feature", "agentra", "status:awaiting-testing"])
    _cron()
    [post] = env.posts
    assert "myapp" in post["text"]
    assert f"<{issue['html_url']}|#{issue['number']} Stuck thing>" in post["text"]
    assert f"{_BASE}/?app=myapp&tab=awaiting-testing" in post["text"]
    assert registry.get_last_awaiting_digest_at("myapp") is not None


def test_second_tick_within_24h_posts_nothing_and_reposts_after(env):
    _add(env, "Stuck", ["bug", "agentra", "status:awaiting-testing"])
    _cron()
    _cron()
    assert len(env.posts) == 1
    registry.record_awaiting_digest("myapp", time.time() - 25 * 3600)
    _cron()
    assert len(env.posts) == 2


def test_item_under_threshold_not_listed_and_no_timestamp(env):
    _add(env, "Fresh", ["feature", "agentra", "status:awaiting-testing"], hours_ago=2)
    _cron()
    assert env.posts == []
    assert registry.get_last_awaiting_digest_at("myapp") is None
    _add(env, "Old", ["feature", "agentra", "status:awaiting-testing"], hours_ago=40)
    _cron()
    [post] = env.posts
    assert "Old" in post["text"] and "Fresh" not in post["text"]


def test_legacy_shipped_included_and_other_stages_excluded(env):
    _add(env, "Legacy", ["feature", "agentra", "status:shipped"])
    _add(env, "Tested", ["feature", "agentra", "status:tested"])
    _add(env, "CodeComplete", ["feature", "agentra", "status:code_complete"])
    _add(env, "NoStatus", ["feature", "agentra"])
    _add(env, "NeedsHuman", ["bug", "agentra", "status:awaiting-testing", "need_human"])
    _cron()
    [post] = env.posts
    assert "Legacy" in post["text"]
    for other in ("Tested", "CodeComplete", "NoStatus", "NeedsHuman"):
        assert other not in post["text"]


def test_missing_updated_at_excluded(env):
    _add(env, "NoTimestamp", ["feature", "agentra", "status:awaiting-testing"], updated=False)
    _cron()
    assert env.posts == []


def test_unconfigured_slack_no_post_no_state_no_github_read(env):
    _add(env, "Stuck", ["feature", "agentra", "status:awaiting-testing"])
    env.monkeypatch.delenv("SLACK_BOT_TOKEN")
    calls = []
    env.monkeypatch.setattr(github_issues, "list_open_issues", lambda *a, **k: calls.append(1) or [])
    assert awaiting_testing.post_awaiting_testing_digest("myapp") is False
    assert env.posts == [] and calls == []
    assert registry.get_last_awaiting_digest_at("myapp") is None
    _cron()
    assert env.posts == []


def test_slack_failure_returns_200_and_retries(env):
    _add(env, "Stuck", ["feature", "agentra", "status:awaiting-testing"])
    env.monkeypatch.setattr(slack, "_post_message", lambda *a, **k: None)
    _cron()
    assert registry.get_last_awaiting_digest_at("myapp") is None
    env.monkeypatch.setattr(slack, "_post_message", lambda text, channel=None, thread_ts=None: env.posts.append(text) or {"ok": True})
    _cron()
    assert len(env.posts) == 1
    assert registry.get_last_awaiting_digest_at("myapp") is not None


def test_github_failure_never_raises_out_of_cron(env):
    def boom(*a, **k):
        raise RuntimeError("github down")

    env.monkeypatch.setattr(github_issues, "list_open_issues", boom)
    _cron()
    env.monkeypatch.setattr(Memory, "shipped_pending_test_items", boom)
    _cron()
    assert env.posts == []


def test_threshold_override_and_bad_values_fall_back(env):
    _add(env, "TwoHours", ["feature", "agentra", "status:awaiting-testing"], hours_ago=2)
    env.monkeypatch.setenv("AGENTRA_AWAITING_TESTING_DIGEST_HOURS", "48")
    assert awaiting_testing.threshold_hours() == 48
    _cron()
    assert env.posts == []
    env.monkeypatch.setenv("AGENTRA_AWAITING_TESTING_DIGEST_HOURS", "1")
    _cron()
    assert len(env.posts) == 1
    for bad in ("abc", "0", "-3", "nan", ""):
        env.monkeypatch.setenv("AGENTRA_AWAITING_TESTING_DIGEST_HOURS", bad)
        assert awaiting_testing.threshold_hours() == 24


def test_more_than_ten_items_truncated_in_single_message(env):
    for n in range(12):
        _add(env, f"Item{n:02d}", ["feature", "agentra", "status:awaiting-testing"])
    _cron()
    [post] = env.posts
    assert post["text"].count("|#") == 10
    assert "+2 more" in post["text"]


def test_no_dashboard_url_still_posts(env):
    env.monkeypatch.delenv("AGENTRA_DASHBOARD_BASE_URL")
    _add(env, "Stuck", ["feature", "agentra", "status:awaiting-testing"])
    _cron()
    [post] = env.posts
    assert "tab=awaiting-testing" not in post["text"]


def test_per_app_channel_used_and_timestamp_isolated_per_app(env):
    _register_tmp_app(env.tmp_path, "other")
    registry.set_slack_channel("myapp", "CMYAPP")
    _add(env, "Stuck", ["feature", "agentra", "status:awaiting-testing"])
    _cron()
    [post] = env.posts
    assert post["channel"] == "CMYAPP"
    assert registry.get_last_awaiting_digest_at("myapp") is not None
    assert registry.get_last_awaiting_digest_at("other") is None


def test_digest_does_not_touch_issue_comments_or_labels(env):
    issue = _add(env, "Stuck", ["feature", "agentra", "status:awaiting-testing"])
    before = (list(env.backend.issues[env.url][issue["number"]]["labels"]), env.backend.list_comments(env.url, issue["number"]))
    _cron()
    after = (list(env.backend.issues[env.url][issue["number"]]["labels"]), env.backend.list_comments(env.url, issue["number"]))
    assert before == after


def test_digest_does_not_block_cycle_enqueue(env):
    _add(env, "Stuck", ["feature", "agentra", "status:awaiting-testing"])
    body = _cron()
    assert body["apps"]["myapp"]["triggered"] is True


def test_pipeline_stages_regression(env):
    resp = _client().get("/pipeline/stages")
    assert resp.status_code == 200
    stage = next(s for s in resp.json()["stages"] if s["key"] == "awaiting-testing")
    assert "status:shipped" in str(stage)

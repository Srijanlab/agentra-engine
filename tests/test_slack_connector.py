"""connectors/slack.py -- outbound-only Slack notifications for
human-in-the-loop escalation (GitHub issue #34). Covers: silently skipping
when unconfigured (never surfaced as a run failure), sending the right
message shape when configured, and treating a Slack-API-level failure
(HTTP 200 with ok: false) the same as not being configured -- both just
return False, never raise.
"""

from agentra.connectors import slack


def _clear_env(monkeypatch):
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_HUMAN_INPUT_CHANNEL", raising=False)


def test_is_configured_false_when_env_vars_unset(monkeypatch):
    _clear_env(monkeypatch)
    assert slack.is_configured() is False


def test_is_configured_false_with_only_one_of_the_two_vars_set(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-fake")
    assert slack.is_configured() is False


def test_is_configured_true_with_both_vars_set(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-fake")
    monkeypatch.setenv("SLACK_HUMAN_INPUT_CHANNEL", "C0123456789")
    assert slack.is_configured() is True


def test_notify_human_input_required_skips_silently_when_unconfigured(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setattr(
        slack.httpx, "post", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not call Slack when unconfigured"))
    )

    result = slack.notify_human_input_required(
        app="myapp", run_id="run1", question="Which auth provider should we use?",
    )

    assert result is False


def test_notify_human_input_required_posts_the_question_and_links_when_configured(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-fake")
    monkeypatch.setenv("SLACK_HUMAN_INPUT_CHANNEL", "C0123456789")

    captured = {}

    class _FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"ok": True, "ts": "1234.5678"}

    def _fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        return _FakeResponse()

    monkeypatch.setattr(slack.httpx, "post", _fake_post)

    result = slack.notify_human_input_required(
        app="myapp",
        run_id="run1",
        question="Which auth provider should we use?",
        issue_url="https://github.com/acme/myapp/issues/42",
        dashboard_url="https://dashboard.example.com/?app=myapp&run=run1",
        branch="dev/abc123-add-auth",
        session_id="sess-xyz",
    )

    assert result is True
    assert captured["url"] == slack.SLACK_API_POST_MESSAGE
    assert captured["headers"]["Authorization"] == "Bearer xoxb-fake"
    assert captured["json"]["channel"] == "C0123456789"
    text = captured["json"]["text"]
    assert "Which auth provider should we use?" in text
    assert "https://github.com/acme/myapp/issues/42" in text
    assert "https://dashboard.example.com/?app=myapp&run=run1" in text
    assert "dev/abc123-add-auth" in text


def test_notify_human_input_required_returns_false_on_slack_api_level_rejection(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-fake")
    monkeypatch.setenv("SLACK_HUMAN_INPUT_CHANNEL", "C0123456789")

    class _FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"ok": False, "error": "channel_not_found"}

    monkeypatch.setattr(slack.httpx, "post", lambda *a, **k: _FakeResponse())

    result = slack.notify_human_input_required(app="myapp", run_id="run1", question="q")

    assert result is False


def test_notify_human_input_required_never_raises_on_network_failure(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-fake")
    monkeypatch.setenv("SLACK_HUMAN_INPUT_CHANNEL", "C0123456789")

    def _raise(*a, **k):
        raise ConnectionError("network is down")

    monkeypatch.setattr(slack.httpx, "post", _raise)

    result = slack.notify_human_input_required(app="myapp", run_id="run1", question="q")

    assert result is False


def test_notify_human_input_required_escalated_message_is_distinguishable(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-fake")
    monkeypatch.setenv("SLACK_HUMAN_INPUT_CHANNEL", "C0123456789")
    captured = {}

    class _FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"ok": True}

    def _fake_post(url, headers=None, json=None, timeout=None):
        captured["json"] = json
        return _FakeResponse()

    monkeypatch.setattr(slack.httpx, "post", _fake_post)

    slack.notify_human_input_required(app="myapp", run_id="run1", question="q", escalated=True)
    escalated_text = captured["json"]["text"]

    slack.notify_human_input_required(app="myapp", run_id="run1", question="q", escalated=False)
    normal_text = captured["json"]["text"]

    assert escalated_text != normal_text
    assert "waiting on a human answer past its configured timeout" in escalated_text

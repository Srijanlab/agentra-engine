"""Tests for Memory.is_transient_failure()/record_failure() -- the policy
that replaced the old write-only memory/failures/*.md ledger (confirmed
nothing in the codebase ever read it back). A permanent failure (a real
defect) gets filed via record_known_bug (creating a real GitHub Issue when
the target repo has one configured); a transient failure (rate/usage
limits, max-turns exhaustion, the CLI's contradictory-result quirk) is
just logged, since a retry next cycle -- not a backlog entry -- is the fix.
"""

from unittest.mock import MagicMock

from agentra.memory import Memory, cannot_be_fixed_by_agentra, is_login_required_failure, is_transient_failure


def test_is_transient_failure_detects_known_patterns():
    assert is_transient_failure("Reached maximum number of turns (5)")
    assert is_transient_failure("Error: rate limit exceeded, please retry")
    assert is_transient_failure("You have hit your usage limit for this period")
    assert is_transient_failure("api.anthropic.com is currently overloaded")
    assert is_transient_failure("Claude Code returned an error result: success")
    # GitHub issue #36: session-limit errors must not create bug reports
    assert is_transient_failure(
        "Claude Code returned an error result: You've hit your session limit"
        " · resets 8:20am (UTC) (exit code: 1)"
    )


def test_is_transient_failure_false_for_a_real_defect():
    assert not is_transient_failure("TypeError: cannot read property 'x' of undefined at line 42")
    assert not is_transient_failure("3 tests failed: test_login, test_logout, test_signup")


def test_record_failure_logs_transient_failures_without_filing_a_bug(tmp_path, monkeypatch):
    mem = Memory(tmp_path)
    monkeypatch.setattr(mem, "record_known_bug", MagicMock(side_effect=AssertionError("should not file a bug")))

    mem.record_failure("run1", "testing", "Reached maximum number of turns (5)")

    log_path = mem.log_root / "run1.log"
    assert "testing failed (transient, not filed as a bug)" in log_path.read_text()


def test_record_failure_files_a_known_bug_for_a_real_defect(tmp_path, monkeypatch):
    mem = Memory(tmp_path)
    recorded = {}

    def fake_record_known_bug(
        run_id, severity, diagnosis, proposed_fix, source="prod-monitoring", external_id=None,
        needs_human=False, blocking_agentra=False,
    ):
        recorded.update(
            run_id=run_id, severity=severity, diagnosis=diagnosis, proposed_fix=proposed_fix, source=source,
            needs_human=needs_human, blocking_agentra=blocking_agentra,
        )

    monkeypatch.setattr(mem, "record_known_bug", fake_record_known_bug)

    mem.record_failure("run1", "testing", "3 tests failed: test_login, test_logout, test_signup", severity="medium")

    assert recorded["run_id"] == "run1"
    assert recorded["severity"] == "medium"
    assert recorded["diagnosis"] == "testing failed during an autonomous cycle"
    assert "test_login" in recorded["proposed_fix"]
    assert recorded["source"] == "autonomous-failure"
    assert recorded["needs_human"] is False
    assert recorded["blocking_agentra"] is False


def test_cannot_be_fixed_by_agentra_detects_auth_and_permission_failures():
    assert cannot_be_fixed_by_agentra("403 Write access to repository not granted")
    assert cannot_be_fixed_by_agentra("Error: 401 Unauthorized")
    assert cannot_be_fixed_by_agentra("git push failed: permission denied")


def test_cannot_be_fixed_by_agentra_false_for_an_ordinary_bug():
    assert not cannot_be_fixed_by_agentra("TypeError: cannot read property 'x' of undefined at line 42")
    assert not cannot_be_fixed_by_agentra("3 tests failed: test_login, test_logout, test_signup")


def test_cannot_be_fixed_by_agentra_ignores_a_bare_status_code_in_unrelated_prose():
    # Real false positive, GitHub issue #17: a Testing Agent report mentioning
    # "401 instead of 200" while describing an already-diagnosed, unrelated
    # ambient env var tripping a webhook auth test (nothing blocking agentra
    # itself) got classified as unfixable, labeled blocking_agentra, and
    # halted every future autonomous cycle. A bare status code alone must
    # never be enough -- only the phrase-based patterns should trigger.
    assert not cannot_be_fixed_by_agentra(
        "test_alarm_trigger_respects_per_app_alarm_enabled, 401 instead of 200. "
        "Root-caused to an ambient sandbox env var ALARM_WEBHOOK_PASSWORD tripping "
        "the webhook's Basic-auth gate -- unrelated to this feature."
    )
    assert not cannot_be_fixed_by_agentra("HTTP 403 returned by a third-party API the app under test calls")


def test_is_login_required_failure_detects_claude_code_auth_failures():
    # GitHub issue #42: a prior autonomous cycle crashed opaquely on this
    # exact text -- the CLI's own "no valid session on this runner" failure.
    assert is_login_required_failure(
        "Claude Code returned an error result: Not logged in · Please run /login (exit code: 1)"
    )
    assert is_login_required_failure("Invalid API key · Please run /login")
    assert is_login_required_failure("OAuth token has expired · Please run /login")


def test_is_login_required_failure_false_for_an_ordinary_failure():
    assert not is_login_required_failure("3 tests failed: test_login, test_logout, test_signup")
    assert not is_login_required_failure("403 Write access to repository not granted")
    assert not is_login_required_failure("Reached maximum number of turns (5)")


def test_cannot_be_fixed_by_agentra_treats_a_login_failure_as_unfixable():
    assert cannot_be_fixed_by_agentra(
        "Claude Code returned an error result: Not logged in · Please run /login (exit code: 1)"
    )


def test_is_transient_failure_false_for_a_login_failure():
    # A login failure must never be treated as "just retry next cycle" --
    # only record_failure's needs_human/blocking_agentra path handles it.
    assert not is_transient_failure(
        "Claude Code returned an error result: Not logged in · Please run /login (exit code: 1)"
    )


def test_record_failure_flags_a_login_failure_as_needing_a_human_and_blocking(tmp_path, monkeypatch):
    mem = Memory(tmp_path)
    recorded = {}

    def fake_record_known_bug(
        run_id, severity, diagnosis, proposed_fix, source="prod-monitoring", external_id=None,
        needs_human=False, blocking_agentra=False,
    ):
        recorded.update(needs_human=needs_human, blocking_agentra=blocking_agentra, diagnosis=diagnosis)

    monkeypatch.setattr(mem, "record_known_bug", fake_record_known_bug)

    mem.record_failure(
        "run1", "autonomous-cycle",
        "Claude Code returned an error result: Not logged in · Please run /login (exit code: 1)",
    )

    assert recorded["needs_human"] is True
    assert recorded["blocking_agentra"] is True


def test_record_failure_flags_an_unfixable_failure_as_needing_a_human_and_blocking(tmp_path, monkeypatch):
    mem = Memory(tmp_path)
    recorded = {}

    def fake_record_known_bug(
        run_id, severity, diagnosis, proposed_fix, source="prod-monitoring", external_id=None,
        needs_human=False, blocking_agentra=False,
    ):
        recorded.update(needs_human=needs_human, blocking_agentra=blocking_agentra)

    monkeypatch.setattr(mem, "record_known_bug", fake_record_known_bug)

    mem.record_failure("run1", "implement_feature", "403 Write access to repository not granted")

    assert recorded["needs_human"] is True
    assert recorded["blocking_agentra"] is True


# -- GitHub issue #42: Slack escalation for Claude Code auth/login failures --


def test_record_failure_notifies_slack_with_an_actionable_message_for_a_new_login_failure(tmp_path, monkeypatch):
    from agentra.connectors import slack

    mem = Memory(tmp_path)
    monkeypatch.setattr(mem, "record_known_bug", lambda *a, **k: 42)
    monkeypatch.setattr(mem, "issue_html_url", lambda n: f"https://github.com/acme/app/issues/{n}")
    monkeypatch.setattr(mem, "_find_similar_open_bug", lambda diagnosis: None)  # genuinely new
    slack_calls = []
    monkeypatch.setattr(slack, "notify_human_input_required", lambda **k: slack_calls.append(k) or True)

    mem.record_failure(
        "run1", "understand_codebase",
        "Claude Code returned an error result: Not logged in · Please run /login (exit code: 1)",
    )

    assert len(slack_calls) == 1
    call = slack_calls[0]
    # The exact actionable message GitHub issue #42 asks for.
    assert call["question"] == "Claude Code session is not authenticated on this runner -- run /login and re-trigger."
    assert call["issue_url"] == "https://github.com/acme/app/issues/42"
    assert call["app"] == tmp_path.name
    assert call["run_id"] == "run1"


def test_record_failure_does_not_notify_slack_for_an_ordinary_unfixable_failure(tmp_path, monkeypatch):
    from agentra.connectors import slack

    mem = Memory(tmp_path)
    monkeypatch.setattr(mem, "record_known_bug", lambda *a, **k: 42)
    monkeypatch.setattr(
        slack, "notify_human_input_required", lambda **k: (_ for _ in ()).throw(AssertionError("must not notify"))
    )

    mem.record_failure("run1", "implement_feature", "403 Write access to repository not granted")


def test_record_failure_does_not_notify_slack_for_a_transient_failure(tmp_path, monkeypatch):
    from agentra.connectors import slack

    mem = Memory(tmp_path)
    monkeypatch.setattr(
        slack, "notify_human_input_required", lambda **k: (_ for _ in ()).throw(AssertionError("must not notify"))
    )

    mem.record_failure("run1", "testing", "Reached maximum number of turns (5)")


def test_record_failure_does_not_re_notify_slack_once_the_same_login_failure_is_already_reported(tmp_path, monkeypatch):
    """GitHub issue #42's requirement (4): once escalated and awaiting a
    human, this must not perpetually resurface as a fresh Slack ping on
    every occurrence -- only a genuinely new (not-already-open) diagnosis
    triggers the notification."""
    from agentra.connectors import slack

    mem = Memory(tmp_path)
    known_bug_calls = []
    monkeypatch.setattr(mem, "record_known_bug", lambda *a, **k: known_bug_calls.append(k) or 42)
    monkeypatch.setattr(mem, "_find_similar_open_bug", lambda diagnosis: "42")  # already open
    monkeypatch.setattr(
        slack, "notify_human_input_required", lambda **k: (_ for _ in ()).throw(AssertionError("must not re-notify"))
    )

    # Must not raise (the Slack mock above throws if it's ever called) --
    # record_known_bug itself is still called (it does its own comment-on-
    # duplicate dedup and keeps needs_human/blocking_agentra applied).
    mem.record_failure(
        "run2", "understand_codebase",
        "Claude Code returned an error result: Not logged in · Please run /login (exit code: 1)",
    )
    assert len(known_bug_calls) == 1
    assert known_bug_calls[0]["needs_human"] is True
    assert known_bug_calls[0]["blocking_agentra"] is True

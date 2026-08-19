"""Tests for Memory.is_transient_failure()/record_failure() -- the policy
that replaced the old write-only memory/failures/*.md ledger (confirmed
nothing in the codebase ever read it back). A permanent failure (a real
defect) gets filed via record_known_bug (creating a real GitHub Issue when
the target repo has one configured); a transient failure (rate/usage
limits, max-turns exhaustion, the CLI's contradictory-result quirk) is
just logged, since a retry next cycle -- not a backlog entry -- is the fix.
"""

from unittest.mock import MagicMock

from agentra.memory import Memory, cannot_be_fixed_by_agentra, is_transient_failure


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

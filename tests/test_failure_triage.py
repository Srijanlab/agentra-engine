"""Tests for Memory.is_transient_failure()/record_failure() -- the policy
that replaced the old write-only memory/failures/*.md ledger (confirmed
nothing in the codebase ever read it back). A permanent failure (a real
defect) gets filed via record_known_bug (creating a real GitHub Issue when
the target repo has one configured); a transient failure (rate/usage
limits, max-turns exhaustion, the CLI's contradictory-result quirk) is
just logged, since a retry next cycle -- not a backlog entry -- is the fix.
"""

from unittest.mock import MagicMock

from agentra.memory import Memory, is_transient_failure


def test_is_transient_failure_detects_known_patterns():
    assert is_transient_failure("Reached maximum number of turns (5)")
    assert is_transient_failure("Error: rate limit exceeded, please retry")
    assert is_transient_failure("You have hit your usage limit for this period")
    assert is_transient_failure("api.anthropic.com is currently overloaded")
    assert is_transient_failure("Claude Code returned an error result: success")


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

    def fake_record_known_bug(run_id, severity, diagnosis, proposed_fix, source="prod-monitoring", external_id=None):
        recorded.update(run_id=run_id, severity=severity, diagnosis=diagnosis, proposed_fix=proposed_fix, source=source)

    monkeypatch.setattr(mem, "record_known_bug", fake_record_known_bug)

    mem.record_failure("run1", "testing", "3 tests failed: test_login, test_logout, test_signup", severity="medium")

    assert recorded["run_id"] == "run1"
    assert recorded["severity"] == "medium"
    assert recorded["diagnosis"] == "testing failed during an autonomous cycle"
    assert "test_login" in recorded["proposed_fix"]
    assert recorded["source"] == "autonomous-failure"

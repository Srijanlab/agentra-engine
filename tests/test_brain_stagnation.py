"""Unit tests for agents/brain.py's stagnation/no-progress breaker
(OrchestratorSession.record_tool_call) and its interaction with the existing
per-tool-name consecutive-failure breaker (record_failure/record_success).

Unlike tests/test_safety_integration.py and tests/test_generic_spawn_integration.py,
this deliberately does NOT drive a real query() call -- record_tool_call and
record_failure are plain, deterministic Python (no LLM/network involved), so this
exercises them directly against a real OrchestratorSession. No pytest dependency
in this repo, so plain unittest -- run explicitly:
    python tests/test_brain_stagnation.py
or via any unittest-compatible runner:
    python -m unittest tests.test_brain_stagnation
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentra.agents.brain import MAX_CONSECUTIVE_TOOL_FAILURES, STAGNATION_WINDOW, OrchestratorSession
from agentra.environments import EnvironmentConfig
from agentra.memory import Memory


class BrainStagnationTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        repo = Path(self._tmp.name)
        self.session = OrchestratorSession(
            repo=repo,
            objective="test objective",
            env=EnvironmentConfig(),
            mem=Memory(repo),
            run_id="testrun1",
        )

    # -- (a) normal progress does not trigger the stagnation breaker ----------------

    def test_varied_calls_with_progress_do_not_trip(self):
        # More than STAGNATION_WINDOW calls, but each is either a different
        # (tool, args) pair or genuinely changes session state (or both) -- this is
        # what a healthy, productive run looks like and must never be flagged.
        self.session.record_tool_call("understand_codebase", {}, state_changed=True)
        self.session.record_tool_call("check_backlog", {}, state_changed=False)
        self.session.record_tool_call("discover_opportunities", {}, state_changed=False)
        self.session.record_tool_call(
            "implement_feature", {"feature_brief": "add X", "resolves_origin": "", "resolves_id": ""}, state_changed=True
        )
        self.session.record_tool_call("run_local_tests", {}, state_changed=True)
        self.session.record_tool_call("deploy_pre_prod", {}, state_changed=True)
        self.session.record_tool_call("verify_pre_prod", {}, state_changed=True)
        self.session.record_tool_call("assess_feedback", {}, state_changed=False)
        self.assertIsNone(self.session.hard_stop_reason)
        self.assertFalse(self.session.stagnation_detected)

    def test_same_tool_different_args_does_not_trip(self):
        # Same tool called repeatedly, but with different args each time (e.g.
        # spawn_custom_agent doing genuinely different one-off tasks) -- the hash
        # differs each call, so this must not look like a stuck loop even though
        # progress_snapshot() itself doesn't move (spawn_custom_agent doesn't touch
        # any session field this breaker watches).
        for i in range(STAGNATION_WINDOW + 3):
            self.session.record_tool_call(
                "spawn_custom_agent", {"task_name": f"audit-{i}", "prompt": "look at thing"}, state_changed=False
            )
        self.assertIsNone(self.session.hard_stop_reason)
        self.assertFalse(self.session.stagnation_detected)

    # -- (b) k identical repeated calls trigger it -----------------------------------

    def test_identical_repeated_calls_trip_the_breaker(self):
        args = {"feature_brief": "same brief every time", "resolves_origin": "", "resolves_id": ""}
        for i in range(STAGNATION_WINDOW - 1):
            self.session.record_tool_call("implement_feature", args, state_changed=False)
            self.assertIsNone(
                self.session.hard_stop_reason, f"should not trip before {STAGNATION_WINDOW} identical calls (call {i + 1})"
            )
        # The k-th identical, no-progress call fills the window and trips it.
        self.session.record_tool_call("implement_feature", args, state_changed=False)
        self.assertTrue(self.session.stagnation_detected)
        self.assertIsNotNone(self.session.hard_stop_reason)
        self.assertIn("implement_feature", self.session.hard_stop_reason)

        stop = self.session.check_hard_stop()
        self.assertIsNotNone(stop)
        self.assertTrue(stop["is_error"])

    def test_identical_calls_with_one_state_change_do_not_trip(self):
        # Same (tool, args) repeated, but one call in the window actually changed
        # something -- e.g. run_local_tests flipping from unknown to passed. A single
        # real change anywhere in the window should be enough to withhold the breaker.
        for i in range(STAGNATION_WINDOW):
            changed = i == 2
            self.session.record_tool_call("run_local_tests", {}, state_changed=changed)
        self.assertIsNone(self.session.hard_stop_reason)
        self.assertFalse(self.session.stagnation_detected)

    def test_window_slides_a_stale_change_falls_out(self):
        # A real change happens once, then STAGNATION_WINDOW more identical no-op
        # calls follow -- once the changed call has slid out of the window, the
        # breaker should still trip on the trailing run of identical no-progress calls.
        self.session.record_tool_call("check_backlog", {}, state_changed=True)
        for _ in range(STAGNATION_WINDOW):
            self.session.record_tool_call("check_backlog", {}, state_changed=False)
        self.assertTrue(self.session.stagnation_detected)

    # -- (c) existing consecutive-failure breaker still works, unaffected -----------

    def test_consecutive_failure_breaker_still_trips_on_its_own(self):
        # No stagnation-relevant calls involved at all -- this is exactly the
        # pre-existing behavior of record_failure/record_success, must be untouched.
        for _ in range(MAX_CONSECUTIVE_TOOL_FAILURES - 1):
            self.session.record_failure("run_local_tests")
            self.assertIsNone(self.session.hard_stop_reason)
        self.session.record_failure("run_local_tests")
        self.assertIsNotNone(self.session.hard_stop_reason)
        self.assertIn("run_local_tests", self.session.hard_stop_reason)
        self.assertFalse(self.session.stagnation_detected)

    def test_failure_breaker_resets_on_success_independent_of_stagnation_tracking(self):
        self.session.record_failure("deploy_pre_prod")
        self.session.record_success("deploy_pre_prod")
        self.session.record_failure("deploy_pre_prod")
        self.assertIsNone(self.session.hard_stop_reason)
        self.assertEqual(self.session.tool_failure_counts["deploy_pre_prod"], 1)

    def test_failure_breaker_trips_even_with_varied_stagnation_history(self):
        # Interleave plenty of varied, "healthy-looking" tool calls (each different,
        # so the stagnation breaker never gets close to tripping) with a failing tool
        # repeated back-to-back -- the failure breaker must still catch it.
        for i in range(3):
            self.session.record_tool_call("check_backlog", {"i": i}, state_changed=True)
        for _ in range(MAX_CONSECUTIVE_TOOL_FAILURES):
            self.session.record_failure("verify_pre_prod")
        self.assertIsNotNone(self.session.hard_stop_reason)
        self.assertIn("verify_pre_prod", self.session.hard_stop_reason)
        self.assertFalse(self.session.stagnation_detected)

    def test_once_tripped_further_tool_calls_are_not_tracked(self):
        args = {"feature_brief": "x", "resolves_origin": "", "resolves_id": ""}
        for _ in range(STAGNATION_WINDOW):
            self.session.record_tool_call("implement_feature", args, state_changed=False)
        self.assertTrue(self.session.stagnation_detected)
        reason_at_trip = self.session.hard_stop_reason
        # Further calls (even ones that would otherwise reset/extend the window)
        # must not overwrite an already-tripped reason.
        self.session.record_tool_call("understand_codebase", {}, state_changed=True)
        self.assertEqual(self.session.hard_stop_reason, reason_at_trip)


if __name__ == "__main__":
    unittest.main()

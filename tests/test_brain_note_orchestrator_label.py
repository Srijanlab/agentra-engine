"""OrchestratorSession.note() must prefix logged lines with "[Orchestrator]",
matching agents/base.py's log_claude_message convention for every sub-agent
("[Implementation Agent] ...", "[Testing Agent] ..."). Without it, the
Orchestrator's own top-level narration (cycle start, check_backlog,
implement_feature: ok=..., cycle complete) is indistinguishable from
unattributed log noise once mixed in with a sub-agent's hundreds of verbose
stream_event lines for the same run.

Confirmed live: standup.py's LLM call reads these same raw lines
(Memory.recent_log_lines) and, unable to tell which ones were the
Orchestrator's own activity, systematically wrote "Yesterday: No activity.
Today: Idle." for Orchestrator on every cycle -- even ones where it clearly
dispatched real work (check_backlog, implement_feature) that run.
"""

import datetime as dt
from pathlib import Path

from agentra import registry
from agentra.agents.brain import OrchestratorSession
from agentra.environments import EnvironmentConfig
from agentra.memory import Memory


def test_note_prefixes_logged_lines_with_orchestrator_label(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "record_run", lambda *a, **k: None)
    repo = tmp_path / "repo"
    repo.mkdir()
    mem = Memory(repo)
    session = OrchestratorSession(
        repo=repo, objective="test objective", env=EnvironmentConfig(), mem=mem, run_id="testrun1",
    )

    session.note("check_backlog")
    session.note("implement_feature: ok=True feature='thing'")

    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)
    lines = mem.recent_log_lines(since)

    assert any("[Orchestrator] check_backlog" in line for line in lines)
    assert any("[Orchestrator] implement_feature: ok=True feature='thing'" in line for line in lines)


def test_note_bumps_the_run_liveness_signal_throttled(tmp_path, monkeypatch):
    """Per-agent cost/tokens live in Langfuse now; note() only keeps the run's
    updated_at fresh for reconcile_stale_runs, and throttles the write."""
    touches = []

    def fake_record_run(run_key, **fields):
        touches.append((run_key, fields))

    monkeypatch.setattr(registry, "record_run", fake_record_run)
    repo = tmp_path / "repo"
    repo.mkdir()
    session = OrchestratorSession(
        repo=repo, objective="test objective", env=EnvironmentConfig(), mem=Memory(repo), run_id="testrun1",
    )

    session.note("check_backlog")
    session.note("implement_feature")  # within the throttle window -- no second write

    assert len(touches) == 1
    assert touches[0][0] == "testrun1"
    assert "updated_at" in touches[0][1]

"""A cycle that raises (or a container killed mid-cycle) used to skip
brain._finish_loop_rollup, stranding the loop at last_run_status="running"
forever -- every later cycle then treated it as still in flight and did nothing.

Root causes fixed:
- _finish_loop_rollup runs from a `finally`, and derives the terminal status
  from the AutonomousCycleReport (not the run record, which the background
  wrapper only marks terminal *after* the cycle returns).
- reconcile_stale_runs also reconciles loops (reconcile_stale_loops).
- roll_up_loop is idempotent per run_key so a reconcile can't double-count.
"""

import time
from pathlib import Path

from agentra import registry
from agentra.agents.brain import AutonomousCycleReport, _finish_loop_rollup


def _isolate(tmp_path: Path, monkeypatch):
    home = tmp_path / "agentra_home"
    monkeypatch.setattr(registry, "AGENTRA_HOME", home)
    monkeypatch.setattr(registry, "_RUNS_PATH", home / "runs.json")
    monkeypatch.setattr(registry, "_LOOPS_PATH", home / "loops.json")


def _loop_with_running_run(app, issue, run_key):
    loop_id = registry.bind_loop(app, issue)
    registry.record_run(run_key, app=app, loop_id=loop_id, issue_number=str(issue),
                        status="running", started_at=time.time())
    return loop_id


def test_finish_rollup_with_no_report_marks_the_loop_failed(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    loop_id = _loop_with_running_run("app", 7, "r1")

    _finish_loop_rollup("r1", None)  # cycle raised before returning

    loop = registry.get_loop(loop_id)
    assert loop["last_run_status"] == "failed"
    assert loop["status"] == "active"  # still needs work, just not in flight


def test_finish_rollup_uses_report_status_not_the_still_running_run(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    loop_id = _loop_with_running_run("app", 7, "r1")
    # The run record is still "running" here -- the background wrapper writes the
    # terminal status only after run_autonomous_cycle returns.
    report = AutonomousCycleReport(run_id="r1", actions=[], final_message="done", cost_usd=1.0)

    _finish_loop_rollup("r1", report)

    assert registry.get_loop(loop_id)["last_run_status"] == "completed"


def test_finish_rollup_marks_a_crashed_report_failed(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    loop_id = _loop_with_running_run("app", 7, "r1")
    report = AutonomousCycleReport(run_id="r1", actions=[], final_message="boom",
                                   cost_usd=0.0, crashed=True)

    _finish_loop_rollup("r1", report)

    assert registry.get_loop(loop_id)["last_run_status"] == "failed"


def test_reconcile_stale_loops_unsticks_a_stranded_loop(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    loop_id = _loop_with_running_run("app", 7, "r1")
    registry.roll_up_loop(loop_id, "r1", "running", 0.0)   # the lost roll-up
    registry.record_run("r1", status="completed", ended_at=time.time())  # run finished

    fixed = registry.reconcile_stale_loops()

    assert loop_id in fixed
    assert registry.get_loop(loop_id)["last_run_status"] == "completed"


def test_reconcile_stale_runs_also_reconciles_loops(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    loop_id = _loop_with_running_run("app", 7, "r1")
    registry.roll_up_loop(loop_id, "r1", "running", 0.0)
    registry.record_run("r1", status="failed", ended_at=time.time())

    registry.reconcile_stale_runs()

    assert registry.get_loop(loop_id)["last_run_status"] == "failed"

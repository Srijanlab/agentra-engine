"""reconcile_stale_loops (engine side): a loop stranded at last_run_status="running"
-- because a cycle on the loop raised before rolling up -- must be reconciled
from its run's actual terminal state on the next scheduled tick.
"""

import time

from agentra import registry


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "AGENTRA_HOME", tmp_path)
    monkeypatch.setattr(registry, "_RUNS_PATH", tmp_path / "runs.json")
    monkeypatch.setattr(registry, "_LOOPS_PATH", tmp_path / "loops.json")


def _loop_with_running_run(app, issue, run_key):
    loop_id = registry.bind_loop(app, issue)
    registry.record_run(run_key, app=app, loop_id=loop_id, issue_number=str(issue),
                        status="running", started_at=time.time())
    return loop_id


def test_roll_up_is_idempotent_per_run_key(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    loop_id = _loop_with_running_run("app", 7, "r1")
    registry.roll_up_loop(loop_id, "r1", "running", 0.25)   # the lost roll-up
    registry.roll_up_loop(loop_id, "r1", "completed", 0.25)  # reconcile re-folds

    loop = registry.get_loop(loop_id)
    assert loop["run_count"] == 1
    assert abs(loop["total_cost_usd"] - 0.25) < 1e-9
    assert loop["last_run_status"] == "completed"


def test_reconcile_stale_loops_unsticks_a_stranded_loop(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    loop_id = _loop_with_running_run("app", 7, "r1")
    registry.roll_up_loop(loop_id, "r1", "running", 0.0)
    registry.record_run("r1", status="completed", ended_at=time.time())

    assert loop_id in registry.reconcile_stale_loops()
    assert registry.get_loop(loop_id)["last_run_status"] == "completed"


def test_reconcile_stale_runs_also_reconciles_loops(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    loop_id = _loop_with_running_run("app", 7, "r1")
    registry.roll_up_loop(loop_id, "r1", "running", 0.0)
    registry.record_run("r1", status="failed", ended_at=time.time())

    registry.reconcile_stale_runs()
    assert registry.get_loop(loop_id)["last_run_status"] == "failed"

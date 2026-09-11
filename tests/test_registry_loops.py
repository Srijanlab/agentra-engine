"""registry/loops.py: the loop as a stored entity (one doc per tracked issue)."""

import time
from pathlib import Path

import pytest

from agentra import registry


def _isolate(tmp_path: Path, monkeypatch):
    home = tmp_path / "agentra_home"
    monkeypatch.setattr(registry, "_ddb", None)
    monkeypatch.setattr(registry, "AGENTRA_HOME", home)
    monkeypatch.setattr(registry, "_RUNS_PATH", home / "runs.json")
    monkeypatch.setattr(registry, "_LOOPS_PATH", home / "loops.json")


def _bind(run_key, app, issue, **kw):
    loop_id = registry.bind_loop(app, issue, **kw)
    registry.record_run(run_key, loop_id=loop_id, issue_number=str(issue))
    return loop_id


def test_bind_loop_creates_the_doc_and_links_the_run(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    registry.record_run("r1", app="app", status="running", started_at=time.time())

    loop_id = _bind("r1", "app", 7, title="add widget", kind="feature", objective="ship")

    loop = registry.get_loop(loop_id)
    assert loop["issue_number"] == "7"
    assert loop["kind"] == "feature"
    assert loop["title"] == "add widget"
    assert loop["status"] == "active"
    assert loop["run_count"] == 0
    assert registry.get_run("r1")["loop_id"] == loop_id


def test_bind_loop_is_idempotent(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    registry.record_run("r1", app="app", status="running", started_at=time.time())
    registry.record_run("r2", app="app", status="running", started_at=time.time())

    a = _bind("r1", "app", 7, title="v1")
    created = registry.get_loop(a)["created_at"]
    b = _bind("r2", "app", 7, title="v2")

    assert a == b
    loop = registry.get_loop(a)
    assert loop["created_at"] == created  # not reset
    assert loop["title"] == "v2"  # refreshed
    assert {r["run_key"] for r in loop["runs"]} == {"r1", "r2"}


def test_roll_up_accumulates_cost_and_run_count(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    registry.record_run("r1", app="app", status="completed", started_at=time.time())
    loop_id = _bind("r1", "app", 7)

    registry.roll_up_loop(loop_id, "r1", "completed", 0.25)
    registry.roll_up_loop(loop_id, "r2", "waiting_for_human", 0.10)

    loop = registry.get_loop(loop_id)
    assert loop["run_count"] == 2
    assert abs(loop["total_cost_usd"] - 0.35) < 1e-9
    assert loop["last_run_status"] == "waiting_for_human"
    assert loop["status"] == "waiting_for_human"


def test_roll_up_is_idempotent_per_run_key(tmp_path, monkeypatch):
    """A reconcile pass re-folding a run whose original roll-up was lost must not
    inflate run_count / total_cost_usd."""
    _isolate(tmp_path, monkeypatch)
    registry.record_run("r1", app="app", status="running", started_at=time.time())
    loop_id = _bind("r1", "app", 7)

    registry.roll_up_loop(loop_id, "r1", "running", 0.25)
    registry.roll_up_loop(loop_id, "r1", "completed", 0.25)

    loop = registry.get_loop(loop_id)
    assert loop["run_count"] == 1
    assert abs(loop["total_cost_usd"] - 0.25) < 1e-9
    assert loop["last_run_status"] == "completed"


def test_list_loops_orders_by_updated_at_and_filters_by_app(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    for rk, app, iss in (("r1", "a", 1), ("r2", "b", 2), ("r3", "a", 3)):
        registry.record_run(rk, app=app, status="running", started_at=time.time())
        _bind(rk, app, iss)
        time.sleep(0.01)

    all_loops = registry.list_loops()
    assert [l["issue_number"] for l in all_loops] == ["3", "2", "1"]
    assert [l["issue_number"] for l in registry.list_loops(app="a")] == ["3", "1"]


def test_list_loops_ignores_administrative_touches_for_ordering(tmp_path, monkeypatch):
    """A retire/reconcile pass that only calls set_loop_status/set_loop_pipeline
    (no real run) must not shove a days-old, already-resolved loop above one
    that genuinely just ran -- confirmed live: a bulk loop-retirement pass
    stamped updated_at on every loop it touched and dumped them all at the top
    of the dashboard's Loops list together."""
    import json

    _isolate(tmp_path, monkeypatch)
    old_loop = _bind("r_old", "app", 1, title="ancient")
    registry.roll_up_loop(old_loop, "r_old", "completed", 0.1)  # ran days ago
    fresh_loop = _bind("r_fresh", "app", 2, title="just happened")
    registry.roll_up_loop(fresh_loop, "r_fresh", "completed", 0.1)  # ran just now

    # Backdate the old loop's real-activity timestamps directly in storage.
    loops = json.loads(registry._LOOPS_PATH.read_text())
    loops[old_loop]["last_run_at"] = time.time() - 999999
    loops[old_loop]["created_at"] = time.time() - 999999
    registry._LOOPS_PATH.write_text(json.dumps(loops))

    # An administrative-only retire touches every loop's updated_at, old and fresh alike.
    registry.set_loop_status(old_loop, "released")
    registry.set_loop_status(fresh_loop, "released")

    ordered = [l["issue_number"] for l in registry.list_loops(app="app")]
    assert ordered == ["2", "1"]  # the fresh one still sorts first despite the admin touch


def test_set_loop_status_rejects_unknown_value(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    registry.record_run("r1", app="app", status="running", started_at=time.time())
    loop_id = _bind("r1", "app", 7)

    with pytest.raises(ValueError):
        registry.set_loop_status(loop_id, "bogus")
    registry.set_loop_status(loop_id, "released")
    assert registry.get_loop(loop_id)["status"] == "released"


def test_get_loop_is_none_for_unknown_id(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    assert registry.get_loop("deadbeef00") is None


def test_bind_loop_for_run_creates_an_objective_keyed_loop(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)

    loop_id = registry.bind_loop_for_run("app", "ship useful features")

    loop = registry.get_loop(loop_id)
    assert loop["kind"] == "objective"
    assert loop["objective"] == "ship useful features"
    assert loop["status"] == "active"
    # Idempotent: same app+objective returns the same id, no duplicate doc.
    assert registry.bind_loop_for_run("app", "ship useful features") == loop_id


def test_bind_loop_for_run_reuses_an_active_issue_loop_instead_of_a_parallel_doc(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    issue_loop_id = _bind("r1", "app", 7, title="fix a bug", kind="bug")

    loop_id = registry.bind_loop_for_run("app", "ship useful features")

    assert loop_id == issue_loop_id  # the in-flight issue loop wins, 1:1 issue<->loop preserved


def test_bind_loop_for_run_ignores_a_blocked_issue_loop(tmp_path, monkeypatch):
    """A loop parked on waiting_for_human / escalated is blocked on a human -- a
    fresh scheduled cycle must not bind to it (that let an orphaned wait whose
    issue was later closed hijack every future cycle -- agentra#20). Mirrors the
    agentra-loop copy; the engine's implementation is the one the loop proxies to."""
    _isolate(tmp_path, monkeypatch)
    blocked = _bind("r1", "app", 20, title="#20", kind="bug")
    registry.set_loop_human_input(blocked, {"question": "q", "issue_number": 20})
    assert registry.get_loop(blocked)["status"] == "waiting_for_human"

    fresh = registry.bind_loop_for_run("app", "ship useful features")

    assert fresh != blocked
    assert registry.get_loop(fresh)["kind"] == "objective"


def test_bind_loop_retires_the_objective_placeholder_from_this_run(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    registry.record_run("r1", app="app", status="running", started_at=time.time())
    placeholder = registry.bind_loop_for_run("app", "ship useful features")

    issue_loop = _bind("r1", "app", 7, title="add widget", kind="feature")

    assert registry.get_loop(placeholder) is None
    assert registry.get_loop(issue_loop) is not None
    assert [l["loop_id"] for l in registry.list_loops(app="app")] == [issue_loop]


def test_bind_loop_keeps_objective_loops_that_have_real_runs(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    registry.record_run("r1", app="app", status="running", started_at=time.time())
    triage_loop = registry.bind_loop_for_run("app", "ship useful features")
    registry.roll_up_loop(triage_loop, "r0", "completed", 0.5)  # a prior triage-only run

    _bind("r1", "app", 7, title="add widget", kind="feature")

    assert registry.get_loop(triage_loop) is not None

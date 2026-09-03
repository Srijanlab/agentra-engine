"""registry/loops.py: the loop as a stored entity (one doc per tracked issue)."""

import time
from pathlib import Path

import pytest

from agentra import registry


def _isolate(tmp_path: Path, monkeypatch):
    home = tmp_path / "agentra_home"
    monkeypatch.setattr(registry, "_db", None)
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
    registry.roll_up_loop(loop_id, "r1", "waiting_for_human", 0.10)

    loop = registry.get_loop(loop_id)
    assert loop["run_count"] == 2
    assert abs(loop["total_cost_usd"] - 0.35) < 1e-9
    assert loop["last_run_status"] == "waiting_for_human"
    assert loop["status"] == "waiting_for_human"


def test_list_loops_orders_by_updated_at_and_filters_by_app(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    for rk, app, iss in (("r1", "a", 1), ("r2", "b", 2), ("r3", "a", 3)):
        registry.record_run(rk, app=app, status="running", started_at=time.time())
        _bind(rk, app, iss)
        time.sleep(0.01)

    all_loops = registry.list_loops()
    assert [l["issue_number"] for l in all_loops] == ["3", "2", "1"]
    assert [l["issue_number"] for l in registry.list_loops(app="a")] == ["3", "1"]


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

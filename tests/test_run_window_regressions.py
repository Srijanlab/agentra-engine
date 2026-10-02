"""Regression tests: run reconcile and loop detail must not depend on global run volume."""

import time

import pytest

from agentra.registry import _dynamo, core, loops, runs
from test_dynamodb_backend import ddb_env  # noqa: F401

NOISE = 320


@pytest.fixture
def local_env(tmp_path, monkeypatch):
    """Local-JSON registry rooted at tmp_path."""
    monkeypatch.setattr(core, "_ddb", None)
    monkeypatch.setattr(core, "AGENTRA_HOME", tmp_path)
    monkeypatch.setattr(core, "APPS_PATH", tmp_path / "apps.json")
    monkeypatch.setattr(core, "INBOX_ROOT", tmp_path / "inbox")
    monkeypatch.setattr(core, "_RUNS_PATH", tmp_path / "runs.json")
    monkeypatch.setattr(core, "_LOOPS_PATH", tmp_path / "loops.json")
    return tmp_path


@pytest.fixture(params=["ddb", "local"])
def env(request, tmp_path):
    """Both storage backends, each with an app named 'a' registered."""
    request.getfixturevalue("ddb_env" if request.param == "ddb" else "local_env")
    core.register_app("a", repo_path=str(tmp_path))
    return request.param


def _seed_noise(count=NOISE, start=1000.0):
    for i in range(count):
        runs.record_run(f"noise{i}", app="other", source="scheduled", status="completed", started_at=start + i)


def test_reconcile_marks_orphan_older_than_300_newer_runs_of_other_apps(env):
    old = time.time() - 4 * 3600
    runs.record_run("orphan", app="a", status="running", started_at=old, updated_at=old)
    runs.record_run("fresh", app="a", status="running", started_at=time.time() - 10)
    _seed_noise(start=time.time() - 3000)

    marked = runs.reconcile_stale_runs()

    assert marked == ["orphan"]
    assert runs.get_run("orphan")["status"] == "failed"
    assert runs.get_run("orphan")["error"].startswith("orphaned:")
    assert runs.get_run("fresh")["status"] == "running"


def test_reconcile_still_covers_unregistered_app_in_global_window(env):
    old = time.time() - 4 * 3600
    runs.record_run("ghost", app="removed", status="queued", started_at=old, updated_at=old)

    assert runs.reconcile_stale_runs() == ["ghost"]


def test_get_loop_returns_run_older_than_newest_300_global_runs(env):
    loop_id = loops.bind_loop("a", 7)
    other_loop = loops.bind_loop("a", 8)
    runs.record_run("old", app="a", loop_id=loop_id, status="completed", started_at=10.0)
    runs.record_run("mid", app="a", loop_id=loop_id, status="completed", started_at=20.0)
    runs.record_run("sibling", app="a", loop_id=other_loop, status="completed", started_at=15.0)
    _seed_noise()

    detail = loops.get_loop(loop_id)

    assert [r["run_key"] for r in detail["runs"]] == ["mid", "old"]
    assert detail["loop_id"] == loop_id and detail["app"] == "a"


def test_get_loop_legacy_doc_without_app_uses_global_window(env):
    loops._write_loop("legacy", {"loop_id": "legacy", "status": "active", "updated_at": 1.0})
    runs.record_run("r1", app="a", loop_id="legacy", status="completed", started_at=5.0)

    assert [r["run_key"] for r in loops.get_loop("legacy")["runs"]] == ["r1"]


def test_get_loop_unknown_returns_none(env):
    assert loops.get_loop("nope") is None


def _seed_mixed():
    for i in range(12):
        runs.record_run(
            f"r{i}", app="a", loop_id="L1" if i % 3 == 0 else "L2",
            status="running" if i % 4 == 0 else "completed", started_at=100.0 + i,
        )
    runs.record_run("elsewhere", app="b", loop_id="L1", status="running", started_at=500.0)


def test_list_app_runs_filters_and_unbounded_limit(env):
    _seed_mixed()

    assert [r["run_key"] for r in runs.list_app_runs("a", statuses=("running",), limit=None)] == ["r8", "r4", "r0"]
    assert [r["run_key"] for r in runs.list_app_runs("a", loop_id="L1", limit=None)] == ["r9", "r6", "r3", "r0"]
    both = runs.list_app_runs("a", statuses=["running"], loop_id="L1", limit=None)
    assert [r["run_key"] for r in both] == ["r0"]
    assert len(runs.list_app_runs("a", limit=None)) == 12
    assert len(runs.list_app_runs("a", limit=5)) == 5


def test_list_app_runs_paginates_over_last_evaluated_key(ddb_env, monkeypatch):
    _seed_mixed()
    pages = []
    real_table = _dynamo.table

    class SmallPages:
        def __init__(self, table):
            self._table = table

        def query(self, **kwargs):
            resp = self._table.query(Limit=2, **kwargs)
            pages.append(resp)
            return resp

    monkeypatch.setattr(_dynamo, "table", lambda name: SmallPages(real_table(name)))

    found = runs.list_app_runs("a", statuses=("running",), limit=None)

    assert [r["run_key"] for r in found] == ["r8", "r4", "r0"]
    assert len(pages) > 1
    assert [r["run_key"] for r in runs.list_app_runs("a", statuses=("running",), limit=2)] == ["r8", "r4"]

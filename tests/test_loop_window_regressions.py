"""Regression tests: loop readers must not depend on a fixed-size recency window."""

import time

import pytest

from agentra.registry import core, loops, runs
from agentra.server.routes import triggers
from test_dynamodb_backend import ddb_env  # noqa: F401
from test_run_window_regressions import local_env  # noqa: F401

NOISE = 320


@pytest.fixture(params=["ddb", "local"])
def env(request, tmp_path):
    """Both storage backends, each with an app named 'a' registered."""
    request.getfixturevalue("ddb_env" if request.param == "ddb" else "local_env")
    core.register_app("a", repo_path=str(tmp_path))
    return request.param


def _seed_old(loop_id: str, app: str = "a", **fields) -> None:
    loops._write_loop(loop_id, {"loop_id": loop_id, "app": app, "created_at": 10.0, "updated_at": 10.0, **fields})


def _seed_noise(app: str = "a", status: str = "active", count: int = NOISE) -> None:
    now = time.time()
    for i in range(count):
        loops._write_loop(
            f"noise-{app}-{i}",
            {"loop_id": f"noise-{app}-{i}", "app": app, "status": status, "created_at": now - 1000 + i, "updated_at": now},
        )


def test_old_waiting_loop_is_listed_and_escalated(env):
    past = time.time() - core.HUMAN_INPUT_MAX_WAIT_SECONDS - 60
    _seed_old("old", status="waiting_for_human", human_input={"waiting_since": past})
    _seed_noise("a")
    _seed_noise("other")

    assert "old" in {l["loop_id"] for l in loops.list_waiting_for_human()}
    assert [l["loop_id"] for l in loops.reconcile_waiting_for_human()] == ["old"]
    assert loops._get_loop_doc("old")["status"] == "escalated"
    assert "old" in {l["loop_id"] for l in loops.list_waiting_for_human()}


def test_list_waiting_excludes_other_statuses(env):
    _seed_old("esc", status="escalated")
    for status in ("shipped", "released", "abandoned"):
        _seed_old(status, status=status)
    _seed_noise("a")

    assert {l["loop_id"] for l in loops.list_waiting_for_human()} == {"esc"}


def test_old_stuck_running_loop_is_unstuck(env):
    runs.record_run("r-old", app="a", status="completed", started_at=10.0)
    _seed_old("old", status="active", last_run_key="r-old", last_run_status="running")
    _seed_noise("a")
    _seed_noise("other")

    assert runs.reconcile_stale_loops() == ["old"]
    assert loops._get_loop_doc("old")["last_run_status"] == "completed"


def test_old_active_loop_with_closed_issue_is_released(env, monkeypatch):
    _seed_old("old", status="active", issue_number="7")
    _seed_noise("a")

    class FakeMemory:
        def __init__(self, repo):
            pass

        def issue_status(self, issue_number):
            return "done"

    monkeypatch.setattr(triggers, "Memory", FakeMemory)
    monkeypatch.setattr(triggers.registry, "get_app_repo", lambda name: object())

    triggers._reconcile_closed_issue_loops("a")

    doc = loops._get_loop_doc("old")
    assert doc["status"] == "released"
    assert doc["pipeline"]["terminal"] is True


def test_list_loops_by_status_filters_app_and_orders_newest_first(env):
    _seed_old("a-old", status="active")
    _seed_noise("a")
    _seed_old("b-old", app="b", status="active")

    found = loops.list_loops_by_status(("active",), app="a")

    assert len(found) == NOISE + 1
    assert {l["app"] for l in found} == {"a"}
    assert found[-1]["loop_id"] == "a-old"
    assert len(loops.list_loops_by_status(("active",))) == NOISE + 2


def test_list_loops_app_filter_applies_before_limit(env):
    _seed_old("mine", status="active")
    _seed_old("mine2", status="active")
    _seed_noise("other", count=30)

    found = loops.list_loops(app="a", limit=2)

    assert {l["loop_id"] for l in found} == {"mine", "mine2"}


def test_bind_loop_for_run_finds_old_active_issue_loop_behind_newer_non_active(env):
    _seed_old("old", status="active", issue_number="9")
    _seed_noise("a", status="shipped", count=25)

    assert loops.bind_loop_for_run("a", "some objective") == "old"

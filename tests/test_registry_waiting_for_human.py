"""registry/loops.py -- list_waiting_for_human / reconcile_waiting_for_human.
The *loop* (one per tracked issue) is what's parked on a blocking human
question -- the run that hit the block terminates normally. Pure state
transitions, no GitHub/Slack calls (dispatched by server/routes/triggers.py).
"""

import time

from agentra import registry


def _isolate_registry(tmp_path, monkeypatch):
    home = tmp_path / "agentra_home"
    monkeypatch.setattr(registry, "_ddb", None, raising=False)
    monkeypatch.setattr(registry, "AGENTRA_HOME", home)
    monkeypatch.setattr(registry, "_RUNS_PATH", home / "runs.json")
    monkeypatch.setattr(registry, "_LOOPS_PATH", home / "loops.json")
    monkeypatch.setattr(registry, "_AGENT_STEPS_PATH", home / "agent_steps.jsonl")


def _park(app, issue, waiting_since):
    loop_id = registry.bind_loop(app, issue, title=f"#{issue}")
    registry.set_loop_human_input(loop_id, {"waiting_since": waiting_since, "question": "q", "issue_number": issue})
    return loop_id


def test_list_waiting_for_human_includes_waiting_and_escalated_but_not_others(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    l1 = _park("app1", 1, time.time())
    l2 = _park("app1", 2, time.time())
    registry.set_loop_status(l2, "escalated")
    registry.bind_loop("app1", 3, title="#3")  # active, not waiting

    waiting = {l["loop_id"] for l in registry.list_waiting_for_human()}

    assert waiting == {l1, l2}


def test_reconcile_waiting_for_human_leaves_a_recent_wait_untouched(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    monkeypatch.setattr(registry.core, "HUMAN_INPUT_MAX_WAIT_SECONDS", 3600.0)
    lid = _park("app1", 1, time.time() - 60)

    assert registry.reconcile_waiting_for_human() == []
    assert registry.get_loop(lid)["status"] == "waiting_for_human"


def test_reconcile_waiting_for_human_escalates_a_loop_past_the_max_wait(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    monkeypatch.setattr(registry.core, "HUMAN_INPUT_MAX_WAIT_SECONDS", 3600.0)
    lid = _park("app1", 1, time.time() - 7200)

    escalated = registry.reconcile_waiting_for_human()

    assert [l["loop_id"] for l in escalated] == [lid]
    assert registry.get_loop(lid)["status"] == "escalated"


def test_reconcile_never_touches_a_loop_with_no_waiting_since(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    monkeypatch.setattr(registry.core, "HUMAN_INPUT_MAX_WAIT_SECONDS", 1.0)
    lid = registry.bind_loop("app1", 1, title="#1")
    registry.set_loop_human_input(lid, {"question": "q"})  # no waiting_since

    assert registry.reconcile_waiting_for_human() == []
    assert registry.get_loop(lid)["status"] == "waiting_for_human"


def test_reconcile_waiting_for_human_is_idempotent_once_escalated(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    monkeypatch.setattr(registry.core, "HUMAN_INPUT_MAX_WAIT_SECONDS", 1.0)
    _park("app1", 1, time.time() - 100)

    first = registry.reconcile_waiting_for_human()
    second = registry.reconcile_waiting_for_human()

    assert len(first) == 1
    assert second == []  # already escalated -- no re-escalate / re-notify

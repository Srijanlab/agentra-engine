"""registry/runs.py -- list_waiting_for_human/reconcile_waiting_for_human
(GitHub issue #34's dashboard 'Needs your input' panel and max-wait
escalation). Pure state-transition logic, no GitHub/Slack calls -- those
are dispatched by the caller (server/routes/triggers.py) using the records
this returns.
"""

import time

from agentra import registry


def _isolate_registry(tmp_path, monkeypatch):
    home = tmp_path / "agentra_home"
    monkeypatch.setattr(registry, "_db", None)
    monkeypatch.setattr(registry, "AGENTRA_HOME", home)
    monkeypatch.setattr(registry, "_RUNS_PATH", home / "runs.json")
    monkeypatch.setattr(registry, "_AGENT_STEPS_PATH", home / "agent_steps.jsonl")


def test_list_waiting_for_human_includes_waiting_and_escalated_but_not_others(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    registry.record_run("r1", app="app1", status="waiting_for_human", started_at=time.time())
    registry.record_run("r2", app="app1", status="escalated", started_at=time.time())
    registry.record_run("r3", app="app1", status="completed", started_at=time.time())
    registry.record_run("r4", app="app1", status="running", started_at=time.time())

    waiting = {r["run_key"] for r in registry.list_waiting_for_human()}

    assert waiting == {"r1", "r2"}


def test_reconcile_waiting_for_human_leaves_a_recent_wait_untouched(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    monkeypatch.setattr(registry.core, "HUMAN_INPUT_MAX_WAIT_SECONDS", 3600.0)
    registry.record_run(
        "r1", app="app1", status="waiting_for_human", started_at=time.time(),
        human_input={"waiting_since": time.time() - 60, "question": "q"},
    )

    escalated = registry.reconcile_waiting_for_human()

    assert escalated == []
    assert registry.get_run("r1")["status"] == "waiting_for_human"


def test_reconcile_waiting_for_human_escalates_a_run_past_the_max_wait(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    monkeypatch.setattr(registry.core, "HUMAN_INPUT_MAX_WAIT_SECONDS", 3600.0)
    registry.record_run(
        "r1", app="app1", status="waiting_for_human", started_at=time.time() - 7200,
        human_input={"waiting_since": time.time() - 7200, "question": "q"},
    )

    escalated = registry.reconcile_waiting_for_human()

    assert len(escalated) == 1
    assert escalated[0]["run_key"] == "r1"
    assert registry.get_run("r1")["status"] == "escalated"


def test_reconcile_waiting_for_human_never_touches_a_run_with_no_waiting_since(tmp_path, monkeypatch):
    """A run marked waiting_for_human without a human_input.waiting_since
    (shouldn't normally happen -- mark_waiting_for_human always sets it --
    but defensive: never guess an age for a run that never recorded one)."""
    _isolate_registry(tmp_path, monkeypatch)
    monkeypatch.setattr(registry.core, "HUMAN_INPUT_MAX_WAIT_SECONDS", 1.0)
    registry.record_run("r1", app="app1", status="waiting_for_human", started_at=time.time() - 10000)

    escalated = registry.reconcile_waiting_for_human()

    assert escalated == []
    assert registry.get_run("r1")["status"] == "waiting_for_human"


def test_reconcile_waiting_for_human_is_idempotent_once_escalated(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    monkeypatch.setattr(registry.core, "HUMAN_INPUT_MAX_WAIT_SECONDS", 1.0)
    registry.record_run(
        "r1", app="app1", status="waiting_for_human", started_at=time.time() - 100,
        human_input={"waiting_since": time.time() - 100, "question": "q"},
    )

    first = registry.reconcile_waiting_for_human()
    second = registry.reconcile_waiting_for_human()

    assert len(first) == 1
    # Already escalated -- status is no longer 'waiting_for_human', so a
    # second reconcile pass must not re-escalate (and re-notify) it again.
    assert second == []

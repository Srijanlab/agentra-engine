"""check_backlog binds an objective-keyed loop the moment it finds actionable work --
not at run creation. A run that finds an empty backlog and exits leaves no loop entry.
"""

import asyncio
from pathlib import Path

from agentra import registry
from agentra.agents import brain
from agentra.environments import EnvironmentConfig
from agentra.memory import Memory


def _isolate(tmp_path, monkeypatch):
    home = tmp_path / "agentra_home"
    monkeypatch.setattr(registry, "_db", None)
    monkeypatch.setattr(registry, "AGENTRA_HOME", home)
    monkeypatch.setattr(registry, "_RUNS_PATH", home / "runs.json")
    monkeypatch.setattr(registry, "_LOOPS_PATH", home / "loops.json")


def _session(tmp_path: Path) -> brain.OrchestratorSession:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    return brain.OrchestratorSession(
        repo=repo, objective="ship agentra", env=EnvironmentConfig(),
        mem=Memory(repo), run_id="run1", _app_name="agentra",
    )


def _tool(session, name):
    return next(t for t in brain._tools_for(session) if t.name == name)


def _stub_backlog(session, monkeypatch, **lists):
    for accessor in ("shipped_features", "shipped_pending_test_items", "code_complete_items",
                     "in_progress_features", "in_progress_items", "known_bugs", "feature_queue"):
        monkeypatch.setattr(session.mem, accessor, lambda a=accessor: lists.get(a, []))
    monkeypatch.setattr(session.mem, "resume_branch_for", lambda *a, **k: None)
    monkeypatch.setattr(session.mem, "resume_run_id_for", lambda *a, **k: None)


def test_empty_backlog_binds_no_loop(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    session = _session(tmp_path)
    _stub_backlog(session, monkeypatch)  # everything empty
    registry.record_run("run1", app="agentra", status="running", started_at=0.0)

    asyncio.run(_tool(session, "check_backlog").handler({}))

    assert registry.get_run("run1").get("loop_id") is None
    assert registry.list_loops(app="agentra") == []


def test_finding_a_known_bug_binds_an_objective_loop_and_links_the_run(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    session = _session(tmp_path)
    _stub_backlog(session, monkeypatch, known_bugs=[
        {"external_id": "9", "diagnosis": "a bug", "needs_human": False},
    ])
    registry.record_run("run1", app="agentra", status="running", started_at=0.0)

    asyncio.run(_tool(session, "check_backlog").handler({}))

    loop_id = registry.get_run("run1")["loop_id"]
    assert loop_id is not None
    loop = registry.get_loop(loop_id)
    assert loop["kind"] == "objective"
    assert loop["app"] == "agentra"

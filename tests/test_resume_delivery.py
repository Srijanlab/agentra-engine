"""resume_delivery: code-complete items go straight to delivery, never back through
implement_feature (which churned issue #3 through 6 runs)."""

import asyncio
import subprocess
from pathlib import Path

import pytest

from agentra import registry
from agentra.agents import brain
from agentra.agents import implementation
from agentra.agents.brain import tools as brain_tools
from agentra.agents.git_ops import fetch_ref  # noqa: F401  (patched by name below)
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
        repo=repo, objective="ship agentra",
        env=EnvironmentConfig(pre_prod_branch="beta", prod_branch="main"),
        mem=Memory(repo), run_id="run1", _app_name="agentra",
    )


def _tool(session, name):
    return next(t for t in brain._tools_for(session) if t.name == name)


@pytest.fixture(autouse=True)
def _stub_git(monkeypatch):
    monkeypatch.setattr("agentra.agents.git_ops.fetch_ref", lambda *a, **k: None)


def _patch_ancestry(monkeypatch, *, in_ref: str | None, branch_on_remote: bool):
    def fake_run(argv, *a, **k):
        rc = 1
        if "merge-base" in argv and in_ref is not None and f"origin/{in_ref}" in argv:
            rc = 0
        if "ls-remote" in argv:
            rc = 0 if branch_on_remote else 2
        return subprocess.CompletedProcess(argv, rc, b"", b"")
    monkeypatch.setattr(brain_tools.subprocess, "run", fake_run)


def test_already_in_production_marks_done_and_does_not_touch_the_branch(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    session = _session(tmp_path)
    registry.record_run("run1", app="agentra", status="running", started_at=0.0)
    monkeypatch.setattr(session.mem, "shipped_commit_for", lambda *_: "de7b8fecabc")
    monkeypatch.setattr(session.mem, "resume_branch_for", lambda *_: "dev/gone")
    done = []
    monkeypatch.setattr(session.mem, "mark_status_done", lambda n: done.append(n))
    checked_out = []
    monkeypatch.setattr(implementation, "_checkout_feature_branch", lambda *a, **k: checked_out.append(a) or True)
    _patch_ancestry(monkeypatch, in_ref="main", branch_on_remote=True)

    res = asyncio.run(_tool(session, "resume_delivery").handler({"resolves_id": "3"}))

    assert done == [3]
    assert checked_out == []
    assert not res.get("is_error")
    assert "already in production" in res["content"][0]["text"]


def test_branch_on_remote_is_checked_out_for_the_deploy_path(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    session = _session(tmp_path)
    registry.record_run("run1", app="agentra", status="running", started_at=0.0)
    monkeypatch.setattr(session.mem, "shipped_commit_for", lambda *_: None)
    monkeypatch.setattr(session.mem, "resume_branch_for", lambda *_: "dev/abc-slack")
    monkeypatch.setattr(session.mem, "resume_session_id_for", lambda *_: "sess-1")
    monkeypatch.setattr(implementation, "_checkout_feature_branch", lambda *a, **k: True)
    _patch_ancestry(monkeypatch, in_ref=None, branch_on_remote=True)

    res = asyncio.run(_tool(session, "resume_delivery").handler({"resolves_id": "5"}))

    assert session.feature_branch == "dev/abc-slack"
    assert session.committed_issue == "5"
    assert session.code_complete_issue_numbers == ["5"]
    assert session.tests_passed is False
    assert "do NOT call implement_feature" in res["content"][0]["text"]


def test_stale_marker_branch_gone_and_commit_nowhere_escalates(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    session = _session(tmp_path)
    registry.record_run("run1", app="agentra", status="running", started_at=0.0)
    monkeypatch.setattr(session.mem, "shipped_commit_for", lambda *_: "abc123")
    monkeypatch.setattr(session.mem, "resume_branch_for", lambda *_: "dev/gone")
    escalated = []
    monkeypatch.setattr(brain_tools, "_escalate_to_human", lambda session, **kw: escalated.append(kw))
    _patch_ancestry(monkeypatch, in_ref=None, branch_on_remote=False)

    res = asyncio.run(_tool(session, "resume_delivery").handler({"resolves_id": "7"}))

    assert res.get("is_error")
    assert escalated and escalated[0]["category"] == "stale_code_complete"


def test_implement_feature_redirects_a_code_complete_issue_to_resume_delivery(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    session = _session(tmp_path)
    session.cb_summary = "summary"
    registry.record_run("run1", app="agentra", status="running", started_at=0.0)
    monkeypatch.setattr(session.mem, "code_complete_items", lambda: [{"external_id": "3"}])
    monkeypatch.setattr(session.mem, "shipped_pending_test_items", lambda: [])

    res = asyncio.run(_tool(session, "implement_feature").handler(
        {"feature_brief": "resume slack thing", "resolves_id": "3"}
    ))

    assert res.get("is_error")
    assert "resume_delivery" in res["content"][0]["text"]

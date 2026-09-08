"""The orchestrator pipeline is a deterministic state machine: node state is
persisted on the issue's loop doc, each node hands off to exactly one next node,
and phase tools refuse out-of-contract calls."""

import asyncio
from pathlib import Path

from agentra import registry
from agentra.agents import brain
from agentra.agents import deployment
from agentra.agents.base import AgentResult
from agentra.environments import EnvironmentConfig
from agentra.memory import Memory


def _isolate(tmp_path, monkeypatch):
    home = tmp_path / "agentra_home"
    monkeypatch.setattr(registry, "AGENTRA_HOME", home)
    monkeypatch.setattr(registry, "_RUNS_PATH", home / "runs.json")
    monkeypatch.setattr(registry, "_LOOPS_PATH", home / "loops.json")


def _session(tmp_path: Path, **overrides) -> brain.OrchestratorSession:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    defaults = dict(
        repo=repo, objective="ship agentra", env=EnvironmentConfig(),
        mem=Memory(repo), run_id="run1", _app_name="agentra", cb_summary="summary",
    )
    defaults.update(overrides)
    return brain.OrchestratorSession(**defaults)


def _tool(session, name):
    return next(t for t in brain._tools_for(session) if t.name == name)


# --- loop-doc pipeline round-trip -------------------------------------------

def test_set_loop_pipeline_round_trips_and_merges(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    lid = registry.bind_loop("agentra", 7, title="x")

    registry.set_loop_pipeline(lid, last_node="run_local_tests", next_node="deploy_pre_prod",
                               tests_passed=True)
    registry.set_loop_pipeline(lid, last_node="deploy_pre_prod", next_node="verify_pre_prod")

    pipe = registry.get_loop_pipeline(lid)
    assert pipe["last_node"] == "deploy_pre_prod"
    assert pipe["next_node"] == "verify_pre_prod"
    assert pipe["tests_passed"] is True  # survived the second partial write
    assert registry.get_loop(lid)["pipeline"] == pipe


def test_set_loop_pipeline_noop_without_a_loop_doc(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    assert registry.set_loop_pipeline("nonexistent", next_node="x") is None
    assert registry.get_loop_pipeline("nonexistent") is None


def test_unknown_pipeline_field_is_ignored(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    lid = registry.bind_loop("agentra", 8, title="x")
    registry.set_loop_pipeline(lid, next_node="deploy_pre_prod", bogus="nope")
    assert "bogus" not in registry.get_loop_pipeline(lid)


def test_roll_up_marks_loop_shipped_when_pipeline_is_terminal(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    lid = registry.bind_loop("agentra", 9, title="x")
    registry.record_run("run1", app="agentra", status="running", started_at=0.0)
    registry.set_loop_pipeline(lid, terminal=True, next_node=None, status="tested")

    registry.roll_up_loop(lid, "run1", "completed", 1.0)

    assert registry.get_loop(lid)["status"] == "shipped"


# --- node guards -----------------------------------------------------------

def test_understand_codebase_noop_when_summary_already_loaded(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    session = _session(tmp_path, cb_summary="already here")
    res = asyncio.run(_tool(session, "understand_codebase").handler({}))
    assert res.get("is_error") is not True
    assert "SKIPPED" in res["content"][0]["text"]
    assert "check_backlog" in res["content"][0]["text"]


def test_run_local_tests_refuses_after_a_deploy_this_run(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    session = _session(tmp_path)
    session.deployed_to_pre_prod = True
    res = asyncio.run(_tool(session, "run_local_tests").handler({}))
    assert res["is_error"] is True
    assert "OUT OF CONTRACT" in res["content"][0]["text"]


def test_run_local_tests_refuses_a_shipped_issue(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    session = _session(tmp_path, committed_issue="4")
    monkeypatch.setattr(session.mem, "issue_status", lambda *_: "shipped")
    res = asyncio.run(_tool(session, "run_local_tests").handler({}))
    assert res["is_error"] is True
    assert "verify_pre_prod" in res["content"][0]["text"]


def test_deploy_pre_prod_refuses_when_already_deployed(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    session = _session(tmp_path, committed_issue="4")
    monkeypatch.setattr(session.mem, "issue_status", lambda *_: "code_complete")
    session.tests_passed = True
    session.feature_branch = "dev/x"
    session.deployed_to_pre_prod = True
    res = asyncio.run(_tool(session, "deploy_pre_prod").handler({}))
    assert res["is_error"] is True
    assert "OUT OF CONTRACT" in res["content"][0]["text"]


def test_verify_pre_prod_noop_when_already_verified(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    session = _session(tmp_path)
    session.pre_prod_verified = True
    res = asyncio.run(_tool(session, "verify_pre_prod").handler({}))
    assert res.get("is_error") is not True
    assert "NOTHING TO VERIFY" in res["content"][0]["text"]


# --- ci_cd_on_push strand: reaches tested, is not re-picked ----------------

def test_ci_cd_on_push_item_reaches_tested_and_is_not_repicked(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    session = _session(tmp_path, committed_issue="12", env=EnvironmentConfig(
        ci_cd_on_push=True, deploy_strategy="vercel_firebase"))
    registry.bind_loop("agentra", 12, title="footer")
    registry.record_run("run1", app="agentra", status="running", started_at=0.0)
    session.tests_passed = True
    session.feature_branch = "dev/footer"
    session.code_complete_issue_numbers = ["12"]
    tested = []
    monkeypatch.setattr(session.mem, "record_shipped_to_preprod", lambda ids, *_: list(ids))
    monkeypatch.setattr(session.mem, "record_tested", lambda ids, *_: tested.extend(ids) or list(ids))
    monkeypatch.setattr(session.mem, "issue_status", lambda *_: "code_complete")

    async def fake_vf(*a, **k):
        return AgentResult(ok=True, text="merged to beta", json_data={"preview_url": None},
                           cost_usd=0.0, turns=1)
    monkeypatch.setitem(deployment.PRE_PROD_STRATEGIES, "vercel_firebase", fake_vf)
    monkeypatch.setattr(brain.tools.change_risk, "classify_change", lambda *a, **k: "standard")

    res = asyncio.run(_tool(session, "deploy_pre_prod").handler({}))

    assert res.get("is_error") is not True
    assert tested == ["12"]
    assert session.pre_prod_verified is True
    assert "end the run" in res["content"][0]["text"]
    lid = registry.loop_id_for_issue("agentra", 12)
    assert registry.get_loop_pipeline(lid)["terminal"] is True


# --- resume_delivery branches on issue status ------------------------------

def test_resume_delivery_shipped_routes_to_verify_only(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    session = _session(tmp_path, env=EnvironmentConfig(deploy_strategy="self_hosted_vm"))
    lid = registry.loop_id_for_issue("agentra", 5)
    registry.bind_loop("agentra", 5, title="x")
    registry.set_loop_pipeline(lid, change_risk="standard", preview_url="https://preview.test")
    monkeypatch.setattr(session.mem, "issue_status", lambda *_: "shipped")
    monkeypatch.setattr(session.mem, "resume_branch_for", lambda *_: "dev/x")
    monkeypatch.setattr(session.mem, "get_spec", lambda *_: None)

    res = asyncio.run(_tool(session, "resume_delivery").handler({"resolves_id": "5"}))

    assert res.get("is_error") is not True
    assert "verify_pre_prod" in res["content"][0]["text"]
    assert "Do NOT re-test or re-deploy" in res["content"][0]["text"]
    assert session.deployed_to_pre_prod is True
    assert registry.get_loop_pipeline(lid)["next_node"] == "verify_pre_prod"


def test_resume_delivery_shipped_ci_cd_is_terminal(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    session = _session(tmp_path, env=EnvironmentConfig(ci_cd_on_push=True, deploy_strategy="vercel_firebase"))
    registry.bind_loop("agentra", 6, title="x")
    monkeypatch.setattr(session.mem, "issue_status", lambda *_: "shipped")
    monkeypatch.setattr(session.mem, "resume_branch_for", lambda *_: "dev/x")
    tested = []
    monkeypatch.setattr(session.mem, "record_tested", lambda ids, *_: tested.extend(ids) or list(ids))

    res = asyncio.run(_tool(session, "resume_delivery").handler({"resolves_id": "6"}))

    assert res.get("is_error") is not True
    assert tested == ["6"]
    assert "end the run" in res["content"][0]["text"]


def test_resume_delivery_tested_marks_done(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    session = _session(tmp_path)
    registry.bind_loop("agentra", 3, title="x")
    monkeypatch.setattr(session.mem, "issue_status", lambda *_: "tested")
    done = []
    monkeypatch.setattr(session.mem, "mark_status_done", lambda n: done.append(n))

    res = asyncio.run(_tool(session, "resume_delivery").handler({"resolves_id": "3"}))

    assert res.get("is_error") is not True
    assert done == [3]
    assert "end the run" in res["content"][0]["text"]


# --- check_backlog emits a single directive -------------------------------

def _stub_backlog(session, monkeypatch, **lists):
    for accessor in ("shipped_features", "shipped_pending_test_items", "code_complete_items",
                     "in_progress_features", "in_progress_items", "known_bugs", "feature_queue"):
        monkeypatch.setattr(session.mem, accessor, lambda a=accessor: lists.get(a, []))
    monkeypatch.setattr(session.mem, "resume_branch_for", lambda *a, **k: None)
    monkeypatch.setattr(session.mem, "resume_run_id_for", lambda *a, **k: None)
    monkeypatch.setattr(session.mem, "issue_status", lambda *_: lists.get("_status", "queue"))


def test_check_backlog_directive_for_a_queue_item(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    session = _session(tmp_path)
    registry.record_run("run1", app="agentra", status="running", started_at=0.0)
    _stub_backlog(session, monkeypatch, feature_queue=[{"external_id": "20", "description": "a feature"}])

    res = asyncio.run(_tool(session, "check_backlog").handler({}))
    text = res["content"][0]["text"]
    assert "DIRECTIVE" in text
    assert "next tool:    implement_feature" in text
    assert "resolves_id:  20" in text


def test_check_backlog_directive_uses_pipeline_next_node(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    session = _session(tmp_path)
    registry.record_run("run1", app="agentra", status="running", started_at=0.0)
    lid = registry.loop_id_for_issue("agentra", 30)
    registry.bind_loop("agentra", 30, title="x")
    registry.set_loop_pipeline(lid, next_node="deploy_pre_prod", terminal=False)
    _stub_backlog(session, monkeypatch, _status="code_complete",
                  code_complete_items=[{"external_id": "30", "description": "y", "kind": "feature"}])

    res = asyncio.run(_tool(session, "check_backlog").handler({}))
    assert "next tool:    deploy_pre_prod" in res["content"][0]["text"]


def test_check_backlog_directive_says_end_run_for_tested_item(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    session = _session(tmp_path)
    registry.record_run("run1", app="agentra", status="running", started_at=0.0)
    _stub_backlog(session, monkeypatch, _status="tested",
                  code_complete_items=[{"external_id": "40", "description": "z", "kind": "feature"}])

    res = asyncio.run(_tool(session, "check_backlog").handler({}))
    assert "already delivered" in res["content"][0]["text"].lower()
    assert "end the run" in res["content"][0]["text"].lower()


# --- mirror-invariant drift guard (engine: RPC allowlist; loop: proxy set) --

def test_new_registry_and_memory_methods_cross_the_loop_engine_boundary():
    names = {
        "set_loop_pipeline", "get_loop_pipeline", "set_loop_human_input",
        "enqueue_job", "claim_next_job", "report_job", "list_jobs",
    }
    try:
        from agentra.server.routes.internal import _MEMORY_METHODS, _REGISTRY_METHODS
        assert names <= _REGISTRY_METHODS
        assert "issue_status" in _MEMORY_METHODS
    except ImportError:  # agentra-loop has no server/routes/internal.py
        from agentra.memory import _ENGINE_PROXIED_METHODS
        from agentra.registry import _ENGINE_PROXIED
        assert names <= _ENGINE_PROXIED
        assert "issue_status" in _ENGINE_PROXIED_METHODS

import asyncio
import subprocess
import time
from pathlib import Path

from fastapi.testclient import TestClient

from agentra import environments, registry, server
from agentra.agents import deployment, git_ops
from agentra.agents.base import AgentResult
from agentra.connectors import github_fake
from agentra.memory import Memory
from agentra.server.routes import triggers


def _close_background_coro(coro):
    coro.close()
    return None


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _register_tmp_app(tmp_path: Path, name: str = "myapp") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("hello\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial commit")
    repo_url = f"https://github.com/acme/{name}.git"
    _git(repo, "remote", "add", "origin", repo_url)
    Memory(repo).set_objective("Ship useful dashboard improvements.")
    registry.register_app(name, str(repo), repo_url=repo_url, branch="main")
    return repo


def _isolate_registry(tmp_path, monkeypatch):
    home = tmp_path / "agentra_home"
    monkeypatch.setattr(registry, "_db", None)
    monkeypatch.setattr(registry, "AGENTRA_HOME", home)
    monkeypatch.setattr(registry, "APPS_PATH", home / "apps.json")
    monkeypatch.setattr(registry, "INBOX_ROOT", home / "inbox")
    monkeypatch.setattr(registry, "PAUSE_PATH", home / "paused.json")
    monkeypatch.setattr(registry, "_RUNS_PATH", home / "runs.json")
    monkeypatch.setattr(registry, "_AGENT_STEPS_PATH", home / "agent_steps.jsonl")
    server._active_runs.clear()
    server._app_locks.clear()
    monkeypatch.setattr(server.asyncio, "create_task", _close_background_coro)
    github_fake.install(monkeypatch=monkeypatch)


def test_scheduled_trigger_respects_per_app_schedule_hours(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    repo = _register_tmp_app(tmp_path)
    environments.save(repo, environments.EnvironmentConfig(schedule_hours=24))
    registry.record_run(
        "recent",
        app="myapp",
        source="scheduled",
        status="completed",
        started_at=time.time(),
        objective="Ship useful dashboard improvements.",
        loop_id=registry.loop_id_for("Ship useful dashboard improvements."),
    )

    response = TestClient(server.app).post("/trigger/scheduled", json={"app": "myapp"})

    assert response.status_code == 200
    assert response.json() == {
        "triggered": False,
        "reason": "not due yet per this app's configured schedule",
    }


def test_scheduled_trigger_with_no_app_fans_out_to_every_registered_app(tmp_path, monkeypatch):
    # Regression for the VM trigger-loop bug: compute.tf's local cron used
    # to hardcode {"app":"agentra"}, so any other registered app was never
    # scheduled at all. The loop now posts a bare {} tick and this
    # endpoint must cover every app in the registry itself.
    _isolate_registry(tmp_path, monkeypatch)
    _register_tmp_app(tmp_path, name="myapp")
    _register_tmp_app(tmp_path, name="otherapp")

    response = TestClient(server.app).post("/trigger/scheduled", json={})

    assert response.status_code == 200
    body = response.json()
    assert set(body["apps"].keys()) == {"myapp", "otherapp"}
    assert body["apps"]["myapp"]["triggered"] is True
    assert body["apps"]["otherapp"]["triggered"] is True


def test_on_demand_run_bypasses_schedule_gate(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    repo = _register_tmp_app(tmp_path)
    environments.save(repo, environments.EnvironmentConfig(schedule_hours=24))
    registry.record_run(
        "recent",
        app="myapp",
        source="scheduled",
        status="completed",
        started_at=time.time(),
        objective="Ship useful dashboard improvements.",
        loop_id=registry.loop_id_for("Ship useful dashboard improvements."),
    )

    response = TestClient(server.app).post("/apps/myapp/run")

    assert response.status_code == 200
    assert response.json()["triggered"] is True
    assert registry.get_run(response.json()["run_key"])["source"] == "on-demand"


def test_engine_queues_on_demand_runs_instead_of_dispatching(tmp_path, monkeypatch):
    """In Firestore/cloud mode the engine can't run cycles -- /apps/{name}/run
    records a queued run and returns queued:true; the loop drains it."""
    _isolate_registry(tmp_path, monkeypatch)
    _register_tmp_app(tmp_path)
    monkeypatch.setattr(registry, "cloud_mode", lambda: True)  # pretend we're the engine

    dispatched = []
    monkeypatch.setattr(
        "agentra.server.routes.triggers._run_autonomous_background",
        lambda *a, **k: dispatched.append(a),
    )

    body = TestClient(server.app).post("/apps/myapp/run").json()
    assert body["triggered"] is True
    assert body.get("queued") is True
    assert dispatched == []
    assert registry.get_run(body["run_key"])["status"] == "queued"


def test_alarm_trigger_respects_per_app_alarm_enabled(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    repo = _register_tmp_app(tmp_path)
    environments.save(repo, environments.EnvironmentConfig(alarm_enabled=False))
    # /trigger/alarm's Basic-auth gate (_verify_alarm_webhook_auth) only
    # activates when ALARM_WEBHOOK_PASSWORD is set in the environment --
    # this test isn't exercising that gate, so pin it unset rather than
    # inheriting whatever happens to be in the ambient shell (confirmed
    # live: a sandbox with that env var set made this test 401 instead of
    # 200, see test_failure_triage.py's regression test for the resulting
    # false-positive-unfixable-failure classification this caused).
    monkeypatch.delenv("ALARM_WEBHOOK_PASSWORD", raising=False)

    response = TestClient(server.app).post("/trigger/alarm", json={"app": "myapp", "symptom": "500s"})

    assert response.status_code == 200
    assert response.json() == {
        "triggered": False,
        "reason": "alarms disabled for this app",
    }


def test_dashboard_feature_request_submission_reaches_app_backlog(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    repo = _register_tmp_app(tmp_path)

    response = TestClient(server.app).post(
        "/apps/myapp/feature-requests",
        json={"description": "Let admins export the shipped list."},
    )

    assert response.status_code == 200
    assert response.json()["submitted"] is True
    queue = Memory(repo).feature_queue()
    assert len(queue) == 1
    assert queue[0]["description"] == "Let admins export the shipped list."


def test_dashboard_bug_submission_reaches_bug_backlog(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    repo = _register_tmp_app(tmp_path)

    response = TestClient(server.app).post(
        "/apps/myapp/backlog",
        json={
            "type": "bug",
            "title": "Export crashes on empty results",
            "severity": "high",
            "description": "Export crashes on an empty result set.",
        },
    )

    assert response.status_code == 200
    assert response.json()["submitted"] is True
    bugs = Memory(repo).known_bugs()
    assert len(bugs) == 1
    assert bugs[0]["diagnosis"] == "Export crashes on empty results"


def test_dashboard_bug_submission_without_a_title_is_rejected(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    _register_tmp_app(tmp_path)

    response = TestClient(server.app).post(
        "/apps/myapp/backlog",
        json={"type": "bug", "severity": "high", "description": "Export crashes on an empty result set."},
    )

    assert response.status_code == 400


def test_promote_endpoint_records_human_approved_run(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    _register_tmp_app(tmp_path)

    response = TestClient(server.app).post("/apps/myapp/promote")

    assert response.status_code == 200
    body = response.json()
    assert body["triggered"] is True
    recorded = registry.get_run(body["run_key"])
    assert recorded["source"] == "promote"
    assert recorded["status"] == "queued"


def test_promote_button_click_merges_tested_commit_and_reports_success_end_to_end(tmp_path, monkeypatch):
    """Issue #2/#5 ("On Promote: merge the tested commit to main, build, and
    deploy to prod"): the other endpoint-level tests in this file only
    check that POST /apps/{app}/promote *dispatches* a run (create_task is
    swallowed via _close_background_coro in _isolate_registry) -- none of
    them actually exercise what happens once that background task runs.
    This drives the real chain the dashboard's Promote button depends on:
    HTTP endpoint -> _run_promote_background -> orchestrator.run_promote ->
    deployment.promote_prod -> a real `git merge`/push of the pre-prod
    branch's tip (the commit the human just reviewed a test report for)
    into the prod branch -- only the LLM run_agent call is faked, same
    convention as test_deployment.py. Confirms the run this produces is
    exactly what RunDetailDrawer.tsx's promote() reads back: result.promoted
    is True, and prod branch content actually reflects the promoted commit.
    """
    _isolate_registry(tmp_path, monkeypatch)
    repo = _register_tmp_app(tmp_path)
    _git(repo, "checkout", "-b", "prod")
    _git(repo, "checkout", "-b", "beta")
    (repo / "README.md").write_text("hello\nbeta feature change\n")
    _git(repo, "commit", "-am", "beta: the feature that was just pre-prod-tested")
    beta_tip = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "beta"], check=True, capture_output=True, text=True
    ).stdout.strip()
    _git(repo, "checkout", "prod")
    environments.save(repo, environments.EnvironmentConfig(pre_prod_branch="beta", prod_branch="prod"))

    def _fake_pull_latest(_repo, branch):
        assert branch == "prod"
        _git(_repo, "checkout", branch)

    def _fake_fetch_ref(_repo, branch):
        assert branch == "beta"
        # No real "origin" remote in this test repo -- fake having fetched
        # by pointing refs/remotes/origin/beta at the real local beta tip,
        # exactly as real as an actual fetch would produce for _merge_and_push's
        # subsequent `git merge origin/beta`.
        _git(_repo, "update-ref", "refs/remotes/origin/beta", "refs/heads/beta")

    def _fake_push_branch(_repo, branch):
        assert branch == "prod"

    async def _fake_run_agent(**kwargs):
        assert kwargs.get("allow_prod") is True
        return AgentResult(ok=True, text="deployed to prod", json_data={"status": "deployed"}, cost_usd=0.01, turns=2)

    monkeypatch.setattr(git_ops, "pull_latest", _fake_pull_latest)
    monkeypatch.setattr(git_ops, "fetch_ref", _fake_fetch_ref)
    monkeypatch.setattr(git_ops, "push_branch", _fake_push_branch)
    monkeypatch.setattr(deployment, "run_agent", _fake_run_agent)
    monkeypatch.setattr(deployment, "persist_audit_trail", lambda _repo, _branch: None)

    response = TestClient(server.app).post("/apps/myapp/promote")
    assert response.status_code == 200
    run_key = response.json()["run_key"]

    # _isolate_registry patches asyncio.create_task to just close the
    # coroutine without running it (so unrelated tests in this file don't
    # need real git plumbing) -- here we run that exact same coroutine for
    # real, which is the only difference between "dispatched" and "the
    # human actually saw it finish."
    asyncio.run(server._run_promote_background(run_key, "myapp", repo))

    recorded = registry.get_run(run_key)
    assert recorded["status"] == "completed"
    assert recorded["result"]["promoted"] is True

    # The prod branch's tip now IS the exact commit that was reviewed/tested
    # on beta -- never a different or newer one -- and its content reflects
    # the merge, proving this isn't just a status flag with no real effect.
    # prod had no commits of its own beyond beta's history here, so this
    # merge fast-forwards; prod_tip landing exactly on beta_tip is the
    # strongest form of "the tested commit, not a different/newer one."
    prod_tip = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "prod"], check=True, capture_output=True, text=True
    ).stdout.strip()
    assert prod_tip == beta_tip
    assert (repo / "README.md").read_text() == "hello\nbeta feature change\n"


def test_promote_background_records_released_features(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    repo = _register_tmp_app(tmp_path)
    environments.save(repo, environments.EnvironmentConfig(prod_branch="production"))
    mem = Memory(repo)
    ready = mem.record_code_complete("Ready to ship dashboard", commit_sha="abc1234")
    already = mem.record_code_complete("Already released feature", commit_sha="def5678")
    mem.record_shipped_to_preprod([str(ready["issue_number"]), str(already["issue_number"])])
    mem.record_released("Already released feature", release_run_id="prior-run", commit_sha="beef999")

    calls = []
    monkeypatch.setattr(server, "_branch_head_sha", lambda _repo, branch: "feedc0de" if branch == "production" else None)
    monkeypatch.setattr(server.deployment, "persist_audit_trail", lambda _repo, branch: calls.append(branch) or None)

    released = server._record_production_release(repo, "promote-run")

    assert released == ["Ready to ship dashboard"]
    assert calls == ["production"]

    ledger = Memory(repo).released_features()
    assert [item["feature"] for item in ledger] == ["Already released feature", "Ready to ship dashboard"]
    assert ledger[1]["release_run_id"] == "promote-run"
    assert ledger[1]["commit_sha"] == "feedc0de"

    response = TestClient(server.app).get("/apps/myapp")
    assert response.status_code == 200
    body = response.json()
    assert body["released_count"] == 2
    assert body["released"][1]["release_run_id"] == "promote-run"
    # Both the newly-released feature and the already-released one get
    # status:done -- "already released" only tracks the old released.json
    # ledger, not the label, so a feature released before this label
    # existed still needs to be marked done as part of the next promotion.
    shipped_by_name = {f["feature"]: f for f in Memory(repo).shipped_features()}
    assert shipped_by_name["Ready to ship dashboard"]["status_done"] is True
    assert shipped_by_name["Already released feature"]["status_done"] is True


def test_promote_background_triggers_teardown_only_after_the_status_write_lands_even_when_release_bookkeeping_is_slow(tmp_path, monkeypatch):
    """GitHub issue #92 (follow-up to #83): promote_prod_self_hosted's outgoing-container
    teardown must never race _run_promote_background's own terminal status write. Even when
    _record_production_release (one GitHub API call per pending feature/bug) takes a while --
    simulated here as "however long it takes", not a real sleep, since ordering is what's
    being proven, not wall-clock timing -- the status write must land in the registry first,
    and the teardown trigger must fire strictly after that, never before and never gated on it.
    """
    _isolate_registry(tmp_path, monkeypatch)
    repo = _register_tmp_app(tmp_path)
    environments.save(repo, environments.EnvironmentConfig(deploy_strategy="self_hosted_vm", prod_branch="production"))

    teardown_info = {
        "cleanup_image": "widget-app:run1",
        "old_container": "widget-app-blue",
        "old_image": "widget-app:oldrun1",
        "sock_gid": "999",
        "delay_seconds": 5,
    }

    async def _fake_self_hosted_promote(_repo, _env, run_id, session_id):
        return AgentResult(
            ok=True, text="promoted", json_data={"status": "deployed", "teardown": teardown_info},
            cost_usd=0.0, turns=0,
        )

    monkeypatch.setitem(deployment.PROD_STRATEGIES, "self_hosted_vm", _fake_self_hosted_promote)

    run_key = "promote-run-92"
    events: list[str] = []

    def _slow_record_production_release(_repo, _run_id):
        # Stands in for many pending features/bugs, each needing its own GitHub API call.
        events.append("release_bookkeeping_ran")
        return ["Feature A", "Feature B", "Feature C"]

    monkeypatch.setattr(triggers, "_record_production_release", _slow_record_production_release)

    def _tracking_set_run(_run_key, **fields):
        if fields.get("status") in ("completed", "failed"):
            events.append("status_write_landed")
        registry.record_run(_run_key, **fields)

    monkeypatch.setattr(triggers, "_set_run", _tracking_set_run)

    def _tracking_trigger_deferred_teardown(json_data):
        events.append("teardown_triggered")
        assert json_data == teardown_info
        # Must be durably readable back from the registry by the time teardown fires --
        # not just "queued to be written before this call returns".
        assert registry.get_run(run_key)["status"] == "completed"

    monkeypatch.setattr(deployment, "trigger_deferred_teardown", _tracking_trigger_deferred_teardown)

    registry.record_run(run_key, app="myapp", source="promote", status="queued", started_at=time.time())

    asyncio.run(triggers._run_promote_background(run_key, "myapp", repo))

    # Release bookkeeping runs, then (and only then) the status write lands, then (and only
    # then) teardown is triggered -- never the reverse, regardless of how long bookkeeping took.
    assert events == ["release_bookkeeping_ran", "status_write_landed", "teardown_triggered"]

    recorded = registry.get_run(run_key)
    assert recorded["status"] == "completed"
    assert recorded["result"]["promoted"] is True
    assert recorded["result"]["released_count"] == 3


def test_prod_debug_background_triggers_teardown_only_after_the_status_write_lands(tmp_path, monkeypatch):
    """Same GitHub issue #92 ordering guarantee, for the auto-remediate hotfix-promotion path
    (_run_prod_debug_background), which also goes through the self_hosted_vm PROD_STRATEGIES
    entry via orchestrator.run_prod_debug_cycle -- must not regress into leaking old prod
    containers now that promote_prod_self_hosted no longer tears them down inline."""
    _isolate_registry(tmp_path, monkeypatch)
    repo = _register_tmp_app(tmp_path)

    from agentra.orchestrator import ProdDebugReport

    teardown_info = {
        "cleanup_image": "widget-app:hotfix1",
        "old_container": "widget-app-green",
        "old_image": "widget-app:oldhotfix1",
        "sock_gid": "999",
        "delay_seconds": 5,
    }

    async def _fake_run_prod_debug_cycle(_repo, _objective, symptom=None, run_id=None):
        return ProdDebugReport(
            run_id=run_id, root_cause_found=True, severity="high", fix_attempted=True,
            promoted_to_prod=True, diagnosis_text="diagnosed and hotfixed", teardown=teardown_info,
        )

    monkeypatch.setattr(triggers, "run_prod_debug_cycle", _fake_run_prod_debug_cycle)

    run_key = "prod-debug-run-92"
    events: list[str] = []

    def _tracking_set_run(_run_key, **fields):
        if fields.get("status") in ("completed", "failed"):
            events.append("status_write_landed")
        registry.record_run(_run_key, **fields)

    monkeypatch.setattr(triggers, "_set_run", _tracking_set_run)

    def _tracking_trigger_deferred_teardown(json_data):
        events.append("teardown_triggered")
        assert json_data == teardown_info
        assert registry.get_run(run_key)["status"] == "completed"

    monkeypatch.setattr(deployment, "trigger_deferred_teardown", _tracking_trigger_deferred_teardown)

    registry.record_run(run_key, app="myapp", source="alarm", status="queued", started_at=time.time())

    asyncio.run(triggers._run_prod_debug_background(run_key, "myapp", repo, "objective", None))

    assert events == ["status_write_landed", "teardown_triggered"]
    assert registry.get_run(run_key)["status"] == "completed"


def test_promote_background_marks_closed_bugs_as_status_done(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    repo = _register_tmp_app(tmp_path)
    environments.save(repo, environments.EnvironmentConfig(prod_branch="production"))
    mem = Memory(repo)
    mem.record_known_bug("run1", "high", "A real bug", "the fix")
    bug_id = mem.known_bugs()[0]["external_id"]
    mem.clear_known_bug(bug_id, "Resolved by agentra.")

    monkeypatch.setattr(server, "_branch_head_sha", lambda _repo, branch: None)
    monkeypatch.setattr(server.deployment, "persist_audit_trail", lambda _repo, branch: None)

    server._record_production_release(repo, "promote-run")

    assert Memory(repo).closed_bugs()[0]["status_done"] is True


def test_run_logs_endpoint_streams_existing_lines(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    repo = _register_tmp_app(tmp_path)
    run_key = "runlog1"
    registry.record_run(
        run_key,
        app="myapp",
        source="scheduled",
        status="completed",
        started_at=time.time(),
        objective="Ship useful dashboard improvements.",
        loop_id=registry.loop_id_for("Ship useful dashboard improvements."),
    )
    mem = Memory(repo)
    mem.log(run_key, "implementation agent: starting on dedicated branch 'feature/foo'")
    mem.log(run_key, "testing agent: ok=True turns=3 cost=$0.1234")

    response = TestClient(server.app).get(f"/runs/{run_key}/logs")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "implementation agent: starting" in response.text
    assert "testing agent: ok=True" in response.text
    assert "event: done" in response.text


def test_get_app_surfaces_in_progress_multi_part_features(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    repo = _register_tmp_app(tmp_path)
    mem = Memory(repo)

    mem.record_code_complete("Big feature", run_id="run1", more_parts_expected=True)

    response = TestClient(server.app).get("/apps/myapp")

    assert response.status_code == 200
    body = response.json()
    assert len(body["in_progress_features"]) == 1
    entry = body["in_progress_features"][0]
    assert entry["description"] == "Big feature"
    assert entry["sub_issues_total"] == 1
    assert entry["sub_issues_completed"] == 1


def test_get_app_surfaces_closed_bugs(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    repo = _register_tmp_app(tmp_path)
    mem = Memory(repo)

    mem.record_known_bug("run1", "high", "A real bug", "the fix")
    bug_id = mem.known_bugs()[0]["external_id"]
    mem.clear_known_bug(bug_id, "Resolved by agentra.")

    response = TestClient(server.app).get("/apps/myapp")

    assert response.status_code == 200
    body = response.json()
    assert len(body["closed_bugs"]) == 1
    assert body["closed_bugs"][0]["diagnosis"] == "A real bug"

"""server/routes/triggers.py's human-in-the-loop reconciliation (GitHub
issue #34) -- the polling-based half of the GitHub-issue-comment answer
channel, and the max-wait escalation/re-notification, both piggybacked on
the existing /trigger/scheduled tick (no new inbound endpoint, no new
infra -- see compute.tf's agentra-trigger-loop, already hitting this
endpoint every 15 minutes).
"""

import asyncio
import subprocess
import time
from pathlib import Path

from fastapi.testclient import TestClient

from agentra import environments, registry, server
from agentra.agents.brain import AutonomousCycleReport
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
    environments.save(repo, environments.EnvironmentConfig(schedule_hours=24))
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


def _escalate(repo: Path, *, tracking_issue: int = 17, branch: str = "dev/abc-add-login") -> int:
    mem = Memory(repo)
    issue_number = mem.record_known_bug(
        "run1", "medium", "Two auth providers are equally valid.",
        "Requires a human decision.", source="implementation-agent-human-input-required",
        needs_human=True, title="Human input required: Add login",
    )
    mem.record_human_input_context(
        issue_number, app=repo.name, run_id="run1", question="Should we use OAuth or magic links?",
        branch=branch, session_id="sess-abc123", tracking_issue=tracking_issue,
    )
    return issue_number


def test_reconcile_human_input_for_app_ignores_a_run_with_no_new_comment(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    repo = _register_tmp_app(tmp_path)
    issue_number = _escalate(repo)
    registry.record_run(
        "orig-run", app="myapp", status="waiting_for_human", started_at=time.time(),
        human_input={"issue_number": issue_number, "question": "q", "waiting_since": time.time()},
    )

    triggers._reconcile_human_input_for_app("myapp")

    assert registry.get_run("orig-run")["status"] == "waiting_for_human"


def test_reconcile_human_input_for_app_resumes_when_a_github_comment_answers_it(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    repo = _register_tmp_app(tmp_path)
    issue_number = _escalate(repo)
    registry.record_run(
        "orig-run", app="myapp", status="waiting_for_human", started_at=time.time(),
        human_input={"issue_number": issue_number, "question": "q", "waiting_since": time.time()},
    )
    # A human replies directly on the GitHub issue instead of using the dashboard.
    Memory(repo).mem_add_comment = None  # no-op guard, just documents intent
    from agentra.connectors import github_issues

    repo_url = f"https://github.com/acme/myapp.git"
    github_issues.add_comment(repo_url, issue_number, "Let's go with OAuth via the existing GitHub App.")

    triggers._reconcile_human_input_for_app("myapp")

    assert registry.get_run("orig-run")["status"] == "answered"
    issue = github_issues.get_issue(repo_url, issue_number)
    assert "need_human" not in issue["labels"]


def test_reconcile_human_input_for_app_also_resumes_an_already_escalated_run(tmp_path, monkeypatch):
    """Regression test: a run that already aged past the max-wait timeout
    and got auto-escalated (registry.reconcile_waiting_for_human, status
    flips to "escalated") must still resume when a human finally replies on
    its GitHub issue -- the poller must not only look at runs still in the
    plain "waiting_for_human" state, or an escalated run would never notice
    its own answer."""
    _isolate_registry(tmp_path, monkeypatch)
    repo = _register_tmp_app(tmp_path)
    issue_number = _escalate(repo)
    registry.record_run(
        "orig-run", app="myapp", status="escalated", started_at=time.time(),
        human_input={"issue_number": issue_number, "question": "q", "waiting_since": time.time() - 100000},
    )
    from agentra.connectors import github_issues

    repo_url = "https://github.com/acme/myapp.git"
    github_issues.add_comment(repo_url, issue_number, "Let's go with OAuth via the existing GitHub App.")

    triggers._reconcile_human_input_for_app("myapp")

    assert registry.get_run("orig-run")["status"] == "answered"


def test_reconcile_human_input_for_app_is_best_effort_per_run(tmp_path, monkeypatch):
    """One run's lookup blowing up must not stop the loop from checking the
    next app's other waiting runs (or the rest of the scheduled dispatch)."""
    _isolate_registry(tmp_path, monkeypatch)
    repo = _register_tmp_app(tmp_path)
    registry.record_run(
        "orig-run", app="myapp", status="waiting_for_human", started_at=time.time(),
        human_input={"issue_number": 12345, "question": "q", "waiting_since": time.time()},
    )

    def _raise(*a, **k):
        raise RuntimeError("GitHub API is down")

    monkeypatch.setattr(Memory, "find_unanswered_human_input_comment", _raise)

    # Must not raise.
    triggers._reconcile_human_input_for_app("myapp")


def test_trigger_scheduled_calls_reconciliation_for_every_app(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    _register_tmp_app(tmp_path, "app1")
    _register_tmp_app(tmp_path, "app2")
    called_for = []
    monkeypatch.setattr(triggers, "_reconcile_human_input_for_app", lambda app_name: called_for.append(app_name))
    monkeypatch.setattr(triggers, "_reconcile_human_input_timeouts", lambda: None)

    response = TestClient(server.app).post("/trigger/scheduled", json={})

    assert response.status_code == 200
    assert set(called_for) == {"app1", "app2"}


def test_reconcile_human_input_timeouts_renotifies_slack_for_escalated_runs(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    monkeypatch.setattr(registry.core, "HUMAN_INPUT_MAX_WAIT_SECONDS", 1.0)
    registry.record_run(
        "old-run", app="myapp", status="waiting_for_human", started_at=time.time() - 1000,
        human_input={
            "waiting_since": time.time() - 1000, "question": "Should we use OAuth?",
            "issue_url": "https://github.com/acme/myapp/issues/17", "branch": "dev/abc", "session_id": "sess-1",
        },
    )
    from agentra.connectors import slack

    slack_calls = []
    monkeypatch.setattr(slack, "notify_human_input_required", lambda **k: slack_calls.append(k) or True)

    triggers._reconcile_human_input_timeouts()

    assert registry.get_run("old-run")["status"] == "escalated"
    assert len(slack_calls) == 1
    assert slack_calls[0]["escalated"] is True
    assert slack_calls[0]["question"] == "Should we use OAuth?"


def test_reconcile_human_input_timeouts_no_op_when_nothing_is_overdue(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    monkeypatch.setattr(registry.core, "HUMAN_INPUT_MAX_WAIT_SECONDS", 3600.0)
    registry.record_run(
        "fresh-run", app="myapp", status="waiting_for_human", started_at=time.time(),
        human_input={"waiting_since": time.time(), "question": "q"},
    )
    from agentra.connectors import slack

    monkeypatch.setattr(
        slack, "notify_human_input_required", lambda **k: (_ for _ in ()).throw(AssertionError("must not notify"))
    )

    triggers._reconcile_human_input_timeouts()

    assert registry.get_run("fresh-run")["status"] == "waiting_for_human"


# -- the primary (non-resume) dispatch path must not clobber waiting_for_human -


def test_run_autonomous_background_preserves_waiting_for_human_status(tmp_path, monkeypatch):
    """Regression test: mark_waiting_for_human (agents/brain/__init__.py)
    writes status=waiting_for_human through to the run registry the moment
    HUMAN_INPUT_REQUIRED fires, mid-cycle. Once run_autonomous_cycle
    actually returns (the cycle ends there -- hard_stop_reason stops the
    brain from calling further tools), _run_autonomous_background used to
    unconditionally overwrite that back to status="completed", so a run
    that had just correctly appeared in the dashboard's 'Needs your input'
    panel would silently vanish from it the instant its own background task
    finished -- the opposite of GitHub issue #34's acceptance criterion
    that the run stays visibly waiting_for_human until a human answers.
    _run_human_resume_background (server/routes/human_input.py) already got
    this right; this covers the OTHER entry point into run_autonomous_cycle
    (a normal scheduled/on-demand dispatch hitting HUMAN_INPUT_REQUIRED for
    the first time, not a resume)."""
    _isolate_registry(tmp_path, monkeypatch)
    repo = _register_tmp_app(tmp_path)

    async def fake_run_autonomous_cycle(repo_arg, objective, env, **kwargs):
        # Simulates mark_waiting_for_human's own mid-cycle write, which a real
        # run_autonomous_cycle call would have already performed by the time
        # it returns.
        registry.record_run(
            kwargs["run_id"], status="waiting_for_human",
            human_input={"question": "Should we use OAuth or magic links?", "waiting_since": time.time()},
        )
        return AutonomousCycleReport(
            run_id=kwargs["run_id"], actions=["escalated to a human"], final_message="blocked", cost_usd=0.01,
            waiting_for_human=True,
        )

    monkeypatch.setattr(triggers, "run_autonomous_cycle", fake_run_autonomous_cycle)
    server._active_runs["run-key-1"] = {"app": "myapp", "source": "scheduled"}
    registry.record_run("run-key-1", app="myapp", source="scheduled", status="queued", started_at=time.time())

    asyncio.run(triggers._run_autonomous_background("run-key-1", "myapp", repo, "objective", None, False))

    recorded = registry.get_run("run-key-1")
    assert recorded["status"] == "waiting_for_human"
    assert recorded["human_input"]["question"] == "Should we use OAuth or magic links?"
    # Also visible in the dashboard's own listing, not just the raw record.
    assert "run-key-1" in {r["run_key"] for r in registry.list_waiting_for_human()}


def test_run_autonomous_background_still_completes_normally(tmp_path, monkeypatch):
    """The other half of the same branch: a cycle that finishes without
    hitting HUMAN_INPUT_REQUIRED must still report status="completed" as
    before -- this fix must not make every run look like it's waiting on a
    human."""
    _isolate_registry(tmp_path, monkeypatch)
    repo = _register_tmp_app(tmp_path)

    async def fake_run_autonomous_cycle(repo_arg, objective, env, **kwargs):
        return AutonomousCycleReport(run_id=kwargs["run_id"], actions=["did stuff"], final_message="ok", cost_usd=0.01)

    monkeypatch.setattr(triggers, "run_autonomous_cycle", fake_run_autonomous_cycle)
    server._active_runs["run-key-2"] = {"app": "myapp", "source": "scheduled"}
    registry.record_run("run-key-2", app="myapp", source="scheduled", status="queued", started_at=time.time())

    asyncio.run(triggers._run_autonomous_background("run-key-2", "myapp", repo, "objective", None, False))

    assert registry.get_run("run-key-2")["status"] == "completed"

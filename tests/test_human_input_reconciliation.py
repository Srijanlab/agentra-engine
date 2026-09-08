"""server/routes/triggers.py's human-in-the-loop reconciliation (GitHub
issue #34) -- the polling-based half of the GitHub-issue-comment answer
channel, and the max-wait escalation/re-notification, both piggybacked on
the existing /trigger/scheduled tick (no new inbound endpoint, no new
infra -- see compute.tf's agentra-trigger-loop, already hitting this
endpoint every 15 minutes).
"""

import subprocess
import time
from pathlib import Path

from fastapi.testclient import TestClient

from agentra import environments, registry, server
from agentra.connectors import github_fake
from agentra.memory import Memory
from agentra.server.routes import triggers



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
    monkeypatch.setattr(registry, "_ddb", None, raising=False)
    monkeypatch.setattr(registry, "AGENTRA_HOME", home)
    monkeypatch.setattr(registry, "APPS_PATH", home / "apps.json")
    monkeypatch.setattr(registry, "INBOX_ROOT", home / "inbox")
    monkeypatch.setattr(registry, "PAUSE_PATH", home / "paused.json")
    monkeypatch.setattr(registry, "_RUNS_PATH", home / "runs.json")
    monkeypatch.setattr(registry, "_LOOPS_PATH", home / "loops.json")
    monkeypatch.setattr(registry, "_AGENT_STEPS_PATH", home / "agent_steps.jsonl")
    server._active_runs.clear()
    server._app_locks.clear()
    github_fake.install(monkeypatch=monkeypatch)


def _park_loop(app: str, issue_number: int, *, waiting_since: float | None = None,
               branch: str = "dev/abc", status: str = "waiting_for_human") -> str:
    """A loop parked on issue #issue_number's blocking human question."""
    loop_id = registry.bind_loop(app, issue_number, title=f"#{issue_number}")
    registry.set_loop_human_input(loop_id, {
        "issue_number": issue_number, "question": "Should we use OAuth or magic links?",
        "issue_url": f"https://github.com/acme/{app}/issues/{issue_number}", "branch": branch,
        "session_id": "sess-1", "waiting_since": waiting_since if waiting_since is not None else time.time(),
    })
    if status != "waiting_for_human":
        registry.set_loop_status(loop_id, status)
    return loop_id


def _escalate(repo: Path, *, branch: str = "dev/abc-add-login") -> int:
    mem = Memory(repo)
    issue_number = mem.record_known_bug(
        "run1", "medium", "Two auth providers are equally valid.",
        "Requires a human decision.", source="implementation-agent-human-input-required",
        needs_human=True, title="Human input required: Add login",
    )
    mem.record_human_input_context(
        issue_number, app=repo.name, run_id="run1", question="Should we use OAuth or magic links?",
        branch=branch, session_id="sess-abc123", tracking_issue=issue_number,
    )
    return issue_number


def test_reconcile_human_input_for_app_ignores_a_loop_with_no_new_comment(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    repo = _register_tmp_app(tmp_path)
    issue_number = _escalate(repo)
    lid = _park_loop("myapp", issue_number)

    triggers._reconcile_human_input_for_app("myapp")

    assert registry.get_loop(lid)["status"] == "waiting_for_human"


def test_reconcile_human_input_for_app_resumes_when_a_github_comment_answers_it(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    repo = _register_tmp_app(tmp_path)
    issue_number = _escalate(repo)
    lid = _park_loop("myapp", issue_number)
    from agentra.connectors import github_issues

    repo_url = "https://github.com/acme/myapp.git"
    github_issues.add_comment(repo_url, issue_number, "Let's go with OAuth via the existing GitHub App.")

    triggers._reconcile_human_input_for_app("myapp")

    # The loop is being worked again -- dropped out of the needs-input listing.
    assert registry.get_loop(lid)["status"] == "active"
    issue = github_issues.get_issue(repo_url, issue_number)
    assert "need_human" not in issue["labels"]


def test_reconcile_human_input_for_app_also_resumes_an_already_escalated_loop(tmp_path, monkeypatch):
    """A loop that already aged past the max-wait and got auto-escalated must
    still resume when a human finally replies on its GitHub issue -- the poller
    must look at 'escalated' loops too, not only plain 'waiting_for_human'."""
    _isolate_registry(tmp_path, monkeypatch)
    repo = _register_tmp_app(tmp_path)
    issue_number = _escalate(repo)
    lid = _park_loop("myapp", issue_number, waiting_since=time.time() - 100000, status="escalated")
    from agentra.connectors import github_issues

    repo_url = "https://github.com/acme/myapp.git"
    github_issues.add_comment(repo_url, issue_number, "Let's go with OAuth via the existing GitHub App.")

    triggers._reconcile_human_input_for_app("myapp")

    assert registry.get_loop(lid)["status"] == "active"


def test_reconcile_human_input_for_app_is_best_effort_per_loop(tmp_path, monkeypatch):
    """One loop's lookup blowing up must not stop the poller checking the rest."""
    _isolate_registry(tmp_path, monkeypatch)
    repo = _register_tmp_app(tmp_path)
    _park_loop("myapp", 12345)

    def _raise(*a, **k):
        raise RuntimeError("GitHub API is down")

    monkeypatch.setattr(Memory, "find_unanswered_human_input_comment", _raise)

    triggers._reconcile_human_input_for_app("myapp")  # must not raise


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


def test_reconcile_human_input_timeouts_renotifies_slack_for_escalated_loops(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    monkeypatch.setattr(registry.core, "HUMAN_INPUT_MAX_WAIT_SECONDS", 1.0)
    lid = _park_loop("myapp", 17, waiting_since=time.time() - 1000)
    from agentra.connectors import slack

    slack_calls = []
    monkeypatch.setattr(slack, "notify_human_input_required", lambda **k: slack_calls.append(k) or True)

    triggers._reconcile_human_input_timeouts()

    assert registry.get_loop(lid)["status"] == "escalated"
    assert len(slack_calls) == 1
    assert slack_calls[0]["escalated"] is True
    assert slack_calls[0]["question"] == "Should we use OAuth or magic links?"


def test_reconcile_human_input_timeouts_no_op_when_nothing_is_overdue(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    monkeypatch.setattr(registry.core, "HUMAN_INPUT_MAX_WAIT_SECONDS", 3600.0)
    lid = _park_loop("myapp", 17, waiting_since=time.time())
    from agentra.connectors import slack

    monkeypatch.setattr(
        slack, "notify_human_input_required", lambda **k: (_ for _ in ()).throw(AssertionError("must not notify"))
    )

    triggers._reconcile_human_input_timeouts()

    assert registry.get_loop(lid)["status"] == "waiting_for_human"

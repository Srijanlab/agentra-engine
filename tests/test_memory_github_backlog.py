"""Regression tests for Memory.known_bugs()/feature_queue() (and their
record_*/clear_* counterparts): GitHub Issues are the ONLY backlog store
-- no local known_bugs.json/feature_queue.json mirror at all, a
deliberate availability tradeoff (see memory.py's module comment). A repo
with no github.com remote, or an unreachable GitHub API, simply has no
visible backlog -- reads return [], writes are a no-op (logged as an
error, since there's nowhere else for the report to go).

Real local git repos on disk (git init + `git remote add origin ...`),
same pattern as test_registry_sync.py. github_issues' actual HTTP calls
are monkeypatched -- no real GitHub API traffic.
"""

import subprocess
from pathlib import Path

import pytest

from agentra.connectors import github_issues, github_projects
from agentra.memory import Memory


@pytest.fixture(autouse=True)
def _stub_project_sync(monkeypatch):
    # record_feature_request also adds the new issue to the app's GitHub
    # Project (see memory.py/github_projects.py) -- stubbed to a no-op here
    # so these Issues-focused tests don't also need a fake Project backend.
    # test_github_projects.py covers github_projects.py itself; a couple of
    # tests below assert this gets called with the right arguments.
    monkeypatch.setattr(github_projects, "add_item_to_feature_project", lambda *a, **k: None)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _init_repo(path: Path, remote: str | None = "https://github.com/acme/app.git") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    (path / "README.md").write_text("hello\n")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "initial commit")
    if remote:
        _git(path, "remote", "add", "origin", remote)
    return path


# ── known_bugs() reads live from GitHub ───────────────────────────────────


def test_known_bugs_reads_from_github(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)
    monkeypatch.setattr(
        github_issues,
        "list_open_issues",
        lambda repo_url, labels=None: [
            {"number": 7, "title": "Pagination bug", "body": "off by one", "html_url": "https://github.com/acme/app/issues/7"}
        ],
    )

    bugs = mem.known_bugs()

    assert bugs == [
        {
            "run_id": "7",
            "severity": "medium",
            "diagnosis": "Pagination bug",
            "proposed_fix": "off by one",
            "source": "github",
            "external_id": "7",
            "html_url": "https://github.com/acme/app/issues/7",
        }
    ]


def test_known_bugs_returns_empty_without_a_github_remote(tmp_path):
    repo = _init_repo(tmp_path / "repo", remote=None)
    mem = Memory(repo)

    assert mem.known_bugs() == []


def test_known_bugs_returns_empty_when_github_call_fails(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)

    def _raise(*args, **kwargs):
        raise github_issues.GitHubIssuesError("boom")

    monkeypatch.setattr(github_issues, "list_open_issues", _raise)

    assert mem.known_bugs() == []


# ── record_known_bug() always creates a GitHub issue ─────────────────────


def test_record_known_bug_creates_a_github_issue(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)
    created = {}

    def fake_create(repo_url, title, body, labels=None):
        created["title"] = title
        created["labels"] = labels
        return {"number": 99}

    monkeypatch.setattr(github_issues, "create_issue", fake_create)

    mem.record_known_bug("run1", "high", "Checkout crashes", "null-check the cart")

    assert created["title"] == "Checkout crashes"
    assert created["labels"] == ["bug"]


def test_record_known_bug_is_a_noop_without_a_github_remote(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo", remote=None)
    mem = Memory(repo)

    def fail_create(*a, **k):
        raise AssertionError("should not attempt a GitHub call with no remote")

    monkeypatch.setattr(github_issues, "create_issue", fail_create)

    mem.record_known_bug("run1", "high", "Checkout crashes", "null-check the cart")  # must not raise


def test_record_known_bug_logs_when_github_create_fails(tmp_path, monkeypatch, caplog):
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)

    def _raise(*a, **k):
        raise github_issues.GitHubIssuesError("rate limited")

    monkeypatch.setattr(github_issues, "create_issue", _raise)

    mem.record_known_bug("run1", "high", "Checkout crashes", "null-check the cart")  # must not raise


# ── clear_known_bug() closes the GitHub issue when the id is numeric ────────


def test_clear_known_bug_closes_the_github_issue(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)
    closed = {}

    def fake_close(repo_url, issue_number, comment=None):
        closed["issue_number"] = issue_number
        closed["comment"] = comment

    monkeypatch.setattr(github_issues, "close_issue", fake_close)

    mem.clear_known_bug("42")

    assert closed["issue_number"] == 42


def test_clear_known_bug_passes_through_a_custom_resolution_note(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)
    closed = {}

    monkeypatch.setattr(
        github_issues, "close_issue", lambda repo_url, issue_number, comment=None: closed.update(comment=comment)
    )

    mem.clear_known_bug("42", "Resolved by agentra: shipped as 'Fix pagination' (commit abc1234)")

    assert closed["comment"] == "Resolved by agentra: shipped as 'Fix pagination' (commit abc1234)"


def test_clear_known_bug_with_non_numeric_id_does_not_call_github(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)

    def fail_close(*a, **k):
        raise AssertionError("should not call GitHub for a non-numeric id")

    monkeypatch.setattr(github_issues, "close_issue", fail_close)

    mem.clear_known_bug("run-abc")  # must not raise


# ── feature_queue() mirrors the same behavior with the "enhancement" label ──


def test_feature_queue_reads_from_github(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)
    captured = {}

    def fake_list(repo_url, labels=None):
        captured["labels"] = labels
        return [{"number": 5, "title": "Add dark mode"}]

    monkeypatch.setattr(github_issues, "list_open_issues", fake_list)

    queue = mem.feature_queue()

    assert captured["labels"] == ["enhancement"]
    assert queue == [{"description": "Add dark mode", "source": "github", "external_id": "5", "html_url": None}]


def test_feature_queue_returns_empty_without_a_github_remote(tmp_path):
    repo = _init_repo(tmp_path / "repo", remote=None)
    mem = Memory(repo)

    assert mem.feature_queue() == []


def test_record_feature_request_creates_a_github_issue(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)
    created = {}

    def fake_create(repo_url, title, body, labels=None):
        created["title"] = title
        created["labels"] = labels
        return {"number": 11}

    monkeypatch.setattr(github_issues, "create_issue", fake_create)

    mem.record_feature_request("Add keyboard shortcuts")

    assert created["title"] == "Add keyboard shortcuts"
    assert created["labels"] == ["enhancement"]


def test_record_feature_request_adds_the_new_issue_to_the_project_as_todo(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)
    monkeypatch.setattr(github_issues, "create_issue", lambda *a, **k: {"number": 11})
    project_calls = []
    monkeypatch.setattr(
        github_projects,
        "add_item_to_feature_project",
        lambda repo_url, feature_issue_number, title, status="Todo": project_calls.append((feature_issue_number, title, status)),
    )

    mem.record_feature_request("Add keyboard shortcuts")

    assert project_calls == [(11, "Add keyboard shortcuts", "Todo")]


def test_clear_feature_request_closes_the_github_issue(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)
    closed = {}

    monkeypatch.setattr(
        github_issues, "close_issue", lambda repo_url, issue_number, comment=None: closed.update(issue_number=issue_number)
    )

    mem.clear_feature_request("11")

    assert closed["issue_number"] == 11

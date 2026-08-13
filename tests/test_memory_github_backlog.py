"""Phase 2 regression tests: Memory.known_bugs()/feature_queue() (and their
record_*/clear_* counterparts) read/write GitHub Issues as the authoritative
backlog when the target repo has a github.com remote, falling back to the
local known_bugs.json/feature_queue.json mirror when GitHub is unreachable,
not configured, or the repo has no github.com remote at all.

Real local git repos on disk (git init + `git remote add origin ...`), same
pattern as test_registry_sync.py -- the point here is real `git remote
get-url origin` behavior. github_issues' actual HTTP calls are monkeypatched
so no real GitHub API traffic happens.
"""

import subprocess
from pathlib import Path

import pytest

from agentra.connectors import github_issues
from agentra.memory import Memory


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


# ── known_bugs() reads live from GitHub when a github.com remote exists ──────


def test_known_bugs_reads_from_github_when_remote_configured(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)
    monkeypatch.setattr(
        github_issues,
        "list_open_issues",
        lambda repo_url, labels=None: [{"number": 7, "title": "Pagination bug", "body": "off by one"}],
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
        }
    ]


def test_known_bugs_falls_back_to_local_json_without_a_github_remote(tmp_path):
    repo = _init_repo(tmp_path / "repo", remote=None)
    mem = Memory(repo)
    mem.known_bugs_path.write_text('[{"run_id": "r1", "severity": "low", "diagnosis": "x", "proposed_fix": "y", "source": "prod-monitoring", "external_id": null}]')

    bugs = mem.known_bugs()

    assert bugs == [
        {"run_id": "r1", "severity": "low", "diagnosis": "x", "proposed_fix": "y", "source": "prod-monitoring", "external_id": None}
    ]


def test_known_bugs_falls_back_to_local_json_when_github_call_fails(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)

    def _raise(*args, **kwargs):
        raise github_issues.GitHubIssuesError("boom")

    monkeypatch.setattr(github_issues, "list_open_issues", _raise)
    mem.known_bugs_path.write_text('[{"run_id": "r1", "severity": "low", "diagnosis": "x", "proposed_fix": "y", "source": "prod-monitoring", "external_id": null}]')

    bugs = mem.known_bugs()

    assert bugs[0]["diagnosis"] == "x"


# ── record_known_bug() creates a GitHub issue and mirrors it locally ────────


def test_record_known_bug_creates_a_github_issue_and_local_mirror(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)
    created = {}

    def fake_create(repo_url, title, body, labels=None):
        created["title"] = title
        created["labels"] = labels
        return {"number": 99}

    monkeypatch.setattr(github_issues, "create_issue", fake_create)
    # known_bugs() itself would hit GitHub too -- keep the assertion scoped
    # to the local mirror, which record_known_bug always writes regardless.
    monkeypatch.setattr(github_issues, "list_open_issues", lambda *a, **k: [])

    mem.record_known_bug("run1", "high", "Checkout crashes", "null-check the cart")

    assert created["title"] == "Checkout crashes"
    assert created["labels"] == ["bug"]
    local = mem._local_known_bugs()
    assert local[0]["external_id"] == "99"
    assert local[0]["diagnosis"] == "Checkout crashes"


def test_record_known_bug_with_existing_external_id_skips_github_creation(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)

    def fail_create(*a, **k):
        raise AssertionError("should not create a GitHub issue when external_id is already given")

    monkeypatch.setattr(github_issues, "create_issue", fail_create)

    mem.record_known_bug("run1", "high", "Ticket from Zendesk", "fix it", external_id="zendesk-123")

    local = mem._local_known_bugs()
    assert local[0]["external_id"] == "zendesk-123"


def test_record_known_bug_falls_back_to_local_only_when_github_create_fails(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)

    def _raise(*a, **k):
        raise github_issues.GitHubIssuesError("rate limited")

    monkeypatch.setattr(github_issues, "create_issue", _raise)

    mem.record_known_bug("run1", "high", "Checkout crashes", "null-check the cart")

    local = mem._local_known_bugs()
    assert local[0]["external_id"] is None
    assert local[0]["diagnosis"] == "Checkout crashes"


# ── clear_known_bug() closes the GitHub issue when the id is numeric ────────


def test_clear_known_bug_closes_the_github_issue(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)
    mem.known_bugs_path.write_text(
        '[{"run_id": "r1", "severity": "high", "diagnosis": "x", "proposed_fix": "y", "source": "github", "external_id": "42"}]'
    )
    closed = {}

    def fake_close(repo_url, issue_number, comment=None):
        closed["issue_number"] = issue_number
        closed["comment"] = comment

    monkeypatch.setattr(github_issues, "close_issue", fake_close)

    mem.clear_known_bug("42")

    assert closed["issue_number"] == 42
    assert mem._local_known_bugs() == []


def test_clear_known_bug_with_non_numeric_id_does_not_call_github(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)
    mem.known_bugs_path.write_text(
        '[{"run_id": "run-abc", "severity": "high", "diagnosis": "x", "proposed_fix": "y", "source": "prod-monitoring", "external_id": null}]'
    )

    def fail_close(*a, **k):
        raise AssertionError("should not call GitHub for a non-numeric run_id")

    monkeypatch.setattr(github_issues, "close_issue", fail_close)

    mem.clear_known_bug("run-abc")

    assert mem._local_known_bugs() == []


# ── feature_queue() mirrors the same behavior with the "enhancement" label ──


def test_feature_queue_reads_from_github_when_remote_configured(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)
    captured = {}

    def fake_list(repo_url, labels=None):
        captured["labels"] = labels
        return [{"number": 5, "title": "Add dark mode"}]

    monkeypatch.setattr(github_issues, "list_open_issues", fake_list)

    queue = mem.feature_queue()

    assert captured["labels"] == ["enhancement"]
    assert queue == [{"description": "Add dark mode", "source": "github", "external_id": "5"}]


def test_record_feature_request_creates_a_github_issue(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)

    monkeypatch.setattr(github_issues, "create_issue", lambda repo_url, title, body, labels=None: {"number": 11})

    mem.record_feature_request("Add keyboard shortcuts")

    local = mem._local_feature_queue()
    assert local[0]["external_id"] == "11"


def test_clear_feature_request_closes_the_github_issue(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)
    mem.feature_queue_path.write_text('[{"description": "x", "source": "github", "external_id": "11"}]')
    closed = {}

    monkeypatch.setattr(
        github_issues, "close_issue", lambda repo_url, issue_number, comment=None: closed.update(issue_number=issue_number)
    )

    mem.clear_feature_request("11")

    assert closed["issue_number"] == 11
    assert mem._local_feature_queue() == []

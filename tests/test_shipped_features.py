"""Regression tests for Memory.shipped_features()/record_shipped(): a
shipped feature is a closed GitHub 'enhancement' issue -- there is no
local shipped.json anymore. record_shipped() either closes the
feature_queue issue it resolves (resolves_id) or opens-and-immediately-
closes a fresh one, stamping run_id/commit_sha into the issue body so
shipped_features() can read them straight back out of the same
state=closed list call. Same pattern as test_memory_github_backlog.py:
real local git repos on disk, github_issues' HTTP calls monkeypatched.
"""

import subprocess
from pathlib import Path

import pytest

from agentra.connectors import github_issues, github_projects
from agentra.memory import Memory


@pytest.fixture(autouse=True)
def _stub_project_sync(monkeypatch):
    # record_shipped also moves the issue's card to "Done" on the app's
    # GitHub Project (see memory.py/github_projects.py) -- stubbed to a
    # no-op here so these Issues-focused tests don't also need a fake
    # Project backend. test_github_projects.py covers github_projects.py
    # itself; a dedicated test below asserts this gets called correctly.
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


def test_shipped_features_reads_from_github(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)
    monkeypatch.setattr(
        github_issues,
        "list_closed_issues",
        lambda repo_url, labels=None, limit=30: [
            {
                "number": 12,
                "title": "Dark mode",
                "body": "Autonomously shipped by agentra.\n\n---\nShipped-Run-ID: run42\nShipped-Commit: abc1234\n",
                "closed_at": "2026-08-10T12:00:00Z",
            }
        ],
    )

    assert mem.shipped_features() == [
        {
            "feature": "Dark mode",
            "commit_sha": "abc1234",
            "run_id": "run42",
            "ts": "2026-08-10T12:00:00Z",
            "external_id": "12",
            "html_url": None,
        }
    ]


def test_shipped_features_tolerates_a_body_with_no_structured_fields(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)
    monkeypatch.setattr(
        github_issues,
        "list_closed_issues",
        lambda repo_url, labels=None, limit=30: [
            {"number": 3, "title": "Old feature", "body": None, "closed_at": "2026-01-01T00:00:00Z"}
        ],
    )

    assert mem.shipped_features() == [
        {
            "feature": "Old feature",
            "commit_sha": None,
            "run_id": None,
            "ts": "2026-01-01T00:00:00Z",
            "external_id": "3",
            "html_url": None,
        }
    ]


def test_shipped_features_returns_empty_without_a_github_remote(tmp_path):
    repo = _init_repo(tmp_path / "repo", remote=None)
    mem = Memory(repo)

    assert mem.shipped_features() == []


def test_record_shipped_creates_and_closes_a_fresh_issue_for_a_self_initiated_feature(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)

    created = {}
    closed = {}

    def _create_issue(repo_url, title, body, labels=None):
        created.update(repo_url=repo_url, title=title, body=body, labels=labels)
        return {"number": 99, "title": title, "body": body}

    def _close_issue(repo_url, issue_number, comment=None, body_suffix=None):
        closed.update(repo_url=repo_url, issue_number=issue_number, comment=comment, body_suffix=body_suffix)

    monkeypatch.setattr(github_issues, "create_issue", _create_issue)
    monkeypatch.setattr(github_issues, "close_issue", _close_issue)

    result = mem.record_shipped("Dark mode", commit_sha="abc1234", run_id="run42")

    assert created["title"] == "Dark mode"
    assert created["labels"] == ["enhancement"]
    assert closed["issue_number"] == 99
    assert "run42" in closed["comment"]
    assert "abc1234" in closed["comment"]
    assert "Shipped-Run-ID: run42" in closed["body_suffix"]
    assert "Shipped-Commit: abc1234" in closed["body_suffix"]
    assert result == 99


def test_record_shipped_closes_the_originating_feature_queue_issue_instead_of_creating_a_new_one(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)

    monkeypatch.setattr(
        github_issues, "create_issue", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not create a new issue"))
    )
    closed = {}
    monkeypatch.setattr(
        github_issues,
        "close_issue",
        lambda repo_url, issue_number, comment=None, body_suffix=None: closed.update(
            issue_number=issue_number, comment=comment, body_suffix=body_suffix
        ),
    )

    result = mem.record_shipped("Approvals queue UI", commit_sha="def5678", run_id="run7", resolves_id="42")

    assert closed["issue_number"] == 42
    assert "Shipped-Run-ID: run7" in closed["body_suffix"]
    assert result == 42


def test_record_shipped_moves_the_project_card_to_done(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)
    monkeypatch.setattr(github_issues, "create_issue", lambda *a, **k: {"number": 99})
    monkeypatch.setattr(github_issues, "close_issue", lambda *a, **k: None)
    project_calls = []
    monkeypatch.setattr(
        github_projects,
        "add_item_to_feature_project",
        lambda repo_url, feature_issue_number, title, issue_number=None, status="Todo": project_calls.append(
            (feature_issue_number, title, issue_number, status)
        ),
    )

    mem.record_shipped("Dark mode", commit_sha="abc1234", run_id="run42")

    assert project_calls == [(99, "Dark mode", None, "Done")]

    project_calls.clear()
    mem.record_shipped("Approvals queue UI", commit_sha="def5678", run_id="run7", resolves_id="42")

    assert project_calls == [(42, "Approvals queue UI", None, "Done")]


def test_record_shipped_with_sub_feature_of_creates_a_linked_sub_issue(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)

    sub_issue_calls = {}

    def _create_sub_issue(repo_url, parent_issue_number, title, body, labels=None):
        sub_issue_calls.update(parent_issue_number=parent_issue_number, title=title, labels=labels)
        return {"number": 55, "title": title}

    monkeypatch.setattr(github_issues, "create_sub_issue", _create_sub_issue)
    monkeypatch.setattr(github_issues, "close_issue", lambda *a, **k: None)
    monkeypatch.setattr(github_issues, "get_issue", lambda repo_url, issue_number: {"number": issue_number, "title": "Big feature"})

    project_calls = []
    monkeypatch.setattr(
        github_projects,
        "add_item_to_feature_project",
        lambda repo_url, feature_issue_number, title, issue_number=None, status="Todo": project_calls.append(
            (feature_issue_number, title, issue_number, status)
        ),
    )

    result = mem.record_shipped("Part two", commit_sha="cafe123", run_id="run9", sub_feature_of="10")

    assert sub_issue_calls == {"parent_issue_number": 10, "title": "Part two", "labels": ["enhancement"]}
    # Lands on the PARENT's board (feature_issue_number=10, the parent's own
    # title), as an additional item (issue_number=55), not a board of its own.
    assert project_calls == [(10, "Big feature", 55, "Done")]
    assert result == 55


def test_record_shipped_with_sub_feature_of_falls_back_to_the_feature_name_when_parent_lookup_fails(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)

    monkeypatch.setattr(github_issues, "create_sub_issue", lambda *a, **k: {"number": 55, "title": "Part two"})
    monkeypatch.setattr(github_issues, "close_issue", lambda *a, **k: None)
    monkeypatch.setattr(github_issues, "get_issue", lambda repo_url, issue_number: None)

    project_calls = []
    monkeypatch.setattr(
        github_projects,
        "add_item_to_feature_project",
        lambda repo_url, feature_issue_number, title, issue_number=None, status="Todo": project_calls.append(
            (feature_issue_number, title, issue_number, status)
        ),
    )

    mem.record_shipped("Part two", sub_feature_of="10")

    assert project_calls == [(10, "Part two", 55, "Done")]


def test_record_shipped_is_a_noop_without_a_github_remote(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo", remote=None)
    mem = Memory(repo)

    called = []
    monkeypatch.setattr(github_issues, "create_issue", lambda *a, **k: called.append("create"))

    mem.record_shipped("Dark mode")

    assert called == []

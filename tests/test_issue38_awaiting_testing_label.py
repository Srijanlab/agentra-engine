"""GitHub issue #38: the pipeline stage label status:shipped was renamed to
status:awaiting-testing. Reads must recognise both; writes emit the new one; an
idempotent migration moves open issues off the old label."""

import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from agentra import registry, server
from agentra.connectors import github_fake, github_issues
from agentra.memory import Memory


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _init_repo(path: Path, remote: str = "https://github.com/acme/app.git") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    (path / "README.md").write_text("hello\n")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "initial commit")
    _git(path, "remote", "add", "origin", remote)
    return path


def test_read_time_recognises_both_the_old_and_new_label(tmp_path, monkeypatch):
    mem = Memory(_init_repo(tmp_path / "repo"))
    monkeypatch.setattr(
        github_issues,
        "list_open_issues",
        lambda repo_url, labels=None: [
            {"number": 1, "title": "Legacy", "body": "", "labels": ["feature", "agentra", "status:shipped"]},
            {"number": 2, "title": "New", "body": "", "labels": ["feature", "agentra", "status:awaiting-testing"]},
        ],
    )
    monkeypatch.setattr(github_issues, "list_closed_issues", lambda repo_url, labels=None, limit=30: [])

    assert {f["external_id"] for f in mem.shipped_features()} == {"1", "2"}
    assert {i["external_id"] for i in mem.shipped_pending_test_items()} == {"1", "2"}


def test_issue_status_maps_both_labels_to_the_awaiting_testing_stage(tmp_path, monkeypatch):
    mem = Memory(_init_repo(tmp_path / "repo"))
    issues = {
        7: {"number": 7, "state": "open", "labels": [{"name": "status:shipped"}]},
        8: {"number": 8, "state": "open", "labels": [{"name": "status:awaiting-testing"}]},
    }
    monkeypatch.setattr(github_issues, "get_issue", lambda repo_url, n: issues.get(n))

    assert mem.issue_status("7") == "awaiting-testing"
    assert mem.issue_status("8") == "awaiting-testing"


def test_transition_into_the_stage_writes_the_new_label_and_strips_the_old(tmp_path, monkeypatch):
    github_fake.install(monkeypatch=monkeypatch)
    repo = _init_repo(tmp_path / "repo")
    repo_url = "https://github.com/acme/app.git"
    issue = github_issues.create_issue(repo_url, "A feature", "body", labels=["feature", "agentra", "status:shipped"])
    github_issues.mark_shipped_to_preprod(repo_url, issue["number"])

    labels = set(github_issues.get_issue(repo_url, issue["number"])["labels"])
    assert "status:awaiting-testing" in labels
    assert "status:shipped" not in labels


def test_migration_is_idempotent_and_noop_when_nothing_to_migrate(tmp_path, monkeypatch):
    github_fake.install(monkeypatch=monkeypatch)
    repo_url = "https://github.com/acme/app.git"
    _init_repo(tmp_path / "repo")
    legacy = github_issues.create_issue(repo_url, "Legacy", "body", labels=["feature", "agentra", "status:shipped"])
    github_issues.create_issue(repo_url, "Untouched", "body", labels=["feature", "agentra"])

    assert github_issues.migrate_awaiting_testing_label(repo_url) == 1
    assert github_issues.migrate_awaiting_testing_label(repo_url) == 0  # idempotent

    labels = set(github_issues.get_issue(repo_url, legacy["number"])["labels"])
    assert labels == {"feature", "agentra", "status:awaiting-testing"}


def test_pipeline_stages_endpoint_exposes_awaiting_testing_between_code_complete_and_production(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "_ddb", None)
    client = TestClient(server.app)
    stages = client.get("/pipeline/stages").json()["stages"]

    keys = [s["key"] for s in stages]
    assert keys.index("code_complete") < keys.index("awaiting-testing") < keys.index("done")

    at = next(s for s in stages if s["key"] == "awaiting-testing")
    assert at["display"] == "Awaiting Testing"
    assert at["label"] == "status:awaiting-testing"
    assert at["legacy_labels"] == ["status:shipped"]

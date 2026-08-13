"""Regression tests for Memory.get_objective()/set_objective(): a single
GitHub Actions Variable (AGENTRA_OBJECTIVE) is the ONLY store -- no local
objective.yaml at all, a deliberate availability tradeoff (see memory.py's
module comment). A repo with no github.com remote, or an unreachable
GitHub API, simply has no objective -- get returns None, set is a no-op
(logged as an error, since there's nowhere else for it to go).
"""

import subprocess
from pathlib import Path

from agentra.connectors import github_variables
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


def test_get_objective_reads_from_github(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    monkeypatch.setattr(github_variables, "list_variables", lambda repo_url: {"AGENTRA_OBJECTIVE": "Ship the dashboard"})

    assert Memory(repo).get_objective() == "Ship the dashboard"


def test_get_objective_returns_none_without_a_github_remote(tmp_path):
    repo = _init_repo(tmp_path / "repo", remote=None)

    assert Memory(repo).get_objective() is None


def test_get_objective_returns_none_when_github_unreachable(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")

    def _raise(repo_url):
        raise github_variables.GitHubVariablesError("boom")

    monkeypatch.setattr(github_variables, "list_variables", _raise)

    assert Memory(repo).get_objective() is None


def test_set_objective_pushes_to_github(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    pushed = {}

    monkeypatch.setattr(github_variables, "set_variable", lambda repo_url, name, value: pushed.update({name: value}))

    Memory(repo).set_objective("Ship the new dashboard.")

    assert pushed == {"AGENTRA_OBJECTIVE": "Ship the new dashboard."}


def test_set_objective_is_a_noop_without_a_github_remote(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo", remote=None)

    def fail_set(*a, **k):
        raise AssertionError("should not attempt a GitHub call with no remote")

    monkeypatch.setattr(github_variables, "set_variable", fail_set)

    Memory(repo).set_objective("Ship the new dashboard.")  # must not raise

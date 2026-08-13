"""Regression tests for Memory.get_objective()/set_objective()'s two-way
sync with a single GitHub Actions Variable (AGENTRA_OBJECTIVE) -- same
hybrid pattern as environments.py's config sync and memory.py's
known_bugs/feature_queue GitHub Issues sync. The dashboard's PATCH
/apps/{name} (server.py's _apply_app_config) calls set_objective()
directly, so this is the entire sync surface.
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


def test_get_objective_prefers_github_variable_over_local_yaml(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    monkeypatch.setattr(github_variables, "set_variable", lambda *a, **k: None)
    mem = Memory(repo)
    mem.set_objective("local objective")

    monkeypatch.setattr(github_variables, "list_variables", lambda repo_url: {"AGENTRA_OBJECTIVE": "github objective"})

    assert mem.get_objective() == "github objective"


def test_get_objective_falls_back_to_local_yaml_when_github_unreachable(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    monkeypatch.setattr(github_variables, "set_variable", lambda *a, **k: None)
    mem = Memory(repo)
    mem.set_objective("local objective")

    def _raise(repo_url):
        raise github_variables.GitHubVariablesError("boom")

    monkeypatch.setattr(github_variables, "list_variables", _raise)

    assert mem.get_objective() == "local objective"


def test_set_objective_pushes_to_github(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    pushed = {}

    monkeypatch.setattr(github_variables, "set_variable", lambda repo_url, name, value: pushed.update({name: value}))

    mem = Memory(repo)
    mem.set_objective("Ship the new dashboard.")

    assert pushed == {"AGENTRA_OBJECTIVE": "Ship the new dashboard."}


def test_set_objective_still_writes_local_file_when_github_push_fails(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")

    def _raise(repo_url, name, value):
        raise github_variables.GitHubVariablesError("boom")

    monkeypatch.setattr(github_variables, "set_variable", _raise)

    mem = Memory(repo)
    mem.set_objective("Ship the new dashboard.")

    assert mem.objective_path.exists()


def test_get_objective_without_a_github_remote_uses_local_yaml_only(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo", remote=None)
    mem = Memory(repo)
    mem.set_objective("local-only objective")

    assert mem.get_objective() == "local-only objective"

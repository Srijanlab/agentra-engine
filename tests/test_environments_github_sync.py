"""Regression tests for environments.py's GitHub-only config storage:
load()/save() read/write GitHub Actions Variables exclusively (one per
environments.yaml field) -- there is no local file at all, a deliberate
availability tradeoff (see environments.py's module-level comment). A
repo with no github.com remote, or an unreachable GitHub API, simply has
no environment config -- load() returns None, save() is a no-op (logged
as an error, since there's nowhere else for it to go).

Real local git repos on disk (git init + `git remote add origin ...`),
same pattern as test_memory_github_backlog.py. github_variables' actual
HTTP calls are monkeypatched -- no real GitHub API traffic.
"""

import subprocess
from pathlib import Path

from agentra import environments
from agentra.connectors import github_variables


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


def test_load_reads_config_from_github_variables(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    monkeypatch.setattr(
        github_variables,
        "list_variables",
        lambda repo_url: {"AGENTRA_SCHEDULE_HOURS": "6.0", "AGENTRA_VERCEL": "true", "AGENTRA_PRE_PROD_BRANCH": "beta"},
    )

    config = environments.load(repo)

    assert config.schedule_hours == 6.0
    assert config.vercel is True
    assert config.pre_prod_branch == "beta"
    # Fields with no matching variable fall back to the dataclass default.
    assert config.prod_branch == "main"


def test_load_reads_self_hosted_vm_flag(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    monkeypatch.setattr(github_variables, "list_variables", lambda repo_url: {"AGENTRA_SELF_HOSTED_VM": "true"})

    config = environments.load(repo)

    assert config.self_hosted_vm is True


def test_save_pushes_self_hosted_vm_flag(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    pushed = {}
    monkeypatch.setattr(github_variables, "set_variable", lambda repo_url, name, value: pushed.update({name: value}))

    environments.save(repo, environments.EnvironmentConfig(self_hosted_vm=True))

    assert pushed["AGENTRA_SELF_HOSTED_VM"] == "true"


def test_load_returns_none_when_github_call_fails(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")

    def _raise(repo_url):
        raise github_variables.GitHubVariablesError("boom")

    monkeypatch.setattr(github_variables, "list_variables", _raise)

    assert environments.load(repo) is None


def test_load_returns_none_without_a_github_remote(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo", remote=None)

    assert environments.load(repo) is None


def test_load_returns_none_when_no_variables_are_set(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    monkeypatch.setattr(github_variables, "list_variables", lambda repo_url: {})

    assert environments.load(repo) is None


def test_save_pushes_every_field_to_github(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    pushed = {}

    monkeypatch.setattr(github_variables, "set_variable", lambda repo_url, name, value: pushed.update({name: value}))

    environments.save(repo, environments.EnvironmentConfig(schedule_hours=12.0, vercel=True, pre_prod_branch="beta"))

    assert pushed["AGENTRA_SCHEDULE_HOURS"] == "12.0"
    assert pushed["AGENTRA_VERCEL"] == "true"
    assert pushed["AGENTRA_PRE_PROD_BRANCH"] == "beta"
    assert len(pushed) == len(environments._GITHUB_VARIABLE_NAMES)


def test_save_is_a_noop_without_a_github_remote(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo", remote=None)

    def fail_set(*a, **k):
        raise AssertionError("should not attempt a GitHub call with no remote")

    monkeypatch.setattr(github_variables, "set_variable", fail_set)

    environments.save(repo, environments.EnvironmentConfig(schedule_hours=48.0))  # must not raise

    assert not (repo / ".agentra" / "environments.yaml").exists()

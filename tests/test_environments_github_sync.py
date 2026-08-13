"""Regression tests for environments.py's two-way GitHub Actions Variables
sync: load() reads GitHub values back over the local YAML mirror when a
github.com remote exists, and save() pushes every field to GitHub in
addition to writing the local file -- the dashboard's PATCH /apps/{name}
(server.py's _apply_app_config) calls these same functions directly, so
this is the entire sync surface; nothing else needed changing.

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


def test_load_overlays_github_variables_over_local_yaml(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    environments.save(_unwired(repo, monkeypatch), environments.EnvironmentConfig(schedule_hours=24.0, vercel=False))

    monkeypatch.setattr(
        github_variables,
        "list_variables",
        lambda repo_url: {"AGENTRA_SCHEDULE_HOURS": "6.0", "AGENTRA_VERCEL": "true"},
    )

    config = environments.load(repo)

    assert config.schedule_hours == 6.0
    assert config.vercel is True
    # Fields with no matching GitHub variable still come from the local file.
    assert config.pre_prod_branch == "beta"


def test_load_falls_back_to_local_yaml_when_github_unreachable(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    environments.save(_unwired(repo, monkeypatch), environments.EnvironmentConfig(schedule_hours=24.0))

    def _raise(repo_url):
        raise github_variables.GitHubVariablesError("boom")

    monkeypatch.setattr(github_variables, "list_variables", _raise)

    config = environments.load(repo)

    assert config.schedule_hours == 24.0


def test_load_returns_a_config_from_github_alone_with_no_local_file(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")

    monkeypatch.setattr(github_variables, "list_variables", lambda repo_url: {"AGENTRA_SCHEDULE_HOURS": "3.0"})

    config = environments.load(repo)

    assert config is not None
    assert config.schedule_hours == 3.0


def test_load_returns_none_when_nothing_configured_anywhere(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo", remote=None)

    config = environments.load(repo)

    assert config is None


def test_save_pushes_every_field_to_github(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    pushed = {}

    def fake_set(repo_url, name, value):
        pushed[name] = value

    monkeypatch.setattr(github_variables, "set_variable", fake_set)

    environments.save(repo, environments.EnvironmentConfig(schedule_hours=12.0, vercel=True, pre_prod_branch="beta"))

    assert pushed["AGENTRA_SCHEDULE_HOURS"] == "12.0"
    assert pushed["AGENTRA_VERCEL"] == "true"
    assert pushed["AGENTRA_PRE_PROD_BRANCH"] == "beta"
    assert len(pushed) == len(environments._GITHUB_VARIABLE_NAMES)


def test_save_still_writes_local_file_when_github_push_fails(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")

    def _raise(repo_url, name, value):
        raise github_variables.GitHubVariablesError("boom")

    monkeypatch.setattr(github_variables, "set_variable", _raise)

    path = environments.save(repo, environments.EnvironmentConfig(schedule_hours=48.0))

    assert path.exists()
    assert environments.load(repo).schedule_hours == 48.0


def _unwired(repo: Path, monkeypatch) -> Path:
    """Helper for tests that need to seed the local YAML file without
    triggering a real GitHub push as a side effect of that seed step."""
    monkeypatch.setattr(github_variables, "set_variable", lambda *a, **k: None)
    return repo

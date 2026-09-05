"""Phase 1 of multi-repo app support: the legacy-shape shim and the new
get_app_repos()/get_coordination_repo() resolution, behind real local git repos
(same technique as test_registry_sync.py) rather than mocks.
"""

import subprocess
from pathlib import Path

import pytest

import agentra.registry as registry


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _init_origin(path: Path, filename: str = "README.md") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    (path / filename).write_text(f"{path.name}\n")
    _git(path, "add", filename)
    _git(path, "commit", "-m", "initial")
    return path


@pytest.fixture
def registry_env(tmp_path, monkeypatch):
    home = tmp_path / "agentra_home"
    monkeypatch.setattr(registry, "AGENTRA_HOME", home)
    monkeypatch.setattr(registry, "APPS_PATH", home / "apps.json")
    monkeypatch.setattr(registry, "INBOX_ROOT", home / "inbox")
    monkeypatch.setattr(registry, "REPOS_ROOT", tmp_path / "repos")
    monkeypatch.setattr(registry, "_db", None)
    yield registry


def test_legacy_app_is_one_repo_that_is_both_coordination_and_code(tmp_path, registry_env):
    origin = _init_origin(tmp_path / "origin")
    registry_env.register_app("myapp", str(tmp_path / "nonexistent"), repo_url=str(origin), branch="main")

    repos = registry_env.get_app_repos("myapp")

    assert set(repos) == {"myapp"}
    spec = repos["myapp"]
    assert spec.role == "coordination"
    assert spec.repo_url == str(origin)
    assert spec.path == registry_env.REPOS_ROOT / "myapp"
    assert spec.path.is_dir()


def test_legacy_get_app_repo_path_is_unchanged(tmp_path, registry_env):
    origin = _init_origin(tmp_path / "origin")
    registry_env.register_app("myapp", str(tmp_path / "nonexistent"), repo_url=str(origin), branch="main")

    assert registry_env.get_app_repo("myapp") == registry_env.REPOS_ROOT / "myapp"


def test_multi_repo_app_resolves_every_repo_under_reposroot_name(tmp_path, registry_env):
    coord_origin = _init_origin(tmp_path / "coord-origin")
    engine_origin = _init_origin(tmp_path / "engine-origin")
    ui_origin = _init_origin(tmp_path / "ui-origin")

    registry_env.register_app(
        "agentra",
        repos=[
            {"name": "backlog", "repo_url": str(coord_origin), "branch": "main", "role": "coordination"},
            {"name": "engine", "repo_url": str(engine_origin), "branch": "main", "role": "code",
             "deploy_strategy": "vercel_firebase"},
            {"name": "ui", "repo_url": str(ui_origin), "branch": "main", "role": "code"},
        ],
    )

    repos = registry_env.get_app_repos("agentra")

    assert set(repos) == {"backlog", "engine", "ui"}
    for repo_name in ("backlog", "engine", "ui"):
        spec = repos[repo_name]
        assert spec.path == registry_env.REPOS_ROOT / "agentra" / repo_name
        assert spec.path.is_dir()
    assert repos["backlog"].role == "coordination"
    assert repos["engine"].role == "code"
    assert repos["engine"].deploy_strategy == "vercel_firebase"


def test_get_coordination_repo_picks_the_coordination_entry(tmp_path, registry_env):
    coord_origin = _init_origin(tmp_path / "coord-origin")
    code_origin = _init_origin(tmp_path / "code-origin")
    registry_env.register_app(
        "agentra",
        repos=[
            {"name": "backlog", "repo_url": str(coord_origin), "branch": "main", "role": "coordination"},
            {"name": "engine", "repo_url": str(code_origin), "branch": "main", "role": "code"},
        ],
    )

    spec = registry_env.get_coordination_repo("agentra")

    assert spec is not None
    assert spec.name == "backlog"
    assert spec.path == registry_env.REPOS_ROOT / "agentra" / "backlog"


def test_get_app_repo_returns_the_coordination_repo_for_multi_repo_apps(tmp_path, registry_env):
    coord_origin = _init_origin(tmp_path / "coord-origin")
    code_origin = _init_origin(tmp_path / "code-origin")
    registry_env.register_app(
        "agentra",
        repos=[
            {"name": "backlog", "repo_url": str(coord_origin), "branch": "main", "role": "coordination"},
            {"name": "engine", "repo_url": str(code_origin), "branch": "main", "role": "code"},
        ],
    )

    assert registry_env.get_app_repo("agentra") == registry_env.REPOS_ROOT / "agentra" / "backlog"


def test_get_app_repos_unknown_app_returns_empty(registry_env):
    assert registry_env.get_app_repos("nope") == {}
    assert registry_env.get_coordination_repo("nope") is None


def test_repo_url_for_path_matches_any_repo_in_a_multi_repo_app(tmp_path, registry_env):
    coord_origin = _init_origin(tmp_path / "coord-origin")
    code_origin = _init_origin(tmp_path / "code-origin")
    registry_env.register_app(
        "agentra",
        repos=[
            {"name": "backlog", "repo_url": str(coord_origin), "branch": "main", "role": "coordination"},
            {"name": "engine", "repo_url": str(code_origin), "branch": "main", "role": "code"},
        ],
    )
    repos = registry_env.get_app_repos("agentra")

    assert registry_env.repo_url_for_path(repos["engine"].path) == str(code_origin)
    assert registry_env.repo_url_for_path(repos["backlog"].path) == str(coord_origin)


def test_get_code_repos_returns_the_one_legacy_repo(tmp_path, registry_env):
    origin = _init_origin(tmp_path / "origin")
    registry_env.register_app("myapp", str(tmp_path / "nonexistent"), repo_url=str(origin), branch="main")

    repos = registry_env.get_code_repos("myapp")

    assert set(repos) == {"myapp"}
    assert repos["myapp"].path == registry_env.REPOS_ROOT / "myapp"


def test_get_code_repos_excludes_the_coordination_repo_for_multi_repo_apps(tmp_path, registry_env):
    coord_origin = _init_origin(tmp_path / "coord-origin")
    engine_origin = _init_origin(tmp_path / "engine-origin")
    ui_origin = _init_origin(tmp_path / "ui-origin")
    registry_env.register_app(
        "agentra",
        repos=[
            {"name": "backlog", "repo_url": str(coord_origin), "branch": "main", "role": "coordination"},
            {"name": "engine", "repo_url": str(engine_origin), "branch": "main", "role": "code"},
            {"name": "ui", "repo_url": str(ui_origin), "branch": "main", "role": "code"},
        ],
    )

    repos = registry_env.get_code_repos("agentra")

    assert set(repos) == {"engine", "ui"}


def test_get_code_repos_unknown_app_returns_empty(registry_env):
    assert registry_env.get_code_repos("nope") == {}


def test_cloud_mode_returns_unresolved_paths_without_touching_disk(tmp_path, registry_env, monkeypatch):
    monkeypatch.setattr(registry, "_db", object())
    monkeypatch.setattr(registry, "list_apps", lambda: {
        "agentra": {
            "repos": [
                {"name": "backlog", "repo_url": "https://github.com/acme/backlog.git",
                 "branch": "main", "role": "coordination"},
                {"name": "engine", "repo_url": "https://github.com/acme/engine.git",
                 "branch": "main", "role": "code"},
            ]
        }
    })

    repos = registry_env.get_app_repos("agentra")

    assert repos["backlog"].path == registry_env.REPOS_ROOT / "agentra" / "backlog"
    assert not repos["backlog"].path.exists()
    assert repos["engine"].path == registry_env.REPOS_ROOT / "agentra" / "engine"

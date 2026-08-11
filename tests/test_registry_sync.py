"""Fast, deterministic, no-API-call pytest module for registry.py's
get_app_repo() staleness resync (the "an objective.yaml edit made via the
dashboard has no effect until something else forces a resync" gap).

Uses real local git repos on disk (a plain, non-bare repo standing in for
"the remote", and a clone of it standing in for "the existing local
checkout agentra already has") rather than mocking git -- the whole point
of this change is real ls-remote/fetch/reset behavior, and subprocess-based
git against a local path is just as fast and deterministic as a mock here.

No pytest dependency issue: this repo already has pytest as a dev/test
dependency (see tests/test_safety_hook.py). Run with:
    pytest tests/test_registry_sync.py
"""

import subprocess
from pathlib import Path

import pytest

import agentra.registry as registry


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True,
    )


def _head_sha(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _init_origin(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    (path / "objective.yaml").write_text("objective: original\n")
    _git(path, "add", "objective.yaml")
    _git(path, "commit", "-m", "initial")
    return path


@pytest.fixture
def registry_env(tmp_path, monkeypatch):
    """Point the whole registry module at a scratch AGENTRA_HOME so tests
    never touch a real ~/.agentra, and reload it fresh afterwards so other
    test modules importing agentra.registry don't inherit these patches."""
    home = tmp_path / "agentra_home"
    monkeypatch.setattr(registry, "AGENTRA_HOME", home)
    monkeypatch.setattr(registry, "APPS_PATH", home / "apps.json")
    monkeypatch.setattr(registry, "INBOX_ROOT", home / "inbox")
    monkeypatch.setattr(registry, "_db", None)
    yield registry


def test_stale_checkout_is_resynced_to_remote_head(tmp_path, registry_env):
    origin = _init_origin(tmp_path / "origin")
    local = tmp_path / "local_checkout"
    subprocess.run(["git", "clone", str(origin), str(local)], check=True, capture_output=True, text=True)
    stale_sha = _head_sha(local)

    # Simulate an externally-made change landing on the remote (e.g. a
    # dashboard objective.yaml edit, or a manual fix pushed to the branch)
    # that never touches this on-disk checkout on its own.
    (origin / "objective.yaml").write_text("objective: updated via dashboard\n")
    _git(origin, "add", "objective.yaml")
    _git(origin, "commit", "-m", "dashboard edit")
    fresh_sha = _head_sha(origin)
    assert fresh_sha != stale_sha

    registry_env.register_app("myapp", str(local), repo_url=str(origin), branch="main")

    repo = registry_env.get_app_repo("myapp")

    assert repo == local
    assert _head_sha(local) == fresh_sha
    assert (local / "objective.yaml").read_text() == "objective: updated via dashboard\n"


def test_up_to_date_checkout_is_left_alone(tmp_path, registry_env):
    origin = _init_origin(tmp_path / "origin")
    local = tmp_path / "local_checkout"
    subprocess.run(["git", "clone", str(origin), str(local)], check=True, capture_output=True, text=True)
    sha = _head_sha(local)

    registry_env.register_app("myapp", str(local), repo_url=str(origin), branch="main")
    repo = registry_env.get_app_repo("myapp")

    assert repo == local
    assert _head_sha(local) == sha


def test_unreachable_remote_falls_back_to_existing_checkout(tmp_path, registry_env):
    origin = _init_origin(tmp_path / "origin")
    local = tmp_path / "local_checkout"
    subprocess.run(["git", "clone", str(origin), str(local)], check=True, capture_output=True, text=True)
    sha = _head_sha(local)

    # repo_url points at nothing -- the remote is simply unreachable.
    bogus_url = str(tmp_path / "does_not_exist")
    registry_env.register_app("myapp", str(local), repo_url=bogus_url, branch="main")

    repo = registry_env.get_app_repo("myapp")

    # Must not raise, and must hand back the existing, untouched checkout.
    assert repo == local
    assert _head_sha(local) == sha


def test_existing_non_git_directory_with_repo_url_is_left_alone(tmp_path, registry_env):
    repo = tmp_path / "fixture_repo"
    repo.mkdir()
    (repo / "objective.yaml").write_text("objective: fixture\n")

    registry_env.register_app(
        "myapp",
        str(repo),
        repo_url="https://github.com/acme/myapp.git",
        branch="main",
    )

    assert registry_env.get_app_repo("myapp") == repo
    assert (repo / "objective.yaml").read_text() == "objective: fixture\n"


def test_clone_from_scratch_path_is_unaffected(tmp_path, registry_env):
    """Registering an app whose repo_path doesn't exist yet still clones,
    same as before this change -- the staleness check only applies to an
    EXISTING checkout."""
    origin = _init_origin(tmp_path / "origin")
    dest = tmp_path / "not_cloned_yet"
    assert not dest.exists()

    registry_env.register_app("myapp", str(dest), repo_url=str(origin), branch="main")
    repo = registry_env.get_app_repo("myapp")

    assert repo == dest
    assert dest.exists()
    assert _head_sha(dest) == _head_sha(origin)

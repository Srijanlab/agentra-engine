"""Fast, deterministic, no-network pytest module for agents/git_ops.py --
the only code in this repo that actually shells out to `git push`/`git
pull`/`git fetch` against a remote.

Real local git repos stand in for "origin" (a bare repo) and "the working
checkout" -- same convention as tests/test_registry_sync.py: subprocess git
against a local path exercises the exact same code paths (fetch/push
rejections, non-fast-forward, missing refs) as a real GitHub remote would,
without any network access or risk of touching a real remote.

_extra_auth_args' GitHub-App-token-then-static-token fallback is tested
separately below by monkeypatching agentra.connectors.github_app's
get_installation_token -- never a real GitHub API call.

Run with:
    pytest tests/test_git_ops.py
"""

import base64
import subprocess
from pathlib import Path

import pytest

from agentra.agents import git_ops
from agentra.connectors import github_app


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True,
    )


def _head_sha(repo: Path, ref: str = "HEAD") -> str:
    return _git(repo, "rev-parse", ref).stdout.strip()


def _current_branch(repo: Path) -> str:
    return _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def _init_bare_origin(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(path)],
        check=True, capture_output=True, text=True,
    )
    return path


def _clone(origin: Path, dest: Path) -> Path:
    subprocess.run(["git", "clone", str(origin), str(dest)], check=True, capture_output=True, text=True)
    _git(dest, "config", "user.email", "test@example.com")
    _git(dest, "config", "user.name", "Test")
    return dest


def _commit_file(repo: Path, name: str, content: str, message: str) -> str:
    (repo / name).write_text(content)
    _git(repo, "add", name)
    _git(repo, "commit", "-m", message)
    return _head_sha(repo)


def _seed_origin(tmp_path: Path) -> Path:
    """A bare origin with one commit on `main`, built via a throwaway seeding
    clone (you can't commit directly into a bare repo)."""
    origin = _init_bare_origin(tmp_path / "origin.git")
    seed = _clone(origin, tmp_path / "_seed")
    _commit_file(seed, "README.md", "hello\n", "initial")
    _git(seed, "push", "origin", "main")
    return origin


@pytest.fixture(autouse=True)
def _no_github_app(monkeypatch):
    """Every git_ops network call funnels through _extra_auth_args ->
    github_app.get_installation_token. Force the "App not configured"
    fallback for every test in this module (except the ones in
    TestExtraAuthArgs that specifically exercise the other branches), so
    these tests never make a real GitHub API call regardless of what's in
    the ambient environment (e.g. GITHUB_APP_ID happening to be set in
    CI/prod)."""

    def _not_configured(repo_url):
        raise github_app.GitHubAppNotConfigured("not configured in tests")

    monkeypatch.setattr(github_app, "get_installation_token", _not_configured)


# -- push_branch --------------------------------------------------------------


def test_push_branch_pushes_local_commit_to_origin(tmp_path):
    origin = _seed_origin(tmp_path)
    work = _clone(origin, tmp_path / "work")
    _git(work, "checkout", "-b", "feature")
    sha = _commit_file(work, "feature.txt", "x\n", "feature work")

    git_ops.push_branch(work, "feature")

    assert _git(origin, "rev-parse", "feature").stdout.strip() == sha


def test_push_branch_raises_giterror_on_non_fast_forward_rejection(tmp_path):
    origin = _seed_origin(tmp_path)
    work = _clone(origin, tmp_path / "work")
    other = _clone(origin, tmp_path / "other")

    # Someone else pushes to main first, so `work`'s main is now stale.
    _commit_file(other, "other.txt", "o\n", "other change")
    _git(other, "push", "origin", "main")

    _commit_file(work, "work.txt", "w\n", "conflicting local change")

    with pytest.raises(git_ops.GitOpError):
        git_ops.push_branch(work, "main")


# -- fetch_ref ------------------------------------------------------------------


def test_fetch_ref_populates_remote_tracking_ref_without_switching_branch(tmp_path):
    origin = _seed_origin(tmp_path)
    seed2 = _clone(origin, tmp_path / "_seed2")
    _git(seed2, "checkout", "-b", "other-branch")
    _commit_file(seed2, "other.txt", "o\n", "other branch commit")
    _git(seed2, "push", "origin", "other-branch")

    work = _clone(origin, tmp_path / "work")  # only knows about `main` so far
    assert _current_branch(work) == "main"

    git_ops.fetch_ref(work, "other-branch")

    # still on main -- fetch_ref must not touch the working tree/checkout
    assert _current_branch(work) == "main"
    assert not (work / "other.txt").exists()
    fetched_sha = _git(work, "rev-parse", "refs/remotes/origin/other-branch").stdout.strip()
    assert fetched_sha == _git(origin, "rev-parse", "other-branch").stdout.strip()


def test_fetch_ref_raises_giterror_for_nonexistent_branch(tmp_path):
    origin = _seed_origin(tmp_path)
    work = _clone(origin, tmp_path / "work")

    with pytest.raises(git_ops.GitOpError):
        git_ops.fetch_ref(work, "does-not-exist-anywhere")


# -- pull_latest ------------------------------------------------------------------


def test_pull_latest_hard_resets_existing_local_branch_to_origin_tip(tmp_path):
    origin = _seed_origin(tmp_path)
    work = _clone(origin, tmp_path / "work")

    other = _clone(origin, tmp_path / "other")
    new_sha = _commit_file(other, "new.txt", "n\n", "upstream change")
    _git(other, "push", "origin", "main")

    # `work` has a local-only commit that pull_latest's hard reset must blow away.
    _commit_file(work, "stray.txt", "s\n", "stray local commit")

    git_ops.pull_latest(work, "main")

    assert _head_sha(work) == new_sha
    assert not (work / "stray.txt").exists()
    assert (work / "new.txt").exists()


def test_pull_latest_creates_and_checks_out_branch_not_yet_local(tmp_path):
    origin = _seed_origin(tmp_path)
    seed2 = _clone(origin, tmp_path / "_seed2")
    _git(seed2, "checkout", "-b", "beta")
    beta_sha = _commit_file(seed2, "beta.txt", "b\n", "beta commit")
    _git(seed2, "push", "origin", "beta")

    work = _clone(origin, tmp_path / "work")  # local clone only ever saw `main`

    git_ops.pull_latest(work, "beta")

    assert _current_branch(work) == "beta"
    assert _head_sha(work) == beta_sha
    assert (work / "beta.txt").exists()


def test_pull_latest_raises_giterror_when_origin_unreachable(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "test@example.com")
    _git(work, "config", "user.name", "Test")
    _commit_file(work, "a.txt", "a\n", "initial")
    _git(work, "remote", "add", "origin", str(tmp_path / "does-not-exist"))

    with pytest.raises(git_ops.GitOpError):
        git_ops.pull_latest(work, "main")


# -- commit_and_push --------------------------------------------------------------


def test_commit_and_push_commits_and_pushes_dirty_paths(tmp_path):
    origin = _seed_origin(tmp_path)
    work = _clone(origin, tmp_path / "work")
    (work / "README.md").write_text("changed\n")

    changed = git_ops.commit_and_push(work, "main", "test commit message", ["README.md"])

    assert changed is True
    assert _git(origin, "log", "-1", "--pretty=%s", "main").stdout.strip() == "test commit message"
    assert _git(origin, "rev-parse", "main").stdout.strip() == _head_sha(work)


def test_commit_and_push_is_noop_when_nothing_dirty_under_paths(tmp_path):
    origin = _seed_origin(tmp_path)
    work = _clone(origin, tmp_path / "work")
    before_sha = _head_sha(work)

    changed = git_ops.commit_and_push(work, "main", "should never be committed")

    assert changed is False
    assert _head_sha(work) == before_sha
    assert _git(origin, "rev-parse", "main").stdout.strip() == before_sha


def test_commit_and_push_only_considers_dirty_state_under_given_paths(tmp_path):
    origin = _seed_origin(tmp_path)
    work = _clone(origin, tmp_path / "work")
    # Dirty a file that's NOT in `paths` -- must not be treated as something to commit.
    (work / "untracked_elsewhere.txt").write_text("noise\n")

    changed = git_ops.commit_and_push(work, "main", "should still be a no-op", ["README.md"])

    assert changed is False


def test_commit_and_push_raises_giterror_when_push_is_rejected(tmp_path):
    origin = _seed_origin(tmp_path)
    work = _clone(origin, tmp_path / "work")
    other = _clone(origin, tmp_path / "other")
    _commit_file(other, "o.txt", "o\n", "other change")
    _git(other, "push", "origin", "main")

    (work / "README.md").write_text("dirty\n")

    with pytest.raises(git_ops.GitOpError):
        git_ops.commit_and_push(work, "main", "will fail to push", ["README.md"])


# -- _extra_auth_args: GitHub-App-token-then-static-token fallback ----------------


class TestExtraAuthArgs:
    def test_empty_when_repo_url_is_none(self):
        assert git_ops._extra_auth_args(None) == []

    def test_empty_when_app_not_configured(self, monkeypatch):
        def _not_configured(repo_url):
            raise github_app.GitHubAppNotConfigured("no app credentials")

        monkeypatch.setattr(github_app, "get_installation_token", _not_configured)

        assert git_ops._extra_auth_args("https://github.com/acme/repo.git") == []

    def test_empty_when_app_configured_but_not_installed_on_repo(self, monkeypatch):
        def _not_installed(repo_url):
            raise github_app.GitHubAppError("not installed on this repo")

        monkeypatch.setattr(github_app, "get_installation_token", _not_installed)

        assert git_ops._extra_auth_args("https://github.com/acme/repo.git") == []

    def test_injects_basic_auth_header_from_installation_token(self, monkeypatch):
        monkeypatch.setattr(github_app, "get_installation_token", lambda repo_url: "fake-installation-token")

        result = git_ops._extra_auth_args("https://github.com/acme/repo.git")

        expected_basic = base64.b64encode(b"x-access-token:fake-installation-token").decode()
        assert result == ["-c", f"http.extraheader=AUTHORIZATION: basic {expected_basic}"]


def test_push_branch_wires_installation_token_through_to_the_real_git_call(tmp_path, monkeypatch):
    """Not just that _extra_auth_args is correct in isolation, but that
    push_branch actually calls it with the real resolved origin URL and
    passes its output through to `git push` -- a local file:// remote just
    ignores an unused http.extraheader config override, so this proves the
    wiring without needing a real HTTPS remote."""
    origin = _seed_origin(tmp_path)
    work = _clone(origin, tmp_path / "work")
    seen_urls = []

    def _fake_token(repo_url):
        seen_urls.append(repo_url)
        return "fake-installation-token"

    monkeypatch.setattr(github_app, "get_installation_token", _fake_token)
    sha = _commit_file(work, "x.txt", "x\n", "commit for auth wiring test")

    git_ops.push_branch(work, "main")

    assert seen_urls == [str(origin)]
    assert _git(origin, "rev-parse", "main").stdout.strip() == sha

"""Regression test for a real, live-confirmed bug: agents/implementation.py's
_checkout_feature_branch used to hand-roll its own unauthenticated `git
fetch` instead of going through git_ops.fetch_ref (the GitHub-App-token
path every other git operation in this codebase uses) -- fine against a
repo the ambient static PAT happened to cover, but 403'd outright the
moment agentra's own repo moved to an org the PAT was never scoped to
(confirmed live: 4 consecutive autonomous-cycle failures, GitHub issues
#7-#10, all "403 Write access to repository not granted" on branch
creation).
"""

import asyncio
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock

from agentra.agents import git_ops, implementation
from agentra.environments import EnvironmentConfig


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _init_repo_with_branch(path: Path, branch: str = "beta") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-b", branch)
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    (path / "README.md").write_text("hello\n")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "initial commit")
    return path


def test_checkout_feature_branch_fetches_via_git_ops_not_a_raw_subprocess_call(tmp_path, monkeypatch):
    """The fix: _checkout_feature_branch must go through git_ops.fetch_ref
    (GitHub App token injected) rather than its own bare `git fetch` (which
    silently relies on whatever ambient static PAT is configured, and has
    no way to pick up per-repo App auth)."""
    repo = _init_repo_with_branch(tmp_path / "repo")
    # A bare-repo "origin" so fetch_ref has something real to fetch from --
    # simpler than mocking subprocess for this one call while letting the
    # real `clean`/`checkout` calls run for real below.
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "beta", str(origin)], check=True, capture_output=True)
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "origin", "beta")

    calls = []
    real_fetch_ref = git_ops.fetch_ref

    def spy_fetch_ref(repo_arg, branch_arg):
        calls.append((repo_arg, branch_arg))
        return real_fetch_ref(repo_arg, branch_arg)

    monkeypatch.setattr(git_ops, "fetch_ref", spy_fetch_ref)
    monkeypatch.setattr(implementation.git_ops, "fetch_ref", spy_fetch_ref)

    implementation._checkout_feature_branch(repo, "feature/test-branch", "beta")

    assert calls == [(repo, "beta")]
    current_branch = _git(repo, "branch", "--show-current").stdout.strip()
    assert current_branch == "feature/test-branch"


def test_run_returns_a_failed_agent_result_on_a_git_op_error_instead_of_raising(tmp_path, monkeypatch):
    """Before the fix, a GitOpError from an authenticated fetch_ref call
    would have propagated straight out of run() uncaught (only
    subprocess.CalledProcessError was handled) -- crashing the whole
    autonomous cycle instead of surfacing a clean, actionable failure the
    Orchestrator can react to."""
    repo = tmp_path / "repo"
    repo.mkdir()

    monkeypatch.setattr(
        implementation.git_ops,
        "fetch_ref",
        lambda repo_arg, branch_arg: (_ for _ in ()).throw(
            git_ops.GitOpError("fetch_ref('beta') failed: 403 Write access to repository not granted")
        ),
    )
    monkeypatch.setattr(implementation, "run_agent", AsyncMock())

    result = asyncio.run(
        implementation.run(
            repo=repo,
            objective="Ship things",
            feature="Some feature",
            codebase_summary="summary",
            env=EnvironmentConfig(pre_prod_branch="beta"),
            feature_branch="feature/test-branch",
        )
    )

    assert result.ok is False
    assert "403 Write access to repository not granted" in result.text
    implementation.run_agent.assert_not_called()

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
from agentra.agents.base import AgentResult
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


# -- resume capability: a durably-pushed feature branch survives losing the local checkout --


def test_checkout_feature_branch_resume_true_continues_the_existing_branch(tmp_path):
    """The whole point of resume: a previous implement_feature call's real,
    pushed commit must be picked back up, not silently discarded by forking
    a fresh branch off pre_prod_branch's tip instead."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "beta", str(origin)], check=True, capture_output=True)

    repo = _init_repo_with_branch(tmp_path / "repo", branch="beta")
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "origin", "beta")

    # Simulate a previous, interrupted implement_feature call: branch off
    # beta, commit real work, push it (exactly what run()'s push_branch call
    # does) -- then simulate this VM's checkout never having seen it locally
    # (e.g. a redeploy re-cloned since then) by deleting the local branch.
    _git(repo, "checkout", "-b", "feature/resume-me")
    (repo / "feature.txt").write_text("work from the interrupted call\n")
    _git(repo, "add", "feature.txt")
    _git(repo, "commit", "-m", "partial work")
    _git(repo, "push", "origin", "feature/resume-me")
    _git(repo, "checkout", "beta")
    _git(repo, "branch", "-D", "feature/resume-me")

    implementation._checkout_feature_branch(repo, "feature/resume-me", "beta", resume=True)

    assert _git(repo, "branch", "--show-current").stdout.strip() == "feature/resume-me"
    assert (repo / "feature.txt").read_text() == "work from the interrupted call\n"


def test_checkout_feature_branch_resume_true_falls_back_when_branch_does_not_exist_remotely(tmp_path):
    """resume=True is best-effort -- a branch that was never pushed (or no
    longer exists) must fall back to the normal fresh-fork-from-pre_prod
    behavior, not raise."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "beta", str(origin)], check=True, capture_output=True)
    repo = _init_repo_with_branch(tmp_path / "repo", branch="beta")
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "origin", "beta")

    implementation._checkout_feature_branch(repo, "feature/never-existed", "beta", resume=True)

    assert _git(repo, "branch", "--show-current").stdout.strip() == "feature/never-existed"
    assert (repo / "README.md").read_text() == "hello\n"  # beta's own content, forked fresh


def test_run_pushes_the_feature_branch_after_a_successful_implementation(tmp_path, monkeypatch):
    """Without this, a feature branch only ever became durable once
    deploy_pre_prod merged it -- which requires local tests to pass first.
    Confirmed live: GitHub issue #13's fix was implemented, then
    permanently lost when tests failed and deploy_pre_prod never ran."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "beta", str(origin)], check=True, capture_output=True)
    repo = _init_repo_with_branch(tmp_path / "repo", branch="beta")
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "origin", "beta")

    async def fake_run_agent(**kwargs):
        (repo / "new_feature.txt").write_text("real work\n")
        _git(repo, "add", "new_feature.txt")
        _git(repo, "commit", "-m", "implement the thing")
        return AgentResult(ok=True, text="done", json_data={"status": "implemented"}, cost_usd=0.01, turns=2)

    monkeypatch.setattr(implementation, "run_agent", fake_run_agent)

    result = asyncio.run(
        implementation.run(
            repo=repo,
            objective="obj",
            feature="a feature",
            codebase_summary="summary",
            env=EnvironmentConfig(pre_prod_branch="beta"),
            feature_branch="feature/push-me",
        )
    )

    assert result.ok is True
    verify = tmp_path / "verify"
    subprocess.run(
        ["git", "clone", "--branch", "feature/push-me", "--single-branch", str(origin), str(verify)],
        check=True, capture_output=True,
    )
    assert (verify / "new_feature.txt").read_text() == "real work\n"


def test_run_still_returns_ok_when_the_push_fails(tmp_path, monkeypatch):
    """A push failure (e.g. a transient network blip) must not turn a
    successful implementation into a reported failure -- the work is still
    committed locally either way, same as before this existed; it just
    won't be resumable if this VM's checkout is lost."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "beta", str(origin)], check=True, capture_output=True)
    repo = _init_repo_with_branch(tmp_path / "repo", branch="beta")
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "origin", "beta")

    async def fake_run_agent(**kwargs):
        (repo / "new_feature.txt").write_text("real work\n")
        _git(repo, "add", "new_feature.txt")
        _git(repo, "commit", "-m", "implement the thing")
        return AgentResult(ok=True, text="done", json_data={"status": "implemented"}, cost_usd=0.01, turns=2)

    monkeypatch.setattr(implementation, "run_agent", fake_run_agent)
    monkeypatch.setattr(
        implementation.git_ops, "push_branch",
        lambda *a, **k: (_ for _ in ()).throw(git_ops.GitOpError("simulated push failure")),
    )

    result = asyncio.run(
        implementation.run(
            repo=repo,
            objective="obj",
            feature="a feature",
            codebase_summary="summary",
            env=EnvironmentConfig(pre_prod_branch="beta"),
            feature_branch="feature/push-fails",
        )
    )

    assert result.ok is True
    assert "Could not push feature branch" in result.text

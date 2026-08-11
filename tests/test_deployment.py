"""Fast, deterministic, no-network pytest module for agents/deployment.py --
the only code path allowed anywhere near a real production branch.

run_agent (the actual Claude Agent SDK call) and every agentra.agents.git_ops
function deployment.py delegates to are monkeypatched out with small
call-recording fakes -- same convention as tests/test_registry_sync.py and
tests/test_server_triggers.py (hand-rolled fakes via monkeypatch, not a
mocking framework). The one exception is `git merge` itself: _merge_and_push
shells out to a real `git merge`/`git merge --abort` directly (not through
git_ops), so those tests use a real local git repo with genuinely
conflicting/non-conflicting branches to prove the actual merge/abort
behavior, not a simulation of it.

Covers the three safety properties called out for this module:
  1. promote_prod is the only function that ever asks run_agent for
     allow_prod=True / ever merges into prod_branch; deploy_pre_prod never
     does either, structurally, regardless of its inputs.
  2. A merge conflict in _merge_and_push aborts cleanly (error result,
     clean working tree, nothing pushed) rather than leaving a dirty tree
     or a half-finished deploy.
  3. deploy_pre_prod never references/touches the configured prod_branch.

Run with:
    pytest tests/test_deployment.py
"""

import asyncio
import subprocess
from pathlib import Path

import pytest

from agentra.agents import deployment, git_ops
from agentra.agents.base import AgentResult
from agentra.environments import EnvironmentConfig

PROD_SENTINEL = "prod-do-not-touch"  # a branch name deploy_pre_prod must never reference


# -- shared git helpers -------------------------------------------------------------


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True,
    )


def _commit_file(repo: Path, name: str, content: str, message: str) -> str:
    (repo / name).write_text(content)
    _git(repo, "add", name)
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    _commit_file(path, "app.txt", "base\n", "initial")
    return path


def _working_tree_is_clean(repo: Path) -> bool:
    return _git(repo, "status", "--porcelain").stdout.strip() == ""


def _merge_in_progress(repo: Path) -> bool:
    return (repo / ".git" / "MERGE_HEAD").exists()


def _current_branch(repo: Path) -> str:
    return _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def _setup_conflicting_branches(repo: Path, base: str, other: str) -> None:
    """Branch `repo`'s current HEAD into `base` and `other`, each editing the
    same line of the same file differently, so merging one into the other
    always conflicts."""
    _commit_file(repo, "shared.txt", "original\n", "add shared file")
    _git(repo, "branch", base)
    _git(repo, "branch", other)
    _git(repo, "checkout", base)
    (repo / "shared.txt").write_text("change from base branch\n")
    _git(repo, "commit", "-am", f"conflicting change on {base}")
    _git(repo, "checkout", other)
    (repo / "shared.txt").write_text("change from other branch\n")
    _git(repo, "commit", "-am", f"conflicting change on {other}")


def _env(**overrides) -> EnvironmentConfig:
    defaults = dict(pre_prod_branch="beta", prod_branch=PROD_SENTINEL, vercel=False, firebase=False)
    defaults.update(overrides)
    return EnvironmentConfig(**defaults)


def _fake_run_agent(calls: list, result: AgentResult):
    async def _run(**kwargs):
        calls.append(kwargs)
        return result

    return _run


# -- _merge_and_push ----------------------------------------------------------------


def test_merge_and_push_success_merges_and_pushes(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    _git(repo, "checkout", "-b", "beta")
    _commit_file(repo, "app.txt", "base\nbeta change\n", "beta commit")
    _git(repo, "checkout", "-b", "feature", "beta")
    _commit_file(repo, "feature.txt", "new feature\n", "feature commit")
    _git(repo, "checkout", "beta")

    push_calls = []
    monkeypatch.setattr(git_ops, "push_branch", lambda r, b: push_calls.append((r, b)))

    error = deployment._merge_and_push(repo, "feature", "beta")

    assert error is None
    assert push_calls == [(repo, "beta")]
    assert (repo / "feature.txt").exists()
    assert _working_tree_is_clean(repo)


def test_merge_and_push_conflict_aborts_cleanly_and_never_pushes(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    _setup_conflicting_branches(repo, base="beta", other="feature")
    _git(repo, "checkout", "beta")

    def _must_not_be_called(r, b):
        raise AssertionError(f"push_branch must not be called after a failed merge, got branch={b!r}")

    monkeypatch.setattr(git_ops, "push_branch", _must_not_be_called)

    error = deployment._merge_and_push(repo, "feature", "beta")

    assert error is not None
    assert "failed" in error.lower()
    # the defining property: no leftover conflict state, no dirty tree, still on the target branch
    assert not _merge_in_progress(repo)
    assert _working_tree_is_clean(repo)
    assert _current_branch(repo) == "beta"


def test_merge_and_push_push_failure_is_reported_without_raising(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    _git(repo, "checkout", "-b", "beta")
    _commit_file(repo, "app.txt", "base\nbeta change\n", "beta commit")
    _git(repo, "checkout", "-b", "feature", "beta")
    _commit_file(repo, "feature.txt", "new feature\n", "feature commit")
    _git(repo, "checkout", "beta")

    def _raise(r, b):
        raise git_ops.GitOpError("push_branch(%r) failed: simulated rejection" % b)

    monkeypatch.setattr(git_ops, "push_branch", _raise)

    error = deployment._merge_and_push(repo, "feature", "beta")

    assert error is not None
    assert "push" in error.lower()
    # the merge itself did succeed locally -- only the push failed
    assert (repo / "feature.txt").exists()


# -- persist_audit_trail --------------------------------------------------------------


def test_persist_audit_trail_delegates_to_git_ops_commit_and_push(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    calls = []

    def _fake_commit_and_push(r, branch, message, paths):
        calls.append((r, branch, message, paths))
        return True

    monkeypatch.setattr(git_ops, "commit_and_push", _fake_commit_and_push)

    error = deployment.persist_audit_trail(repo, "beta")

    assert error is None
    assert len(calls) == 1
    called_repo, called_branch, called_message, called_paths = calls[0]
    assert called_repo == repo
    assert called_branch == "beta"
    assert called_paths == [".agentra/"]
    assert "audit trail" in called_message.lower()


def test_persist_audit_trail_reports_git_op_error(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()

    def _raise(r, branch, message, paths):
        raise git_ops.GitOpError("simulated failure")

    monkeypatch.setattr(git_ops, "commit_and_push", _raise)

    error = deployment.persist_audit_trail(repo, "beta")

    assert error is not None
    assert "beta" in error
    assert "simulated failure" in error


# -- deploy_pre_prod: must never reach prod_branch -----------------------------------


def _setup_pre_prod_repo(tmp_path, feature_branch="dev/1234-feature"):
    repo = _init_repo(tmp_path / "repo")
    _git(repo, "checkout", "-b", "beta")
    _commit_file(repo, "app.txt", "base\nbeta change\n", "beta commit")
    _git(repo, "checkout", "-b", feature_branch, "beta")
    _commit_file(repo, "feature.txt", "feature work\n", "feature commit")
    _git(repo, "checkout", "main")
    return repo


def _guarded_pull_latest(calls, allowed_branch):
    def _pull(repo, branch):
        assert branch == allowed_branch, (
            f"pull_latest must only ever be called with {allowed_branch!r}, got {branch!r}"
        )
        calls.append(branch)
        _git(repo, "checkout", branch)

    return _pull


def _guarded_push_branch(calls, allowed_branch):
    def _push(repo, branch):
        assert branch == allowed_branch, (
            f"push_branch must only ever be called with {allowed_branch!r}, got {branch!r}"
        )
        calls.append(branch)

    return _push


def test_deploy_pre_prod_never_touches_prod_branch_and_requests_no_prod_permissions(tmp_path, monkeypatch):
    feature_branch = "dev/1234-feature"
    repo = _setup_pre_prod_repo(tmp_path, feature_branch)
    env = _env()

    pull_calls, push_calls, run_agent_calls = [], [], []
    monkeypatch.setattr(git_ops, "pull_latest", _guarded_pull_latest(pull_calls, env.pre_prod_branch))
    monkeypatch.setattr(git_ops, "push_branch", _guarded_push_branch(push_calls, env.pre_prod_branch))
    monkeypatch.setattr(
        deployment, "run_agent",
        _fake_run_agent(run_agent_calls, AgentResult(ok=True, text="ok", json_data={"status": "deployed"}, cost_usd=0.01, turns=2)),
    )

    result = asyncio.run(deployment.deploy_pre_prod(repo, env, feature_branch))

    assert result.ok is True
    assert pull_calls == [env.pre_prod_branch]
    assert push_calls == [env.pre_prod_branch]
    assert PROD_SENTINEL not in pull_calls and PROD_SENTINEL not in push_calls
    assert len(run_agent_calls) == 1
    assert run_agent_calls[0]["allow_prod"] is False
    assert PROD_SENTINEL not in run_agent_calls[0]["system_prompt"]


def test_deploy_pre_prod_merge_conflict_aborts_without_deploying_or_touching_prod(tmp_path, monkeypatch):
    feature_branch = "feature"
    repo = _init_repo(tmp_path / "repo")
    _setup_conflicting_branches(repo, base="beta", other=feature_branch)
    _git(repo, "checkout", "main")
    env = _env()

    pull_calls, push_calls, run_agent_calls = [], [], []
    monkeypatch.setattr(git_ops, "pull_latest", _guarded_pull_latest(pull_calls, env.pre_prod_branch))
    monkeypatch.setattr(git_ops, "push_branch", _guarded_push_branch(push_calls, env.pre_prod_branch))
    monkeypatch.setattr(
        deployment, "run_agent",
        _fake_run_agent(run_agent_calls, AgentResult(ok=True, text="should not be reached", json_data=None, cost_usd=0.0, turns=0)),
    )

    result = asyncio.run(deployment.deploy_pre_prod(repo, env, feature_branch))

    assert result.ok is False
    assert "merge" in result.text.lower()
    assert push_calls == []  # never pushed
    assert run_agent_calls == []  # never even tried to deploy
    assert not _merge_in_progress(repo)
    assert _working_tree_is_clean(repo)


# -- promote_prod: the only path allowed to touch prod, only via run_agent(allow_prod=True) --


def _setup_promote_repo(tmp_path):
    """`beta` one commit ahead of `main`(==prod); no real "origin" remote --
    fetch_ref is faked to create refs/remotes/origin/beta via a plain
    `git update-ref`, which is exactly as real a merge source as an actual
    fetch would produce, without needing a network-reachable remote."""
    repo = _init_repo(tmp_path / "repo")
    _git(repo, "checkout", "-b", "prod")
    _git(repo, "checkout", "-b", "beta")
    _commit_file(repo, "app.txt", "base\nbeta change\n", "beta commit")
    _git(repo, "checkout", "prod")
    return repo


def _guarded_fetch_ref(calls, allowed_branch, ref_target_branch):
    def _fetch(repo, branch):
        assert branch == allowed_branch, (
            f"fetch_ref must only ever be called with {allowed_branch!r}, got {branch!r}"
        )
        calls.append(branch)
        _git(repo, "update-ref", f"refs/remotes/origin/{branch}", f"refs/heads/{ref_target_branch}")

    return _fetch


def test_promote_prod_only_merges_into_prod_when_calling_run_agent_with_allow_prod_true(tmp_path, monkeypatch):
    repo = _setup_promote_repo(tmp_path)
    env = _env(prod_branch="prod")

    pull_calls, fetch_calls, push_calls, run_agent_calls = [], [], [], []
    monkeypatch.setattr(git_ops, "pull_latest", _guarded_pull_latest(pull_calls, env.prod_branch))
    monkeypatch.setattr(git_ops, "fetch_ref", _guarded_fetch_ref(fetch_calls, env.pre_prod_branch, "beta"))
    monkeypatch.setattr(git_ops, "push_branch", _guarded_push_branch(push_calls, env.prod_branch))
    monkeypatch.setattr(
        deployment, "run_agent",
        _fake_run_agent(run_agent_calls, AgentResult(ok=True, text="ok", json_data={"status": "deployed"}, cost_usd=0.02, turns=3)),
    )

    result = asyncio.run(deployment.promote_prod(repo, env))

    assert result.ok is True
    assert pull_calls == [env.prod_branch]
    assert fetch_calls == [env.pre_prod_branch]
    assert push_calls == [env.prod_branch]
    assert len(run_agent_calls) == 1
    assert run_agent_calls[0]["allow_prod"] is True
    assert _git(repo, "log", "-1", "--pretty=%s", "prod").stdout.strip() != "initial"


def test_promote_prod_fetch_failure_never_merges_pushes_or_deploys(tmp_path, monkeypatch):
    repo = _setup_promote_repo(tmp_path)
    env = _env(prod_branch="prod")
    prod_sha_before = _git(repo, "rev-parse", "prod").stdout.strip()

    pull_calls, push_calls, run_agent_calls = [], [], []
    monkeypatch.setattr(git_ops, "pull_latest", _guarded_pull_latest(pull_calls, env.prod_branch))

    def _raise(repo, branch):
        raise git_ops.GitOpError("simulated fetch failure")

    monkeypatch.setattr(git_ops, "fetch_ref", _raise)
    monkeypatch.setattr(git_ops, "push_branch", _guarded_push_branch(push_calls, env.prod_branch))
    monkeypatch.setattr(
        deployment, "run_agent",
        _fake_run_agent(run_agent_calls, AgentResult(ok=True, text="should not be reached", json_data=None, cost_usd=0.0, turns=0)),
    )

    result = asyncio.run(deployment.promote_prod(repo, env))

    assert result.ok is False
    assert "simulated fetch failure" in result.text
    assert push_calls == []
    assert run_agent_calls == []
    assert _git(repo, "rev-parse", "prod").stdout.strip() == prod_sha_before


def test_promote_prod_merge_conflict_aborts_cleanly_and_never_deploys(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    _setup_conflicting_branches(repo, base="prod", other="beta")
    _git(repo, "checkout", "prod")
    env = _env(prod_branch="prod")

    pull_calls, fetch_calls, push_calls, run_agent_calls = [], [], [], []
    monkeypatch.setattr(git_ops, "pull_latest", _guarded_pull_latest(pull_calls, env.prod_branch))
    monkeypatch.setattr(git_ops, "fetch_ref", _guarded_fetch_ref(fetch_calls, env.pre_prod_branch, "beta"))
    monkeypatch.setattr(git_ops, "push_branch", _guarded_push_branch(push_calls, env.prod_branch))
    monkeypatch.setattr(
        deployment, "run_agent",
        _fake_run_agent(run_agent_calls, AgentResult(ok=True, text="should not be reached", json_data=None, cost_usd=0.0, turns=0)),
    )

    result = asyncio.run(deployment.promote_prod(repo, env))

    assert result.ok is False
    assert "merge" in result.text.lower()
    assert push_calls == []
    assert run_agent_calls == []
    assert not _merge_in_progress(repo)
    assert _working_tree_is_clean(repo)
    assert _current_branch(repo) == "prod"

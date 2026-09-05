"""The "external" deploy_strategy (agents/deployment.py) -- added for multi-repo
Phase 3, for a code repo with no Vercel/Firebase/self-hosted-VM target agentra can
drive directly (e.g. agentra-loop, deployed via its own push-triggered GitHub
Actions workflow to AWS). Pre-prod is a plain merge+push (real local git, same
_merge_and_push primitive deploy_pre_prod/merge_to_pre_prod_only use -- see
test_deployment.py); promotion is opening/merging a pre_prod_branch -> prod_branch
PR via connectors/github_pulls.py (monkeypatched -- no real GitHub call).
"""

import asyncio
import subprocess
from pathlib import Path

from agentra.agents import deployment, git_ops
from agentra.environments import EnvironmentConfig


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _commit_file(repo: Path, name: str, content: str, message: str) -> None:
    (repo / name).write_text(content)
    _git(repo, "add", name)
    _git(repo, "commit", "-m", message)


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    _commit_file(path, "app.txt", "base\n", "initial")
    return path


def _env(**overrides) -> EnvironmentConfig:
    defaults = dict(pre_prod_branch="beta", prod_branch="main", deploy_strategy="external")
    defaults.update(overrides)
    return EnvironmentConfig(**defaults)


# -- pre-prod: merge + push, nothing else --------------------------------------------


def test_deploy_pre_prod_external_merges_and_pushes_without_any_build_or_deploy_step(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    _git(repo, "checkout", "-b", "beta")
    _commit_file(repo, "app.txt", "base\nbeta change\n", "beta commit")
    _git(repo, "checkout", "-b", "feature", "beta")
    _commit_file(repo, "feature.txt", "feature work\n", "feature commit")
    _git(repo, "checkout", "main")
    env = _env()

    pull_calls, push_calls = [], []
    monkeypatch.setattr(git_ops, "pull_latest", lambda r, b: (pull_calls.append(b), _git(r, "checkout", b)))
    monkeypatch.setattr(git_ops, "push_branch", lambda r, b: push_calls.append(b))

    strategy = deployment.PRE_PROD_STRATEGIES["external"]
    result = asyncio.run(strategy(repo, env, "feature", "run1", "sess1"))

    assert result.ok is True
    assert push_calls == ["beta"]
    assert "own CI/CD" in result.text
    assert (repo / "feature.txt").exists()


def test_deploy_pre_prod_external_merge_conflict_is_reported_and_never_pushes(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    _commit_file(repo, "shared.txt", "original\n", "add shared")
    _git(repo, "branch", "beta")
    _git(repo, "branch", "feature")
    _git(repo, "checkout", "beta")
    (repo / "shared.txt").write_text("beta change\n")
    _git(repo, "commit", "-am", "conflicting change on beta")
    _git(repo, "checkout", "feature")
    (repo / "shared.txt").write_text("feature change\n")
    _git(repo, "commit", "-am", "conflicting change on feature")
    _git(repo, "checkout", "main")
    env = _env()

    push_calls = []
    monkeypatch.setattr(git_ops, "pull_latest", lambda r, b: _git(r, "checkout", b))
    monkeypatch.setattr(git_ops, "push_branch", lambda r, b: push_calls.append(b))

    strategy = deployment.PRE_PROD_STRATEGIES["external"]
    result = asyncio.run(strategy(repo, env, "feature", "run1", "sess1"))

    assert result.ok is False
    assert "merge" in result.text.lower()
    assert push_calls == []


# -- promotion: open/merge a PR, nothing else ----------------------------------------


def test_promote_prod_external_opens_and_merges_a_promotion_pr(tmp_path, monkeypatch):
    from agentra import registry
    from agentra.connectors import github_pulls

    repo = tmp_path / "repo"
    repo.mkdir()
    env = _env()
    monkeypatch.setattr(registry, "repo_url_for_path", lambda r: "https://github.com/acme/loop.git")
    calls = []

    def fake_open_or_merge(repo_url, head, base, *, title):
        calls.append((repo_url, head, base, title))
        return "Merged PR #7: 'beta' -> 'main'."

    monkeypatch.setattr(github_pulls, "open_or_merge_promotion_pr", fake_open_or_merge)

    strategy = deployment.PROD_STRATEGIES["external"]
    result = asyncio.run(strategy(repo, env, "run1", "sess1"))

    assert result.ok is True
    assert calls == [("https://github.com/acme/loop.git", "beta", "main", "Promote beta to main")]
    assert "Merged PR #7" in result.text


def test_promote_prod_external_fails_cleanly_without_a_github_remote(tmp_path, monkeypatch):
    from agentra import registry

    repo = tmp_path / "repo"
    repo.mkdir()
    env = _env()
    monkeypatch.setattr(registry, "repo_url_for_path", lambda r: None)

    strategy = deployment.PROD_STRATEGIES["external"]
    result = asyncio.run(strategy(repo, env, "run1", "sess1"))

    assert result.ok is False
    assert "no github.com remote" in result.text


def test_promote_prod_external_reports_a_not_yet_mergeable_pr_without_raising(tmp_path, monkeypatch):
    from agentra import registry
    from agentra.connectors import github_pulls

    repo = tmp_path / "repo"
    repo.mkdir()
    env = _env()
    monkeypatch.setattr(registry, "repo_url_for_path", lambda r: "https://github.com/acme/loop.git")
    monkeypatch.setattr(
        github_pulls, "open_or_merge_promotion_pr",
        lambda *a, **k: "PR #7 (https://github.com/acme/loop/pull/7) is open but not mergeable yet (checks pending, or a conflict) -- not merged this cycle.",
    )

    strategy = deployment.PROD_STRATEGIES["external"]
    result = asyncio.run(strategy(repo, env, "run1", "sess1"))

    assert result.ok is True  # not an error -- just not mergeable yet, reported as such
    assert "not mergeable yet" in result.text

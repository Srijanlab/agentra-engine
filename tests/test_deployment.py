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

from agentra import environments
from agentra.agents import deployment, git_ops
from agentra.agents.base import AgentResult
from agentra.environments import EnvironmentConfig, SelfHostedVMConfig

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
# Real git repos, not mocked git_ops calls, for the same reason
# test_implementation_git_auth.py uses them: this is exactly the kind of
# cross-branch git-state bug ("assumed HEAD is already on `branch`, wasn't")
# a mocked-out git_ops call would never have caught. A bare "origin" gives
# pull_latest/push_branch something real to fetch from and push to.


def _bare_origin_with_branches(tmp_path: Path, *branches: str) -> Path:
    """A bare repo with each of `branches` pointing at its own initial
    commit (content: the branch name) -- enough for pull_latest(repo,
    branch) to have something real to fetch."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", branches[0], str(origin)], check=True, capture_output=True)
    seed = tmp_path / "_seed"
    _git_clone_and_seed(seed, origin, branches)
    return origin


def _git_clone_and_seed(seed: Path, origin: Path, branches: tuple[str, ...]) -> None:
    subprocess.run(["git", "clone", str(origin), str(seed)], check=True, capture_output=True)
    _git(seed, "config", "user.email", "test@example.com")
    _git(seed, "config", "user.name", "Test")
    for branch in branches:
        _git(seed, "checkout", "-B", branch)
        (seed / "app.txt").write_text(f"{branch}\n")
        _git(seed, "add", "app.txt")
        _git(seed, "commit", "-m", f"seed {branch}")
        _git(seed, "push", "origin", branch)


def _clone_single_branch(origin: Path, dest: Path, branch: str) -> Path:
    subprocess.run(
        ["git", "clone", "--branch", branch, "--single-branch", str(origin), str(dest)],
        check=True, capture_output=True,
    )
    _git(dest, "config", "user.email", "test@example.com")
    _git(dest, "config", "user.name", "Test")
    return dest


def test_persist_audit_trail_returns_none_when_nothing_is_dirty(tmp_path, monkeypatch):
    origin = _bare_origin_with_branches(tmp_path, "main")
    repo = _clone_single_branch(origin, tmp_path / "repo", "main")
    monkeypatch.setattr(git_ops, "_extra_auth_args", lambda repo_url: [])

    error = deployment.persist_audit_trail(repo, "main")

    assert error is None
    assert _current_branch(repo) == "main"  # never touched -- nothing to sync for


def test_persist_audit_trail_pushes_onto_branch_even_when_head_is_on_a_different_one(tmp_path, monkeypatch):
    """The real bug (GitHub issue from this session): a cycle that never
    reached deploy_pre_prod leaves HEAD on the feature branch (or, on a
    checkout fresh since the last redeploy, on `main` with no local `beta`
    ref at all) -- persist_audit_trail must still get dirty .agentra/
    changes onto `branch` and pushed, not fail with "src refspec beta does
    not match any"."""
    origin = _bare_origin_with_branches(tmp_path, "main", "beta")
    # Single-branch clone of `main` only -- no local `beta` ref, matching a
    # freshly re-cloned VM checkout (clone_repo is always --single-branch).
    repo = _clone_single_branch(origin, tmp_path / "repo", "main")
    monkeypatch.setattr(git_ops, "_extra_auth_args", lambda repo_url: [])
    assert _current_branch(repo) == "main"

    (repo / ".agentra").mkdir()
    (repo / ".agentra" / "note.txt").write_text("dirty audit trail content\n")

    error = deployment.persist_audit_trail(repo, "beta")

    assert error is None
    assert _current_branch(repo) == "beta"
    assert (repo / ".agentra" / "note.txt").read_text() == "dirty audit trail content\n"
    assert (repo / "app.txt").read_text() == "beta\n"  # beta's own content, untouched

    # Prove it actually landed on the remote, not just the local checkout.
    verify = _clone_single_branch(origin, tmp_path / "verify", "beta")
    assert (verify / ".agentra" / "note.txt").read_text() == "dirty audit trail content\n"


def test_persist_audit_trail_works_when_head_is_already_on_branch(tmp_path, monkeypatch):
    """The normal case (deploy_pre_prod already merged and left HEAD on
    `branch`) must keep working -- specifically, pull_latest's hard reset
    (which runs AFTER the temp commit capturing .agentra/'s dirty state)
    must not lose that commit just because it's on the same branch name
    pull_latest is about to reset."""
    origin = _bare_origin_with_branches(tmp_path, "beta")
    repo = _clone_single_branch(origin, tmp_path / "repo", "beta")
    monkeypatch.setattr(git_ops, "_extra_auth_args", lambda repo_url: [])

    (repo / ".agentra").mkdir()
    (repo / ".agentra" / "note.txt").write_text("already on beta\n")

    error = deployment.persist_audit_trail(repo, "beta")

    assert error is None
    assert (repo / ".agentra" / "note.txt").read_text() == "already on beta\n"
    verify = _clone_single_branch(origin, tmp_path / "verify", "beta")
    assert (verify / ".agentra" / "note.txt").read_text() == "already on beta\n"


def test_persist_audit_trail_reports_an_error_when_branch_does_not_exist_anywhere(tmp_path, monkeypatch):
    origin = _bare_origin_with_branches(tmp_path, "main")
    repo = _clone_single_branch(origin, tmp_path / "repo", "main")
    monkeypatch.setattr(git_ops, "_extra_auth_args", lambda repo_url: [])

    (repo / ".agentra").mkdir()
    (repo / ".agentra" / "note.txt").write_text("dirty\n")

    error = deployment.persist_audit_trail(repo, "does-not-exist")

    assert error is not None
    assert "does-not-exist" in error


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


# -- deploy_pre_prod_self_hosted / teardown_self_hosted_preprod: agentra's own repo,
# no run_agent call at all -- deterministic docker CLI plumbing only ---------------


def _self_hosted_config(**overrides) -> SelfHostedVMConfig:
    # Deliberately NOT named "agentra" anywhere -- proves deploy_pre_prod_self_hosted/
    # promote_prod_self_hosted derive every identifier from this config, not
    # from any hardcoded agentra-specific constant.
    defaults = dict(
        vm_name="widget-vm", vm_zone="us-east1-b", gcp_project="widget-prod",
        image_repo="registry.example.com/acme/widget-app",
        anchor_container="widget-proxy", app_network="widget-app-net",
        preprod_network="widget-preprod-net", data_mount="/mnt/disks/widget-data",
        firestore_project="widget-prod",
    )
    defaults.update(overrides)
    return SelfHostedVMConfig(**defaults)


def _fake_docker_run(calls: list, *, build_ok=True, run_ok=True, health_ok=True, reachable_ok=True, inspect_env=None):
    def _run(args, **kwargs):
        calls.append(list(args))
        import subprocess as _subprocess

        if args[:2] == ["docker", "build"]:
            return _subprocess.CompletedProcess(args, 0 if build_ok else 1, stdout="", stderr="" if build_ok else "build failed")
        if args[:2] == ["docker", "run"]:
            return _subprocess.CompletedProcess(args, 0 if run_ok else 1, stdout="", stderr="" if run_ok else "run failed")
        if args[:2] == ["docker", "exec"]:
            return _subprocess.CompletedProcess(args, 0 if health_ok else 1, stdout="", stderr="")
        if args[:1] == ["curl"]:
            # The orchestrator-process-side reachability check (no `docker exec` --
            # a bare curl, exactly what would fail with "Could not resolve host"
            # if this process's own container were never joined to preprod_network).
            return _subprocess.CompletedProcess(
                args, 0 if reachable_ok else 6, stdout="", stderr="" if reachable_ok else "Could not resolve host",
            )
        if args[:2] == ["docker", "inspect"]:
            import json as _json

            return _subprocess.CompletedProcess(args, 0, stdout=_json.dumps(inspect_env or []), stderr="")
        if args[:1] == ["stat"]:
            return _subprocess.CompletedProcess(args, 0, stdout="999\n", stderr="")
        # network create/connect, rm -f, rmi -- always "succeed" (best-effort/idempotent in the real code)
        return _subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    return _run


async def _fake_sleep(*a, **k):
    return None


def test_deploy_pre_prod_self_hosted_builds_runs_and_returns_the_internal_preview_url(tmp_path, monkeypatch):
    feature_branch = "dev/1234-feature"
    repo = _setup_pre_prod_repo(tmp_path, feature_branch)
    env = _env()
    config = _self_hosted_config()

    pull_calls, push_calls, docker_calls = [], [], []
    monkeypatch.setattr(git_ops, "pull_latest", _guarded_pull_latest(pull_calls, env.pre_prod_branch))
    monkeypatch.setattr(git_ops, "push_branch", _guarded_push_branch(push_calls, env.pre_prod_branch))
    monkeypatch.setattr(environments, "load_self_hosted_vm_config", lambda repo: config)
    monkeypatch.setattr(deployment.subprocess, "run", _fake_docker_run(docker_calls))
    monkeypatch.setattr(deployment.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(deployment, "_own_container_name", lambda: "widget-app-blue")

    result = asyncio.run(deployment.deploy_pre_prod_self_hosted(repo, env, feature_branch, "run123"))

    assert result.ok is True
    assert result.json_data["status"] == "deployed"
    assert result.json_data["preview_url"] == "http://widget-app-preprod-run123:8080"
    assert pull_calls == [env.pre_prod_branch]
    assert push_calls == [env.pre_prod_branch]
    assert PROD_SENTINEL not in pull_calls and PROD_SENTINEL not in push_calls

    build_calls = [c for c in docker_calls if c[:2] == ["docker", "build"]]
    assert build_calls == [["docker", "build", "-t", "widget-app-preprod:run123", str(repo)]]
    run_calls = [c for c in docker_calls if c[:2] == ["docker", "run"]]
    assert len(run_calls) == 1
    run_cmd = run_calls[0]
    assert "widget-app-preprod-run123" in run_cmd
    assert "widget-preprod-net" in run_cmd
    assert "--memory=1g" in run_cmd and "--cpus=1" in run_cmd
    assert "AGENTRA_FIRESTORE_PROJECT=widget-prod" in run_cmd
    assert "/mnt/disks/widget-data/claude:/home/agentuser/.claude:ro" in run_cmd
    # The orchestrator's OWN (currently-active blue/green) container is joined to
    # preprod_network -- never config.anchor_container ("widget-proxy"), which is
    # the nginx reverse-proxy the orchestrator process doesn't run inside of and
    # was the actual root cause of the DNS-resolution bug this pins.
    network_connect_calls = [c for c in docker_calls if c[:3] == ["docker", "network", "connect"]]
    assert network_connect_calls == [["docker", "network", "connect", "widget-preprod-net", "widget-app-blue"]]
    assert not any("widget-proxy" in c for c in network_connect_calls)
    # A genuine end-to-end reachability check (bare curl from this process,
    # not `docker exec` into the sibling) must run before reporting "deployed".
    reachability_calls = [c for c in docker_calls if c[:1] == ["curl"]]
    assert reachability_calls == [["curl", "-sf", "-m", "5", "http://widget-app-preprod-run123:8080/health"]]


def test_deploy_pre_prod_self_hosted_reports_a_clear_error_when_repo_has_no_config(tmp_path, monkeypatch):
    feature_branch = "dev/1234-feature"
    repo = _setup_pre_prod_repo(tmp_path, feature_branch)
    env = _env()

    monkeypatch.setattr(environments, "load_self_hosted_vm_config", lambda repo: None)
    monkeypatch.setattr(
        git_ops, "pull_latest", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not touch git without config"))
    )

    result = asyncio.run(deployment.deploy_pre_prod_self_hosted(repo, env, feature_branch, "run123"))

    assert result.ok is False
    assert "deployment.md" in result.text


def test_deploy_pre_prod_self_hosted_returns_error_when_build_fails(tmp_path, monkeypatch):
    feature_branch = "dev/1234-feature"
    repo = _setup_pre_prod_repo(tmp_path, feature_branch)
    env = _env()

    docker_calls = []
    monkeypatch.setattr(git_ops, "pull_latest", lambda repo, branch: None)
    monkeypatch.setattr(git_ops, "push_branch", lambda repo, branch: None)
    monkeypatch.setattr(environments, "load_self_hosted_vm_config", lambda repo: _self_hosted_config())
    monkeypatch.setattr(deployment.subprocess, "run", _fake_docker_run(docker_calls, build_ok=False))
    monkeypatch.setattr(deployment.asyncio, "sleep", _fake_sleep)

    result = asyncio.run(deployment.deploy_pre_prod_self_hosted(repo, env, feature_branch, "run123"))

    assert result.ok is False
    assert "build" in result.text.lower()
    # Never got as far as `docker run` after a failed build.
    assert not any(c[:2] == ["docker", "run"] for c in docker_calls)


def test_deploy_pre_prod_self_hosted_returns_failed_status_when_health_check_never_succeeds(tmp_path, monkeypatch):
    feature_branch = "dev/1234-feature"
    repo = _setup_pre_prod_repo(tmp_path, feature_branch)
    env = _env()

    docker_calls = []
    monkeypatch.setattr(git_ops, "pull_latest", lambda repo, branch: None)
    monkeypatch.setattr(git_ops, "push_branch", lambda repo, branch: None)
    monkeypatch.setattr(environments, "load_self_hosted_vm_config", lambda repo: _self_hosted_config())
    monkeypatch.setattr(deployment.subprocess, "run", _fake_docker_run(docker_calls, health_ok=False))
    monkeypatch.setattr(deployment, "_own_container_name", lambda: "widget-app-blue")
    monkeypatch.setattr(deployment.asyncio, "sleep", _fake_sleep)

    result = asyncio.run(deployment.deploy_pre_prod_self_hosted(repo, env, feature_branch, "run123"))

    assert result.ok is False
    assert result.json_data["status"] == "failed"
    assert result.json_data["preview_url"] == "http://widget-app-preprod-run123:8080"


# -- regression tests for the preprod-network DNS-resolution bug -------------------
#
# Root cause: deploy_pre_prod_self_hosted used to Docker-network-connect only
# config.anchor_container (nginx) to preprod_network. The orchestrator process
# that runs verify_pre_prod's Testing Agent turn (and any curl/getent/python
# socket call it issues) actually lives inside the currently-active blue/green
# APP container, never nginx -- so that container could never resolve the
# pre-prod sibling's Docker DNS name, and deploy_pre_prod_self_hosted still
# reported status=deployed because its only health check (_wait_for_healthy)
# execs into the sibling itself and never exercises this network path at all.


def test_deploy_pre_prod_self_hosted_joins_its_own_container_not_the_anchor_container(tmp_path, monkeypatch):
    """Pins the actual fix: preprod_network must be connected to whichever
    container this orchestrator process is itself running in, never to
    config.anchor_container (nginx) -- the old, buggy behavior."""
    feature_branch = "dev/1234-feature"
    repo = _setup_pre_prod_repo(tmp_path, feature_branch)
    env = _env()
    config = _self_hosted_config()

    docker_calls = []
    monkeypatch.setattr(git_ops, "pull_latest", lambda repo, branch: None)
    monkeypatch.setattr(git_ops, "push_branch", lambda repo, branch: None)
    monkeypatch.setattr(environments, "load_self_hosted_vm_config", lambda repo: config)
    monkeypatch.setattr(deployment.subprocess, "run", _fake_docker_run(docker_calls))
    monkeypatch.setattr(deployment.asyncio, "sleep", _fake_sleep)
    # Simulate running inside the "green" app container this time, to prove
    # the target is resolved dynamically rather than hardcoded to one color.
    monkeypatch.setattr(deployment, "_own_container_name", lambda: "widget-app-green")

    result = asyncio.run(deployment.deploy_pre_prod_self_hosted(repo, env, feature_branch, "run123"))

    assert result.ok is True
    network_connect_calls = [c for c in docker_calls if c[:3] == ["docker", "network", "connect"]]
    assert network_connect_calls == [["docker", "network", "connect", "widget-preprod-net", "widget-app-green"]]
    assert config.anchor_container not in [name for c in network_connect_calls for name in c]


def test_deploy_pre_prod_self_hosted_fails_clearly_when_own_container_name_cannot_be_determined(tmp_path, monkeypatch):
    """If this process's own container identity can't be resolved (HOSTNAME
    unset / docker inspect failure), refuse to deploy something nothing would
    be able to reach, rather than silently skipping the network join and
    reporting a false 'deployed' (the old bug's exact failure shape)."""
    feature_branch = "dev/1234-feature"
    repo = _setup_pre_prod_repo(tmp_path, feature_branch)
    env = _env()

    docker_calls = []
    monkeypatch.setattr(git_ops, "pull_latest", lambda repo, branch: None)
    monkeypatch.setattr(git_ops, "push_branch", lambda repo, branch: None)
    monkeypatch.setattr(environments, "load_self_hosted_vm_config", lambda repo: _self_hosted_config())
    monkeypatch.setattr(deployment.subprocess, "run", _fake_docker_run(docker_calls))
    monkeypatch.setattr(deployment.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(deployment, "_own_container_name", lambda: None)

    result = asyncio.run(deployment.deploy_pre_prod_self_hosted(repo, env, feature_branch, "run123"))

    assert result.ok is False
    assert result.json_data["status"] == "failed"
    assert "own container" in result.text.lower()
    # Never got as far as `docker run` -- refused before starting the sibling.
    assert not any(c[:2] == ["docker", "run"] for c in docker_calls)


def test_deploy_pre_prod_self_hosted_reports_failed_when_unreachable_from_orchestrator_even_if_sibling_health_check_passes(
    tmp_path, monkeypatch
):
    """This is the class of failure the brief describes: the sibling container's
    OWN curl-against-itself (_wait_for_healthy) can pass while the orchestrator's
    own process still can't resolve/reach preview_url at all (e.g. network join
    silently failed) -- that must be caught here and reported as status=failed
    with a reachability/network diagnostic, not reported as status=deployed
    only to fail opaquely in a later, separate verify_pre_prod call."""
    feature_branch = "dev/1234-feature"
    repo = _setup_pre_prod_repo(tmp_path, feature_branch)
    env = _env()

    docker_calls = []
    monkeypatch.setattr(git_ops, "pull_latest", lambda repo, branch: None)
    monkeypatch.setattr(git_ops, "push_branch", lambda repo, branch: None)
    monkeypatch.setattr(environments, "load_self_hosted_vm_config", lambda repo: _self_hosted_config())
    monkeypatch.setattr(
        deployment.subprocess, "run", _fake_docker_run(docker_calls, health_ok=True, reachable_ok=False)
    )
    monkeypatch.setattr(deployment.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(deployment, "_own_container_name", lambda: "widget-app-blue")

    result = asyncio.run(deployment.deploy_pre_prod_self_hosted(repo, env, feature_branch, "run123"))

    assert result.ok is False
    assert result.json_data["status"] == "failed"
    assert result.json_data["preview_url"] == "http://widget-app-preprod-run123:8080"
    text_lower = result.text.lower()
    assert "reachable" in text_lower or "network" in text_lower or "dns" in text_lower
    # The reachability check (bare curl, no docker exec) did run -- retried up to
    # _HEALTH_CHECK_ATTEMPTS times, same as _wait_for_healthy -- and every attempt failed.
    reachability_calls = [c for c in docker_calls if c[:1] == ["curl"]]
    assert len(reachability_calls) == deployment._HEALTH_CHECK_ATTEMPTS
    assert all(c == ["curl", "-sf", "-m", "5", "http://widget-app-preprod-run123:8080/health"] for c in reachability_calls)


def test_own_container_name_resolves_via_docker_inspect_of_the_hostname_env_var(monkeypatch):
    """Unit-level pin for the resolution mechanism itself: HOSTNAME (Docker's
    default container hostname, its own short ID) is fed to `docker inspect`
    to recover the actual --name Docker assigned, e.g. 'agentra-blue'."""
    monkeypatch.setenv("HOSTNAME", "abc123def456")
    calls = []

    def _fake_run(args, **kwargs):
        calls.append(list(args))
        import subprocess as _subprocess

        return _subprocess.CompletedProcess(args, 0, stdout="/agentra-blue\n", stderr="")

    monkeypatch.setattr(deployment.subprocess, "run", _fake_run)

    name = deployment._own_container_name()

    assert name == "agentra-blue"
    assert calls == [["docker", "inspect", "abc123def456", "--format", "{{.Name}}"]]


def test_own_container_name_is_none_when_hostname_env_var_is_unset(monkeypatch):
    monkeypatch.delenv("HOSTNAME", raising=False)

    assert deployment._own_container_name() is None


def test_teardown_self_hosted_preprod_removes_container_and_image(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    docker_calls = []
    monkeypatch.setattr(environments, "load_self_hosted_vm_config", lambda repo: _self_hosted_config())
    monkeypatch.setattr(deployment.subprocess, "run", _fake_docker_run(docker_calls))

    deployment.teardown_self_hosted_preprod(repo, "run123")

    assert ["docker", "rm", "-f", "widget-app-preprod-run123"] in docker_calls
    assert ["docker", "rmi", "widget-app-preprod:run123"] in docker_calls


def test_teardown_self_hosted_preprod_noops_without_config(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(environments, "load_self_hosted_vm_config", lambda repo: None)
    monkeypatch.setattr(
        deployment.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not call docker without config"))
    )

    deployment.teardown_self_hosted_preprod(repo, "run123")  # must not raise


# -- promote_prod_self_hosted: nginx blue/green flip, no run_agent call ------------


def test_promote_prod_self_hosted_flips_nginx_to_the_inactive_color_and_removes_the_old_container(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    env = _env(prod_branch="prod")
    config = _self_hosted_config()

    pull_calls, fetch_calls, push_calls, docker_calls = [], [], [], []
    monkeypatch.setattr(git_ops, "pull_latest", _guarded_pull_latest(pull_calls, env.prod_branch))
    monkeypatch.setattr(git_ops, "fetch_ref", _guarded_fetch_ref(fetch_calls, env.pre_prod_branch, "beta"))
    monkeypatch.setattr(git_ops, "push_branch", _guarded_push_branch(push_calls, env.prod_branch))
    monkeypatch.setattr(environments, "load_self_hosted_vm_config", lambda repo: config)

    def _run(args, **kwargs):
        docker_calls.append(list(args))
        if args[:3] == ["docker", "exec", "widget-proxy"] and "cat" in args:
            return subprocess.CompletedProcess(args, 0, stdout="blue\n", stderr="")
        if args[:2] == ["docker", "build"]:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if args[:2] == ["docker", "inspect"] and "{{.Config.Image}}" in args:
            return subprocess.CompletedProcess(args, 0, stdout="widget-app:oldrun123\n", stderr="")
        if args[:2] == ["docker", "inspect"]:
            return subprocess.CompletedProcess(args, 0, stdout="[]", stderr="")
        if args[:1] == ["stat"]:
            return subprocess.CompletedProcess(args, 0, stdout="999\n", stderr="")
        if args[:2] == ["docker", "run"]:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if args[:2] == ["docker", "exec"] and "curl" in args:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if args[:2] == ["docker", "exec"] and "nginx" in " ".join(args):
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(deployment.subprocess, "run", _run)
    monkeypatch.setattr(deployment.asyncio, "sleep", _fake_sleep)

    result = asyncio.run(deployment.promote_prod_self_hosted(repo, env, "run456"))

    assert result.ok is True
    assert "blue -> green" in result.json_data["notes"]
    assert pull_calls == [env.prod_branch]
    assert push_calls == [env.prod_branch]

    run_calls = [c for c in docker_calls if c[:2] == ["docker", "run"]]
    assert len(run_calls) == 1
    assert "widget-app-green" in run_calls[0]
    assert "widget-app-net" in run_calls[0]
    assert "/mnt/disks/widget-data/claude:/home/agentuser/.claude" in run_calls[0]
    assert "/mnt/disks/widget-data/agentra-home:/home/agentuser/.agentra" in run_calls[0]
    assert "/mnt/disks/widget-data/repos:/workspace" in run_calls[0]
    assert "/var/run/docker.sock:/var/run/docker.sock" in run_calls[0]

    reload_calls = [c for c in docker_calls if "nginx -s reload" in " ".join(c)]
    assert len(reload_calls) == 1

    remove_calls = [c for c in docker_calls if c[:3] == ["docker", "rm", "-f"]]
    assert ["docker", "rm", "-f", "widget-app-blue"] in remove_calls

    # The outgoing color's image must be reclaimed too, not just its container --
    # otherwise every successful promotion leaves one more full image (~GBs)
    # permanently orphaned (confirmed live: this exact leak filled a VM's disk
    # to 100%, deadlocking every subsequent autonomous cycle).
    rmi_calls = [c for c in docker_calls if c[:2] == ["docker", "rmi"]]
    assert ["docker", "rmi", "widget-app:oldrun123"] in rmi_calls


def test_promote_prod_self_hosted_aborts_without_flipping_when_new_color_never_becomes_healthy(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    env = _env(prod_branch="prod")
    config = _self_hosted_config()

    monkeypatch.setattr(git_ops, "pull_latest", lambda *a, **k: None)
    monkeypatch.setattr(git_ops, "fetch_ref", lambda *a, **k: None)
    monkeypatch.setattr(git_ops, "push_branch", lambda *a, **k: None)
    monkeypatch.setattr(environments, "load_self_hosted_vm_config", lambda repo: config)

    nginx_calls = []
    docker_calls = []

    def _run(args, **kwargs):
        docker_calls.append(list(args))
        if args[:3] == ["docker", "exec", "widget-proxy"] and "cat" in args:
            return subprocess.CompletedProcess(args, 0, stdout="blue\n", stderr="")
        if "nginx" in " ".join(args):
            nginx_calls.append(list(args))
        if args[:2] == ["docker", "exec"] and "curl" in args:
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="")  # never healthy
        if args[:1] == ["stat"]:
            return subprocess.CompletedProcess(args, 0, stdout="999\n", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="[]" if args[:2] == ["docker", "inspect"] else "", stderr="")

    monkeypatch.setattr(deployment.subprocess, "run", _run)
    monkeypatch.setattr(deployment.asyncio, "sleep", _fake_sleep)

    result = asyncio.run(deployment.promote_prod_self_hosted(repo, env, "run456"))

    assert result.ok is False
    assert "blue" in result.text  # still live, promotion aborted
    assert nginx_calls == []  # never flipped

    # The never-became-healthy candidate's image must not be left behind either --
    # only its container removal was covered before this.
    assert ["docker", "rmi", "widget-app:run456"] in docker_calls


def test_promote_prod_self_hosted_removes_the_new_image_when_nginx_reload_fails(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    env = _env(prod_branch="prod")
    config = _self_hosted_config()

    monkeypatch.setattr(git_ops, "pull_latest", lambda *a, **k: None)
    monkeypatch.setattr(git_ops, "fetch_ref", lambda *a, **k: None)
    monkeypatch.setattr(git_ops, "push_branch", lambda *a, **k: None)
    monkeypatch.setattr(environments, "load_self_hosted_vm_config", lambda repo: config)

    docker_calls = []

    def _run(args, **kwargs):
        docker_calls.append(list(args))
        if args[:3] == ["docker", "exec", "widget-proxy"] and "cat" in args:
            return subprocess.CompletedProcess(args, 0, stdout="blue\n", stderr="")
        if args[:2] == ["docker", "exec"] and "curl" in args:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")  # healthy
        if "nginx -s reload" in " ".join(args):
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="reload failed")
        if args[:1] == ["stat"]:
            return subprocess.CompletedProcess(args, 0, stdout="999\n", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="[]" if args[:2] == ["docker", "inspect"] else "", stderr="")

    monkeypatch.setattr(deployment.subprocess, "run", _run)
    monkeypatch.setattr(deployment.asyncio, "sleep", _fake_sleep)

    result = asyncio.run(deployment.promote_prod_self_hosted(repo, env, "run456"))

    assert result.ok is False
    assert "blue" in result.text  # still live, promotion aborted

    remove_calls = [c for c in docker_calls if c[:3] == ["docker", "rm", "-f"]]
    assert ["docker", "rm", "-f", "widget-app-green"] in remove_calls  # the candidate, not the still-live old color
    assert ["docker", "rmi", "widget-app:run456"] in docker_calls
    # The old (still-live) color must never be touched on this failure path.
    assert ["docker", "rm", "-f", "widget-app-blue"] not in remove_calls


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


def _fake_cleanup_docker(calls: list, *, container_ages_hours: dict | None = None, images: list | None = None):
    """container_ages_hours: {name: age_in_hours}; those names are what
    `docker ps -a --filter name=^prefix` returns. images: list of
    "repo:tag" strings `docker images` returns."""
    import datetime as _dt

    container_ages_hours = container_ages_hours or {}
    images = images or []

    def _run(args, **kwargs):
        calls.append(list(args))
        if args[:2] == ["docker", "ps"]:
            return subprocess.CompletedProcess(args, 0, stdout="\n".join(container_ages_hours) + ("\n" if container_ages_hours else ""), stderr="")
        if args[:2] == ["docker", "inspect"]:
            name = args[2]
            age = container_ages_hours.get(name)
            if age is None:
                return subprocess.CompletedProcess(args, 1, stdout="", stderr="no such object")
            created = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=age)
            return subprocess.CompletedProcess(args, 0, stdout=created.strftime("%Y-%m-%dT%H:%M:%S.000000000Z"), stderr="")
        if args[:2] == ["docker", "images"]:
            return subprocess.CompletedProcess(args, 0, stdout="\n".join(images) + ("\n" if images else ""), stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    return _run


def test_cleanup_stale_preprod_removes_containers_and_images_older_than_the_threshold(monkeypatch):
    config = _self_hosted_config()
    docker_calls = []
    monkeypatch.setattr(
        deployment.subprocess, "run",
        _fake_cleanup_docker(
            docker_calls,
            container_ages_hours={"widget-app-preprod-old": 5.0, "widget-app-preprod-fresh": 0.1},
            images=["widget-app-preprod:old", "widget-app-preprod:fresh", "widget-app-preprod:orphaned-image"],
        ),
    )

    removed = deployment.cleanup_stale_preprod(config)

    assert "widget-app-preprod-old" in removed
    assert "widget-app-preprod-fresh" not in removed
    rm_calls = [c for c in docker_calls if c[:3] == ["docker", "rm", "-f"]]
    assert ["docker", "rm", "-f", "widget-app-preprod-old"] in rm_calls
    assert ["docker", "rm", "-f", "widget-app-preprod-fresh"] not in rm_calls
    # The old container's image is removed (no live container backs it anymore);
    # the fresh one's image is kept; an image with no matching container at all
    # (never listed in `docker ps`) is also swept, since nothing is using it.
    assert "widget-app-preprod:old" in removed
    assert "widget-app-preprod:fresh" not in removed
    assert "widget-app-preprod:orphaned-image" in removed


def test_cleanup_stale_preprod_leaves_everything_alone_when_nothing_is_stale(monkeypatch):
    config = _self_hosted_config()
    docker_calls = []
    monkeypatch.setattr(
        deployment.subprocess, "run",
        _fake_cleanup_docker(docker_calls, container_ages_hours={"widget-app-preprod-fresh": 0.1}, images=["widget-app-preprod:fresh"]),
    )

    removed = deployment.cleanup_stale_preprod(config)

    assert removed == []
    assert not any(c[:2] == ["docker", "rm"] for c in docker_calls)
    assert not any(c[:2] == ["docker", "rmi"] for c in docker_calls)


def test_deploy_pre_prod_self_hosted_sweeps_stale_siblings_before_deploying(tmp_path, monkeypatch):
    feature_branch = "dev/1234-feature"
    repo = _setup_pre_prod_repo(tmp_path, feature_branch)
    env = _env()
    config = _self_hosted_config()

    monkeypatch.setattr(git_ops, "pull_latest", lambda repo, branch: None)
    monkeypatch.setattr(git_ops, "push_branch", lambda repo, branch: None)
    monkeypatch.setattr(environments, "load_self_hosted_vm_config", lambda repo: config)
    monkeypatch.setattr(deployment.asyncio, "sleep", _fake_sleep)

    cleanup_calls = []
    monkeypatch.setattr(deployment, "cleanup_stale_preprod", lambda cfg: cleanup_calls.append(cfg) or [])
    docker_calls = []
    monkeypatch.setattr(deployment.subprocess, "run", _fake_docker_run(docker_calls))
    monkeypatch.setattr(deployment, "_own_container_name", lambda: "widget-app-blue")

    result = asyncio.run(deployment.deploy_pre_prod_self_hosted(repo, env, feature_branch, "run123"))

    assert result.ok is True
    assert cleanup_calls == [config]

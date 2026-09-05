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
from agentra.agents import deployment, deployment_network, git_ops
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


def test_persist_audit_trail_falls_back_to_the_checked_out_branch_when_the_target_is_missing(tmp_path, monkeypatch):
    """A multi-repo app's coordination repo has no pre-prod/prod split -- env.pre_prod_branch
    defaults to 'beta', which doesn't exist there. Rather than fail the whole persist on a
    missing ref (and lose the .agentra/memory bookkeeping), fall back to whatever branch is
    checked out (its default, e.g. 'main')."""
    origin = _bare_origin_with_branches(tmp_path, "main")
    repo = _clone_single_branch(origin, tmp_path / "repo", "main")
    monkeypatch.setattr(git_ops, "_extra_auth_args", lambda repo_url: [])

    (repo / ".agentra").mkdir()
    (repo / ".agentra" / "note.txt").write_text("dirty\n")

    error = deployment.persist_audit_trail(repo, "beta")  # no 'beta' on the remote

    assert error is None
    assert _current_branch(repo) == "main"
    verify = _clone_single_branch(origin, tmp_path / "verify", "main")
    assert (verify / ".agentra" / "note.txt").read_text() == "dirty\n"


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


# Fake values _fake_docker_run returns for the two GitHub issue #45 lookups
# (`docker port`, `docker network inspect ... Gateway`) -- a plausible
# Docker-assigned ephemeral host port and a plausible bridge-network
# gateway IP, so every test's expected preview_url is built from these two
# constants rather than repeating magic strings everywhere.
_FAKE_HOST_PORT = "32768"
_FAKE_GATEWAY_IP = "172.20.0.1"


def _fake_docker_run(
    calls: list, *, build_ok=True, run_ok=True, health_ok=True, inspect_env=None,
    port_ok=True, gateway_ok=True, reachable_ok=True, preprod_network="widget-preprod-net",
    own_container_joined_preprod=False,
):
    """own_container_joined_preprod controls what `docker inspect <container>
    --format {{json .NetworkSettings.Networks}}` reports for _container_networks/
    _ensure_network_joined -- False (the default) means every test that doesn't
    care about the alias-based reachability path keeps exercising the
    host-gateway fallback exactly as before."""

    def _run(args, **kwargs):
        calls.append(list(args))
        import subprocess as _subprocess

        if args[:2] == ["docker", "build"]:
            return _subprocess.CompletedProcess(args, 0 if build_ok else 1, stdout="", stderr="" if build_ok else "build failed")
        if args[:2] == ["docker", "run"]:
            return _subprocess.CompletedProcess(args, 0 if run_ok else 1, stdout="", stderr="" if run_ok else "run failed")
        if args[:2] == ["docker", "port"]:
            if not port_ok:
                return _subprocess.CompletedProcess(args, 1, stdout="", stderr="Error: No such container")
            return _subprocess.CompletedProcess(args, 0, stdout=f"0.0.0.0:{_FAKE_HOST_PORT}\n[::]:{_FAKE_HOST_PORT}\n", stderr="")
        if args[:3] == ["docker", "network", "inspect"]:
            if not gateway_ok:
                return _subprocess.CompletedProcess(args, 1, stdout="", stderr="Error: No such network")
            return _subprocess.CompletedProcess(args, 0, stdout=f"{_FAKE_GATEWAY_IP}\n", stderr="")
        if args[:2] == ["docker", "exec"]:
            return _subprocess.CompletedProcess(args, 0 if health_ok else 1, stdout="", stderr="")
        if args[:1] == ["curl"]:
            # The orchestrator-process-side reachability check (no `docker exec` --
            # a bare curl, exactly what would fail with "Could not resolve host"
            # if this process's own container were never joined to preprod_network).
            return _subprocess.CompletedProcess(
                args, 0 if reachable_ok else 6, stdout="", stderr="" if reachable_ok else "Could not resolve host",
            )
        if args[:2] == ["docker", "inspect"] and "{{json .NetworkSettings.Networks}}" in args:
            import json as _json

            networks = {preprod_network: {}} if own_container_joined_preprod else {}
            return _subprocess.CompletedProcess(args, 0, stdout=_json.dumps(networks), stderr="")
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
    assert result.json_data["preview_url"] == f"http://{_FAKE_GATEWAY_IP}:{_FAKE_HOST_PORT}"
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
    # GitHub #101/#93: reap zombies + cap a runaway process leak on the shared host
    assert "--init" in run_cmd
    assert "--pids-limit" in run_cmd and "512" in run_cmd
    assert "/mnt/disks/widget-data/claude:/home/agentuser/.claude:ro" in run_cmd
    # GitHub issue #45: 8080 is published to a Docker-assigned free host
    # port (bare "-p 8080", no host port hardcoded/derived) so preview_url
    # can be built from a host-reachable address instead of the
    # never-resolves-for-the-verifier container-name DNS name.
    assert "-p" in run_cmd and "8080" in run_cmd[run_cmd.index("-p") + 1]
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
    assert reachability_calls == [["curl", "-sf", "-m", "5", f"http://{_FAKE_GATEWAY_IP}:{_FAKE_HOST_PORT}/health"]]

    # The actual GitHub issue #45 lookups: the published host port via
    # `docker port`, and config.app_network's gateway (not
    # preprod_network's -- the verifying process's own container is on
    # app_network, never preprod_network).
    port_calls = [c for c in docker_calls if c[:2] == ["docker", "port"]]
    assert port_calls == [["docker", "port", "widget-app-preprod-run123", "8080/tcp"]]
    gateway_calls = [c for c in docker_calls if c[:3] == ["docker", "network", "inspect"]]
    assert gateway_calls == [["docker", "network", "inspect", "widget-app-net", "--format", "{{(index .IPAM.Config 0).Gateway}}"]]


def test_deploy_pre_prod_self_hosted_passes_through_the_github_app_credentials(tmp_path, monkeypatch):
    """Without these, the pre-prod dashboard has no GitHub App configured at all -- every
    pre-prod deploy lands on the "Connect GitHub to get started" gate with zero registered-app
    data to verify against, no matter what the change under test touches (confirmed live: a
    fully successful pre-prod deploy still showed the connect screen, not real data). Inherited
    read-only from this process's own live (currently-active blue/green) container -- the same
    trust boundary agentra's own production containers already run under, not a new secret
    fetch or a wider grant. Unrelated env entries on the source container (ALARM_WEBHOOK_PASSWORD
    here) must NOT leak into the pre-prod container -- only the two GitHub App keys."""
    feature_branch = "dev/1234-feature"
    repo = _setup_pre_prod_repo(tmp_path, feature_branch)
    env = _env()
    config = _self_hosted_config()

    pull_calls, push_calls, docker_calls = [], [], []
    monkeypatch.setattr(git_ops, "pull_latest", _guarded_pull_latest(pull_calls, env.pre_prod_branch))
    monkeypatch.setattr(git_ops, "push_branch", _guarded_push_branch(push_calls, env.pre_prod_branch))
    monkeypatch.setattr(environments, "load_self_hosted_vm_config", lambda repo: config)
    monkeypatch.setattr(
        deployment.subprocess, "run",
        _fake_docker_run(
            docker_calls,
            inspect_env=[
                "GITHUB_APP_ID=12345",
                "GITHUB_APP_PRIVATE_KEY=-----BEGIN RSA PRIVATE KEY-----fake-----END-----",
                "ALARM_WEBHOOK_PASSWORD=super-secret-unrelated",
            ],
        ),
    )
    monkeypatch.setattr(deployment.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(deployment, "_own_container_name", lambda: "widget-app-blue")

    result = asyncio.run(deployment.deploy_pre_prod_self_hosted(repo, env, feature_branch, "run123"))

    assert result.ok is True
    run_cmd = next(c for c in docker_calls if c[:2] == ["docker", "run"])
    assert "GITHUB_APP_ID=12345" in run_cmd
    assert "GITHUB_APP_PRIVATE_KEY=-----BEGIN RSA PRIVATE KEY-----fake-----END-----" in run_cmd
    assert not any("ALARM_WEBHOOK_PASSWORD" in arg for arg in run_cmd)

    # Inherited from this process's own currently-active container, not a hardcoded name.
    env_inspect_calls = [
        c for c in docker_calls
        if c[:2] == ["docker", "inspect"] and "{{json .Config.Env}}" in c
    ]
    assert env_inspect_calls == [["docker", "inspect", "widget-app-blue", "--format", "{{json .Config.Env}}"]]


def test_deploy_pre_prod_self_hosted_passes_through_the_dynamodb_credentials(tmp_path, monkeypatch):
    """The AWS credentials (DynamoDB registry access) must never land in the
    committed self-hosted-vm-config YAML the way a non-secret Firestore
    project id once could -- inherited from this process's own live
    container instead, the same trust boundary as the GitHub App keys."""
    feature_branch = "dev/1234-feature"
    repo = _setup_pre_prod_repo(tmp_path, feature_branch)
    env = _env()
    config = _self_hosted_config()

    pull_calls, push_calls, docker_calls = [], [], []
    monkeypatch.setattr(git_ops, "pull_latest", _guarded_pull_latest(pull_calls, env.pre_prod_branch))
    monkeypatch.setattr(git_ops, "push_branch", _guarded_push_branch(push_calls, env.pre_prod_branch))
    monkeypatch.setattr(environments, "load_self_hosted_vm_config", lambda repo: config)
    monkeypatch.setattr(
        deployment.subprocess, "run",
        _fake_docker_run(
            docker_calls,
            inspect_env=[
                "AGENTRA_DYNAMODB_TABLE_PREFIX=agentra-",
                "AGENTRA_AWS_ACCESS_KEY_ID=AKIAFAKE",
                "AGENTRA_AWS_SECRET_ACCESS_KEY=fake-secret",
                "AGENTRA_AWS_REGION=us-west-2",
            ],
        ),
    )
    monkeypatch.setattr(deployment.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(deployment, "_own_container_name", lambda: "widget-app-blue")

    result = asyncio.run(deployment.deploy_pre_prod_self_hosted(repo, env, feature_branch, "run123"))

    assert result.ok is True
    run_cmd = next(c for c in docker_calls if c[:2] == ["docker", "run"])
    assert "AGENTRA_DYNAMODB_TABLE_PREFIX=agentra-" in run_cmd
    assert "AGENTRA_AWS_ACCESS_KEY_ID=AKIAFAKE" in run_cmd
    assert "AGENTRA_AWS_SECRET_ACCESS_KEY=fake-secret" in run_cmd
    assert "AGENTRA_AWS_REGION=us-west-2" in run_cmd


def test_deploy_pre_prod_self_hosted_returns_a_url_reachable_via_a_plain_socket_connection(tmp_path, monkeypatch):
    """The regression this issue asked for: assert the URL handed to
    verification is actually reachable via a plain socket connection in the
    same environment run_pre_prod executes in -- not just that it's
    *shaped* like a URL. A real TCP server bound to 127.0.0.1 stands in for
    "the docker daemon's published-port target"; deploy_pre_prod_self_hosted
    is monkeypatched to report that server's own host:port as if `docker
    port`/`docker network inspect` had returned it, then a plain
    socket.create_connection call (exactly what a reachability check in the
    verification environment would do) proves the returned preview_url is
    genuinely connectable, unlike the old container-name DNS name which
    never was."""
    import socket as _socket

    feature_branch = "dev/1234-feature"
    repo = _setup_pre_prod_repo(tmp_path, feature_branch)
    env = _env()
    config = _self_hosted_config()

    server = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    host, port = server.getsockname()

    def _fake_run(args, **kwargs):
        import subprocess as _subprocess

        if args[:2] == ["docker", "port"]:
            return _subprocess.CompletedProcess(args, 0, stdout=f"{host}:{port}\n", stderr="")
        if args[:3] == ["docker", "network", "inspect"]:
            return _subprocess.CompletedProcess(args, 0, stdout=f"{host}\n", stderr="")
        if args[:2] == ["docker", "exec"]:
            return _subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        return _subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    try:
        monkeypatch.setattr(git_ops, "pull_latest", lambda repo, branch: None)
        monkeypatch.setattr(git_ops, "push_branch", lambda repo, branch: None)
        monkeypatch.setattr(environments, "load_self_hosted_vm_config", lambda repo: config)
        monkeypatch.setattr(deployment.subprocess, "run", _fake_run)
        monkeypatch.setattr(deployment.asyncio, "sleep", _fake_sleep)
        monkeypatch.setattr(deployment, "_own_container_name", lambda: "widget-app-blue")

        result = asyncio.run(deployment.deploy_pre_prod_self_hosted(repo, env, feature_branch, "run123"))

        assert result.ok is True
        preview_url = result.json_data["preview_url"]
        assert preview_url == f"http://{host}:{port}"

        parsed_host, parsed_port = preview_url.removeprefix("http://").split(":")
        conn = _socket.create_connection((parsed_host, int(parsed_port)), timeout=2)
        conn.close()
    finally:
        server.close()


def test_deploy_pre_prod_self_hosted_fails_cleanly_when_the_published_port_cannot_be_determined(tmp_path, monkeypatch):
    feature_branch = "dev/1234-feature"
    repo = _setup_pre_prod_repo(tmp_path, feature_branch)
    env = _env()
    config = _self_hosted_config()

    docker_calls = []
    monkeypatch.setattr(git_ops, "pull_latest", lambda repo, branch: None)
    monkeypatch.setattr(git_ops, "push_branch", lambda repo, branch: None)
    monkeypatch.setattr(environments, "load_self_hosted_vm_config", lambda repo: config)
    monkeypatch.setattr(deployment.subprocess, "run", _fake_docker_run(docker_calls, port_ok=False))
    monkeypatch.setattr(deployment.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(deployment, "_own_container_name", lambda: "widget-app-blue")

    result = asyncio.run(deployment.deploy_pre_prod_self_hosted(repo, env, feature_branch, "run123"))

    assert result.ok is False
    assert result.json_data["preview_url"] is None
    # Never got as far as the health check -- no reachable URL to check yet.
    assert not any(c[:2] == ["docker", "exec"] for c in docker_calls)


def test_deploy_pre_prod_self_hosted_fails_cleanly_when_the_app_network_gateway_cannot_be_determined(tmp_path, monkeypatch):
    feature_branch = "dev/1234-feature"
    repo = _setup_pre_prod_repo(tmp_path, feature_branch)
    env = _env()
    config = _self_hosted_config()

    docker_calls = []
    monkeypatch.setattr(git_ops, "pull_latest", lambda repo, branch: None)
    monkeypatch.setattr(git_ops, "push_branch", lambda repo, branch: None)
    monkeypatch.setattr(environments, "load_self_hosted_vm_config", lambda repo: config)
    monkeypatch.setattr(deployment.subprocess, "run", _fake_docker_run(docker_calls, gateway_ok=False))
    monkeypatch.setattr(deployment.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(deployment, "_own_container_name", lambda: "widget-app-blue")

    result = asyncio.run(deployment.deploy_pre_prod_self_hosted(repo, env, feature_branch, "run123"))

    assert result.ok is False
    assert result.json_data["preview_url"] is None


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


def test_deploy_pre_prod_self_hosted_refuses_with_an_infra_diagnosis_when_the_disk_is_full(tmp_path, monkeypatch):
    """GitHub #134: a full docker root fails `docker build` deep in a base-layer extract
    with a cryptic buildkit error, and no retry fixes it -- so refuse up front with a
    clear infrastructure message and never start the build."""
    feature_branch = "dev/1234-feature"
    repo = _setup_pre_prod_repo(tmp_path, feature_branch)
    env = _env()

    docker_calls = []
    monkeypatch.setattr(git_ops, "pull_latest", lambda repo, branch: None)
    monkeypatch.setattr(git_ops, "push_branch", lambda repo, branch: None)
    monkeypatch.setattr(environments, "load_self_hosted_vm_config", lambda repo: _self_hosted_config())
    monkeypatch.setattr(deployment.subprocess, "run", _fake_docker_run(docker_calls))
    monkeypatch.setattr(deployment, "_docker_root_free_bytes", lambda: 400 * 1024**2)  # 400 MiB
    monkeypatch.setattr(deployment.asyncio, "sleep", _fake_sleep)

    result = asyncio.run(deployment.deploy_pre_prod_self_hosted(repo, env, feature_branch, "run123"))

    assert result.ok is False
    assert "out of disk" in result.text.lower()
    assert not any(c[:2] == ["docker", "build"] for c in docker_calls)


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
    assert result.json_data["preview_url"] == f"http://{_FAKE_GATEWAY_IP}:{_FAKE_HOST_PORT}"


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


def test_deploy_pre_prod_self_hosted_falls_back_to_gateway_address_when_own_container_name_cannot_be_determined(
    tmp_path, monkeypatch
):
    """If this process's own container identity can't be resolved (HOSTNAME
    unset / docker inspect failure), that alone must not block the deploy --
    _select_preview_url falls back to the host-gateway address (GitHub issue
    #45's path), which doesn't depend on knowing this process's own container
    name at all. Only a *truly* unreachable instance (see the next test)
    should be reported as failed."""
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

    assert result.ok is True
    assert result.json_data["status"] == "deployed"
    assert result.json_data["preview_url"] == f"http://{_FAKE_GATEWAY_IP}:{_FAKE_HOST_PORT}"
    # No network connect was even attempted -- there's no container name to connect.
    assert not any(c[:3] == ["docker", "network", "connect"] for c in docker_calls)


def test_deploy_pre_prod_self_hosted_fails_clearly_when_no_reachable_address_can_be_found_at_all(tmp_path, monkeypatch):
    """When this process's own container can't be determined/joined AND the
    host-gateway fallback also can't be resolved (published port lookup
    fails), that's a genuinely unreachable instance -- refuse to deploy
    something nothing would be able to reach, with a clear, actionable
    reason, rather than a bare curl exit code surfacing later."""
    feature_branch = "dev/1234-feature"
    repo = _setup_pre_prod_repo(tmp_path, feature_branch)
    env = _env()

    docker_calls = []
    monkeypatch.setattr(git_ops, "pull_latest", lambda repo, branch: None)
    monkeypatch.setattr(git_ops, "push_branch", lambda repo, branch: None)
    monkeypatch.setattr(environments, "load_self_hosted_vm_config", lambda repo: _self_hosted_config())
    monkeypatch.setattr(deployment.subprocess, "run", _fake_docker_run(docker_calls, port_ok=False))
    monkeypatch.setattr(deployment.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(deployment, "_own_container_name", lambda: None)

    result = asyncio.run(deployment.deploy_pre_prod_self_hosted(repo, env, feature_branch, "run123"))

    assert result.ok is False
    assert result.json_data["status"] == "failed"
    assert result.json_data["preview_url"] is None
    assert "own container" in result.text.lower()
    assert "preprod_network" in result.text.lower() or "preprod-net" in result.text.lower()
    # Never got as far as the health check -- no reachable URL to check yet.
    assert not any(c[:2] == ["docker", "exec"] for c in docker_calls)


def test_deploy_pre_prod_self_hosted_prefers_the_docker_alias_address_when_own_container_is_confirmed_joined(
    tmp_path, monkeypatch
):
    """The actual fix this bug asked for: once this process's own container
    is confirmed (not just fire-and-forget-connected) joined to
    preprod_network, prefer the sibling's Docker-network alias/service name
    (a direct container-to-container path) over the host-only bridge-IP
    gateway address -- the gateway path is what produced the reported curl
    exit 28 (hairpin NAT back through a custom bridge's gateway is
    unreliable), while the alias path is a plain same-network DNS lookup."""
    feature_branch = "dev/1234-feature"
    repo = _setup_pre_prod_repo(tmp_path, feature_branch)
    env = _env()
    config = _self_hosted_config()

    docker_calls = []
    monkeypatch.setattr(git_ops, "pull_latest", lambda repo, branch: None)
    monkeypatch.setattr(git_ops, "push_branch", lambda repo, branch: None)
    monkeypatch.setattr(environments, "load_self_hosted_vm_config", lambda repo: config)
    monkeypatch.setattr(
        deployment.subprocess, "run",
        _fake_docker_run(docker_calls, preprod_network=config.preprod_network, own_container_joined_preprod=True),
    )
    monkeypatch.setattr(deployment.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(deployment, "_own_container_name", lambda: "widget-app-blue")

    result = asyncio.run(deployment.deploy_pre_prod_self_hosted(repo, env, feature_branch, "run123"))

    assert result.ok is True
    assert result.json_data["status"] == "deployed"
    assert result.json_data["preview_url"] == "http://widget-app-preprod-run123:8080"
    # No `docker network connect` was needed -- membership was already confirmed.
    assert not any(c[:3] == ["docker", "network", "connect"] for c in docker_calls)
    # Never fell back to the GitHub issue #45 gateway lookups -- unnecessary
    # once the alias path is confirmed reachable.
    assert not any(c[:2] == ["docker", "port"] for c in docker_calls)
    reachability_calls = [c for c in docker_calls if c[:1] == ["curl"]]
    assert reachability_calls == [["curl", "-sf", "-m", "5", "http://widget-app-preprod-run123:8080/health"]]


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
    assert result.json_data["preview_url"] == f"http://{_FAKE_GATEWAY_IP}:{_FAKE_HOST_PORT}"
    text_lower = result.text.lower()
    assert "reachable" in text_lower or "network" in text_lower or "dns" in text_lower
    # The reachability check (bare curl, no docker exec) did run -- retried up to
    # _HEALTH_CHECK_ATTEMPTS times, same as _wait_for_healthy -- and every attempt failed.
    reachability_calls = [c for c in docker_calls if c[:1] == ["curl"]]
    assert len(reachability_calls) == deployment._HEALTH_CHECK_ATTEMPTS
    assert all(c == ["curl", "-sf", "-m", "5", f"http://{_FAKE_GATEWAY_IP}:{_FAKE_HOST_PORT}/health"] for c in reachability_calls)


def _mute_own_container_id_fallbacks(monkeypatch):
    """Blanks out the /proc-derived detection candidates so tests can pin
    behavior for exactly one source at a time, deterministically -- without
    this, tests would depend on whatever /proc/self/cgroup and
    /proc/self/mountinfo happen to contain in whatever environment pytest
    itself runs in (e.g. inside agentra's own containers, where they'd
    actually resolve to a real container and make an unrelated fallback
    look like it succeeded)."""
    monkeypatch.setattr(deployment_network, "_own_container_id_from_cgroup", lambda: None)
    monkeypatch.setattr(deployment_network, "_own_container_id_from_mountinfo", lambda: None)


def test_own_container_name_resolves_via_docker_inspect_of_the_hostname_env_var(monkeypatch):
    """Unit-level pin for the resolution mechanism itself: HOSTNAME (Docker's
    default container hostname, its own short ID) is fed to `docker inspect`
    to recover the actual --name Docker assigned, e.g. 'agentra-blue'."""
    monkeypatch.setenv("HOSTNAME", "abc123def456")
    _mute_own_container_id_fallbacks(monkeypatch)
    calls = []

    def _fake_run(args, **kwargs):
        calls.append(list(args))
        import subprocess as _subprocess

        return _subprocess.CompletedProcess(args, 0, stdout="/agentra-blue\n", stderr="")

    monkeypatch.setattr(deployment.subprocess, "run", _fake_run)

    name = deployment._own_container_name()

    assert name == "agentra-blue"
    assert calls == [["docker", "inspect", "abc123def456", "--format", "{{.Name}}"]]


def test_own_container_name_falls_back_to_cgroup_or_mountinfo_when_hostname_is_stale_or_unset(monkeypatch):
    """The actual fix this bug asked for at the detection level: a stale or
    unset HOSTNAME must not be the single hardcoded source of truth --
    when `docker inspect` on the HOSTNAME candidate fails (or HOSTNAME is
    unset entirely), fall through to the ID the kernel itself records for
    this process's cgroup/mount namespace, which can't go stale the way an
    env var can."""
    monkeypatch.delenv("HOSTNAME", raising=False)
    monkeypatch.setattr(deployment_network, "_own_container_id_from_cgroup", lambda: None)
    monkeypatch.setattr(
        deployment_network, "_own_container_id_from_mountinfo",
        lambda: "9e2a3811ee92bba15de986ba4d64ec79a7ea9c916d83a8d98fcf959403e097d1",
    )
    calls = []

    def _fake_run(args, **kwargs):
        calls.append(list(args))
        import subprocess as _subprocess

        return _subprocess.CompletedProcess(args, 0, stdout="/agentra-green\n", stderr="")

    monkeypatch.setattr(deployment.subprocess, "run", _fake_run)

    name = deployment._own_container_name()

    assert name == "agentra-green"
    assert calls == [[
        "docker", "inspect",
        "9e2a3811ee92bba15de986ba4d64ec79a7ea9c916d83a8d98fcf959403e097d1",
        "--format", "{{.Name}}",
    ]]


def test_own_container_name_skips_a_stale_hostname_that_docker_inspect_rejects(monkeypatch):
    """If HOSTNAME resolves to something `docker inspect` no longer
    recognizes (e.g. stale/overridden), that single failed lookup must not
    be treated as final -- the next candidate should still be tried."""
    monkeypatch.setenv("HOSTNAME", "stale000000")
    monkeypatch.setattr(deployment_network, "_own_container_id_from_cgroup", lambda: None)
    monkeypatch.setattr(
        deployment_network, "_own_container_id_from_mountinfo",
        lambda: "fedcba9876543210fedcba9876543210fedcba9876543210fedcba98765432",
    )
    calls = []

    def _fake_run(args, **kwargs):
        calls.append(list(args))
        import subprocess as _subprocess

        if args[2] == "stale000000":
            return _subprocess.CompletedProcess(args, 1, stdout="", stderr="Error: No such object: stale000000")
        return _subprocess.CompletedProcess(args, 0, stdout="/agentra-blue\n", stderr="")

    monkeypatch.setattr(deployment.subprocess, "run", _fake_run)

    name = deployment._own_container_name()

    assert name == "agentra-blue"
    assert [c[2] for c in calls] == [
        "stale000000", "fedcba9876543210fedcba9876543210fedcba9876543210fedcba98765432",
    ]


def test_own_container_name_is_none_when_every_detection_method_is_exhausted(monkeypatch):
    monkeypatch.delenv("HOSTNAME", raising=False)
    _mute_own_container_id_fallbacks(monkeypatch)

    assert deployment._own_container_name() is None


def test_own_container_id_from_cgroup_extracts_the_id_from_a_cgroup_v1_style_path(monkeypatch):
    fake_id = "a" * 64
    monkeypatch.setattr(
        deployment.Path, "read_text",
        lambda self: f"12:pids:/docker/{fake_id}\n11:cpu,cpuacct:/docker/{fake_id}\n" if str(self) == "/proc/self/cgroup" else "",
    )

    assert deployment._own_container_id_from_cgroup() == fake_id


def test_own_container_id_from_cgroup_is_none_for_a_bare_cgroup_v2_root(monkeypatch):
    """The actual reported-bug scenario: a cgroup v2 host with no per-
    container cgroup path at all -- must fall through cleanly, not raise or
    misparse."""
    monkeypatch.setattr(
        deployment.Path, "read_text", lambda self: "0::/\n" if str(self) == "/proc/self/cgroup" else "",
    )

    assert deployment._own_container_id_from_cgroup() is None


def test_own_container_id_from_mountinfo_extracts_the_id_from_the_containers_path(monkeypatch):
    fake_id = "b" * 64
    line = (
        f"645 635 8:1 /var/lib/docker/containers/{fake_id}/resolv.conf "
        "/etc/resolv.conf rw,nosuid,nodev,relatime - ext4 /dev/sda1 rw\n"
    )
    monkeypatch.setattr(
        deployment.Path, "read_text", lambda self: line if str(self) == "/proc/self/mountinfo" else "",
    )

    assert deployment._own_container_id_from_mountinfo() == fake_id


def test_own_container_id_from_mountinfo_is_none_without_a_matching_containers_path(monkeypatch):
    monkeypatch.setattr(
        deployment.Path, "read_text",
        lambda self: "645 635 8:1 / / rw - overlay overlay rw\n" if str(self) == "/proc/self/mountinfo" else "",
    )

    assert deployment._own_container_id_from_mountinfo() is None


def test_own_container_id_candidates_falls_back_from_cgroup_v2_to_mountinfo(monkeypatch):
    """Pins the reason /proc/self/mountinfo exists as a candidate at all:
    cgroup v2 hosts (this repo's own reported bug environment) often report
    a cgroup path with no container ID embedded in it at all (e.g. a bare
    '0::/'), so cgroup-based detection alone isn't enough."""
    monkeypatch.delenv("HOSTNAME", raising=False)
    monkeypatch.setattr(deployment_network, "_own_container_id_from_cgroup", lambda: None)
    monkeypatch.setattr(deployment_network, "_own_container_id_from_mountinfo", lambda: "1" * 64)

    assert deployment._own_container_id_candidates() == ["1" * 64]


def test_ensure_network_joined_returns_true_without_connecting_when_already_a_member(monkeypatch):
    calls = []

    def _fake_run(args, **kwargs):
        calls.append(list(args))
        import subprocess as _subprocess
        import json as _json

        return _subprocess.CompletedProcess(args, 0, stdout=_json.dumps({"widget-preprod-net": {}}), stderr="")

    monkeypatch.setattr(deployment.subprocess, "run", _fake_run)

    assert deployment._ensure_network_joined("widget-preprod-net", "widget-app-blue") is True
    # Only the membership check -- no redundant `docker network connect` call.
    assert not any(c[:3] == ["docker", "network", "connect"] for c in calls)


def test_ensure_network_joined_connects_then_verifies_membership(monkeypatch):
    state = {"joined": False}

    def _fake_run(args, **kwargs):
        import subprocess as _subprocess
        import json as _json

        if args[:3] == ["docker", "network", "connect"]:
            state["joined"] = True
            return _subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        networks = {"widget-preprod-net": {}} if state["joined"] else {}
        return _subprocess.CompletedProcess(args, 0, stdout=_json.dumps(networks), stderr="")

    monkeypatch.setattr(deployment.subprocess, "run", _fake_run)

    assert deployment._ensure_network_joined("widget-preprod-net", "widget-app-blue") is True


def test_ensure_network_joined_returns_false_when_connect_exits_zero_but_membership_never_sticks(monkeypatch):
    """The actual fix this bug asked for at the join level: a `docker
    network connect` call that exits 0 must not be trusted blindly --
    membership is verified by re-inspecting, and a silent no-op connect
    (the reported bug's actual failure mode) must be reported as not
    joined, not papered over."""

    def _fake_run(args, **kwargs):
        import subprocess as _subprocess
        import json as _json

        if args[:3] == ["docker", "network", "connect"]:
            return _subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        return _subprocess.CompletedProcess(args, 0, stdout=_json.dumps({}), stderr="")

    monkeypatch.setattr(deployment.subprocess, "run", _fake_run)

    assert deployment._ensure_network_joined("widget-preprod-net", "widget-app-blue") is False


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


def test_promote_prod_self_hosted_flips_nginx_to_the_inactive_color_and_reports_teardown_info_without_deferring_it_inline(tmp_path, monkeypatch):
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

    # GitHub issue #92: only the new container is ever launched inline here --
    # no sibling teardown container gets started as a side effect of this call
    # anymore (that used to race the caller's own status write when enough
    # post-promotion bookkeeping made it take longer than the sibling's fixed
    # delay). promote_prod_self_hosted now only *reports* what a caller needs
    # to schedule that teardown later, via result.json_data["teardown"].
    run_calls = [c for c in docker_calls if c[:2] == ["docker", "run"]]
    assert len(run_calls) == 1
    new_container_run = run_calls[0]
    assert "widget-app-green" in new_container_run
    assert "widget-app-net" in new_container_run
    assert "/mnt/disks/widget-data/claude:/home/agentuser/.claude" in new_container_run
    assert "/mnt/disks/widget-data/agentra-home:/home/agentuser/.agentra" in new_container_run
    assert "/mnt/disks/widget-data/repos:/workspace" in new_container_run
    assert "/var/run/docker.sock:/var/run/docker.sock" in new_container_run

    reload_calls = [c for c in docker_calls if "nginx -s reload" in " ".join(c)]
    assert len(reload_calls) == 1

    # The old (currently-live, possibly self) container/image must not be torn
    # down synchronously in this process (GitHub issue #83), nor scheduled via
    # a sibling container synchronously either (GitHub issue #92) -- doing
    # either here would race/kill the caller's own status write.
    remove_calls = [c for c in docker_calls if c[:3] == ["docker", "rm", "-f"]]
    assert ["docker", "rm", "-f", "widget-app-blue"] not in remove_calls
    rmi_calls = [c for c in docker_calls if c[:2] == ["docker", "rmi"]]
    assert ["docker", "rmi", "widget-app:oldrun123"] not in rmi_calls

    teardown = result.json_data["teardown"]
    assert teardown["cleanup_image"] == "widget-app:run456"
    assert teardown["old_container"] == "widget-app-blue"
    assert teardown["old_image"] == "widget-app:oldrun123"
    assert teardown["sock_gid"] == "999"


def test_trigger_deferred_teardown_launches_the_sibling_from_promote_prod_self_hosteds_reported_info(monkeypatch):
    """GitHub issue #92: the sibling-launching side effect promote_prod_self_hosted used to
    perform inline now lives entirely in trigger_deferred_teardown, driven off the json_data
    it hands back -- called separately, and only once a caller's own status write has landed."""
    docker_calls = []
    monkeypatch.setattr(
        deployment.subprocess, "run",
        lambda args, **kwargs: docker_calls.append(list(args)) or subprocess.CompletedProcess(args, 0, stdout="", stderr=""),
    )

    deployment.trigger_deferred_teardown({
        "status": "deployed",
        "teardown": {
            "cleanup_image": "widget-app:run456",
            "old_container": "widget-app-blue",
            "old_image": "widget-app:oldrun123",
            "sock_gid": "999",
            "delay_seconds": 5,
        },
    })

    assert len(docker_calls) == 1
    call = docker_calls[0]
    assert call[:3] == ["docker", "run", "-d"]
    script = call[-1]
    assert "docker rm -f widget-app-blue" in script
    assert "docker rmi widget-app:oldrun123" in script


def test_trigger_deferred_teardown_is_a_noop_without_teardown_info(monkeypatch):
    """Covers both a non-self-hosted deploy strategy's json_data (no 'teardown' key at all)
    and a failed/no-op promotion (json_data=None) -- neither should ever launch a sibling."""
    docker_calls = []
    monkeypatch.setattr(
        deployment.subprocess, "run",
        lambda args, **kwargs: docker_calls.append(list(args)) or subprocess.CompletedProcess(args, 0, stdout="", stderr=""),
    )

    deployment.trigger_deferred_teardown(None)
    deployment.trigger_deferred_teardown({"status": "failed"})
    deployment.trigger_deferred_teardown({"status": "deployed", "notes": "vercel_firebase, no teardown info"})

    assert docker_calls == []


def test_defer_old_container_removal_launches_a_detached_sibling_that_removes_container_and_image(monkeypatch):
    docker_calls = []
    monkeypatch.setattr(
        deployment.subprocess, "run",
        lambda args, **kwargs: docker_calls.append(list(args)) or subprocess.CompletedProcess(args, 0, stdout="", stderr=""),
    )

    deployment._defer_old_container_removal("widget-app:new123", "widget-app-blue", "widget-app:oldrun123", "999")

    assert len(docker_calls) == 1
    call = docker_calls[0]
    assert call[:3] == ["docker", "run", "-d"]
    assert "--rm" in call
    assert "widget-app:new123" in call  # the sibling runs off the already-local, just-verified image
    assert "--group-add" in call and "999" in call
    script = call[-1]
    assert "sleep" in script
    assert "docker rm -f widget-app-blue" in script
    assert "docker rmi widget-app:oldrun123" in script
    # docker rm must come before docker rmi -- the container referencing the
    # image has to be gone before the image itself can be removed.
    assert script.index("docker rm -f widget-app-blue") < script.index("docker rmi widget-app:oldrun123")


def test_defer_old_container_removal_skips_rmi_when_old_image_is_unknown(monkeypatch):
    docker_calls = []
    monkeypatch.setattr(
        deployment.subprocess, "run",
        lambda args, **kwargs: docker_calls.append(list(args)) or subprocess.CompletedProcess(args, 0, stdout="", stderr=""),
    )

    deployment._defer_old_container_removal("widget-app:new123", "widget-app-blue", None, "")

    script = docker_calls[0][-1]
    assert "docker rm -f widget-app-blue" in script
    assert "docker rmi" not in script
    assert "--group-add" not in docker_calls[0]


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


# -- GitHub issue #117: pre-prod health check ------------------------------------


def test_wait_for_healthy_bails_early_when_the_container_exited(monkeypatch):
    """A sibling that crashes during startup should report that (with its logs),
    not silently burn the full timeout window."""
    sleeps = []

    def _run(args, **kwargs):
        if args[:2] == ["docker", "exec"]:
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="curl: connection refused")
        if args[:3] == ["docker", "inspect", "-f"]:
            return subprocess.CompletedProcess(args, 0, stdout="false\n", stderr="")
        if args[:2] == ["docker", "logs"]:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="ModuleNotFoundError: no module named 'x'")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    async def _no_sleep(*a, **k):
        sleeps.append(1)

    monkeypatch.setattr(deployment.subprocess, "run", _run)
    monkeypatch.setattr(deployment.asyncio, "sleep", _no_sleep)

    healthy, detail = asyncio.run(deployment._wait_for_healthy("widget-preprod-abc"))

    assert healthy is False
    assert "exited during startup" in detail
    assert "ModuleNotFoundError" in detail
    assert sleeps == []  # bailed on the first attempt, never slept the window out


def test_wait_for_healthy_returns_true_once_health_answers(monkeypatch):
    def _run(args, **kwargs):
        if args[:2] == ["docker", "exec"]:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(deployment.subprocess, "run", _run)
    monkeypatch.setattr(deployment.asyncio, "sleep", _fake_sleep)

    healthy, detail = asyncio.run(deployment._wait_for_healthy("widget-preprod-abc"))
    assert healthy is True
    assert detail == ""

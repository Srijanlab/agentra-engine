"""Deployment Agent, split across the environment pipeline."""

import asyncio
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from agentra.agents.base import AgentResult, run_agent
from agentra.agents.deployment_network import (
    _container_networks,  # noqa: F401 -- re-exported for tests/other callers
    _ensure_network_joined,
    _own_container_id_candidates,  # noqa: F401 -- re-exported for tests
    _own_container_id_from_cgroup,  # noqa: F401 -- re-exported for tests
    _own_container_id_from_mountinfo,  # noqa: F401 -- re-exported for tests
    _own_container_name,
)
from agentra.environments import EnvironmentConfig

PRE_PROD_SYSTEM_PROMPT = """You are the Deployment Agent, deploying to the \
PRE-PROD environment only. You must never touch production.

{feature_branch} has already been merged into {pre_prod_branch} and pushed \
for you -- you are currently on {pre_prod_branch} with that merge in place. \
Do not touch git history or switch branches; just deploy.

Environment for this deploy:
- Vercel configured: {vercel}
- Firebase configured: {firebase} (pre-prod alias: {firebase_pre_prod_alias})

Steps:
0. If deploying requires a decision outside your remit as a deterministic deploy agent -- \
   e.g. an ambiguous target environment/alias with no clear default, or a Vercel/Firebase \
   configuration that looks broken in a way pointing at a real infra/credential/environment \
   decision rather than a transient, retryable tool error -- do not guess and do not proceed. \
   Report status HUMAN_INPUT_REQUIRED instead.
1. If Vercel is configured, run a preview deploy (`vercel deploy`, never \
   `--prod`) and capture the resulting preview URL.
2. If Firebase is configured, switch to the pre-prod alias \
   (`firebase use {firebase_pre_prod_alias}`) and deploy functions \
   (`firebase deploy --only functions`). Never switch to any other alias.
3. Run any available smoke test against the preview URL.
4. If neither Vercel nor Firebase is configured, report status "skipped" \
   and do nothing else.

End your response with a fenced ```json block shaped like:
{{
  "status": "deployed" | "skipped" | "failed" | "HUMAN_INPUT_REQUIRED",
  "preview_url": "...",
  "firebase_pre_prod_deployed": true | false,
  "notes": "...",
  "reason": "... (only when status is HUMAN_INPUT_REQUIRED)",
  "question": "... (only when status is HUMAN_INPUT_REQUIRED)",
  "options": ["..."]
}}
"""

# Used instead of PRE_PROD_SYSTEM_PROMPT when EnvironmentConfig.ci_cd_on_push is True --
PRE_PROD_CI_CD_SYSTEM_PROMPT = """You are the Deployment Agent, verifying the PRE-PROD \
deploy for this app. You must never touch production.

{feature_branch} has already been merged into {pre_prod_branch} and pushed for you -- \
that push just triggered this app's own CI/CD (GitHub Actions, Vercel's git integration, \
etc.), which handles the actual deploy. Do NOT run `vercel deploy` or `firebase deploy` \
yourself -- that would be redundant with, and could conflict with, the pipeline the push \
already kicked off.

Steps:
0. If deploying requires a decision outside your remit as a deterministic deploy agent -- \
   e.g. an ambiguous target environment/alias with no clear default, or a Vercel/Firebase \
   configuration that looks broken in a way pointing at a real infra/credential/environment \
   decision rather than a transient, retryable tool error -- do not guess and do not proceed. \
   Report status HUMAN_INPUT_REQUIRED instead.
1. If the `gh` CLI is available, check the status of the CI run this push triggered \
   (e.g. `gh run list --branch {pre_prod_branch} --limit 1`, then `gh run watch <id>` if \
   still in progress) and report what it says.
2. If `gh` isn't available or you can't determine the run's outcome, report status \
   "deployed" anyway with a note that verification wasn't possible -- the push itself is \
   the trigger, and pipeline failures are the app owner's to monitor separately.
3. Do not attempt to discover or guess a preview URL if the pipeline doesn't surface one \
   directly -- leave preview_url null rather than fabricating one.

End your response with a fenced ```json block shaped like:
{{
  "status": "deployed" | "failed" | "HUMAN_INPUT_REQUIRED",
  "preview_url": "..." | null,
  "firebase_pre_prod_deployed": true | false,
  "notes": "...",
  "reason": "... (only when status is HUMAN_INPUT_REQUIRED)",
  "question": "... (only when status is HUMAN_INPUT_REQUIRED)",
  "options": ["..."]
}}
"""

PROD_SYSTEM_PROMPT = """You are the Deployment Agent, promoting a change to \
PRODUCTION. This call has been explicitly authorized — either by a human \
running `agentra promote`, or by the Production Debugging Agent's opted-in \
auto-remediate path after the fix passed pre-prod testing.

{pre_prod_branch} has already been merged into {prod_branch} and pushed for \
you -- you are currently on {prod_branch} with that merge in place. Do not \
touch git history or switch branches; just deploy. Do exactly this and \
nothing more:

Environment for this promotion:
- Vercel configured: {vercel}
- Firebase configured: {firebase} (prod alias: {firebase_prod_alias})

Steps:
1. If Vercel is configured, run `vercel deploy --prod`.
2. If Firebase is configured, switch to the prod alias \
   (`firebase use {firebase_prod_alias}`) and deploy functions \
   (`firebase deploy --only functions`).
3. Run any available smoke test against the production URL.
4. Report clearly if anything failed — do not retry destructive steps.

End your response with a fenced ```json block shaped like:
{{
  "status": "deployed" | "failed",
  "prod_url": "...",
  "notes": "..."
}}
"""

# Used instead of PROD_SYSTEM_PROMPT when EnvironmentConfig.ci_cd_on_push is True -- same
PROD_CI_CD_SYSTEM_PROMPT = """You are the Deployment Agent, verifying a PRODUCTION \
promotion. This call has been explicitly authorized — either by a human running \
`agentra promote`, or by the Production Debugging Agent's opted-in auto-remediate path \
after the fix passed pre-prod testing.

{pre_prod_branch} has already been merged into {prod_branch} and pushed for you -- that \
push just triggered this app's own CI/CD, which handles the actual production deploy. Do \
NOT run `vercel deploy --prod` or `firebase deploy` yourself -- redundant with, and could \
conflict with, the pipeline the push already kicked off.

Steps:
1. If the `gh` CLI is available, check the status of the CI run this push triggered \
   (e.g. `gh run list --branch {prod_branch} --limit 1`, then `gh run watch <id>` if \
   still in progress) and report what it says.
2. If `gh` isn't available or you can't determine the run's outcome, report status \
   "deployed" anyway with a note that verification wasn't possible.
3. Report clearly if the CI run itself failed — do not retry or attempt any workaround.

End your response with a fenced ```json block shaped like:
{{
  "status": "deployed" | "failed",
  "prod_url": "..." | null,
  "notes": "..."
}}
"""


def _sync_branch_to_remote(repo: Path, branch: str) -> None:
    """Make local `branch` exist and exactly match origin/`branch`."""
    from agentra.agents.git_ops import pull_latest

    pull_latest(repo, branch)


# Paths under here are Memory's own regenerated notes (understand_codebase's
_AUTO_RESOLVE_OURS_PREFIX = ".agentra/memory/"


def _merge_and_push(repo: Path, source_ref: str, target_branch: str) -> str | None:
    """Merge source_ref into target_branch (already synced to its remote tip) and push."""
    from agentra.agents.git_ops import GitOpError, push_branch

    merge = subprocess.run(
        ["git", "-C", str(repo), "merge", "--no-edit", source_ref], capture_output=True, text=True,
    )
    if merge.returncode != 0:
        conflicts = subprocess.run(
            ["git", "-C", str(repo), "diff", "--name-only", "--diff-filter=U"],
            capture_output=True, text=True,
        ).stdout.split()
        if conflicts and all(path.startswith(_AUTO_RESOLVE_OURS_PREFIX) for path in conflicts):
            checkout = subprocess.run(
                ["git", "-C", str(repo), "checkout", source_ref, "--", *conflicts],
                capture_output=True, text=True,
            )
            add = subprocess.run(["git", "-C", str(repo), "add", *conflicts], capture_output=True, text=True)
            commit = subprocess.run(
                ["git", "-C", str(repo), "commit", "--no-edit"], capture_output=True, text=True,
            )
            if checkout.returncode == 0 and add.returncode == 0 and commit.returncode == 0:
                try:
                    push_branch(repo, target_branch)
                except GitOpError as exc:
                    return f"Push of {target_branch!r} failed: {exc}"
                return None
            # Auto-resolve itself failed unexpectedly -- fall through to the
        subprocess.run(["git", "-C", str(repo), "merge", "--abort"], capture_output=True, text=True)
        # git writes the actually-useful diagnostic ("CONFLICT (content):
        detail = "\n".join(part for part in (merge.stdout.strip(), merge.stderr.strip()) if part)
        return f"Merge of {source_ref!r} into {target_branch!r} failed, aborted: {detail}"
    try:
        push_branch(repo, target_branch)
    except GitOpError as exc:
        return f"Push of {target_branch!r} failed: {exc}"
    return None


def persist_audit_trail(repo: Path, branch: str) -> str | None:
    """Commit and push any dirty .agentra/ bookkeeping (released.json, memory/*, feedback_sync_state.json, codebase_spec_commit.json) onto `branch`."""
    from agentra.agents.git_ops import GitOpError, commit_and_push, pull_latest

    status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain", "--", ".agentra/"],
        capture_output=True, text=True,
    )
    if not status.stdout.strip():
        return None

    try:
        subprocess.run(["git", "-C", str(repo), "add", ".agentra/"], check=True, capture_output=True, text=True)
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-m", "agentra: capture audit trail (pre-sync)"],
            check=True, capture_output=True, text=True,
        )
        source_sha = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True,
        ).stdout.strip()
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr if isinstance(exc.stderr, str) else exc.stderr.decode(errors="replace")
        return f"Failed to persist audit trail to {branch!r}: could not capture .agentra/ changes: {stderr}"

    try:
        pull_latest(repo, branch)
    except GitOpError as exc:
        return f"Failed to persist audit trail to {branch!r}: {exc}"

    try:
        subprocess.run(
            ["git", "-C", str(repo), "checkout", source_sha, "--", ".agentra/"],
            check=True, capture_output=True, text=True,
        )
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr if isinstance(exc.stderr, str) else exc.stderr.decode(errors="replace")
        return f"Failed to persist audit trail to {branch!r}: could not restore captured .agentra/ state onto {branch!r}: {stderr}"

    try:
        commit_and_push(repo, branch, "agentra: persist audit trail (shipped/backlog/memory)", [".agentra/"])
    except GitOpError as exc:
        return f"Failed to persist audit trail to {branch!r}: {exc}"
    return None


async def merge_to_pre_prod_only(repo: Path, env: EnvironmentConfig, feature_branch: str) -> AgentResult:
    """Lands feature_branch on pre_prod_branch without spinning up any deploy or live verification -- the path agents/brain/tools.py's deploy_pre_prod tool takes when change_risk.classify_change() says TRIVIAL (a test fix, docs edit, rename, or a couple-line bug fix)."""
    _sync_branch_to_remote(repo, env.pre_prod_branch)
    error = _merge_and_push(repo, feature_branch, env.pre_prod_branch)
    if error:
        return AgentResult(ok=False, text=error, json_data=None, cost_usd=0.0, turns=0)
    return AgentResult(
        ok=True,
        text=(
            "Trivial change (test/docs/config-only or a very small diff) merged to "
            f"{env.pre_prod_branch!r} without a full pre-prod deploy or live verification -- "
            "the passing local test suite is sufficient proof for a change this size."
        ),
        json_data={"status": "skipped_light", "preview_url": None},
        cost_usd=0.0, turns=0,
    )


async def deploy_pre_prod(
    repo: Path, env: EnvironmentConfig, feature_branch: str, session_id: str | None = None
) -> AgentResult:
    _sync_branch_to_remote(repo, env.pre_prod_branch)
    error = _merge_and_push(repo, feature_branch, env.pre_prod_branch)
    if error:
        return AgentResult(ok=False, text=error, json_data=None, cost_usd=0.0, turns=0)

    prompt = "Deploy the current pre-prod state, following your system prompt."
    if env.ci_cd_on_push:
        system_prompt = PRE_PROD_CI_CD_SYSTEM_PROMPT.format(
            feature_branch=feature_branch, pre_prod_branch=env.pre_prod_branch,
        )
    else:
        system_prompt = PRE_PROD_SYSTEM_PROMPT.format(
            feature_branch=feature_branch,
            pre_prod_branch=env.pre_prod_branch,
            vercel=env.vercel,
            firebase=env.firebase,
            firebase_pre_prod_alias=env.firebase_pre_prod_alias,
        )
    return await run_agent(
        prompt=prompt,
        system_prompt=system_prompt,
        cwd=repo,
        allowed_tools=["Read", "Bash", "Glob", "Grep"],
        permission_mode="bypassPermissions",
        max_turns=20,
        allow_prod=False,
        agent_label="Deployment Agent",
        resume=session_id,
    )


_HEALTH_CHECK_ATTEMPTS = 10
_HEALTH_CHECK_DELAY_SECONDS = 3
_NOT_CONFIGURED_TEXT = (
    "self_hosted_vm strategy selected but this repo has no "
    ".agentra/memory/architecture/deployment.md (or it's missing required fields) -- "
    "see environments.SelfHostedVMConfig for the required shape."
)


def _local_image_name(config: "environments.SelfHostedVMConfig") -> str:
    """Short, local-only name derived from the registry image path (e.g."""
    return config.image_repo.rstrip("/").rsplit("/", 1)[-1]


def _preprod_container_name(config: "environments.SelfHostedVMConfig", run_id: str) -> str:
    return f"{_local_image_name(config)}-preprod-{run_id}"


def _preprod_image_tag(config: "environments.SelfHostedVMConfig", run_id: str) -> str:
    return f"{_local_image_name(config)}-preprod:{run_id}"


_STALE_PREPROD_MAX_AGE_HOURS = 2.0


def cleanup_stale_preprod(config: "environments.SelfHostedVMConfig", max_age_hours: float = _STALE_PREPROD_MAX_AGE_HOURS) -> list[str]:
    """Sweeps any deploy_pre_prod_self_hosted sibling container/image left behind by a run that crashed, hit its cost cap, or was killed before verify_pre_prod's own single-shot teardown_self_hosted_preprod ran -- that teardown only fires on the happy path (see its own docstring), so nothing else ever reclaims a leaked sibling otherwise."""
    prefix = f"{_local_image_name(config)}-preprod-"
    removed: list[str] = []

    ps = subprocess.run(
        ["docker", "ps", "-a", "--filter", f"name=^{prefix}", "--format", "{{.Names}}"],
        capture_output=True, text=True,
    )
    live_names = {n for n in ps.stdout.splitlines() if n.strip()}
    for name in list(live_names):
        created = subprocess.run(
            ["docker", "inspect", name, "--format", "{{.Created}}"], capture_output=True, text=True,
        )
        if created.returncode != 0:
            continue
        try:
            # Docker's .Created is RFC3339 with nanosecond precision (e.g.
            raw = created.stdout.strip().rstrip("Z")
            if "." in raw:
                whole, frac = raw.split(".", 1)
                raw = f"{whole}.{frac[:6]}"
            created_at = datetime.fromisoformat(raw).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        age_hours = (datetime.now(timezone.utc) - created_at).total_seconds() / 3600
        if age_hours < max_age_hours:
            continue
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, text=True)
        removed.append(name)
        live_names.discard(name)

    images = subprocess.run(
        ["docker", "images", f"{_local_image_name(config)}-preprod", "--format", "{{.Repository}}:{{.Tag}}"],
        capture_output=True, text=True,
    )
    for image in (i for i in images.stdout.splitlines() if i.strip()):
        run_id = image.rsplit(":", 1)[-1]
        if f"{prefix}{run_id}" in live_names:
            continue  # still backing a live (non-stale) container
        subprocess.run(["docker", "rmi", image], capture_output=True, text=True)
        removed.append(image)

    return removed


def _docker_build_env() -> dict:
    """Env for every `docker build` subprocess call in this module -- explicitly enables BuildKit (DOCKER_BUILDKIT=1) rather than relying on whatever the legacy default happens to be on the build host: the legacy builder is deprecated and, unlike BuildKit, doesn't skip/cache unchanged layers as effectively, which matters directly on a disk-constrained long-lived host."""
    return {**os.environ, "DOCKER_BUILDKIT": "1"}


def _reclaim_build_disk_space() -> None:
    """Best-effort disk reclaim, run immediately before every `docker build` call in this module -- same non-fatal, swallow-everything pattern as cleanup_stale_preprod above, just aimed at generic build-cache/dangling- image bloat rather than leaked preprod siblings specifically."""
    subprocess.run(["docker", "builder", "prune", "-f"], capture_output=True, text=True)
    subprocess.run(["docker", "image", "prune", "-f"], capture_output=True, text=True)


def _inherit_env(container_name: str | None, keys: list[str]) -> list[str]:
    """`-e KEY=VALUE` docker-run args for just `keys`, copied from `container_name`'s own live env (its actual GITHUB_APP_ID/GITHUB_APP_PRIVATE_KEY, not a fresh secret fetch) -- same inheritance pattern promote_prod_self_hosted uses for the full env, just scoped to a named subset."""
    if not container_name:
        return []
    inspect = subprocess.run(
        ["docker", "inspect", container_name, "--format", "{{json .Config.Env}}"],
        capture_output=True, text=True,
    )
    if inspect.returncode != 0:
        return []
    try:
        entries = json.loads(inspect.stdout)
    except json.JSONDecodeError:
        return []
    wanted = set(keys)
    env_args = []
    for entry in entries:
        key, _, value = entry.partition("=")
        if key in wanted:
            env_args += ["-e", f"{key}={value}"]
    return env_args


async def deploy_pre_prod_self_hosted(
    repo: Path, env: EnvironmentConfig, feature_branch: str, run_id: str
) -> AgentResult:
    """Pre-prod deploy for the "self_hosted_vm" deploy_strategy -- a generic capability for any repo that runs on its own Docker-based VM rather than Vercel/Firebase, not agentra-specific code."""
    from agentra import environments

    config = environments.load_self_hosted_vm_config(repo)
    if config is None:
        return AgentResult(ok=False, text=_NOT_CONFIGURED_TEXT, json_data=None, cost_usd=0.0, turns=0)

    cleanup_stale_preprod(config)

    _sync_branch_to_remote(repo, env.pre_prod_branch)
    error = _merge_and_push(repo, feature_branch, env.pre_prod_branch)
    if error:
        return AgentResult(ok=False, text=error, json_data=None, cost_usd=0.0, turns=0)

    container_name = _preprod_container_name(config, run_id)
    image_tag = _preprod_image_tag(config, run_id)
    # A real HOST path, not this container's own -- see
    claude_home = f"{config.data_mount}/claude"

    _reclaim_build_disk_space()
    build = subprocess.run(
        ["docker", "build", "-t", image_tag, str(repo)], capture_output=True, text=True,
        env=_docker_build_env(),
    )
    if build.returncode != 0:
        return AgentResult(
            ok=False, text=f"docker build {image_tag} failed: {build.stderr.strip()}",
            json_data=None, cost_usd=0.0, turns=0,
        )

    # This process's OWN container -- not config.anchor_container (nginx) --
    # needs to be joined to preprod_network to reach the sibling by its
    # internal Docker-DNS name, the reliable path (direct container-to-
    # container over a shared bridge, not dependent on hairpin NAT back
    # through a bridge gateway, which is what actually produced the reported
    # curl exit 28 against a host-gateway address). own_container is
    # resolved fresh on every call (see _own_container_name) since
    # blue/green promotion changes which color is running this process, and
    # the join is verified rather than trusted (_ensure_network_joined) --
    # a detection/permissions failure here is no longer fatal on its own:
    # _select_preview_url below falls back to a host-gateway address when
    # the join can't be confirmed, so this degrades gracefully instead of
    # blocking every pre-prod deploy outright.
    subprocess.run(["docker", "network", "create", config.preprod_network], capture_output=True, text=True)
    own_container = _own_container_name()
    own_joined_preprod = bool(own_container) and _ensure_network_joined(config.preprod_network, own_container)

    subprocess.run(["docker", "rm", "-f", container_name], capture_output=True, text=True)
    env_args = []
    if config.firestore_project:
        env_args = ["-e", f"AGENTRA_FIRESTORE_PROJECT={config.firestore_project}"]
    # Without these, this dashboard has no GitHub App configured at all -- every pre-prod
    # deploy lands on the "Connect GitHub to get started" gate with zero registered-app data
    # to verify against, regardless of what the change under test actually touches (confirmed
    # live: a fully successful pre-prod deploy still showed the connect screen, not real data).
    # Read-only from this process's own live container, same trust boundary agentra's own
    # production containers already run under -- not a new secret fetch or a wider grant.
    env_args += _inherit_env(own_container, ["GITHUB_APP_ID", "GITHUB_APP_PRIVATE_KEY"])
    run = subprocess.run(
        [
            "docker", "run", "-d", "--name", container_name,
            "--network", config.preprod_network,
            # GitHub issue #45: publish 8080 to a Docker-assigned free host
            "-p", "8080",
            "--memory=1g", "--cpus=1",
            "-v", f"{claude_home}:/home/agentuser/.claude:ro",
            *env_args,
            image_tag, "serve", "--port", "8080",
        ],
        capture_output=True, text=True,
    )
    if run.returncode != 0:
        return AgentResult(
            ok=False, text=f"docker run {container_name} failed: {run.stderr.strip()}",
            json_data=None, cost_usd=0.0, turns=0,
        )

    preview_url, unreachable_reason = _select_preview_url(config, container_name, own_joined_preprod)
    if preview_url is None:
        return AgentResult(
            ok=False,
            text=f"{container_name} started but no reachable preview_url could be determined for it "
            f"({unreachable_reason}) -- nothing to hand to verify_pre_prod.",
            json_data={"status": "failed", "preview_url": None},
            cost_usd=0.0, turns=0,
        )
    healthy = await _wait_for_healthy(container_name)

    if not healthy:
        return AgentResult(
            ok=False,
            text=f"{container_name} did not become healthy on {preview_url}/health within "
            f"{_HEALTH_CHECK_ATTEMPTS * _HEALTH_CHECK_DELAY_SECONDS}s.",
            json_data={"status": "failed", "preview_url": preview_url},
            cost_usd=0.0, turns=0,
        )

    # _wait_for_healthy only proves the sibling's own process is up (it
    reachable, reach_detail = await _wait_for_reachable_from_orchestrator(preview_url)
    if not reachable:
        if own_joined_preprod:
            guidance = (
                f"this process's own container ({own_container!r}) IS confirmed joined to "
                f"{config.preprod_network!r} but still couldn't reach {container_name!r} over it -- "
                "check the sibling's own logs/health, and whether anything (host firewall, iptables, "
                "docker daemon config) is blocking inter-container traffic on that network."
            )
        else:
            guidance = (
                f"this process's own container ({(own_container or 'undetermined')!r}) "
                f"could not be confirmed joined to {config.preprod_network!r}, so this fell back to a "
                "host-gateway address -- check that the orchestrator container has access to the docker "
                "socket and permission to run `docker network connect`, and that "
                f"{config.preprod_network!r} exists."
            )
        return AgentResult(
            ok=False,
            text=(
                f"pre-prod instance not reachable from the orchestrator's own network path: "
                f"{reach_detail} (preview_url={preview_url!r}). {guidance}"
            ),
            json_data={"status": "failed", "preview_url": preview_url},
            cost_usd=0.0, turns=0,
        )

    return AgentResult(
        ok=True,
        text=f"Self-hosted pre-prod instance {container_name!r} is up at {preview_url} and reachable from the orchestrator.",
        json_data={"status": "deployed", "preview_url": preview_url, "notes": f"internal-only, image {image_tag!r}"},
        cost_usd=0.0, turns=0,
    )


def _select_preview_url(
    config: "environments.SelfHostedVMConfig", container_name: str, own_joined_preprod: bool,
) -> tuple[str | None, str | None]:
    """Picks the address to hand to verify_pre_prod. Prefers the sibling's
    Docker-network alias (its own --name, resolvable via preprod_network's
    embedded DNS) once this process's own container is confirmed joined to
    that network: a direct container-to-container path that doesn't depend
    on hairpin NAT back through a bridge gateway, which is unreliable on
    custom (`docker network create`) bridges and is exactly what produced
    the reported curl exit 28 against a host-gateway address. Falls back to
    the host-gateway address (_host_reachable_preview_url, GitHub issue #45)
    only when that join couldn't be confirmed. Returns (url, None) on
    success, or (None, reason) when neither address could be determined."""
    if own_joined_preprod:
        return f"http://{container_name}:8080", None
    gateway_url = _host_reachable_preview_url(config, container_name)
    if gateway_url:
        return gateway_url, None
    return None, (
        f"this process's own container could not be confirmed joined to {config.preprod_network!r}, and "
        f"its published port and/or {config.app_network!r} gateway address could not be determined either"
    )


def _host_reachable_preview_url(config: "environments.SelfHostedVMConfig", container_name: str) -> str | None:
    """Fallback preview_url when this process's own container couldn't be
    confirmed joined to config.preprod_network (see _select_preview_url).
    GitHub issue #45: preview_url used to be built as f"http://{container_name}:8080" -- a Docker embedded-DNS name that only resolves for containers actually joined to config.preprod_network."""
    port = subprocess.run(
        ["docker", "port", container_name, "8080/tcp"], capture_output=True, text=True,
    )
    if port.returncode != 0 or not port.stdout.strip():
        return None
    # "docker port" prints one "<host-ip>:<host-port>" line per bound host
    host_port = port.stdout.strip().splitlines()[0].rsplit(":", 1)[-1].strip()
    if not host_port.isdigit():
        return None

    gateway = subprocess.run(
        ["docker", "network", "inspect", config.app_network, "--format", "{{(index .IPAM.Config 0).Gateway}}"],
        capture_output=True, text=True,
    )
    host_address = gateway.stdout.strip() if gateway.returncode == 0 else ""
    if not host_address:
        return None
    return f"http://{host_address}:{host_port}"


async def _wait_for_healthy(container_name: str) -> bool:
    for _ in range(_HEALTH_CHECK_ATTEMPTS):
        check = subprocess.run(
            ["docker", "exec", container_name, "curl", "-sf", "http://localhost:8080/health"],
            capture_output=True, text=True,
        )
        if check.returncode == 0:
            return True
        await asyncio.sleep(_HEALTH_CHECK_DELAY_SECONDS)
    return False


async def _wait_for_reachable_from_orchestrator(preview_url: str) -> tuple[bool, str]:
    """Curls preview_url directly from THIS process -- no `docker exec` into the sibling -- the same network path (this process's own container's view of preprod_network) verify_pre_prod's Testing Agent turn will use."""
    last_error = "no attempt made"
    for _ in range(_HEALTH_CHECK_ATTEMPTS):
        check = subprocess.run(
            ["curl", "-sf", "-m", "5", f"{preview_url}/health"],
            capture_output=True, text=True,
        )
        if check.returncode == 0:
            return True, "reachable"
        last_error = check.stderr.strip() or f"curl exited {check.returncode}"
        await asyncio.sleep(_HEALTH_CHECK_DELAY_SECONDS)
    return False, last_error


def teardown_self_hosted_preprod(repo: Path, run_id: str) -> None:
    """Best-effort cleanup of a deploy_pre_prod_self_hosted sibling -- single-shot, ephemeral lifecycle, so nothing accumulates across features tested over time."""
    from agentra import environments

    config = environments.load_self_hosted_vm_config(repo)
    if config is None:
        return
    container_name = _preprod_container_name(config, run_id)
    image_tag = _preprod_image_tag(config, run_id)
    subprocess.run(["docker", "rm", "-f", container_name], capture_output=True, text=True)
    subprocess.run(["docker", "rmi", image_tag], capture_output=True, text=True)


def _candidate_image_tag(config: "environments.SelfHostedVMConfig", run_id: str) -> str:
    return f"{_local_image_name(config)}:{run_id}"


def _image_of(container_name: str) -> str | None:
    """The image reference (as originally passed to `docker run`, e.g."""
    inspect = subprocess.run(
        ["docker", "inspect", container_name, "--format", "{{.Config.Image}}"],
        capture_output=True, text=True,
    )
    return inspect.stdout.strip() if inspect.returncode == 0 and inspect.stdout.strip() else None


_OLD_CONTAINER_TEARDOWN_DELAY_SECONDS = 5


def _defer_old_container_removal(
    cleanup_image: str, old_container: str, old_image: str | None, sock_gid: str,
) -> None:
    """GitHub issue #83: promote_prod_self_hosted's caller runs inside old_container, so removing old_container in-process kills this Python process before it can return an AgentResult and the caller can durably record status="completed" in the registry. Schedules the removal from a short-lived sibling container instead (launched over the same bind-mounted docker.sock) -- a process outside old_container's own namespace, so it isn't killed along with it and survives to finish the teardown after a brief delay."""
    script = f"sleep {_OLD_CONTAINER_TEARDOWN_DELAY_SECONDS} && docker rm -f {old_container}"
    if old_image:
        script += f" && docker rmi {old_image}"
    subprocess.run(
        [
            "docker", "run", "-d", "--rm", "--network", "none",
            "-v", "/var/run/docker.sock:/var/run/docker.sock",
            *(["--group-add", sock_gid] if sock_gid else []),
            "--entrypoint", "sh",
            cleanup_image,
            "-c", script,
        ],
        capture_output=True, text=True,
    )


def _other_color(color: str) -> str:
    return "green" if color == "blue" else "blue"


def _nginx_conf(backend_container: str) -> str:
    """Full config.anchor_container nginx conf, proxying to backend_container (a docker DNS name on config.app_network) on port 8080."""
    return f"""server {{
    listen 8080;
    location / {{
        proxy_pass http://{backend_container}:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }}
}}
"""


async def promote_prod_self_hosted(repo: Path, env: EnvironmentConfig, run_id: str) -> AgentResult:
    """Production promotion for the "self_hosted_vm" deploy_strategy -- nginx-fronted blue/green instead of the in-place container replacement the manual VM redeploy process (docs/deployment.md) still uses."""
    from agentra import environments

    config = environments.load_self_hosted_vm_config(repo)
    if config is None:
        return AgentResult(ok=False, text=_NOT_CONFIGURED_TEXT, json_data=None, cost_usd=0.0, turns=0)

    _sync_branch_to_remote(repo, env.prod_branch)
    try:
        from agentra.agents.git_ops import GitOpError, fetch_ref

        fetch_ref(repo, env.pre_prod_branch)
    except GitOpError as exc:
        return AgentResult(ok=False, text=str(exc), json_data=None, cost_usd=0.0, turns=0)
    error = _merge_and_push(repo, f"origin/{env.pre_prod_branch}", env.prod_branch)
    if error:
        return AgentResult(ok=False, text=error, json_data=None, cost_usd=0.0, turns=0)

    active = subprocess.run(
        ["docker", "exec", config.anchor_container, "cat", "/etc/nginx/active_color"],
        capture_output=True, text=True,
    )
    current_color = active.stdout.strip() if active.returncode == 0 and active.stdout.strip() in ("blue", "green") else "blue"
    new_color = _other_color(current_color)

    base_name = _local_image_name(config)
    image_tag = _candidate_image_tag(config, run_id)
    new_container = f"{base_name}-{new_color}"

    _reclaim_build_disk_space()
    build = subprocess.run(
        ["docker", "build", "-t", image_tag, str(repo)], capture_output=True, text=True,
        env=_docker_build_env(),
    )
    if build.returncode != 0:
        return AgentResult(
            ok=False, text=f"docker build {image_tag} failed: {build.stderr.strip()}",
            json_data=None, cost_usd=0.0, turns=0,
        )

    # Inherit the currently-live container's actual environment (secrets
    old_container = f"{base_name}-{current_color}"
    inspect = subprocess.run(
        ["docker", "inspect", old_container, "--format", "{{json .Config.Env}}"],
        capture_output=True, text=True,
    )
    env_args = []
    if inspect.returncode == 0:
        try:
            for entry in json.loads(inspect.stdout):
                key, _, value = entry.partition("=")
                env_args += ["-e", f"{key}={value}"]
        except json.JSONDecodeError:
            pass  # best-effort -- the new container still boots, just without inherited env

    sock_gid = subprocess.run(
        ["stat", "-c", "%g", "/var/run/docker.sock"], capture_output=True, text=True,
    ).stdout.strip()

    subprocess.run(["docker", "rm", "-f", new_container], capture_output=True, text=True)
    run = subprocess.run(
        [
            "docker", "run", "-d", "--name", new_container, "--restart=always",
            "--network", config.app_network,
            "-v", f"{config.data_mount}/claude:/home/agentuser/.claude",
            "-v", f"{config.data_mount}/agentra-home:/home/agentuser/.agentra",
            "-v", f"{config.data_mount}/repos:/workspace",
            "-v", "/var/run/docker.sock:/var/run/docker.sock",
            *(["--group-add", sock_gid] if sock_gid else []),
            *env_args,
            image_tag, "serve", "--port", "8080",
        ],
        capture_output=True, text=True,
    )
    if run.returncode != 0:
        return AgentResult(
            ok=False, text=f"docker run {new_container} failed: {run.stderr.strip()}",
            json_data=None, cost_usd=0.0, turns=0,
        )

    if not await _wait_for_healthy(new_container):
        subprocess.run(["docker", "rm", "-f", new_container], capture_output=True, text=True)
        subprocess.run(["docker", "rmi", image_tag], capture_output=True, text=True)
        return AgentResult(
            ok=False,
            text=f"{new_container} did not become healthy -- promotion aborted, "
            f"{current_color} ({base_name}-{current_color}) is still live.",
            json_data={"status": "failed"},
            cost_usd=0.0, turns=0,
        )

    reload = subprocess.run(
        [
            "docker", "exec", "-i", config.anchor_container, "sh", "-c",
            f"cat > /etc/nginx/conf.d/default.conf && echo {new_color} > /etc/nginx/active_color && nginx -s reload",
        ],
        input=_nginx_conf(new_container), capture_output=True, text=True,
    )
    if reload.returncode != 0:
        subprocess.run(["docker", "rm", "-f", new_container], capture_output=True, text=True)
        subprocess.run(["docker", "rmi", image_tag], capture_output=True, text=True)
        return AgentResult(
            ok=False,
            text=f"nginx reload to {new_color} failed: {reload.stderr.strip()} -- promotion aborted, "
            f"{current_color} is still live.",
            json_data={"status": "failed"},
            cost_usd=0.0, turns=0,
        )

    # The outgoing color's image is never referenced again once its container
    # is gone. old_container may be this very process's own container (this
    # process runs *inside* the currently-live color) -- removing it in-process
    # would kill this process before it can return and the caller can record
    # status="completed" (GitHub issue #83), so it's deferred to a sibling
    # container instead of done synchronously here.
    old_image = _image_of(old_container)
    _defer_old_container_removal(image_tag, old_container, old_image, sock_gid)

    return AgentResult(
        ok=True,
        text=f"Promoted {new_container!r} to production (was {current_color}, now {new_color}).",
        json_data={"status": "deployed", "notes": f"blue/green flip: {current_color} -> {new_color}"},
        cost_usd=0.0, turns=0,
    )


async def promote_prod(repo: Path, env: EnvironmentConfig, session_id: str | None = None) -> AgentResult:
    from agentra.agents.git_ops import GitOpError, fetch_ref

    _sync_branch_to_remote(repo, env.prod_branch)
    try:
        # fetch_ref, not pull_latest: we need to stay on prod_branch (just
        fetch_ref(repo, env.pre_prod_branch)
    except GitOpError as exc:
        return AgentResult(ok=False, text=str(exc), json_data=None, cost_usd=0.0, turns=0)
    error = _merge_and_push(repo, f"origin/{env.pre_prod_branch}", env.prod_branch)
    if error:
        return AgentResult(ok=False, text=error, json_data=None, cost_usd=0.0, turns=0)

    prompt = "Promote the current production state, following your system prompt."
    if env.ci_cd_on_push:
        system_prompt = PROD_CI_CD_SYSTEM_PROMPT.format(
            pre_prod_branch=env.pre_prod_branch, prod_branch=env.prod_branch,
        )
    else:
        system_prompt = PROD_SYSTEM_PROMPT.format(
            pre_prod_branch=env.pre_prod_branch,
            prod_branch=env.prod_branch,
            vercel=env.vercel,
            firebase=env.firebase,
            firebase_prod_alias=env.firebase_prod_alias,
        )
    return await run_agent(
        prompt=prompt,
        system_prompt=system_prompt,
        cwd=repo,
        allowed_tools=["Read", "Bash", "Glob", "Grep"],
        permission_mode="bypassPermissions",
        max_turns=20,
        allow_prod=True,
        agent_label="Deployment Agent",
        resume=session_id,
    )


# ── Strategy registries: EnvironmentConfig.deploy_strategy -> implementation ──


async def _deploy_pre_prod_vercel_firebase(
    repo: Path, env: EnvironmentConfig, feature_branch: str, run_id: str, session_id: str | None
) -> AgentResult:
    return await deploy_pre_prod(repo, env, feature_branch, session_id=session_id)


async def _deploy_pre_prod_self_hosted_adapter(
    repo: Path, env: EnvironmentConfig, feature_branch: str, run_id: str, session_id: str | None
) -> AgentResult:
    return await deploy_pre_prod_self_hosted(repo, env, feature_branch, run_id)


PRE_PROD_STRATEGIES = {
    "vercel_firebase": _deploy_pre_prod_vercel_firebase,
    "self_hosted_vm": _deploy_pre_prod_self_hosted_adapter,
}


async def _promote_prod_vercel_firebase(
    repo: Path, env: EnvironmentConfig, run_id: str, session_id: str | None
) -> AgentResult:
    return await promote_prod(repo, env, session_id=session_id)


async def _promote_prod_self_hosted_adapter(
    repo: Path, env: EnvironmentConfig, run_id: str, session_id: str | None
) -> AgentResult:
    return await promote_prod_self_hosted(repo, env, run_id)


PROD_STRATEGIES = {
    "vercel_firebase": _promote_prod_vercel_firebase,
    "self_hosted_vm": _promote_prod_self_hosted_adapter,
}

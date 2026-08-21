"""Deployment Agent (vision.md 5.8), split across the environment pipeline.

deploy_pre_prod() is what every regular cycle calls: push to the pre-prod
branch, deploy the isolated Firebase pre-prod project + a Vercel preview,
smoke test. It never touches production.

deploy_pre_prod_self_hosted() is the equivalent for agentra's own repo
(EnvironmentConfig.self_hosted_vm), which has neither Vercel nor Firebase --
it builds and runs a sibling orchestrator container on the same VM instead,
reachable only over an internal Docker network. See its own docstring.

promote_prod() is the one place in the whole system that's allowed to touch
production, and only when called with allow_prod=True by a caller that has
already checked EnvironmentConfig.auto_remediate_prod (see
agents/prod_debug.py) or by a human running `agentra promote`.

The git merge+push in both is done here in plain Python, not left as prose
in the agent's system prompt -- same rationale as implementation.py's
deterministic checkout/commit: an LLM given "merge X into Y and push" as an
instruction is a plausible place for the exact same kind of silent
non-compliance observed there, and a skipped/wrong merge here is worse (it
would mean pre-prod or, in promote's case, PRODUCTION never actually gets
the change, while the agent still goes on to "deploy" whatever was already
checked out). The LLM agent is left with only the parts that genuinely need
its judgment: interpreting `vercel`/`firebase` CLI output, capturing preview
URLs, running smoke tests.
"""

import asyncio
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from agentra.agents.base import AgentResult, run_agent
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
# this app's own CI/CD (GitHub Actions, Vercel's git integration, etc.) already deploys on
# push to pre_prod_branch, which the merge above just triggered. Running `vercel deploy`/
# `firebase deploy` here too would be redundant at best and a racing/conflicting second
# deploy at worst -- so this agent only verifies, never deploys directly.
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
# rationale as PRE_PROD_CI_CD_SYSTEM_PROMPT above, applied to the prod promotion path.
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
    """Make local `branch` exist and exactly match origin/`branch`. This
    used to be its own hand-rolled fetch+checkout+reset (byte-for-byte the
    same shape as git_ops.pull_latest) with a plain, un-authed `_run` --
    meaning the fetch would 403 on any repo only reachable via the GitHub
    App connector, same bug class as the two pushes above. Delegates to
    the one already-fixed implementation instead of carrying a second,
    driftable copy of it."""
    from agentra.agents.git_ops import pull_latest

    pull_latest(repo, branch)


# Paths under here are Memory's own regenerated notes (understand_codebase's
# architecture summary, per-run decision/feature/metric write-ups) -- nobody
# hand-edits these, each branch just independently overwrites them on its
# own cycles, so a content conflict here reflects two branches re-describing
# the same repo slightly differently, never an actual code disagreement that
# needs a human's judgment. Confirmed live: a real promote blocked on
# exactly this (conflict in .agentra/memory/architecture/codebase.md, two
# unrelated code files in the same merge auto-merged fine) -- safe to always
# take source_ref's copy here since it's the freshest regeneration, same
# choice a human resolving it by hand would make.
_AUTO_RESOLVE_OURS_PREFIX = ".agentra/memory/"


def _merge_and_push(repo: Path, source_ref: str, target_branch: str) -> str | None:
    """Merge source_ref into target_branch (already synced to its remote tip) and push.
    Returns an error message on failure (merge left aborted, nothing pushed), None on success.

    The push goes through git_ops.push_branch, not a raw `_run`, for the
    same GitHub-App-then-static-token auth fallback reason as
    persist_audit_trail above -- a raw push here would 403 on any repo
    only reachable via the App connector.
    """
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
            # normal abort-and-report path rather than leaving a half-resolved
            # merge in place.
        subprocess.run(["git", "-C", str(repo), "merge", "--abort"], capture_output=True, text=True)
        # git writes the actually-useful diagnostic ("CONFLICT (content):
        # Merge conflict in ...", "Automatic merge failed...") to STDOUT,
        # not stderr -- confirmed live: a real failed promote reported this
        # error with stderr present-but-empty, so the human saw "failed,
        # aborted: " and nothing else. stderr kept too since some failure
        # modes (e.g. an unrelated-histories error) do write there instead.
        detail = "\n".join(part for part in (merge.stdout.strip(), merge.stderr.strip()) if part)
        return f"Merge of {source_ref!r} into {target_branch!r} failed, aborted: {detail}"
    try:
        push_branch(repo, target_branch)
    except GitOpError as exc:
        return f"Push of {target_branch!r} failed: {exc}"
    return None


def persist_audit_trail(repo: Path, branch: str) -> str | None:
    """Commit and push any dirty .agentra/ bookkeeping (released.json, memory/*,
    feedback_sync_state.json, codebase_spec_commit.json) onto `branch`.
    shipped/known_bugs/feature_queue live on GitHub Issues now (see memory.py),
    not in this directory at all.

    Without this, Memory.write() calls only ever land in the working copy --
    nothing ever committed them, so a fresh checkout (the next scheduled CI run)
    starts with an empty memory/ every time.

    run_autonomous_cycle calls this unconditionally at the end of EVERY cycle,
    not just ones that reached deploy_pre_prod -- so, unlike the docstring this
    replaced claimed, HEAD is NOT reliably already on `branch` here. Confirmed
    live: a cycle that hit its cost cap right after implement_feature (still on
    the feature branch, deploy_pre_prod never reached) failed outright with
    "src refspec beta does not match any" -- on a checkout fresh since the last
    redeploy (REPOS_ROOT is re-cloned single-branch from the app's registered
    branch), `branch` may not even exist locally yet, let alone be checked out.

    Fixed by decoupling "capture the dirty .agentra/ state" from "which branch
    is it going onto": commit the dirty paths onto whatever's currently checked
    out first (an orphaned, harmless commit if that's a feature branch nothing
    else ever merges), record that commit's sha, THEN sync `branch` to its
    remote tip (git_ops.pull_latest -- creates it locally if it doesn't exist),
    overlay just the .agentra/ tree from the captured sha onto that now-current
    `branch` (a plain `git checkout <sha> -- .agentra/`, not a merge/cherry-pick
    -- always takes the freshest regeneration, same "ours"-wins reasoning
    _merge_and_push's _AUTO_RESOLVE_OURS_PREFIX already applies to a real merge
    conflict here applied directly instead), and commit+push that. The
    intermediate sha stays reachable by its own hash through all of this even
    when `branch` turns out to already be the checked-out branch (pull_latest's
    hard reset only moves branch pointers, never garbage-collects an object
    still referenced by name within the same process).

    Delegates to git_ops.pull_latest/commit_and_push for the actual git
    plumbing rather than hand-rolling it, so this benefits from the same
    GitHub-App-then-static-token auth fallback clone_repo/push_branch already
    have -- otherwise this would 403 on any repo only reachable via the App
    connector, same bug TASK-016's registration hit before that fallback
    existed. Returns an error message on failure, None on success or if
    nothing was dirty.
    """
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
    """Lands feature_branch on pre_prod_branch without spinning up any deploy
    or live verification -- the path agents/brain/tools.py's deploy_pre_prod
    tool takes when change_risk.classify_change() says TRIVIAL (a test fix,
    docs edit, rename, or a couple-line bug fix). A full pre-prod deploy +
    Testing Agent turn costs real docker resources and an LLM turn actually
    driving a browser; for a change this small, a passing local test suite is
    already as much confidence as that heavier path would add. Strategy-
    agnostic (git-only) since every deploy_strategy merges to the same
    pre_prod_branch regardless of how it then deploys."""
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
    """Short, local-only name derived from the registry image path (e.g.
    'agentra' from '.../agentra-prod/agentra/agentra') -- used to name
    sibling/candidate containers and images distinctly from the registry tag
    itself, which always stays config.image_repo:staging."""
    return config.image_repo.rstrip("/").rsplit("/", 1)[-1]


def _preprod_container_name(config: "environments.SelfHostedVMConfig", run_id: str) -> str:
    return f"{_local_image_name(config)}-preprod-{run_id}"


def _preprod_image_tag(config: "environments.SelfHostedVMConfig", run_id: str) -> str:
    return f"{_local_image_name(config)}-preprod:{run_id}"


_STALE_PREPROD_MAX_AGE_HOURS = 2.0


def cleanup_stale_preprod(config: "environments.SelfHostedVMConfig", max_age_hours: float = _STALE_PREPROD_MAX_AGE_HOURS) -> list[str]:
    """Sweeps any deploy_pre_prod_self_hosted sibling container/image left
    behind by a run that crashed, hit its cost cap, or was killed before
    verify_pre_prod's own single-shot teardown_self_hosted_preprod ran --
    that teardown only fires on the happy path (see its own docstring), so
    nothing else ever reclaims a leaked sibling otherwise. A real pre-prod
    deploy + verify + teardown cycle takes minutes; anything still alive
    past max_age_hours is orphaned, not mid-flight.

    Runs at the start of every deploy_pre_prod_self_hosted call (a handful
    of cheap docker list/inspect calls) rather than needing a separate cron
    entry or scheduling infra of its own -- self-healing on next use.
    Best-effort throughout: a removal failure just leaves that one resource
    for the next sweep to retry, never fails the caller's actual deploy.
    Returns the names of everything it removed, purely for logging."""
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
            # "...123456789Z") -- datetime.fromisoformat only accepts up to
            # microseconds, so truncate before the trailing Z.
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


def _own_container_id() -> str | None:
    """The container ID Docker gave *this* process's own container, read
    from HOSTNAME -- Docker sets a container's hostname to its own short ID
    by default, and nothing in compute.tf's `docker run` for agentra-blue/
    agentra-green passes --hostname to override that. None if HOSTNAME isn't
    set at all (e.g. running outside a container, as under pytest/local
    dev) -- callers must treat that as "can't determine, don't guess"."""
    hostname = os.environ.get("HOSTNAME", "").strip()
    return hostname or None


def _own_container_name() -> str | None:
    """Resolves this process's own container's Docker --name (e.g.
    'agentra-blue' or 'agentra-green') by asking the host daemon (reachable
    over the bind-mounted docker.sock) to inspect the container ID from
    _own_container_id.

    This must be figured out dynamically at call time, never hardcoded or
    read from static config: blue/green promotion (promote_prod_self_hosted)
    flips which color is actually running this very process on every
    promotion, and the previous bug here was exactly that kind of static
    assumption -- deploy_pre_prod_self_hosted used to Docker-network-connect
    config.anchor_container (the nginx reverse-proxy) to preprod_network,
    but the orchestrator process that runs verify_pre_prod's Testing Agent
    turn (and any curl/getent/python socket call issued from inside it)
    lives in the blue/green *app* container, not nginx -- so it was never on
    preprod_network at all and could never resolve the pre-prod sibling's
    Docker DNS name, regardless of which color was active. Connecting
    whichever container this code is actually running in, found fresh on
    every call, fixes that for either color without needing to know which
    one is live.

    Returns None if the ID can't be determined or `docker inspect` fails
    (e.g. running outside Docker entirely) -- callers must treat that as a
    hard failure, not silently skip the network join and report success
    anyway."""
    container_id = _own_container_id()
    if not container_id:
        return None
    inspect = subprocess.run(
        ["docker", "inspect", container_id, "--format", "{{.Name}}"],
        capture_output=True, text=True,
    )
    if inspect.returncode != 0 or not inspect.stdout.strip():
        return None
    return inspect.stdout.strip().lstrip("/")


def _docker_build_env() -> dict:
    """Env for every `docker build` subprocess call in this module --
    explicitly enables BuildKit (DOCKER_BUILDKIT=1) rather than relying on
    whatever the legacy default happens to be on the build host: the legacy
    builder is deprecated and, unlike BuildKit, doesn't skip/cache unchanged
    layers as effectively, which matters directly on a disk-constrained
    long-lived host. Merges into (rather than replaces) the current
    environment so PATH/HOME/etc the docker CLI itself needs stay intact."""
    return {**os.environ, "DOCKER_BUILDKIT": "1"}


def _reclaim_build_disk_space() -> None:
    """Best-effort disk reclaim, run immediately before every `docker build`
    call in this module -- same non-fatal, swallow-everything pattern as
    cleanup_stale_preprod above, just aimed at generic build-cache/dangling-
    image bloat rather than leaked preprod siblings specifically. On the
    long-lived self-hosted build host, dangling layers and build cache from
    earlier runs accumulate across every deploy unless something reclaims
    them, which is the actual root cause of "no space left on device" build
    failures. `docker builder prune -f` / `docker image prune -f` only ever
    remove cache/images that aren't backing anything currently in use, so
    this never touches a live container's image out from under it. A prune
    failure (e.g. docker not reachable) just leaves that space unclaimed for
    this build to potentially fail on -- never blocks the deploy attempt
    itself."""
    subprocess.run(["docker", "builder", "prune", "-f"], capture_output=True, text=True)
    subprocess.run(["docker", "image", "prune", "-f"], capture_output=True, text=True)


async def deploy_pre_prod_self_hosted(
    repo: Path, env: EnvironmentConfig, feature_branch: str, run_id: str
) -> AgentResult:
    """Pre-prod deploy for the "self_hosted_vm" deploy_strategy -- a generic
    capability for any repo that runs on its own Docker-based VM rather than
    Vercel/Firebase, not agentra-specific code. Every identifier (network
    names, the anchor container to connect to, the image repo path) comes
    from environments.load_self_hosted_vm_config(repo), that repo's own
    committed .agentra/memory/architecture/deployment.md -- never hardcoded
    here. Builds and runs a second, isolated instance of the app alongside
    the running prod one, on the same VM, reachable only over an internal
    Docker network -- never the public internet.

    Deliberately no run_agent/LLM call anywhere in this function: spinning up
    a sibling container requires /var/run/docker.sock access (mounted into
    the calling container only for this purpose), and that capability must
    stay fixed, deterministic plumbing an agent turn can never freely
    improvise over -- same reasoning as _merge_and_push/persist_audit_trail's
    git operations, just a higher-stakes one (host-level Docker control, not
    just this repo's own git history).

    Returns the same AgentResult/json_data shape (status/preview_url/notes)
    as the generic deploy_pre_prod, so every downstream consumer (tools.py's
    deploy_pre_prod wrapper, verify_pre_prod, HUMAN_INPUT_REQUIRED handling)
    works completely unmodified regardless of which strategy produced it.
    Call teardown_self_hosted_preprod(repo, run_id) once the report this
    enables has been produced -- this function does not tear down after
    itself, since the caller still needs the container alive to verify it.
    """
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
    # SelfHostedVMConfig.data_mount's docstring for why: docker commands
    # issued here run against the host's daemon over the bind-mounted
    # socket, so -v source paths are resolved by the host, never by this
    # container's own filesystem.
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
    # must be on preprod_network: the Testing Agent turn verify_pre_prod
    # spawns, and every curl/getent/python-socket call it issues, runs
    # inside whichever blue/green app container is running *this very
    # Python process*, never inside nginx. Determined dynamically on every
    # call (see _own_container_name) since blue/green promotion moves which
    # color that is. Idempotent: both no-op harmlessly (network already
    # exists / container already attached) on every call after the first.
    subprocess.run(["docker", "network", "create", config.preprod_network], capture_output=True, text=True)
    own_container = _own_container_name()
    if own_container is None:
        return AgentResult(
            ok=False,
            text=(
                "Could not determine this orchestrator process's own container name "
                "(HOSTNAME unset or `docker inspect` failed) -- refusing to deploy a "
                "pre-prod instance nothing would be able to reach: without knowing which "
                f"container to join to {config.preprod_network!r}, verify_pre_prod would "
                "fail with an unresolvable host."
            ),
            json_data={"status": "failed"},
            cost_usd=0.0, turns=0,
        )
    subprocess.run(
        ["docker", "network", "connect", config.preprod_network, own_container],
        capture_output=True, text=True,
    )

    subprocess.run(["docker", "rm", "-f", container_name], capture_output=True, text=True)
    env_args = []
    if config.firestore_project:
        env_args = ["-e", f"AGENTRA_FIRESTORE_PROJECT={config.firestore_project}"]
    run = subprocess.run(
        [
            "docker", "run", "-d", "--name", container_name,
            "--network", config.preprod_network,
            # GitHub issue #45: publish 8080 to a Docker-assigned free host
            # port (bare "-p 8080", no host-side port given) -- see
            # _host_reachable_preview_url's docstring for why preview_url is
            # built from this instead of container_name's Docker-embedded-DNS
            # name. Auto-assigned rather than hashed/derived from run_id so
            # concurrent runs can never collide on a host port -- the OS/
            # Docker's own ephemeral-port allocator already solves that
            # uniqueness problem, no need for agentra to reinvent it. Freed
            # automatically the moment this container is removed (teardown_
            # self_hosted_preprod / cleanup_stale_preprod's existing `docker
            # rm -f`) -- no separate port bookkeeping needed.
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

    preview_url = _host_reachable_preview_url(config, container_name)
    if preview_url is None:
        return AgentResult(
            ok=False,
            text=f"{container_name} started but its published port and/or {config.app_network!r} gateway "
            "address could not be determined -- no reachable preview_url to hand to verify_pre_prod.",
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
    # curls itself via `docker exec`) -- it never exercises the network path
    # verify_pre_prod's Testing Agent turn will actually use. Genuinely curl
    # preview_url from THIS process (the orchestrator itself), over whatever
    # real network path exists to it, before ever reporting "deployed": this
    # is exactly the check that was missing before, which is why a broken
    # network join (previously: only nginx on preprod_network, never the app
    # container) used to report status=deployed and fail opaquely one step
    # later in verify_pre_prod instead of failing clearly here.
    reachable, reach_detail = await _wait_for_reachable_from_orchestrator(preview_url)
    if not reachable:
        return AgentResult(
            ok=False,
            text=(
                f"pre-prod instance not reachable from the orchestrator's own network path: "
                f"{reach_detail} (preview_url={preview_url!r}). This usually means this "
                f"process's own container isn't actually joined to {config.preprod_network!r} "
                "-- see deploy_pre_prod_self_hosted/_own_container_name."
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


def _host_reachable_preview_url(config: "environments.SelfHostedVMConfig", container_name: str) -> str | None:
    """GitHub issue #45: preview_url used to be built as
    f"http://{container_name}:8080" -- a Docker embedded-DNS name that only
    resolves for containers actually joined to config.preprod_network. But
    only config.anchor_container (nginx) is ever connected to that network
    (see the `docker network connect` call above, in
    deploy_pre_prod_self_hosted) -- the process that actually verifies this
    URL (testing.run_pre_prod's Playwright screenshot + HTTP checks) is
    spawned as a Claude Agent SDK subprocess of the very "agentra" app
    container this function itself runs inside (see this module's own
    docstring: no run_agent call here because this needs the bind-mounted
    docker.sock the calling container has), which per compute.tf is
    attached to config.app_network, never config.preprod_network -- so the
    hostname could never resolve there. Confirmed via curl/getent/python
    socket resolution and a browser ERR_NAME_NOT_RESOLVED.

    Fixed by publishing the container's port 8080 to a Docker-assigned free
    host port (the bare `-p 8080` docker-run arg above) and addressing it
    through config.app_network's own gateway IP instead of a container
    name: that gateway is a real interface of the HOST's own network stack
    (every Docker bridge network's gateway is), so any container attached
    to config.app_network -- which the verifying process's own container
    always is, by construction, since that's the network the persistent
    "agentra" app container itself runs on -- can reach it, and Docker's
    published-port DNAT rules bind on every host-side interface including
    that one. Deliberately does NOT touch compute.tf's own container
    network wiring (agentra-app-net/agentra-proxy attachment) -- that's
    shared with the production blue/green promotion anchor and every
    self_hosted_vm repo, out of scope and too risky for this fix; this only
    reads config.app_network's already-existing gateway, never modifies who
    is attached to it.

    Returns None if either the port-publish or the gateway lookup fails --
    callers must treat that as a deploy failure, never silently fall back
    to the unreachable-by-design container-name URL this issue was filed
    over."""
    port = subprocess.run(
        ["docker", "port", container_name, "8080/tcp"], capture_output=True, text=True,
    )
    if port.returncode != 0 or not port.stdout.strip():
        return None
    # "docker port" prints one "<host-ip>:<host-port>" line per bound host
    # address (0.0.0.0:NNNNN and/or [::]:NNNNN for the default "publish on
    # every interface" bare `-p 8080`) -- same port number on every line,
    # so just taking the first is fine; host port is whatever follows the
    # last ':'.
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
    """Curls preview_url directly from THIS process -- no `docker exec` into
    the sibling -- the same network path (this process's own container's
    view of preprod_network) verify_pre_prod's Testing Agent turn will use.
    Distinct from, and strictly more meaningful than, _wait_for_healthy:
    that only proves the sibling's own curl-against-itself succeeds, which
    stays green even when nothing else on the host can resolve its name at
    all (exactly the bug this function exists to catch). Returns
    (reachable, diagnostic); diagnostic is the last curl error seen, or
    "reachable" on success."""
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
    """Best-effort cleanup of a deploy_pre_prod_self_hosted sibling -- single-shot,
    ephemeral lifecycle, so nothing accumulates across features tested over time.
    Swallows failures: a leftover container/image is a minor disk/resource cost,
    not worth failing the run over. No-ops quietly if this repo has no
    self-hosted-VM config at all (nothing to tear down)."""
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
    """The image reference (as originally passed to `docker run`, e.g.
    'agentra:a1b2c3d') backing a running container -- captured before
    removing that container, since the container name won't resolve it
    afterward. None if the container doesn't exist / inspect fails
    (best-effort, same as every other cleanup call in this module)."""
    inspect = subprocess.run(
        ["docker", "inspect", container_name, "--format", "{{.Config.Image}}"],
        capture_output=True, text=True,
    )
    return inspect.stdout.strip() if inspect.returncode == 0 and inspect.stdout.strip() else None


def _other_color(color: str) -> str:
    return "green" if color == "blue" else "blue"


def _nginx_conf(backend_container: str) -> str:
    """Full config.anchor_container nginx conf, proxying to backend_container
    (a docker DNS name on config.app_network) on port 8080. Regenerated and
    piped in whole on every promotion (see promote_prod_self_hosted) rather
    than templated/patched in place -- simplest thing that's still trivially
    correct, and this is the exact same content compute.tf's startup-script
    writes for the VM's very first boot (before any promotion has run)."""
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
    """Production promotion for the "self_hosted_vm" deploy_strategy --
    nginx-fronted blue/green instead of the in-place container replacement
    the manual VM redeploy process (docs/deployment.md) still uses. Builds
    the new candidate image, runs it as the inactive color alongside the
    currently-live one, health-checks it, flips config.anchor_container's
    (nginx) active upstream to it, then tears down the now-inactive old
    color -- zero-downtime, no window where neither color is serving.

    Never called from the autonomous tool loop -- only from orchestrator.py's
    run_promote, itself only reachable via the explicit `agentra promote`
    CLI command or the dashboard's Promote button. Same human-approval gate
    every other app's production promotion already goes through; this
    function adds no gating of its own because it doesn't need to.

    Deliberately no run_agent/LLM call, same reasoning as
    deploy_pre_prod_self_hosted -- fixed, deterministic docker/nginx
    plumbing only."""
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
    # included) rather than re-fetching from Secret Manager here -- this
    # function runs as plain Python inside a container, not the bash
    # startup-script's gcs()-via-throwaway-container trick, and the running
    # container already has everything compute.tf baked in at last boot.
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
    # is gone -- captured before removal (the container name won't resolve it
    # afterward) and reclaimed right alongside it. Without this, every
    # successful promotion leaves one more full image (~GBs) permanently
    # orphaned: _reclaim_build_disk_space's `docker image prune -f` only
    # catches *dangling* (untagged) images, and this one still carries its
    # real tag. Confirmed live: this exact leak filled the VM's disk to 100%
    # (16G/16G) after a handful of same-day promotions, deadlocking every
    # subsequent autonomous cycle at pull_latest before real work could start.
    old_image = _image_of(old_container)
    subprocess.run(["docker", "rm", "-f", old_container], capture_output=True, text=True)
    if old_image:
        subprocess.run(["docker", "rmi", old_image], capture_output=True, text=True)

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
        # synced above) with pre_prod_branch merely fetched as a merge
        # source -- pull_latest would check out pre_prod_branch instead.
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
#
# The single source of truth for "what deployment mechanisms exist" -- adding
# a third strategy later means adding one function + one registry entry here,
# not touching the dispatch call sites (agents/brain/tools.py's deploy_pre_prod
# tool, orchestrator.py's run_promote). Adapter closures exist only to give
# every strategy a uniform call signature despite deploy_pre_prod/promote_prod
# and their self_hosted_vm counterparts taking different native parameters
# (session_id vs run_id) -- the underlying functions above are unchanged.


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

"""Deployment Agent (vision.md 5.8), split across the environment pipeline.

deploy_pre_prod() is what every regular cycle calls: push to the pre-prod
branch, deploy the isolated Firebase pre-prod project + a Vercel preview,
smoke test. It never touches production.

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

import subprocess
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

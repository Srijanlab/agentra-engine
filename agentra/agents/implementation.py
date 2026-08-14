"""Implementation Agent (vision.md 5.6).

Owns the implement -> test -> fix -> retry loop for a single feature. Runs
inside one long agent turn so it can self-correct against its own test runs
before handing off to the independent Testing Agent for final QA.

Each call gets its own dedicated feature_branch (see environments.py's
feature_branch_name), forked fresh from the remote pre-prod branch's tip --
never reused across runs and never just "whatever HEAD happened to be." Two
features built in separate cycles must not pile onto one shared branch (they
could conflict, or one could silently carry the other's incomplete work), and
branching from pre-prod's tip (not main, not local stale state) means the
feature starts from everything already shipped-but-not-yet-promoted, so
Deployment Agent's later merge into pre-prod (see agents/deployment.py) is a
clean fast-forward/merge instead of fighting drift.

Checkout and the end-of-turn commit are both handled here in plain Python,
not left as instructions in the agent's own system prompt -- observed twice
in practice (two separate dogfood runs against a real repo) that an LLM
agent given "checkout branch X first, commit at the end" as prose does not
reliably do either: both times it just edited files directly on whatever was
already checked out and left the result uncommitted. Deterministic git
plumbing around the agent turn removes that failure mode entirely rather
than hoping a better-worded prompt fixes it.
"""

import subprocess
from pathlib import Path

from agentra.agents import git_ops
from agentra.agents.base import AgentResult, run_agent
from agentra.environments import EnvironmentConfig

SYSTEM_PROMPT = """You are the Implementation Agent in an autonomous product \
engineering system. You are given a codebase summary and a specific feature \
to build. You are already checked out on your dedicated branch, {feature_branch} \
— stay on it; never switch to {pre_prod_branch}, {prod_branch}, or any other \
branch. Work in a tight loop:

1. Implement the smallest coherent version of the feature.
2. Run EVERY test/build command actually configured in the project yourself \
   via Bash -- e.g. both a Python suite and a separate frontend one, if both \
   exist -- not just whichever you think is relevant to your change. You are \
   responsible for the whole repo being green when you're done, not just the \
   file(s) you touched: a change scoped to one part of the codebase can still \
   leave an unrelated suite red, and the next agent to run has no way to tell \
   "pre-existing" from "I broke this" unless you check now, while you still \
   know which one it is.
3. If you added new tests, they must pass too.
4. If anything fails, fix it and re-run. Repeat until every suite is green. \
   The one exception: a failure you've confirmed is pre-existing and \
   unrelated to your change (e.g. via `git stash` and re-running against the \
   base branch) is not yours to silently absorb scope-creeping into — fix it \
   anyway if it's a small, safe, unrelated fix, but if it's not, say so \
   explicitly in self_test_result/notes below rather than reporting "pass" \
   over a suite that isn't actually green.
5. Make a git commit of your change once it's working. Do not push, do not \
   open a PR, do not touch git history beyond one commit.

Constraints:
- Prefer minimal, targeted changes over refactors.
- Never touch secrets, billing, or production config.
- Never run destructive or irreversible commands.

End your response with a fenced ```json block shaped like:
{{
  "feature": "...",
  "status": "implemented" | "partially_implemented" | "blocked",
  "files_changed": ["..."],
  "self_test_result": "pass" | "fail" | "not_run",
  "notes": "..."
}}
"""


def _checkout_feature_branch(repo: Path, feature_branch: str, pre_prod_branch: str, resume: bool = False) -> None:
    if resume:
        # Resuming an interrupted call's work (see brain.py's resume_branch arg,
        # memory.py's known_bugs()/feature_queue() resume_branch field) -- the
        # branch was pushed by a previous implement_feature call (see run()
        # below) that never made it to deploy_pre_prod. Fetch and check it out
        # directly instead of forking a fresh one from pre_prod_branch, so this
        # call continues on top of the same commits rather than redoing them.
        # Best-effort: any failure here (branch was force-pushed over, deleted,
        # this VM's clone predates it existing, etc.) falls through to the
        # normal fresh-fork path below rather than failing the whole call --
        # resuming is an optimization, not something implement_feature can
        # depend on succeeding.
        try:
            git_ops.fetch_ref(repo, feature_branch)
            # Same untracked-.agentra/-file conflict the fresh-fork path below
            # guards against (see its own comment) -- a resume can run against
            # a checkout whose untracked .agentra/ state differs from what
            # feature_branch's tree wants there, and checkout refuses rather
            # than clobber it.
            subprocess.run(["git", "-C", str(repo), "clean", "-fd", ".agentra/"], check=True, capture_output=True, text=True)
            subprocess.run(
                ["git", "-C", str(repo), "checkout", "-B", feature_branch, f"origin/{feature_branch}"],
                check=True, capture_output=True, text=True,
            )
            return
        except (git_ops.GitOpError, subprocess.CalledProcessError):
            pass
    # git_ops.fetch_ref, not a hand-rolled subprocess call -- confirmed live (4
    # consecutive autonomous-cycle failures, GitHub issues #7-#10, after this repo
    # moved to an org, Srijanlab/srijanlab-agentra): a bare `git fetch` here relied
    # entirely on the ambient GIT_ASKPASS/GITHUB_TOKEN static PAT, bypassing the
    # GitHub App installation-token path every other git operation in this codebase
    # (git_ops.py, deployment.py) already goes through -- and that static PAT was
    # never scoped to the new org, so the fetch 403'd outright. fetch_ref does the
    # exact same explicit-refspec fetch (a single-branch clone's restricted fetch
    # refspec means fetching by branch name alone can silently never create
    # refs/remotes/origin/<branch> locally, leaving the checkout below failing with
    # "origin/<branch> is not a commit") but with the App token injected first.
    git_ops.fetch_ref(repo, pre_prod_branch)
    # codebase.py's understand_codebase step (run just before this, in both
    # orchestrator.py and brain.py) writes .agentra/memory/... as plain files.
    # Whether those paths are tracked differs by branch -- e.g. committed on
    # pre_prod_branch from an earlier cycle, untracked on whatever branch this
    # process happened to start checked out on. An untracked file that the
    # target branch's tree wants to place there makes checkout refuse to
    # proceed rather than silently clobber it. Confirmed live in the first
    # real scheduled GitHub Actions run: "untracked working tree file
    # .agentra/memory/architecture/codebase.md would be overwritten by
    # checkout" -- three identical failures, correctly recognized as
    # non-transient by the Orchestrator Agent, which stopped and explained
    # rather than retrying blindly or faking success.
    # .agentra/ is entirely agentra's own regenerable bookkeeping (logs,
    # memory, ledgers) -- never a human's uncommitted work -- so clearing
    # untracked content there before checkout is always safe.
    subprocess.run(
        ["git", "-C", str(repo), "clean", "-fd", ".agentra/"],
        check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "checkout", "-B", feature_branch, f"origin/{pre_prod_branch}"],
        check=True, capture_output=True, text=True,
    )


def _commit_if_dirty(repo: Path, feature: str) -> bool:
    """Safety net for the agent finishing its turn without committing (observed in
    practice, see module docstring). Returns True if a commit was made here."""
    status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        check=True, capture_output=True, text=True,
    )
    if not status.stdout.strip():
        return False
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", f"{feature}\n\nAuto-committed by agentra: agent turn ended without committing."],
        check=True, capture_output=True, text=True,
    )
    return True


async def run(
    repo: Path,
    objective: str,
    feature: str,
    codebase_summary: str,
    env: EnvironmentConfig,
    feature_branch: str,
    resume: bool = False,
    spec: str = "",
) -> AgentResult:
    try:
        _checkout_feature_branch(repo, feature_branch, env.pre_prod_branch, resume=resume)
    except git_ops.GitOpError as exc:
        return AgentResult(
            ok=False,
            text=f"Could not create feature branch {feature_branch!r} from {env.pre_prod_branch!r}: {exc}",
            json_data=None, cost_usd=0.0, turns=0,
        )
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr if isinstance(exc.stderr, str) else exc.stderr.decode(errors="replace")
        return AgentResult(
            ok=False,
            text=f"Could not create feature branch {feature_branch!r} from {env.pre_prod_branch!r}: {stderr}",
            json_data=None, cost_usd=0.0, turns=0,
        )

    spec_section = f"\nFinalized spec (Requirements Agent):\n{spec}\n" if spec else ""
    prompt = f"""Business objective: {objective}

Feature to implement: {feature}
{spec_section}
Codebase summary:
{codebase_summary}

Implement this feature now, following the loop in your system prompt."""
    system_prompt = SYSTEM_PROMPT.format(
        feature_branch=feature_branch,
        pre_prod_branch=env.pre_prod_branch,
        prod_branch=env.prod_branch,
    )
    result = await run_agent(
        prompt=prompt,
        system_prompt=system_prompt,
        cwd=repo,
        allowed_tools=["Read", "Write", "Edit", "Glob", "Grep", "Bash"],
        permission_mode="bypassPermissions",
        max_turns=60,
        # This agent commits real changes; a blind from-scratch retry on the
        # self-contradictory CLI result (see base.py's _CONTRADICTORY_RESULT_SUFFIX)
        # could re-attempt on top of a first pass that already committed. Let it
        # surface as a normal failure instead -- _commit_if_dirty below still saves
        # any uncommitted work either way.
        retry_on_contradictory_result=False,
        agent_label="Implementation Agent",
    )

    try:
        if _commit_if_dirty(repo, feature):
            result.text += "\n\n[agentra] Uncommitted changes were present after the agent turn ended; auto-committed them."
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr if isinstance(exc.stderr, str) else exc.stderr.decode(errors="replace")
        result.text += f"\n\n[agentra] Safety-net commit failed: {stderr}"

    # Push the feature branch now, regardless of what happens next this cycle
    # (tests failing, deploy never being reached, a hard stop) -- previously
    # this branch only ever became durable once deploy_pre_prod merged it,
    # which requires local tests to pass first. Confirmed live: GitHub issue
    # #13's fix was implemented here, then permanently lost when
    # run_local_tests failed and deploy_pre_prod never ran -- the next VM
    # redeploy re-clones REPOS_ROOT from scratch (VM-local only), wiping the
    # never-pushed branch. Best-effort: a push failure just means this
    # particular call's work isn't resumable/recoverable, same as before this
    # existed -- it must not fail the call itself.
    try:
        git_ops.push_branch(repo, feature_branch)
    except git_ops.GitOpError as exc:
        result.text += f"\n\n[agentra] Could not push feature branch {feature_branch!r} (work is committed locally only, not recoverable after a redeploy): {exc}"

    return result

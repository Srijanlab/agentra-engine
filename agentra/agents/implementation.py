"""Implementation Agent."""

import subprocess
from pathlib import Path

from agentra.agents import codegraph, git_ops
from agentra.agents.base import AgentResult, run_agent
from agentra.environments import EnvironmentConfig

SYSTEM_PROMPT = """You are the Implementation Agent in an autonomous product \
engineering system. You are given a codebase summary and a specific feature \
to build. You are already checked out on your dedicated branch, {feature_branch} \
— stay on it; never switch to {pre_prod_branch}, {prod_branch}, or any other \
branch. Work in a tight loop:

0. Before writing any code: if the brief requires a decision outside your authority to make \
   unilaterally -- an ambiguous choice with no clear default, a breaking schema change with \
   no safe backward-compatible path, a security/credential-handling decision, or an \
   irreversible destructive migration -- do not guess and do not implement. Stop and report \
   status HUMAN_INPUT_REQUIRED instead, with a concrete reason, the specific question, and \
   the discrete options if there are any.
1. If mcp__graphify__* tools are available (a local, prebuilt code graph of this repo -- see \
   the codebase summary below for an excerpt), query it before grepping or reading files blind: \
   query_graph for anything touching more than one file, shortest_path to see how two things \
   connect, get_neighbors/get_node for a focused look at one symbol. This is local and free (no \
   LLM call) -- prefer it over guessing at blast radius from memory or from a partial grep.
2. Implement the smallest coherent version of the feature.
3. Run EVERY test/build command actually configured in the project yourself \
   via Bash -- e.g. both a Python suite and a separate frontend one, if both \
   exist -- not just whichever you think is relevant to your change. You are \
   responsible for the whole repo being green when you're done, not just the \
   file(s) you touched: a change scoped to one part of the codebase can still \
   leave an unrelated suite red, and the next agent to run has no way to tell \
   "pre-existing" from "I broke this" unless you check now, while you still \
   know which one it is.
4. If you added new tests, they must pass too.
5. If anything fails, fix it and re-run. Repeat until every suite is green. \
   The one exception: a failure you've confirmed is pre-existing and \
   unrelated to your change (e.g. via `git stash` and re-running against the \
   base branch) is not yours to silently absorb scope-creeping into — fix it \
   anyway if it's a small, safe, unrelated fix, but if it's not, say so \
   explicitly in self_test_result/notes below rather than reporting "pass" \
   over a suite that isn't actually green.
6. Make a git commit of your change once it's working. Do not push, do not \
   open a PR, do not touch git history beyond one commit.

Constraints:
- Prefer minimal, targeted changes over refactors.
- Never touch secrets, billing, or production config.
- Never run destructive or irreversible commands.

End your response with a fenced ```json block shaped like:
{{
  "feature": "...",
  "status": "implemented" | "partially_implemented" | "blocked" | "HUMAN_INPUT_REQUIRED",
  "files_changed": ["..."],
  "self_test_result": "pass" | "fail" | "not_run",
  "notes": "...",
  "reason": "... (only when status is HUMAN_INPUT_REQUIRED)",
  "question": "... (only when status is HUMAN_INPUT_REQUIRED)",
  "options": ["..."]
}}
"""


def _checkout_feature_branch(repo: Path, feature_branch: str, pre_prod_branch: str, resume: bool = False) -> bool:
    """Returns True if `resume` was requested and actually succeeded (the branch's prior commits are intact and checked out), False otherwise (either resume wasn't requested, or it was and silently fell back to forking fresh from pre_prod_branch's tip -- see the except clause below)."""
    if resume:
        # Resuming an interrupted call's work (see brain.py's resume_branch arg,
        try:
            git_ops.fetch_ref(repo, feature_branch)
            # Same untracked-.agentra/-file conflict the fresh-fork path below
            subprocess.run(["git", "-C", str(repo), "clean", "-fd", ".agentra/"], check=True, capture_output=True, text=True)
            # Same reasoning, for already-TRACKED .agentra/ paths with local
            subprocess.run(["git", "-C", str(repo), "checkout", "--", ".agentra/"], capture_output=True, text=True)
            subprocess.run(
                ["git", "-C", str(repo), "checkout", "-B", feature_branch, f"origin/{feature_branch}"],
                check=True, capture_output=True, text=True,
            )
            return True
        except (git_ops.GitOpError, subprocess.CalledProcessError) as exc:
            detail = exc.stderr if isinstance(getattr(exc, "stderr", None), str) else str(exc)
            print(
                f"[agentra] resume of {feature_branch!r} failed, falling back to a fresh fork "
                f"from {pre_prod_branch!r} -- prior commits on this branch will NOT be present: {detail}",
                flush=True,
            )
    # git_ops.fetch_ref, not a hand-rolled subprocess call -- confirmed live (4
    git_ops.fetch_ref(repo, pre_prod_branch)
    # codebase.py's understand_codebase step (run just before this, in both
    subprocess.run(
        ["git", "-C", str(repo), "clean", "-fd", ".agentra/"],
        check=True, capture_output=True, text=True,
    )
    # Confirmed live (run 26bf7dee, 2026-08-18): clean -fd above only removes
    subprocess.run(["git", "-C", str(repo), "checkout", "--", ".agentra/"], capture_output=True, text=True)
    subprocess.run(
        ["git", "-C", str(repo), "checkout", "-B", feature_branch, f"origin/{pre_prod_branch}"],
        check=True, capture_output=True, text=True,
    )
    return False


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
    session_id: str | None = None,
) -> AgentResult:
    """resume (bool) continues an interrupted call's git branch (see..."""
    try:
        resumed = _checkout_feature_branch(repo, feature_branch, env.pre_prod_branch, resume=resume)
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

    # resume was requested but _checkout_feature_branch silently fell back to a
    resume_mismatch = resume and not resumed
    if resume_mismatch:
        session_id = None

    spec_section = f"\nFinalized spec (Requirements Agent):\n{spec}\n" if spec else ""
    resume_note = (
        "\nNote: a resume of this feature's prior branch was attempted but did not succeed "
        "(see server logs) -- this is a FRESH start on a branch re-forked from "
        f"{env.pre_prod_branch!r}, NOT a continuation. Do not assume any prior edits are "
        "present; explore the current state of the repo before making changes.\n"
        if resume_mismatch else ""
    )
    prompt = f"""Business objective: {objective}

Feature to implement: {feature}
{spec_section}{resume_note}
Codebase summary:
{codebase_summary}

Implement this feature now, following the loop in your system prompt."""
    system_prompt = SYSTEM_PROMPT.format(
        feature_branch=feature_branch,
        pre_prod_branch=env.pre_prod_branch,
        prod_branch=env.prod_branch,
    )
    # mcp_config is {} when no graph has been built for `repo` yet (best-effort,
    mcp_servers = codegraph.mcp_config(repo)
    allowed_tools = ["Read", "Write", "Edit", "Glob", "Grep", "Bash"] + (
        codegraph.READ_ONLY_MCP_TOOLS if mcp_servers else []
    )
    result = await run_agent(
        prompt=prompt,
        system_prompt=system_prompt,
        cwd=repo,
        allowed_tools=allowed_tools,
        permission_mode="bypassPermissions",
        # Raised from 60 -- confirmed live (issue #34) that a genuinely
        max_turns=120,
        # This agent commits real changes; a blind from-scratch retry on the
        retry_on_contradictory_result=False,
        agent_label="Implementation Agent",
        resume=session_id,
        mcp_servers=mcp_servers,
    )

    try:
        if _commit_if_dirty(repo, feature):
            result.text += "\n\n[agentra] Uncommitted changes were present after the agent turn ended; auto-committed them."
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr if isinstance(exc.stderr, str) else exc.stderr.decode(errors="replace")
        result.text += f"\n\n[agentra] Safety-net commit failed: {stderr}"

    # Push the feature branch now, regardless of what happens next this cycle.
    # result.pushed gates status:code_complete (GitHub issue #75/#78) -- code
    # that only exists in a local commit must not be marked code-complete, since
    # this repo checkout is ephemeral (destroyed on the next container swap).
    try:
        git_ops.push_branch(repo, feature_branch)
        result.pushed = True
    except git_ops.GitOpError as exc:
        result.pushed = False
        result.text += f"\n\n[agentra] Could not push feature branch {feature_branch!r} (work is committed locally only, not recoverable after a redeploy): {exc}"

    # End-of-run graph refresh: whatever code changed above (commit_if_dirty's
    codegraph.refresh(repo)

    return result

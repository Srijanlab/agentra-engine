"""Deterministic git pull/push helpers shared by any agent path that needs
them outside the fixed implement->deploy pipeline (agents/generic.py's
spawn(), TASK-014).

implementation.py and deployment.py already do their own git plumbing in
plain Python rather than trusting an LLM system prompt to remember to run
`git pull`/`git push` (see implementation.py's module docstring for the
observed failure mode this avoids). Those two stay as-is — this module
exists so agents/generic.py::spawn() can offer the same reliability
guarantee to ad hoc, non-implementation/non-deployment tasks (e.g. a spawned
audit that needs today's actual latest code, or a one-off cleanup that
commits and pushes its own change) without duplicating that logic inline.

Auth: relies entirely on the environment already being configured for git
push/pull to succeed -- GIT_ASKPASS pointed at git-askpass.sh with
GITHUB_TOKEN set (see Dockerfile / deploy/gcp/terraform/cloudrun.tf). This
module does not touch credentials itself; a misconfigured environment
simply surfaces as a normal, logged git failure below, same as any other
git error.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


class GitOpError(Exception):
    """Raised by pull_latest/push_branch on failure. Callers must not treat
    this as retryable-by-default -- a real auth/conflict failure needs a
    human or a different approach, not a blind retry (same reasoning as
    agents/base.py's run_agent on why implementation.py disables its own
    contradictory-result retry)."""


def pull_latest(repo: Path, branch: str) -> None:
    """Fetch and hard-reset the local `branch` to match origin/`branch`,
    creating/checking it out if it doesn't exist locally yet. Raises
    GitOpError with the real git stderr on failure -- never silently leaves
    the working tree on stale or partially-updated state."""
    try:
        # Explicit refspec, not `fetch origin <branch>` -- see
        # implementation.py's _checkout_feature_branch for why a
        # single-branch clone needs this to create
        # refs/remotes/origin/<branch> at all.
        subprocess.run(
            ["git", "-C", str(repo), "fetch", "origin", f"+{branch}:refs/remotes/origin/{branch}"],
            check=True, capture_output=True, text=True,
        )
        checkout = subprocess.run(
            ["git", "-C", str(repo), "checkout", branch], capture_output=True, text=True,
        )
        if checkout.returncode == 0:
            subprocess.run(
                ["git", "-C", str(repo), "reset", "--hard", f"origin/{branch}"],
                check=True, capture_output=True, text=True,
            )
        else:
            subprocess.run(
                ["git", "-C", str(repo), "checkout", "-B", branch, f"origin/{branch}"],
                check=True, capture_output=True, text=True,
            )
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr if isinstance(exc.stderr, str) else exc.stderr.decode(errors="replace")
        raise GitOpError(f"pull_latest({branch!r}) failed: {stderr}") from exc


def clone_repo(url: str, dest: Path, branch: str = "main") -> None:
    """Clone `url` into `dest` (must not already exist), for TASK-016's
    register-a-repo-from-the-dashboard path. Reuses the same GIT_ASKPASS/
    GITHUB_TOKEN credential docker-entrypoint.sh's own clone-on-start step
    relies on -- both are plain `git clone`, auth resolved the same way, so
    nothing here needs to know the token itself. Raises GitOpError (with
    git's real stderr) on failure, e.g. a bad URL or an expired token."""
    if dest.exists():
        raise GitOpError(f"clone_repo: destination {dest} already exists")
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            ["git", "clone", "--branch", branch, "--single-branch", url, str(dest)],
            check=True, capture_output=True, text=True,
        )
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr if isinstance(exc.stderr, str) else exc.stderr.decode(errors="replace")
        raise GitOpError(f"clone_repo({url!r}) failed: {stderr}") from exc


def push_branch(repo: Path, branch: str) -> None:
    """Push the local `branch` to origin. Raises GitOpError (with git's own
    message -- e.g. a rejected non-fast-forward push from a conflicting
    remote change) rather than swallowing the failure; callers are
    responsible for logging/surfacing it (agents/generic.py::spawn() writes
    it into the returned AgentResult and the run's Memory log)."""
    try:
        subprocess.run(
            ["git", "-C", str(repo), "push", "origin", branch],
            check=True, capture_output=True, text=True,
        )
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr if isinstance(exc.stderr, str) else exc.stderr.decode(errors="replace")
        raise GitOpError(f"push_branch({branch!r}) failed: {stderr}") from exc

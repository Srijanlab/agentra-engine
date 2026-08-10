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

import base64
import subprocess
from pathlib import Path

from agentra.connectors import github_app


def _extra_auth_args(repo_url: str | None) -> list[str]:
    """git -c flags that inject a GitHub App installation token as this
    one invocation's HTTP Basic auth, without touching the URL, global git
    config, or GIT_ASKPASS. Empty list (ambient GIT_ASKPASS/GITHUB_TOKEN
    handles auth as before) whenever the App isn't configured, isn't
    installed on this particular repo, or repo_url isn't resolvable (None,
    or a non-github.com URL) -- always a silent fallback, never a hard
    failure, since the static-token path is still fully functional."""
    if not repo_url:
        return []
    try:
        token = github_app.get_installation_token(repo_url)
    except (github_app.GitHubAppNotConfigured, github_app.GitHubAppError):
        return []
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return ["-c", f"http.extraheader=AUTHORIZATION: basic {basic}"]


def _origin_url(repo: Path) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(repo), "remote", "get-url", "origin"],
        capture_output=True, text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else None


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
        auth = _extra_auth_args(_origin_url(repo))
        # Explicit refspec, not `fetch origin <branch>` -- see
        # implementation.py's _checkout_feature_branch for why a
        # single-branch clone needs this to create
        # refs/remotes/origin/<branch> at all.
        subprocess.run(
            ["git", "-C", str(repo), *auth, "fetch", "origin", f"+{branch}:refs/remotes/origin/{branch}"],
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
    register-a-repo-from-the-dashboard path. Tries a GitHub App
    installation token first (github_app connector -- scales to any org
    the App is installed on, not just whatever the static token happened
    to be scoped to at creation time; confirmed live, registering an
    org-owned repo 403'd under the static-token-only path this replaces),
    falling back to the ambient GIT_ASKPASS/GITHUB_TOKEN credential
    docker-entrypoint.sh's own clone-on-start step also relies on. Raises
    GitOpError (with git's real stderr) on failure, e.g. a bad URL, an
    expired token, or the App simply not installed on this repo."""
    if dest.exists():
        raise GitOpError(f"clone_repo: destination {dest} already exists")
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        auth = _extra_auth_args(url)
        subprocess.run(
            ["git", *auth, "clone", "--branch", branch, "--single-branch", url, str(dest)],
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
        auth = _extra_auth_args(_origin_url(repo))
        subprocess.run(
            ["git", "-C", str(repo), *auth, "push", "origin", branch],
            check=True, capture_output=True, text=True,
        )
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr if isinstance(exc.stderr, str) else exc.stderr.decode(errors="replace")
        raise GitOpError(f"push_branch({branch!r}) failed: {stderr}") from exc

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


def remote_head_sha(repo_url: str, branch: str, timeout: float = 15) -> str | None:
    """git ls-remote for `branch`'s current commit sha on `repo_url`, or
    None if it can't be determined (network/timeout/branch doesn't exist).
    Uses the same GitHub-App-then-static-token auth as every other
    operation in this module -- registry/core.py's get_app_repo used to
    run a bare `git ls-remote` here itself, bypassing the App token like
    implementation.py's old _checkout_feature_branch did (see that
    module's docstring for the same failure class). Confirmed live: since
    get_app_repo's staleness check runs on nearly every dashboard/API
    request, that bare call was 403ing continuously against any repo the
    App token covers but the ambient static PAT doesn't, flooding the logs
    on every single poll rather than once."""
    try:
        auth = _extra_auth_args(repo_url)
        result = subprocess.run(
            ["git", *auth, "ls-remote", repo_url, f"refs/heads/{branch}"],
            capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return result.stdout.split()[0]


def fetch_ref(repo: Path, branch: str) -> None:
    """Fetch `branch` into refs/remotes/origin/<branch> without touching
    the working tree/current checkout -- for a caller that just needs
    origin/<branch> to exist locally (e.g. as a merge source) while
    staying on whatever branch it's currently on. Explicit refspec, not
    `fetch origin <branch>` -- see implementation.py's
    _checkout_feature_branch for why a single-branch clone needs this to
    create refs/remotes/origin/<branch> at all. Raises GitOpError with the
    real git stderr on failure."""
    try:
        auth = _extra_auth_args(_origin_url(repo))
        subprocess.run(
            ["git", "-C", str(repo), *auth, "fetch", "origin", f"+{branch}:refs/remotes/origin/{branch}"],
            check=True, capture_output=True, text=True,
        )
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr if isinstance(exc.stderr, str) else exc.stderr.decode(errors="replace")
        raise GitOpError(f"fetch_ref({branch!r}) failed: {stderr}") from exc


def pull_latest(repo: Path, branch: str) -> None:
    """Fetch and hard-reset the local `branch` to match origin/`branch`,
    creating/checking it out if it doesn't exist locally yet. Raises
    GitOpError with the real git stderr on failure -- never silently leaves
    the working tree on stale or partially-updated state."""
    fetch_ref(repo, branch)
    try:
        # Same reasoning/fix as implementation.py::_checkout_feature_branch
        # (issue #24): a prior step in this repo clone (e.g. understand_codebase
        # regenerating .agentra/memory/architecture/*.md without committing) can
        # leave local MODIFICATIONS to already-tracked .agentra/ paths, which
        # `git checkout <branch>` and the `checkout -B` fallback below both
        # refuse to silently overwrite -- "Your local changes to the following
        # files would be overwritten by checkout", confirmed reproducible here
        # the same way as the implementation.py case. Best-effort, no
        # check=True: harmless no-op if .agentra/ isn't tracked yet at all.
        subprocess.run(["git", "-C", str(repo), "checkout", "--", ".agentra/"], capture_output=True, text=True)
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


def commit_and_push(repo: Path, branch: str, message: str, paths: list[str] | None = None) -> bool:
    """git add (`paths`, default the whole tree) -> commit -> push, reusing
    push_branch's GitHub-App-then-static-token auth (see its docstring) --
    unlike a hand-rolled `_run`-based commit+push, this works for repos
    only the App connector can reach, not just the ones the static
    GITHUB_TOKEN happens to be scoped to. Returns False (no-op, not an
    error) if there was nothing dirty under `paths` to commit -- e.g.
    calling this after a registration that set no objective/env
    overrides/notes. Raises GitOpError on a real add/commit/push failure."""
    paths = paths or ["."]
    status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain", "--", *paths],
        capture_output=True, text=True,
    )
    if not status.stdout.strip():
        return False
    try:
        subprocess.run(["git", "-C", str(repo), "add", *paths], check=True, capture_output=True, text=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-m", message], check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr if isinstance(exc.stderr, str) else exc.stderr.decode(errors="replace")
        raise GitOpError(f"commit_and_push: add/commit failed: {stderr}") from exc
    push_branch(repo, branch)
    return True


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

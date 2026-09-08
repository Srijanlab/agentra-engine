"""Deterministic git pull/push helpers shared by any agent path that needs them outside the fixed implement->deploy pipeline (agents/generic.py's spawn(), TASK-014)."""

from __future__ import annotations

import base64
import subprocess
from pathlib import Path

from agentra.connectors import github_app


def _extra_auth_args(repo_url: str | None) -> list[str]:
    """git -c flags that inject a GitHub App installation token as this one invocation's HTTP Basic auth, without touching the URL, global git config, or GIT_ASKPASS."""
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
    """Raised by pull_latest/push_branch on failure."""


def remote_head_sha(repo_url: str, branch: str, timeout: float = 15) -> str | None:
    """git ls-remote for `branch`'s current commit sha on `repo_url`, or None if it can't be determined (network/timeout/branch doesn't exist)."""
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
    """Fetch `branch` into refs/remotes/origin/<branch> without touching the working tree/current checkout -- for a caller that just needs origin/<branch> to exist locally (e.g."""
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
    """Fetch and hard-reset the local `branch` to match origin/`branch`, creating/checking it out if it doesn't exist locally yet."""
    fetch_ref(repo, branch)
    try:
        # Same reasoning/fix as implementation.py::_checkout_feature_branch
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
    """Clone `url` into `dest` (must not already exist), for TASK-016's register-a-repo-from-the-dashboard path."""
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
    """git add (`paths`, default the whole tree) -> commit -> push, reusing push_branch's GitHub-App-then-static-token auth (see its docstring) -- unlike a hand-rolled `_run`-based commit+push, this works for repos only the App connector can reach, not just the ones the static GITHUB_TOKEN happens to be scoped to."""
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


_NON_FAST_FORWARD_MARKERS = ("non-fast-forward", "fetch first", "[rejected]", "updates were rejected")


def _looks_like_non_fast_forward_rejection(stderr: str) -> bool:
    lowered = stderr.lower()
    return any(marker in lowered for marker in _NON_FAST_FORWARD_MARKERS)


def push_branch(repo: Path, branch: str) -> None:
    """Push the local `branch` to origin. If origin/`branch` has moved since this checkout last synced with it (a concurrent push elsewhere -- GitHub issue #88), pulls the new remote tip into the local branch and retries the push once, rather than immediately failing on the first non-fast-forward rejection."""
    auth = _extra_auth_args(_origin_url(repo))
    try:
        subprocess.run(
            ["git", "-C", str(repo), *auth, "push", "origin", branch],
            check=True, capture_output=True, text=True,
        )
        return
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr if isinstance(exc.stderr, str) else exc.stderr.decode(errors="replace")
        if not _looks_like_non_fast_forward_rejection(stderr):
            raise GitOpError(f"push_branch({branch!r}) failed: {stderr}") from exc

    # Merge (not rebase) the new remote tip into the local branch -- some
    # callers (e.g. deployment.py's _merge_and_push) push a branch whose tip
    # is itself a merge commit, and rebasing a merge commit onto a new base
    # has surprising, history-reshaping semantics; a plain merge integrates
    # cleanly regardless of what shape the local branch's history is.
    try:
        subprocess.run(
            ["git", "-C", str(repo), *auth, "fetch", "origin", f"+{branch}:refs/remotes/origin/{branch}"],
            check=True, capture_output=True, text=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "merge", "--no-edit", f"origin/{branch}"],
            check=True, capture_output=True, text=True,
        )
    except subprocess.CalledProcessError as exc:
        subprocess.run(["git", "-C", str(repo), "merge", "--abort"], capture_output=True, text=True)
        stderr = exc.stderr if isinstance(exc.stderr, str) else exc.stderr.decode(errors="replace")
        raise GitOpError(
            f"push_branch({branch!r}): origin moved and merging its new tip in failed "
            f"(likely a real conflict, not just a stale ref): {stderr}"
        ) from exc

    try:
        subprocess.run(
            ["git", "-C", str(repo), *auth, "push", "origin", branch],
            check=True, capture_output=True, text=True,
        )
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr if isinstance(exc.stderr, str) else exc.stderr.decode(errors="replace")
        raise GitOpError(f"push_branch({branch!r}) failed even after merging origin's new tip in: {stderr}") from exc

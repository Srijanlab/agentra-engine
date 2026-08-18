"""Regression test for a real, live-confirmed bug: agents/implementation.py's
_checkout_feature_branch cleared untracked .agentra/ files before checking
out a new/resumed feature branch, but never discarded local MODIFICATIONS
to already-tracked .agentra/ paths -- a different failure mode `git clean
-fd` can't touch.

Confirmed live (run 26bf7dee, 2026-08-18): once a prior cycle's
persist_audit_trail had committed architecture/codebase.md onto
pre_prod_branch, the current run's own understand_codebase step (which
always runs first, and regenerates that file without committing) left a
local modification to a now-tracked path. implement_feature then failed
twice in a row on "Your local changes to the following files would be
overwritten by checkout", tripping the consecutive-failure circuit breaker
and ending the run with nothing built -- an infrastructure hiccup, not a
feature/code problem, but one that silently blocked every implement_feature
call until a human intervened.
"""

import subprocess
from pathlib import Path

from agentra.agents import implementation


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _init_repo_with_branch(path: Path, branch: str = "beta") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-b", branch)
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    (path / "README.md").write_text("hello\n")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "initial commit")
    return path


def _write_and_commit_codebase_md(repo: Path, content: str, message: str) -> None:
    path = repo / ".agentra" / "memory" / "architecture" / "codebase.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    _git(repo, "add", ".agentra/")
    _git(repo, "commit", "-m", message)


def test_fresh_fork_checkout_discards_local_modifications_to_already_tracked_agentra_files(tmp_path):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "beta", str(origin)], check=True, capture_output=True)

    repo = _init_repo_with_branch(tmp_path / "repo", branch="beta")
    _git(repo, "remote", "add", "origin", str(origin))
    _write_and_commit_codebase_md(repo, "v1 summary\n", "initial codebase scan")
    _git(repo, "push", "origin", "beta")

    # beta advances elsewhere with a DIFFERENT committed codebase.md (e.g. a
    # later cycle's persist_audit_trail, from a different clone) --
    # simulates this checkout being behind origin/beta's tip.
    other_clone = tmp_path / "other-clone"
    subprocess.run(["git", "clone", str(origin), str(other_clone)], check=True, capture_output=True)
    _git(other_clone, "config", "user.email", "test@example.com")
    _git(other_clone, "config", "user.name", "Test")
    _write_and_commit_codebase_md(other_clone, "v2 summary\n", "later codebase scan")
    _git(other_clone, "push", "origin", "beta")

    # This run's own understand_codebase step regenerates the file locally
    # WITHOUT committing -- a local modification to an already-tracked path.
    (repo / ".agentra" / "memory" / "architecture" / "codebase.md").write_text("local uncommitted rescan\n")
    assert "modified" in _git(repo, "status", "--porcelain").stdout or _git(repo, "status", "--porcelain").stdout

    # Must not raise (CalledProcessError, before the fix) despite the dirty tracked file.
    implementation._checkout_feature_branch(repo, "feature/new-work", "beta", resume=False)

    assert _git(repo, "branch", "--show-current").stdout.strip() == "feature/new-work"
    # Landed on origin/beta's actual latest content -- the local scratch
    # modification was discarded, not silently carried onto the new branch.
    assert (repo / ".agentra" / "memory" / "architecture" / "codebase.md").read_text() == "v2 summary\n"
    assert _git(repo, "status", "--porcelain").stdout.strip() == ""


def test_resume_checkout_discards_local_modifications_to_already_tracked_agentra_files(tmp_path):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "beta", str(origin)], check=True, capture_output=True)

    repo = _init_repo_with_branch(tmp_path / "repo", branch="beta")
    _git(repo, "remote", "add", "origin", str(origin))
    _write_and_commit_codebase_md(repo, "beta summary\n", "initial codebase scan")
    _git(repo, "push", "origin", "beta")

    # A previous, interrupted implement_feature call: forked, committed a
    # DIFFERENT codebase.md (its own understand_codebase run), pushed it --
    # exactly what a real resumable branch looks like.
    _git(repo, "checkout", "-b", "feature/resume-me")
    _write_and_commit_codebase_md(repo, "resume-branch summary\n", "work in progress")
    _git(repo, "push", "origin", "feature/resume-me")
    _git(repo, "checkout", "beta")

    # This run's own understand_codebase step regenerates the file locally
    # WITHOUT committing, before implement_feature(resume_branch=...) runs.
    (repo / ".agentra" / "memory" / "architecture" / "codebase.md").write_text("local uncommitted rescan\n")

    # Must not raise, and must actually land on the RESUMED branch/content
    # (not silently fall through to a fresh fork because the resume attempt
    # itself failed on the dirty tracked file).
    implementation._checkout_feature_branch(repo, "feature/resume-me", "beta", resume=True)

    assert _git(repo, "branch", "--show-current").stdout.strip() == "feature/resume-me"
    assert (repo / ".agentra" / "memory" / "architecture" / "codebase.md").read_text() == "resume-branch summary\n"
    assert _git(repo, "status", "--porcelain").stdout.strip() == ""

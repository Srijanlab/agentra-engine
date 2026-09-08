"""deployment.persist_repo_specs — the R1 guarantee: a code repo's `.agentra/`
spec files are only ever committed on the pre-prod branch, never on the feature
branch the cycle may currently be sitting on.
"""

import subprocess
from pathlib import Path

from agentra.agents import deployment, git_ops


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _origin_with_branches(tmp_path: Path, *branches: str) -> Path:
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", branches[0], str(origin)], check=True, capture_output=True)
    seed = tmp_path / "_seed"
    subprocess.run(["git", "clone", str(origin), str(seed)], check=True, capture_output=True)
    _git(seed, "config", "user.email", "t@e.com")
    _git(seed, "config", "user.name", "T")
    for b in branches:
        _git(seed, "checkout", "-B", b)
        (seed / "app.txt").write_text(f"{b}\n")
        _git(seed, "add", "app.txt")
        _git(seed, "commit", "-m", f"seed {b}")
        _git(seed, "push", "origin", b)
    return origin


def _clone(origin: Path, dest: Path, branch: str) -> Path:
    subprocess.run(["git", "clone", "--branch", branch, "--single-branch", str(origin), str(dest)],
                   check=True, capture_output=True)
    _git(dest, "config", "user.email", "t@e.com")
    _git(dest, "config", "user.name", "T")
    return dest


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def test_specs_land_on_beta_not_the_feature_branch(tmp_path, monkeypatch):
    monkeypatch.setattr(git_ops, "_extra_auth_args", lambda repo_url: [])
    origin = _origin_with_branches(tmp_path, "main", "beta")
    repo = _clone(origin, tmp_path / "repo", "beta")

    # Cycle is mid-feature: on a feature branch forked from beta, with its own commit.
    _git(repo, "checkout", "-b", "dev/feature")
    (repo / "src.py").write_text("feature code\n")
    _git(repo, "add", "src.py")
    _git(repo, "commit", "-m", "feature work")
    feature_head_before = _head(repo)

    # sync_spec wrote spec files into the working tree on the feature branch.
    (repo / ".agentra").mkdir()
    (repo / ".agentra" / "architecture.md").write_text("<!-- owner: agent:codebase -->\n<!-- source-sha: x -->\n# repo\n")
    (repo / ".agentra" / "state.json").write_text('{"indexed_sha": "x"}\n')

    err = deployment.persist_repo_specs(repo, "beta")

    assert err is None
    # Feature branch's own commit is untouched — no spec commit slipped onto it.
    assert _git(repo, "rev-parse", "dev/feature").stdout.strip() == feature_head_before
    r = subprocess.run(["git", "-C", str(repo), "cat-file", "-e", "dev/feature:.agentra/architecture.md"],
                       capture_output=True)
    assert r.returncode != 0  # architecture.md does NOT exist on the feature branch

    # beta got exactly one .agentra/-only commit, visible on the remote.
    verify = _clone(origin, tmp_path / "verify", "beta")
    assert (verify / ".agentra" / "architecture.md").read_text().startswith("<!-- owner:")
    assert not (verify / "src.py").exists()  # feature code did NOT ride along


def test_no_op_when_specs_are_clean(tmp_path, monkeypatch):
    monkeypatch.setattr(git_ops, "_extra_auth_args", lambda repo_url: [])
    origin = _origin_with_branches(tmp_path, "beta")
    repo = _clone(origin, tmp_path / "repo", "beta")
    assert deployment.persist_repo_specs(repo, "beta") is None


def test_falls_back_to_current_branch_when_target_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(git_ops, "_extra_auth_args", lambda repo_url: [])
    origin = _origin_with_branches(tmp_path, "main")
    repo = _clone(origin, tmp_path / "repo", "main")
    (repo / ".agentra").mkdir()
    (repo / ".agentra" / "state.json").write_text('{"indexed_sha": "y"}\n')

    err = deployment.persist_repo_specs(repo, "beta")  # no origin/beta

    assert err is None
    verify = _clone(origin, tmp_path / "verify", "main")
    assert (verify / ".agentra" / "state.json").exists()

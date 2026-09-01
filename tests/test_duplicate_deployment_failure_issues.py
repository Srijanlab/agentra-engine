"""Regression: a recurring 'deployment failed ... agentra-preprod-<hash> did not become
healthy' failure filed a fresh GitHub issue every autonomous cycle (issues #116-#124)
instead of commenting on the open one. Root cause: difflib's autojunk heuristic collapsed
the similarity ratio for these long, repetitive diagnoses below the dedup threshold."""

import subprocess
from pathlib import Path

from agentra.connectors import github_fake
from agentra.memory import Memory


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _make_repo(tmp_path: Path, name: str = "myapp") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("hello\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial commit")
    _git(repo, "remote", "add", "origin", f"https://github.com/acme/{name}.git")
    return repo


def _health_failure(container_hash: str) -> str:
    return (
        f"agentra-preprod-{container_hash} did not become healthy on "
        f"http://agentra-preprod-{container_hash}:8080/health within 30s."
    )


def test_recurring_preprod_health_failure_is_deduped_across_cycles(tmp_path, monkeypatch):
    github_fake.install(monkeypatch=monkeypatch)
    mem = Memory(_make_repo(tmp_path))

    mem.record_failure("run1", "deployment", _health_failure("5d6b6774"))
    mem.record_failure("run2", "deployment", _health_failure("56e3932c"))
    mem.record_failure("run3", "deployment", _health_failure("6afa58ee"))

    open_bugs = mem.known_bugs()
    assert len(open_bugs) == 1, [b["diagnosis"] for b in open_bugs]


def test_genuinely_different_failures_still_file_separately(tmp_path, monkeypatch):
    github_fake.install(monkeypatch=monkeypatch)
    mem = Memory(_make_repo(tmp_path))

    mem.record_failure("run1", "deployment", _health_failure("5d6b6774"))
    mem.record_failure("run2", "testing", "3 tests failed: test_login, test_logout, test_signup")

    assert len(mem.known_bugs()) == 2

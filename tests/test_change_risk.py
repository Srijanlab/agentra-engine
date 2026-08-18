"""change_risk.classify_change -- TRIVIAL vs STANDARD, purely from git's own
diff stats between a base branch and a feature branch forked from it. Real
local git repo (no remote needed -- classify_change only ever diffs local
refs), same convention as tests/test_git_ops.py.
"""

import subprocess
from pathlib import Path

from agentra import change_risk


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "app.py").write_text("print('hello')\n")
    (repo / "README.md").write_text("# hi\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial")
    _git(repo, "branch", "feature")
    return repo


def _on_feature(repo: Path):
    _git(repo, "checkout", "-q", "feature")


def _commit(repo: Path, name: str, content: str) -> None:
    (repo / name).write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", f"edit {name}")


def test_test_only_change_is_trivial(tmp_path):
    repo = _repo(tmp_path)
    _on_feature(repo)
    (repo / "tests").mkdir()
    _commit(repo, "tests/test_app.py", "def test_x():\n    assert True\n" * 5)

    assert change_risk.classify_change(repo, "main", "feature") == change_risk.TRIVIAL


def test_docs_only_change_is_trivial(tmp_path):
    repo = _repo(tmp_path)
    _on_feature(repo)
    _commit(repo, "README.md", "# hi\n\nA lot more docs here.\n" * 5)

    assert change_risk.classify_change(repo, "main", "feature") == change_risk.TRIVIAL


def test_pure_rename_is_trivial_regardless_of_file_size(tmp_path):
    repo = _repo(tmp_path)
    (repo / "app.py").write_text("print('hello')\n" * 50)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "grow app.py")
    _git(repo, "branch", "-f", "feature")
    _on_feature(repo)
    _git(repo, "mv", "app.py", "main.py")
    _git(repo, "commit", "-q", "-m", "rename app.py to main.py")

    assert change_risk.classify_change(repo, "main", "feature") == change_risk.TRIVIAL


def test_small_real_code_change_is_trivial(tmp_path):
    repo = _repo(tmp_path)
    _on_feature(repo)
    _commit(repo, "app.py", "print('hello world')\n")

    assert change_risk.classify_change(repo, "main", "feature") == change_risk.TRIVIAL


def test_large_real_code_change_is_standard(tmp_path):
    repo = _repo(tmp_path)
    _on_feature(repo)
    _commit(repo, "app.py", "\n".join(f"print({i})" for i in range(60)) + "\n")

    assert change_risk.classify_change(repo, "main", "feature") == change_risk.STANDARD


def test_many_small_files_is_standard(tmp_path):
    repo = _repo(tmp_path)
    _on_feature(repo)
    for i in range(5):
        (repo / f"module_{i}.py").write_text(f"x = {i}\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "five new modules")

    assert change_risk.classify_change(repo, "main", "feature") == change_risk.STANDARD


def test_mixed_trivial_and_real_code_change_counts_only_the_real_part(tmp_path):
    repo = _repo(tmp_path)
    _on_feature(repo)
    (repo / "tests").mkdir()
    _commit(repo, "tests/test_app.py", "def test_x():\n    assert True\n" * 20)
    _commit(repo, "app.py", "print('a tiny real change')\n")

    assert change_risk.classify_change(repo, "main", "feature") == change_risk.TRIVIAL


def test_unreadable_diff_defaults_to_standard(tmp_path):
    repo = _repo(tmp_path)
    assert change_risk.classify_change(repo, "main", "does-not-exist") == change_risk.STANDARD

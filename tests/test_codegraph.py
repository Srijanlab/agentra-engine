"""Regression tests for agents/codegraph.py -- the local, deterministic
graphify step run against whatever app repo agentra is working on.

Real local git repo + real `graphify` subprocess calls, same pattern as
test_codebase_agent_cache.py and test_registry_sync.py -- the point here is
real graphify CLI behavior (flags, output shape, exclude-file handling), not
a mocked one.
"""

import subprocess
from pathlib import Path

from agentra.agents import codegraph


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _init_repo_with_code(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    src = path / "src"
    src.mkdir()
    (src / "main.py").write_text(
        "def helper():\n    return 1\n\n\ndef main():\n    return helper() + 1\n"
    )
    _git(path, "add", ".")
    _git(path, "commit", "-m", "initial commit")
    return path


def test_load_or_build_builds_graph_and_returns_summary_when_none_exists(tmp_path):
    repo = _init_repo_with_code(tmp_path / "repo")

    summary = codegraph.load_or_build(repo)

    assert (repo / "graphify-out" / "graph.json").exists()
    assert "helper" in summary
    assert "graphify query" in summary  # points the agent at how to go deeper


def test_load_or_build_reuses_existing_graph_without_rebuilding(tmp_path, monkeypatch):
    """The whole point of the split from a single build-or-update: a read
    must never pay for an extract/update -- it just reads what's already
    there. Adding a new file after the first build must NOT show up in the
    summary, since load_or_build never re-extracts."""
    repo = _init_repo_with_code(tmp_path / "repo")
    codegraph.load_or_build(repo)  # first call: builds fresh

    calls = []
    real_run = subprocess.run

    def spy_run(cmd, *args, **kwargs):
        calls.append(cmd)
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy_run)

    (repo / "src" / "second.py").write_text("def brand_new_symbol():\n    return 2\n")

    summary = codegraph.load_or_build(repo)

    assert "brand_new_symbol" not in summary  # not re-extracted -- stale is expected here
    assert all(cmd[1] not in ("extract", "update", "cluster-only") for cmd in calls)  # read-only calls only


def test_load_or_build_excludes_output_locally_not_in_gitignore(tmp_path):
    repo = _init_repo_with_code(tmp_path / "repo")

    codegraph.load_or_build(repo)

    exclude_text = (repo / ".git" / "info" / "exclude").read_text()
    assert "graphify-out/" in exclude_text.splitlines()
    assert not (repo / ".gitignore").exists()  # never touches the target repo's own tracked files

    status = _git(repo, "status", "--porcelain").stdout
    assert "graphify-out" not in status  # excluded, so it never shows as a diff in the target app


def test_load_or_build_is_best_effort_on_missing_binary(tmp_path, monkeypatch):
    repo = _init_repo_with_code(tmp_path / "repo")

    def _raise(*args, **kwargs):
        raise FileNotFoundError("graphify not found")

    monkeypatch.setattr(subprocess, "run", _raise)

    assert codegraph.load_or_build(repo) == ""


def test_load_or_build_on_non_git_repo_does_not_raise(tmp_path):
    """Defence in depth: if `repo` somehow isn't a git checkout, the exclude-file
    step must no-op rather than crash the calling cycle."""
    plain_dir = tmp_path / "not-a-repo"
    plain_dir.mkdir()
    (plain_dir / "main.py").write_text("def f():\n    return 1\n")

    result = codegraph.load_or_build(plain_dir)

    assert isinstance(result, str)


def test_refresh_picks_up_new_code_after_a_change(tmp_path):
    """refresh() is the one that's actually allowed to re-extract -- called at
    the end of Implementation Agent's run, after code may have changed. A
    brand new, unconnected function may not be prominent enough to land in
    the curated god-nodes/report excerpt, so check the underlying graph.json
    directly rather than the summary text."""
    repo = _init_repo_with_code(tmp_path / "repo")
    codegraph.load_or_build(repo)  # build the initial graph
    graph_json = repo / "graphify-out" / "graph.json"
    before = graph_json.read_text()
    assert "brand_new_symbol" not in before

    (repo / "src" / "second.py").write_text("def brand_new_symbol():\n    return 2\n")
    codegraph.refresh(repo)

    after = graph_json.read_text()
    assert "brand_new_symbol" in after


def test_refresh_is_a_noop_when_no_graph_exists_yet(tmp_path, monkeypatch):
    repo = _init_repo_with_code(tmp_path / "repo")

    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a) or (_ for _ in ()).throw(AssertionError("should not be called")))

    codegraph.refresh(repo)  # no graphify-out yet -- must return without touching subprocess

    assert calls == []


def test_refresh_is_best_effort_on_missing_binary(tmp_path, monkeypatch):
    repo = _init_repo_with_code(tmp_path / "repo")
    codegraph.load_or_build(repo)

    def _raise(*args, **kwargs):
        raise FileNotFoundError("graphify not found")

    monkeypatch.setattr(subprocess, "run", _raise)

    codegraph.refresh(repo)  # must not raise


def test_mcp_config_empty_when_no_graph_built_yet(tmp_path):
    repo = _init_repo_with_code(tmp_path / "repo")

    assert codegraph.mcp_config(repo) == {}


def test_mcp_config_points_graphify_mcp_at_the_built_graph(tmp_path):
    repo = _init_repo_with_code(tmp_path / "repo")
    codegraph.load_or_build(repo)

    config = codegraph.mcp_config(repo)

    assert set(config) == {codegraph.MCP_SERVER_NAME}
    server = config[codegraph.MCP_SERVER_NAME]
    assert server["command"] == "graphify-mcp"
    assert server["args"] == [str(repo / "graphify-out" / "graph.json")]


def test_read_only_mcp_tools_excludes_github_pr_tools():
    """list_prs/get_pr_impact/triage_prs hit the GitHub API -- not a local
    read against graph.json, and not relevant to assessing a brief that
    hasn't been implemented yet -- so they must never be granted here."""
    for name in codegraph.READ_ONLY_MCP_TOOLS:
        assert "pr" not in name.lower()
    assert all(name.startswith(f"mcp__{codegraph.MCP_SERVER_NAME}__") for name in codegraph.READ_ONLY_MCP_TOOLS)

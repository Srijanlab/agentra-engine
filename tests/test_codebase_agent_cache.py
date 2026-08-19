"""Regression tests for agents/codebase.py's run_cached() -- the Codebase
Agent's repo scan is a real multi-turn LLM call, so this must only ever run
it once per repo (when no cached summary exists yet) and reuse the cache
unconditionally after that, even across new commits.

This used to instead compare HEAD against the SHA the cache was generated
at, and regenerate on any mismatch -- which never actually saved anything
in practice, since every cycle's own bookkeeping commits (persist_audit_trail)
move HEAD regardless of whether the real source tree changed, so a real
(paid) scan ran on every single cycle. Confirmed live via VM run logs
(GitHub issue #20, "orchestrator firing codebase agent every time even
though codebase.md available"). Fixed to match that issue's own proposed
behavior: call the real scan only if the file is missing.

Real local git repo on disk (a plain `git init` + commits), same pattern
as test_registry_sync.py -- the point of this test is real git behavior,
not a mocked one.
"""

import asyncio
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock

from agentra.agents import codebase, codegraph
from agentra.agents.base import AgentResult
from agentra.memory import Memory


def _stub_no_graph(monkeypatch):
    """codegraph.load_or_build shells out to the real `graphify` CLI; these
    tests are about run_cached's caching behavior, not graphify's output for
    whatever tiny throwaway repo _init_repo built, so stub it out."""
    monkeypatch.setattr(codegraph, "load_or_build", lambda repo: "")


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    (path / "README.md").write_text("hello\n")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "initial commit")
    return path


def _fake_result(text: str) -> AgentResult:
    return AgentResult(ok=True, text=text, json_data={"framework": "test"}, cost_usd=0.05, turns=3)


def test_run_cached_calls_run_on_first_invocation(tmp_path, monkeypatch):
    _stub_no_graph(monkeypatch)
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)
    mock_run = AsyncMock(return_value=_fake_result("summary v1"))
    monkeypatch.setattr(codebase, "run", mock_run)

    result = asyncio.run(codebase.run_cached(repo, mem))

    assert result.text == "summary v1"
    assert mock_run.await_count == 1
    assert mem.read("architecture", "codebase") == "summary v1"
    assert mem.codebase_spec_commit() == _git(repo, "rev-parse", "HEAD").stdout.strip()


def test_run_cached_reuses_cache_when_head_unchanged(tmp_path, monkeypatch):
    _stub_no_graph(monkeypatch)
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)
    mock_run = AsyncMock(return_value=_fake_result("summary v1"))
    monkeypatch.setattr(codebase, "run", mock_run)

    first = asyncio.run(codebase.run_cached(repo, mem))
    second = asyncio.run(codebase.run_cached(repo, mem))

    # The real (expensive) scan must only have run once -- the second call
    # found a cached summary and served it instead.
    assert mock_run.await_count == 1
    assert second.text == first.text == "summary v1"
    assert second.cost_usd == 0.0
    assert second.turns == 0
    assert second.ok is True


def test_run_cached_reuses_cache_even_after_a_new_commit(tmp_path, monkeypatch):
    _stub_no_graph(monkeypatch)
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)
    mock_run = AsyncMock(side_effect=[_fake_result("summary v1"), _fake_result("summary v2")])
    monkeypatch.setattr(codebase, "run", mock_run)

    asyncio.run(codebase.run_cached(repo, mem))

    # Simulate Implementation Agent (or persist_audit_trail's own bookkeeping
    # commit) moving HEAD -- must NOT trigger a real re-scan on its own.
    (repo / "feature.py").write_text("x = 1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "add feature")

    second = asyncio.run(codebase.run_cached(repo, mem))

    assert mock_run.await_count == 1
    assert second.text == "summary v1"


def test_run_cached_does_not_cache_a_failed_scan(tmp_path, monkeypatch):
    _stub_no_graph(monkeypatch)
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)
    failed = AgentResult(ok=False, text="agent error", json_data=None, cost_usd=0.01, turns=1)
    mock_run = AsyncMock(return_value=failed)
    monkeypatch.setattr(codebase, "run", mock_run)

    result = asyncio.run(codebase.run_cached(repo, mem))

    assert result.ok is False
    assert mem.codebase_spec_commit() is None
    assert mem.read("architecture", "codebase") is None


def test_run_cached_reads_graph_summary_fresh_every_call(tmp_path, monkeypatch):
    """The graph excerpt is read (never rebuilt here -- see codegraph.load_or_build)
    on every call, even a cache hit, and appended to the *returned* text without
    polluting the persisted architecture/codebase.md cache."""
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)
    mock_run = AsyncMock(return_value=_fake_result("summary v1"))
    monkeypatch.setattr(codebase, "run", mock_run)
    calls = []

    def fake_load_or_build(repo_arg):
        calls.append(repo_arg)
        return "--- graphify code graph ---\nsome excerpt"

    monkeypatch.setattr(codegraph, "load_or_build", fake_load_or_build)

    first = asyncio.run(codebase.run_cached(repo, mem))
    second = asyncio.run(codebase.run_cached(repo, mem))

    assert "some excerpt" in first.text
    assert "some excerpt" in second.text
    assert mem.read("architecture", "codebase") == "summary v1"  # cache stays unpolluted
    assert calls == [repo, repo]  # read fresh on both calls, cache hit or not

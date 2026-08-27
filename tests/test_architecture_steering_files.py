"""Tests for the "steering files" (architecture/{codebase,design,
local-test-summary,documentation}.md) each being kept live by their
responsible agent -- Codebase Agent already owned codebase.md; this adds
design.md (from the same scan, no extra cost) and Testing Agent owning
local-test-summary.md. Both are overwritten fresh each real run, same
freshness semantics as codebase.md already had -- not an accumulating
log (contrast with memory.py's append_documentation()).

GitHub issue #84: local-test-summary.md (the Testing Agent's
machine-generated lint/typecheck summary) used to share
architecture/testing-notes.md with the human-authored "Testing Notes"
app setting (server/routes/apps.py), so every local-test run silently
clobbered whatever a human had written there. They're now separate keys;
testing-notes stays exclusively human-owned and is never touched here.
"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

from agentra.agents import codebase, testing
from agentra.agents.base import AgentResult
from agentra.memory import Memory


def _init_git_repo(path: Path) -> Path:
    import subprocess

    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True, capture_output=True)
    (path / "README.md").write_text("hello\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=path, check=True, capture_output=True)
    return path


def test_codebase_agent_writes_design_md_alongside_codebase_md(tmp_path, monkeypatch):
    repo = _init_git_repo(tmp_path / "repo")
    mem = Memory(repo)
    fake_result = AgentResult(
        ok=True,
        text="```json\n{\"design_notes\": \"Hybrid GitHub-authoritative config with local fallback.\"}\n```",
        json_data={"design_notes": "Hybrid GitHub-authoritative config with local fallback."},
        cost_usd=0.05,
        turns=3,
    )
    monkeypatch.setattr(codebase, "run", AsyncMock(return_value=fake_result))

    asyncio.run(codebase.run_cached(repo, mem))

    assert mem.read("architecture", "design") == "Hybrid GitHub-authoritative config with local fallback."


def test_codebase_agent_skips_design_md_when_field_absent(tmp_path, monkeypatch):
    repo = _init_git_repo(tmp_path / "repo")
    mem = Memory(repo)
    fake_result = AgentResult(ok=True, text="some text, no design_notes", json_data={}, cost_usd=0.05, turns=3)
    monkeypatch.setattr(codebase, "run", AsyncMock(return_value=fake_result))

    asyncio.run(codebase.run_cached(repo, mem))

    assert mem.read("architecture", "design") is None


def test_testing_agent_writes_local_test_summary_on_success(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    mem = Memory(repo)
    fake_result = AgentResult(
        ok=True,
        text="...",
        json_data={"status": "pass", "lint_status": "pass", "typecheck_status": "not_configured", "notes": "No e2e configured."},
        cost_usd=0.02,
        turns=5,
    )
    monkeypatch.setattr(testing, "run_agent", AsyncMock(return_value=fake_result))

    asyncio.run(testing.run_local(repo, "codebase summary", mem))

    notes = mem.read("architecture", "local-test-summary")
    assert "Lint: pass" in notes
    assert "Typecheck: not_configured" in notes
    assert "No e2e configured." in notes


def test_testing_agent_does_not_write_notes_without_a_memory_instance(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    fake_result = AgentResult(ok=True, text="...", json_data={"status": "pass"}, cost_usd=0.02, turns=5)
    monkeypatch.setattr(testing, "run_agent", AsyncMock(return_value=fake_result))

    # No mem passed -- must not raise, and must not write anything.
    result = asyncio.run(testing.run_local(repo, "codebase summary"))

    assert result.ok is True
    assert not (repo / ".agentra").exists()


def test_testing_agent_does_not_write_notes_on_a_failed_run(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    mem = Memory(repo)
    fake_result = AgentResult(ok=False, text="agent error", json_data=None, cost_usd=0.0, turns=1)
    monkeypatch.setattr(testing, "run_agent", AsyncMock(return_value=fake_result))

    asyncio.run(testing.run_local(repo, "codebase summary", mem))

    assert mem.read("architecture", "local-test-summary") is None


def test_run_local_tests_does_not_overwrite_a_humans_testing_notes(tmp_path, monkeypatch):
    """GitHub issue #84 regression: a human-authored testing_notes value (written via the
    Register/Edit App modals -> server/routes/apps.py::_apply_app_config, same
    mem.write("architecture", "testing-notes", ...) call) must survive a run_local_tests
    pass unchanged -- the machine-generated summary now lands in its own
    architecture/local-test-summary key instead of clobbering it."""
    repo = tmp_path / "repo"
    repo.mkdir()
    mem = Memory(repo)
    mem.write("architecture", "testing-notes", "human notes: run the manual smoke checklist too")

    fake_result = AgentResult(
        ok=True,
        text="...",
        json_data={"status": "pass", "lint_status": "pass", "typecheck_status": "pass", "notes": "All green."},
        cost_usd=0.02,
        turns=5,
    )
    monkeypatch.setattr(testing, "run_agent", AsyncMock(return_value=fake_result))

    asyncio.run(testing.run_local(repo, "codebase summary", mem))

    assert mem.read("architecture", "testing-notes") == "human notes: run the manual smoke checklist too"
    summary = mem.read("architecture", "local-test-summary")
    assert "Lint: pass" in summary
    assert "Typecheck: pass" in summary
    assert "All green." in summary

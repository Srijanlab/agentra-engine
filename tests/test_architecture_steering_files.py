"""Tests for the four "steering files" (architecture/{codebase,design,
testing-notes,documentation}.md) each being kept live by their responsible
agent -- Codebase Agent already owned codebase.md; this adds design.md
(from the same scan, no extra cost) and Testing Agent owning
testing-notes.md. Both are overwritten fresh each real run, same
freshness semantics as codebase.md already had -- not an accumulating
log (contrast with memory.py's append_documentation()).
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


def test_testing_agent_writes_testing_notes_on_success(tmp_path, monkeypatch):
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

    notes = mem.read("architecture", "testing-notes")
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

    assert mem.read("architecture", "testing-notes") is None

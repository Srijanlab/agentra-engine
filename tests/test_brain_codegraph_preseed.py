"""The brain preseed loads each repo's committed `.agentra/architecture.md`
spec into the orchestrator prompt. It does NOT build or read a code graph --
codegraph is a runtime navigation tool that implementation.py /
architecture_review.py build lazily against the active code repo, decoupled
from the spec (docs/agentra-spec.md).
"""

import asyncio
from pathlib import Path

from agentra.agents import brain
from agentra.environments import EnvironmentConfig
from agentra.memory import Memory
from agentra.memory.core import spec_header


def _fake_query_capturing(captured):
    async def _fake_query(prompt, options):
        parts = []
        async for item in prompt:
            content = item.get("message", {}).get("content", "")
            if content:
                parts.append(content)
        captured["prompt"] = "\n".join(parts)
        return
        yield  # pragma: no cover

    return _fake_query


def _repo_with_committed_spec(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "main.py").write_text("def helper():\n    return 1\n")
    mem = Memory(repo)
    mem.write_spec("architecture", spec_header("agent:codebase", "abc1234") + "# repo — Architecture\n## Purpose\nA tiny helper module.")
    return repo


def test_preseed_loads_the_committed_spec_into_the_prompt(tmp_path, monkeypatch):
    repo = _repo_with_committed_spec(tmp_path)
    captured = {}
    monkeypatch.setattr(brain, "query", _fake_query_capturing(captured))

    asyncio.run(brain.run_autonomous_cycle(repo, "Ship useful features.", EnvironmentConfig()))

    assert "A tiny helper module." in captured["prompt"]


def test_preseed_builds_no_code_graph(tmp_path, monkeypatch):
    repo = _repo_with_committed_spec(tmp_path)
    monkeypatch.setattr(brain, "query", _fake_query_capturing({}))

    asyncio.run(brain.run_autonomous_cycle(repo, "Ship useful features.", EnvironmentConfig()))

    assert not (repo / "graphify-out").exists()


def test_preseed_makes_no_codegraph_call(tmp_path, monkeypatch):
    repo = _repo_with_committed_spec(tmp_path)
    monkeypatch.setattr(brain, "query", _fake_query_capturing({}))
    import agentra.agents.codegraph as codegraph_mod
    monkeypatch.setattr(codegraph_mod, "load_or_build",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("codegraph called in preseed")))

    asyncio.run(brain.run_autonomous_cycle(repo, "Ship useful features.", EnvironmentConfig()))

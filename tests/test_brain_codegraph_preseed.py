"""Confirmed live on the deployed agentra-orchestrator VM: on a repo with an
existing cached architecture/codebase.md, run_autonomous_cycle pre-seeds
session.cb_summary directly from that cache (see __init__.py's own comment
on why -- avoiding a wasted understand_codebase call) and the model, seeing
a summary already loaded, has every reason never to call understand_codebase
at all this cycle. codebase.run_cached -- the only place that used to call
codegraph.load_or_build -- was therefore never invoked either, so
mcp_config(repo) returned {} for assess_design_impact/implement_feature on
every cycle of any mature/cached repo, silently never building a graph.
grep on the VM's own logs for "graphify" turned up nothing, which is what
surfaced this.

Fix: run_autonomous_cycle also calls codegraph.load_or_build directly
alongside the cb_summary pre-seed, so the graph exists regardless of
whether the model calls understand_codebase this cycle.
"""

import asyncio
from pathlib import Path

from agentra.agents import brain
from agentra.memory import Memory


def _fake_query_capturing(captured):
    async def _fake_query(prompt, options):
        # prompt is an async generator (single_prompt_stream wraps the plain string);
        # drain it so callers can assert on the actual text content.
        prompt_text_parts = []
        async for item in prompt:
            content = item.get("message", {}).get("content", "")
            if content:
                prompt_text_parts.append(content)
        captured["prompt"] = "\n".join(prompt_text_parts)
        captured["options"] = options
        return
        yield  # pragma: no cover -- makes this an async generator, never reached

    return _fake_query


def _repo_with_code_and_cached_summary(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "main.py").write_text(
        "def helper():\n    return 1\n\n\ndef main():\n    return helper() + 1\n"
    )
    Memory(repo).write("architecture", "codebase", "a cached codebase summary from a prior cycle")
    return repo


def test_run_autonomous_cycle_builds_graph_even_when_summary_is_pre_seeded_from_cache(tmp_path, monkeypatch):
    repo = _repo_with_code_and_cached_summary(tmp_path)
    captured = {}
    monkeypatch.setattr(brain, "query", _fake_query_capturing(captured))

    from agentra.environments import EnvironmentConfig

    asyncio.run(brain.run_autonomous_cycle(repo, "Ship useful features.", EnvironmentConfig()))

    assert (repo / "graphify-out" / "graph.json").exists(), (
        "the graph must be built at cycle start regardless of whether the model "
        "ever calls understand_codebase this cycle -- see codebase.run_cached, "
        "the only other call site, which the pre-seed above bypasses entirely"
    )


def test_run_autonomous_cycle_appends_graph_summary_to_the_preseeded_cb_summary(tmp_path, monkeypatch):
    repo = _repo_with_code_and_cached_summary(tmp_path)
    captured = {}
    monkeypatch.setattr(brain, "query", _fake_query_capturing(captured))

    from agentra.environments import EnvironmentConfig

    asyncio.run(brain.run_autonomous_cycle(repo, "Ship useful features.", EnvironmentConfig()))

    assert "helper" in captured["prompt"]  # the graph excerpt made it into the model's prompt


def test_run_autonomous_cycle_does_not_build_a_graph_when_no_cache_exists_yet(tmp_path, monkeypatch):
    """No cached summary -> the model is expected to call understand_codebase
    itself (which builds the graph via codebase.run_cached) -- nothing for
    this pre-seed shortcut to do in that case."""
    repo = tmp_path / "repo"
    repo.mkdir()
    captured = {}
    monkeypatch.setattr(brain, "query", _fake_query_capturing(captured))

    from agentra.environments import EnvironmentConfig

    asyncio.run(brain.run_autonomous_cycle(repo, "Ship useful features.", EnvironmentConfig()))

    assert not (repo / "graphify-out").exists()

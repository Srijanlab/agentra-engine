"""Codebase Understanding Agent."""

import subprocess
from dataclasses import replace
from pathlib import Path

from agentra.agents import codegraph
from agentra.agents.base import AgentResult, extract_json_block, run_agent
from agentra.memory import Memory

SYSTEM_PROMPT = """You are the Codebase Understanding Agent in an autonomous \
product engineering system. Your only job is to build an accurate, concise \
picture of the repository you are pointed at. You are read-only: never \
propose edits, never run mutating commands.

Investigate:
- Framework(s) and language(s) in use
- Overall architecture (monolith, serverless, microservices, etc.)
- Backend/data layer
- Existing user-facing features
- Test and build tooling already configured (so later agents know what to run)
- Key design decisions and patterns actually visible in the code -- not \
  guesses about intent, only what the code itself demonstrates (e.g. "auth \
  is a hybrid: GitHub-authoritative when reachable, falls back to a local \
  JSON mirror otherwise" or "git operations that must not silently fail are \
  done deterministically in plain Python, never left to an LLM agent turn")

End your response with a fenced ```json block shaped like:
{
  "framework": "...",
  "backend": "...",
  "architecture": "...",
  "features": ["...", "..."],
  "test_commands": ["..."],
  "build_commands": ["..."],
  "notes": "...",
  "design_notes": "one paragraph (or a short bulleted list) of the concrete design decisions/patterns you actually found, not generic best-practice advice"
}
"""


async def run(repo: Path) -> AgentResult:
    prompt = (
        "Scan this repository and produce the codebase understanding summary "
        "described in your system prompt."
    )
    return await run_agent(
        prompt=prompt,
        system_prompt=SYSTEM_PROMPT,
        cwd=repo,
        allowed_tools=["Read", "Glob", "Grep"],
        permission_mode="bypassPermissions",
        agent_label="Codebase Agent",
    )


def _current_head_sha(repo: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10
        )
    except Exception:
        return None
    return result.stdout.strip() if result.returncode == 0 else None


async def run_cached(repo: Path, mem: Memory) -> AgentResult:
    """Like run(), but skips the (real, multi-turn) LLM scan whenever a cached summary already exists at all -- only runs a real scan the first time, when architecture/codebase.md doesn't exist yet."""
    cached_text = mem.read("architecture", "codebase")
    if cached_text:
        result = AgentResult(
            ok=True,
            text=cached_text,
            json_data=extract_json_block(cached_text),
            cost_usd=0.0,
            turns=0,
        )
    else:
        result = await run(repo)
        if result.ok:
            mem.write("architecture", "codebase", result.text)
            design_notes = (result.json_data or {}).get("design_notes")
            if design_notes:
                # architecture/design.md: a live, agent-maintained snapshot of
                mem.write("architecture", "design", design_notes)
            new_head = _current_head_sha(repo)
            if new_head is not None:
                mem.set_codebase_spec_commit(new_head)

    if result.ok:
        graph_summary = codegraph.load_or_build(repo)
        if graph_summary:
            result = replace(result, text=result.text + "\n\n" + graph_summary)
    return result

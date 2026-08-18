"""Codebase Understanding Agent (vision.md 5.2).

Read-only scan of the target repo: framework, architecture, existing
features. Output feeds every downstream agent, so it never touches Write/Edit/Bash.
"""

import subprocess
from pathlib import Path

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
    """Like run(), but skips the (real, multi-turn) LLM scan whenever a
    cached summary already exists at all -- only runs a real scan the
    first time, when architecture/codebase.md doesn't exist yet.

    Previously this compared HEAD against the SHA the cache was generated
    at, and only reused the cache on an exact match. That never actually
    fired in practice: every cycle's own persist_audit_trail commits
    .agentra/ bookkeeping (shipped.json, decisions, work_updates, and
    codebase.md/design.md themselves) onto the same branch this scans, so
    HEAD moves on every single cycle regardless of whether the real source
    tree changed -- confirmed live via VM run logs, understand_codebase
    paying for a full real scan on every cycle. Reported as "orchestrator
    firing codebase agent every time even though codebase.md available"
    (GitHub issue #20), whose own proposed fix is exactly this: call the
    real scan only if the file is missing. Every call site that used to do
    `cb = await codebase.run(repo); mem.write(...)` should call this
    instead -- the mem.write() on a fresh generation happens here too, so
    callers don't need to duplicate that."""
    cached_text = mem.read("architecture", "codebase")
    if cached_text:
        return AgentResult(
            ok=True,
            text=cached_text,
            json_data=extract_json_block(cached_text),
            cost_usd=0.0,
            turns=0,
        )

    result = await run(repo)
    if result.ok:
        mem.write("architecture", "codebase", result.text)
        design_notes = (result.json_data or {}).get("design_notes")
        if design_notes:
            # architecture/design.md: a live, agent-maintained snapshot of
            # actual design decisions/patterns found in the code -- like
            # codebase.md, overwritten fresh each real scan, not an
            # accumulating log (see memory.py's append_documentation for
            # the deliberately different, changelog-style file).
            mem.write("architecture", "design", design_notes)
        new_head = _current_head_sha(repo)
        if new_head is not None:
            mem.set_codebase_spec_commit(new_head)
    return result
